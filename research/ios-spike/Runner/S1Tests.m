// S1: can a third-party XCUITest runner perform real AX actions (no synthesized touch)?
// Three routes per action, each logged as "S1 ..." with a timestamp; ground truth is the
// test app's DTA log (accessibilityActivate vs touch_began), never this output.
#import <XCTest/XCTest.h>
#import <objc/message.h>
#import <objc/runtime.h>

static NSString *const kApp = @"dev.droidctl.s1.testapp";

static NSString *stamp(void) {
  static NSISO8601DateFormatter *f;
  if (!f) { f = [NSISO8601DateFormatter new]; f.formatOptions = NSISO8601DateFormatWithInternetDateTime | NSISO8601DateFormatWithFractionalSeconds; }
  return [f stringFromDate:[NSDate date]];
}
static void S1(NSString *fmt, ...) NS_FORMAT_FUNCTION(1, 2);
static void S1(NSString *fmt, ...) {
  va_list a; va_start(a, fmt); NSString *s = [[NSString alloc] initWithFormat:fmt arguments:a]; va_end(a);
  printf("%s S1 %s\n", stamp().UTF8String, s.UTF8String); fflush(stdout);
}


static void dumpClass(NSString *name) {
  Class c = NSClassFromString(name);
  if (!c) { S1(@"class %@ missing", name); return; }
  for (int meta = 0; meta < 2; meta++) {
    unsigned n = 0; Method *m = class_copyMethodList(meta ? object_getClass(c) : c, &n);
    NSMutableArray *names = [NSMutableArray array];
    for (unsigned i = 0; i < n; i++) [names addObject:NSStringFromSelector(method_getName(m[i]))];
    free(m);
    S1(@"%@ %@methods: %@", name, meta ? @"class " : @"", [names componentsJoinedByString:@" "]);
  }
  unsigned n = 0; Ivar *iv = class_copyIvarList(c, &n);
  NSMutableArray *names = [NSMutableArray array];
  for (unsigned i = 0; i < n; i++) [names addObject:[NSString stringWithFormat:@"%s:%s", ivar_getName(iv[i]), ivar_getTypeEncoding(iv[i])]];
  free(iv);
  S1(@"%@ ivars: %@", name, [names componentsJoinedByString:@" "]);
}

static id makeAction(int number) {
  Class c = NSClassFromString(@"XCUIAccessibilityAction");
  SEL init = NSSelectorFromString(@"initWithActionNumber:");
  if ([c instancesRespondToSelector:init]) return ((id (*)(id, SEL, int))objc_msgSend)([c alloc], init, number);
  id a = [[c alloc] init];
  @try { [a setValue:@(number) forKey:@"actionNumber"]; } @catch (NSException *e) { S1(@"cannot set actionNumber: %@", e.reason); return nil; }
  return a;
}

@interface S1Tests : XCTestCase @end

@implementation S1Tests

- (id)axElementFor:(XCUIElement *)el {
  NSError *err = nil;
  id snap = [el snapshotWithError:&err];
  if (!snap) { S1(@"snapshot error %@", err); return nil; }
  @try { return [snap valueForKey:@"accessibilityElement"]; }
  @catch (NSException *e) { S1(@"no accessibilityElement on %@: %@", [snap class], e.reason); return nil; }
}

// Route A: runner -> testmanagerd, _XCT_performAccessibilityAction:onElement:withValue:reply:
- (NSString *)routeDaemon:(int)action el:(id)ax value:(id)value {
  Class cls = NSClassFromString(@"XCTRunnerDaemonSession");
  id session = ((id (*)(id, SEL))objc_msgSend)(cls, NSSelectorFromString(@"sharedSession"));
  id proxy = [session valueForKey:@"daemonProxy"];
  SEL sel = NSSelectorFromString(@"_XCT_performAccessibilityAction:onElement:withValue:reply:");
  if (![proxy respondsToSelector:sel]) return [NSString stringWithFormat:@"proxy %@ lacks selector", [proxy class]];
  __block NSString *res = @"timeout";
  dispatch_semaphore_t sem = dispatch_semaphore_create(0);
  ((void (*)(id, SEL, int, id, id, void (^)(NSError *)))objc_msgSend)(proxy, sel, action, ax, value, ^(NSError *e) {
    res = e ? [NSString stringWithFormat:@"error %@ %ld %@", e.domain, (long)e.code, e.localizedDescription] : @"ok";
    dispatch_semaphore_signal(sem);
  });
  dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC));
  return res;
}

// Route B: in-process XCAXClient_iOS performAction:onElement:value:error: (agent-device: kAXErrorCannotComplete)
- (NSString *)routeClient:(int)action el:(id)ax value:(id)value {
  id client = ((id (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"accessibilityInterface"));
  SEL sel = NSSelectorFromString(@"performAction:onElement:value:error:");
  if (![client respondsToSelector:sel]) return [NSString stringWithFormat:@"client %@ lacks selector", [client class]];
  NSError *err = nil;
  BOOL ok = ((BOOL (*)(id, SEL, id, id, id, NSError **))objc_msgSend)(client, sel, makeAction(action), ax, value, &err);
  return ok ? @"ok" : [NSString stringWithFormat:@"error %@ %ld %@", err.domain, (long)err.code, err.localizedDescription];
}

// Route C: the client's remoteAccessibilityInterface performAccessibilityAction:onElement:value:reply:
- (NSString *)routeRemote:(int)action el:(id)ax value:(id)value {
  id client = ((id (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"accessibilityInterface"));
  id remote = nil;
  @try { remote = [client valueForKey:@"remoteAccessibilityInterface"]; } @catch (NSException *e) { return [NSString stringWithFormat:@"no remote: %@", e.reason]; }
  SEL sel = NSSelectorFromString(@"performAccessibilityAction:onElement:value:reply:");
  if (![remote respondsToSelector:sel]) return [NSString stringWithFormat:@"remote %@ lacks selector", [remote class]];
  __block NSString *res = @"timeout";
  dispatch_semaphore_t sem = dispatch_semaphore_create(0);
  ((void (*)(id, SEL, id, id, id, void (^)(NSError *)))objc_msgSend)(remote, sel, makeAction(action), ax, value, ^(NSError *e) {
    res = e ? [NSString stringWithFormat:@"error %@ %ld %@", e.domain, (long)e.code, e.localizedDescription] : @"ok";
    dispatch_semaphore_signal(sem);
  });
  dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC));
  return res;
}

- (void)run:(NSString *)route action:(int)action target:(NSString *)ident app:(XCUIApplication *)app {
  XCUIElement *el = [[app descendantsMatchingType:XCUIElementTypeAny] matchingIdentifier:ident].firstMatch;
  id ax = [self axElementFor:el];
  if (!ax) { S1(@"route=%@ action=%d target=%@ result=no-ax-element", route, action, ident); return; }
  S1(@"begin route=%@ action=%d target=%@ ax=%@", route, action, ident, ax);
  CFAbsoluteTime t0 = CFAbsoluteTimeGetCurrent();
  NSString *r;
  @try {
    r = [route isEqualToString:@"A"] ? [self routeDaemon:action el:ax value:nil]
      : [route isEqualToString:@"B"] ? [self routeClient:action el:ax value:nil]
      : [self routeRemote:action el:ax value:nil];
  } @catch (NSException *e) { r = [NSString stringWithFormat:@"threw %@: %@", e.name, e.reason]; }
  S1(@"end route=%@ action=%d target=%@ result=%@ ms=%.0f", route, action, ident, r, (CFAbsoluteTimeGetCurrent() - t0) * 1000);
  [NSThread sleepForTimeInterval:1.5];
}

- (void)testS1 {
  XCUIApplication *app = [[XCUIApplication alloc] initWithBundleIdentifier:kApp];
  [app launch];
  [app.buttons[@"dta_button"] waitForExistenceWithTimeout:10];
  S1(@"launched; capabilities=%@", [((id (*)(id, SEL))objc_msgSend)(NSClassFromString(@"XCTRunnerDaemonSession"), NSSelectorFromString(@"sharedSession")) valueForKey:@"remoteInterfaceCapabilities"]);
  dumpClass(@"XCUIAccessibilityAction");
  for (int p = 0; p < 6; p++) {
    @try {
      id inc = ((id (*)(id, SEL, long))objc_msgSend)(NSClassFromString(@"XCUIAccessibilityAction"), NSSelectorFromString(@"incrementActionForPlatform:"), p);
      id dec = ((id (*)(id, SEL, long))objc_msgSend)(NSClassFromString(@"XCUIAccessibilityAction"), NSSelectorFromString(@"decrementActionForPlatform:"), p);
      S1(@"platform %d increment=%@ (%@) decrement=%@ (%@)", p, inc, [inc valueForKey:@"actionNumber"], dec, [dec valueForKey:@"actionNumber"]);
    } @catch (NSException *e) { S1(@"platform %d factory threw %@", p, e.reason); }
  }
  for (NSString *route in @[ @"A", @"B", @"C" ]) {
    [self run:route action:2010 target:@"dta_button" app:app];   // Press
    [self run:route action:2010 target:@"dta_custom" app:app];   // Press on a non-UIControl
    [self run:route action:2010 target:@"dta_switch" app:app];   // Press toggles?
    [self run:route action:2006 target:@"dta_scroll" app:app];   // ScrollDownByPage
  }
  // Increment/decrement have no known number; sweep the unknowns on the slider with the daemon route only.
  for (int a = 2000; a <= 2020; a++) {
    if (a == 2003 || a == 2006 || a == 2007 || a == 2008 || a == 2009 || a == 2010) continue;
    [self run:@"A" action:a target:@"dta_slider" app:app];
  }
  // Baseline: a synthesized tap, so the DTA log shows what a touch looks like.
  S1(@"begin route=tap target=dta_custom");
  [[[app descendantsMatchingType:XCUIElementTypeAny] matchingIdentifier:@"dta_custom"].firstMatch tap];
  S1(@"end route=tap target=dta_custom");
  [NSThread sleepForTimeInterval:1.0];
}

@end
