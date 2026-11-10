// S2-S4 spike server: NDJSON over TCP, one request per line, token-checked.
// Port 18100 binds 127.0.0.1, 18101 binds 0.0.0.0 (to test which one usbmux / the CoreDevice tunnel reach).
// Every request runs on the main thread (XCTest APIs); the socket threads only frame lines.
#import <XCTest/XCTest.h>
#import <objc/message.h>
#import <arpa/inet.h>
#import <netinet/in.h>
#import <sys/socket.h>
#import <unistd.h>
#import <mach/mach.h>
#import <objc/runtime.h>
#import <dlfcn.h>

@interface S2Server : XCTestCase
@property(nonatomic, copy) NSString *token;
@property(nonatomic, strong) NSMutableDictionary<NSString *, id> *axById;  // "pid/elementID" -> XCAccessibilityElement
@property(nonatomic, strong) NSMutableDictionary<NSString *, XCUIApplication *> *apps;
@property(atomic) BOOL done;
@property(nonatomic, strong) NSMutableArray<NSString *> *issues;  // XCTest failures recorded during the current request
@end

#ifndef S2_BUILD_ID
#define S2_BUILD_ID "unset"
#endif

static double nowMs(void) { return CFAbsoluteTimeGetCurrent() * 1000.0; }

static NSString *axKey(id ax) {
  // "AX element pid: 757, elementOrHash.elementID: 4385397904.10" -> "757/4385397904.10"
  NSString *d = [ax description];
  NSRange p = [d rangeOfString:@"pid: "], e = [d rangeOfString:@"elementID: "];
  if (p.location == NSNotFound || e.location == NSNotFound) return d;
  NSString *pid = [[d substringFromIndex:NSMaxRange(p)] componentsSeparatedByString:@","][0];
  return [NSString stringWithFormat:@"%@/%@", pid, [d substringFromIndex:NSMaxRange(e)]];
}


static id daemonProxy(void) {
  id session = ((id (*)(id, SEL))objc_msgSend)(NSClassFromString(@"XCTRunnerDaemonSession"), NSSelectorFromString(@"sharedSession"));
  return [session valueForKey:@"daemonProxy"];
}

// Spin the main run loop until done() or timeout; replies may be delivered on the main queue.
static BOOL spinUntil(BOOL (^done)(void), double seconds) {
  NSDate *end = [NSDate dateWithTimeIntervalSinceNow:seconds];
  while (!done() && [end timeIntervalSinceNow] > 0) [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.002]];
  return done();
}

// XCPointerEventPath + XCSynthesizedEventRecord through XCUIDevice.eventSynthesizer (WDA's path, BSD-3; re-typed, not copied).
static long gTapOrient = 0;  // 0 = derive from the device orientation
static long interfaceOrientation(void) {
  if (gTapOrient) return gTapOrient;
  switch ([XCUIDevice sharedDevice].orientation) {
    case UIDeviceOrientationLandscapeLeft: return UIInterfaceOrientationLandscapeRight;
    case UIDeviceOrientationLandscapeRight: return UIInterfaceOrientationLandscapeLeft;
    case UIDeviceOrientationPortraitUpsideDown: return UIInterfaceOrientationPortraitUpsideDown;
    default: return UIInterfaceOrientationPortrait;
  }
}
static NSString *synthesize(NSArray<NSValue *> *points, double duration, double hold) {
  CGPoint p0 = points[0].CGPointValue;
  id path = ((id (*)(id, SEL, CGPoint, double))objc_msgSend)([NSClassFromString(@"XCPointerEventPath") alloc], NSSelectorFromString(@"initForTouchAtPoint:offset:"), p0, 0.0);
  double t = hold;
  if (hold > 0) ((void (*)(id, SEL, CGPoint, double))objc_msgSend)(path, NSSelectorFromString(@"moveToPoint:atOffset:"), p0, hold);
  for (NSUInteger i = 1; i < points.count; i++) {
    t += duration / (points.count - 1);
    ((void (*)(id, SEL, CGPoint, double))objc_msgSend)(path, NSSelectorFromString(@"moveToPoint:atOffset:"), points[i].CGPointValue, t);
  }
  ((void (*)(id, SEL, double))objc_msgSend)(path, NSSelectorFromString(@"liftUpAtOffset:"), t + 0.05);
  Class rc = NSClassFromString(@"XCSynthesizedEventRecord");
  id rec = [rc instancesRespondToSelector:NSSelectorFromString(@"initWithName:displayID:interfaceOrientation:")]
    ? ((id (*)(id, SEL, id, unsigned long long, long))objc_msgSend)([rc alloc], NSSelectorFromString(@"initWithName:displayID:interfaceOrientation:"), @"droidctl", 0ULL, interfaceOrientation())
    : ((id (*)(id, SEL, id, long))objc_msgSend)([rc alloc], NSSelectorFromString(@"initWithName:interfaceOrientation:"), @"droidctl", interfaceOrientation());
  ((void (*)(id, SEL, id))objc_msgSend)(rec, NSSelectorFromString(@"addPointerEventPath:"), path);
  id synth = ((id (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"eventSynthesizer"));
  __block BOOL fin = NO; __block NSString *res = @"ok";
  ((void (*)(id, SEL, id, void (^)(BOOL, NSError *)))objc_msgSend)(synth, NSSelectorFromString(@"synthesizeEvent:completion:"), rec, ^(BOOL ok, NSError *e) {
    if (e) res = e.localizedDescription;
    fin = YES;
  });
  if (!spinUntil(^{ return fin; }, 20)) return @"timeout";
  return res;
}

static id pointerPath(NSArray<NSValue *> *points, double duration) {
  id path = ((id (*)(id, SEL, CGPoint, double))objc_msgSend)([NSClassFromString(@"XCPointerEventPath") alloc], NSSelectorFromString(@"initForTouchAtPoint:offset:"), points[0].CGPointValue, 0.0);
  double t = 0;
  for (NSUInteger i = 1; i < points.count; i++) {
    t += duration / (points.count - 1);
    ((void (*)(id, SEL, CGPoint, double))objc_msgSend)(path, NSSelectorFromString(@"moveToPoint:atOffset:"), points[i].CGPointValue, t);
  }
  ((void (*)(id, SEL, double))objc_msgSend)(path, NSSelectorFromString(@"liftUpAtOffset:"), t + 0.05);
  return path;
}

static NSString *synthesizePaths(NSArray *paths) {
  Class rc = NSClassFromString(@"XCSynthesizedEventRecord");
  long orient = (long)[XCUIDevice sharedDevice].orientation;
  if (orient < 1 || orient > 4) orient = UIInterfaceOrientationPortrait;
  id rec = ((id (*)(id, SEL, id, unsigned long long, long))objc_msgSend)([rc alloc], NSSelectorFromString(@"initWithName:displayID:interfaceOrientation:"), @"droidctl-multi", 0ULL, orient);
  for (id p in paths) ((void (*)(id, SEL, id))objc_msgSend)(rec, NSSelectorFromString(@"addPointerEventPath:"), p);
  id synth = ((id (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"eventSynthesizer"));
  __block BOOL fin = NO; __block NSString *res = @"ok";
  ((void (*)(id, SEL, id, void (^)(BOOL, NSError *)))objc_msgSend)(synth, NSSelectorFromString(@"synthesizeEvent:completion:"), rec, ^(BOOL ok, NSError *e) {
    if (e) res = e.localizedDescription;
    fin = YES;
  });
  if (!spinUntil(^{ return fin; }, 20)) return @"timeout";
  return res;
}

static double residentMB(void) {
  struct mach_task_basic_info info; mach_msg_type_number_t n = MACH_TASK_BASIC_INFO_COUNT;
  if (task_info(mach_task_self(), MACH_TASK_BASIC_INFO, (task_info_t)&info, &n) != KERN_SUCCESS) return -1;
  return info.resident_size / 1048576.0;
}


// ---- AX notification capture (settle research). Notifications reach XCTRunnerDaemonSession over XPC and are
// forwarded to its axNotificationHandler; we wrap both session entry points and record every arrival.
static NSMutableArray<NSDictionary *> *gAX;
static IMP gOrigRecv3, gOrigRecv2;
static NSString *axName(int n) {
  static NSString *(*f)(int);
  if (!f) f = (NSString * (*)(int))dlsym(RTLD_DEFAULT, "XCTStringFromAXNotification");
  return f ? (f(n) ?: @"") : @"?";
}
static NSString *shortPayload(id payload) {
  if (!payload) return nil;
  id obj = payload;
  if ([payload isKindOfClass:NSData.class]) {
    @try {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
      obj = [NSKeyedUnarchiver unarchiveObjectWithData:payload] ?: payload;
#pragma clang diagnostic pop
    } @catch (__unused NSException *e) {}
  }
  NSString *d = [[obj description] stringByReplacingOccurrencesOfString:@"\n" withString:@" "];
  return d.length > 600 ? [d substringToIndex:600] : d;
}
static NSString *axKey(id ax);
static void recordAX(int n, id el, id payload, int via) {
  NSDictionary *e = @{@"t" : @(CFAbsoluteTimeGetCurrent() * 1000.0), @"n" : @(n), @"name" : axName(n),
                      @"el" : el ? axKey(el) : @"", @"p" : shortPayload(payload) ?: @"",
                      @"main" : @([NSThread isMainThread]), @"via" : @(via)};
  @synchronized(gAX) { [gAX addObject:e]; if (gAX.count > 2000) [gAX removeObjectAtIndex:0]; }
}
static void hookRecv3(id self, SEL _cmd, int n, id el, id payload) {
  recordAX(n, el, payload, 3);
  ((void (*)(id, SEL, int, id, id))gOrigRecv3)(self, _cmd, n, el, payload);
}
static void hookRecv2(id self, SEL _cmd, int n, id payload) {
  recordAX(n, nil, payload, 2);
  ((void (*)(id, SEL, int, id))gOrigRecv2)(self, _cmd, n, payload);
}
static void installAXHooks(void) {
  gAX = [NSMutableArray array];
  Class c = NSClassFromString(@"XCTRunnerDaemonSession");
  Method m3 = class_getInstanceMethod(c, NSSelectorFromString(@"_XCT_receivedAccessibilityNotification:fromElement:payload:"));
  Method m2 = class_getInstanceMethod(c, NSSelectorFromString(@"_XCT_receivedAccessibilityNotification:withPayload:"));
  if (m3) gOrigRecv3 = method_setImplementation(m3, (IMP)hookRecv3);
  if (m2) gOrigRecv2 = method_setImplementation(m2, (IMP)hookRecv2);
  NSLog(@"S2 ax hooks m3=%d m2=%d", m3 != NULL, m2 != NULL);
}

// Cheap screen signature: type|frame|label|value of every node in a daemon snapshot.
static void sigWalk(id<XCUIElementSnapshot> s, NSMutableString *acc, int *count) {
  (*count)++;
  CGRect f = s.frame;
  // Only on-screen nodes count: off-screen recycled table cells come back with shuffled labels on every read.
  CGSize z = UIScreen.mainScreen.bounds.size;
  BOOL on = CGRectIntersectsRect(f, CGRectMake(0, 0, z.width, z.height)) && f.size.width > 0 && f.size.height > 0;
  if (on) [acc appendFormat:@"%lu|%.0f,%.0f,%.0f,%.0f|%@|%@;", (unsigned long)s.elementType, f.origin.x, f.origin.y, f.size.width, f.size.height, s.label ?: @"", s.value ?: @""];
  for (id<XCUIElementSnapshot> c in s.children) sigWalk(c, acc, count);
}

@implementation S2Server

- (XCUIApplication *)app:(NSString *)bundle {
  if (!bundle) bundle = @"com.apple.springboard";
  XCUIApplication *a = self.apps[bundle];
  if (!a) { a = [[XCUIApplication alloc] initWithBundleIdentifier:bundle]; self.apps[bundle] = a; }
  return a;
}

- (NSDictionary *)node:(id<XCUIElementSnapshot>)s depth:(int)depth count:(int *)count {
  (*count)++;
  NSMutableDictionary *n = [NSMutableDictionary dictionary];
  n[@"type"] = @(s.elementType);
  if (s.identifier.length) n[@"id"] = s.identifier;
  if (s.label.length) n[@"label"] = s.label;
  if (s.value && ![s.value isEqual:@""]) n[@"value"] = [s.value description];
  if (s.placeholderValue.length) n[@"ph"] = s.placeholderValue;
  CGRect f = s.frame;
  n[@"rect"] = @[ @(f.origin.x), @(f.origin.y), @(f.size.width), @(f.size.height) ];
  if (!s.enabled) n[@"disabled"] = @YES;
  if (s.selected) n[@"selected"] = @YES;
  if (s.hasFocus) n[@"focused"] = @YES;
  id ax = nil;
  @try { ax = [(id)s valueForKey:@"accessibilityElement"]; } @catch (__unused NSException *e) {}
  if (ax) { NSString *k = axKey(ax); n[@"ax"] = k; self.axById[k] = ax; }
  NSMutableArray *kids = [NSMutableArray array];
  for (id<XCUIElementSnapshot> c in s.children) [kids addObject:[self node:c depth:depth + 1 count:count]];
  if (kids.count) n[@"children"] = kids;
  return n;
}

- (NSString *)daemonAction:(int)action ax:(id)ax {
  return [self daemonAction:action ax:ax timeout:10];
}

- (NSString *)daemonAction:(int)action ax:(id)ax timeout:(double)timeout {
  id session = ((id (*)(id, SEL))objc_msgSend)(NSClassFromString(@"XCTRunnerDaemonSession"), NSSelectorFromString(@"sharedSession"));
  id proxy = [session valueForKey:@"daemonProxy"];
  __block NSString *res = @"timeout";
  dispatch_semaphore_t sem = dispatch_semaphore_create(0);
  ((void (*)(id, SEL, int, id, id, void (^)(NSError *)))objc_msgSend)(
      proxy, NSSelectorFromString(@"_XCT_performAccessibilityAction:onElement:withValue:reply:"), action, ax, nil, ^(NSError *e) {
        res = e ? ([e.localizedDescription containsString:@"kAXErrorCannotComplete"] ? @"refused" : e.localizedDescription) : @"ok";
        dispatch_semaphore_signal(sem);
      });
  dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(timeout * NSEC_PER_SEC)));
  return res;
}


// Lock guard: the front element at screen centre tells us who owns the screen. Only when that is
// SpringBoard do we pay for a SpringBoard snapshot to tell "locked" from "cover sheet up".
- (NSString *)screenState {
  __block id hitEl = nil; __block BOOL fin = NO;
  CGRect b = [XCUIScreen mainScreen].accessibilityFrame;
  if (CGRectIsEmpty(b)) b = CGRectMake(0, 0, 390, 844);
  ((void (*)(id, SEL, CGPoint, void (^)(id, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_requestElementAtPoint:reply:"),
      CGPointMake(CGRectGetMidX(b), CGRectGetMidY(b)), ^(id el, NSError *e) { hitEl = el; fin = YES; });
  spinUntil(^{ return fin; }, 5);
  if (hitEl) {
    int pid = [[axKey(hitEl) componentsSeparatedByString:@"/"][0] intValue];
    __block NSString *bid = nil; __block BOOL fin2 = NO;
    ((void (*)(id, SEL, int, void (^)(NSString *, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_requestBundleIDForPID:reply:"),
        pid, ^(NSString *b, NSError *e) { bid = b; fin2 = YES; });
    spinUntil(^{ return fin2; }, 5);
    if (bid && ![bid isEqualToString:@"com.apple.springboard"]) return @"clear";
  }
  // No hit (seen on a locked device with the screen off), or SpringBoard in front: look at SpringBoard.
  XCUIApplication *sb = [self app:@"com.apple.springboard"];
  NSError *err = nil;
  id<XCUIElementSnapshot> snap = [sb snapshotWithError:&err];
  BOOL cover = NO, locked = NO;
  NSMutableArray *stack = snap ? [NSMutableArray arrayWithObject:snap] : [NSMutableArray array];
  while (stack.count) {
    id<XCUIElementSnapshot> n = stack.lastObject; [stack removeLastObject];
    if ([n.identifier isEqualToString:@"SBCoverSheetWindow"]) cover = YES;
    if ([n.label isEqualToString:@"Locked"]) locked = YES;
    [stack addObjectsFromArray:n.children];
  }
  if (!snap) return @"screen-unknown";
  return locked ? @"locked" : cover ? @"cover-sheet" : (hitEl ? @"springboard" : @"screen-unknown");
}


- (id)daemonSnapshot:(NSString *)bundle attrs:(NSArray *)attrs params:(NSDictionary *)extra err:(NSString **)errOut {
  XCUIApplication *a = [self app:bundle];
  id root = [a valueForKey:@"accessibilityElement"];
  id client = ((id (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"accessibilityInterface"));
  NSMutableDictionary *params = [[client valueForKey:@"defaultParameters"] mutableCopy] ?: [NSMutableDictionary dictionary];
  for (NSString *k in extra) params[k] = extra[k];
  __block id snapOut = nil; __block NSError *snapErr = nil; __block BOOL fin = NO;
  ((void (*)(id, SEL, id, id, id, void (^)(id, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_requestSnapshotForElement:attributes:parameters:reply:"),
      root, attrs, params, ^(id snap, NSError *e) { snapOut = snap; snapErr = e; fin = YES; });
  spinUntil(^{ return fin; }, 30);
  if (!snapOut) { if (errOut) *errOut = snapErr.localizedDescription ?: @"timeout"; return nil; }
  if ([snapOut respondsToSelector:NSSelectorFromString(@"elementSnapshot")]) snapOut = [snapOut valueForKey:@"elementSnapshot"];
  return snapOut;
}

- (NSDictionary *)signature:(NSString *)bundle {
  return [self signature:bundle lines:nil];
}

- (NSDictionary *)signature:(NSString *)bundle lines:(NSMutableArray **)linesOut {
  static NSArray *attrs;
  if (!attrs) attrs = @[ @"XC_kAXXCAttributeElementType", @"XC_kAXXCAttributeLabel", @"XC_kAXXCAttributeValue", @"XC_kAXXCAttributeFrame" ];
  double t = nowMs(); NSString *err = nil;
  id snap = [self daemonSnapshot:bundle attrs:attrs params:nil err:&err];
  if (!snap) return @{@"err" : err ?: @"?", @"ms" : @(nowMs() - t)};
  NSMutableString *acc = [NSMutableString string]; int n = 0;
  sigWalk(snap, acc, &n);
  if (linesOut) *linesOut = [[acc componentsSeparatedByString:@";"] mutableCopy];
  // FNV-1a over the whole string (NSString.hash samples only ~96 characters, so it misses most changes).
  const char *b = acc.UTF8String; uint64_t h = 1469598103934665603ULL;
  for (; *b; b++) { h ^= (unsigned char)*b; h *= 1099511628211ULL; }
  return @{@"h" : [NSString stringWithFormat:@"%016llx", h], @"nodes" : @(n), @"ms" : @(nowMs() - t)};
}

- (NSString *)idle:(NSString *)kind bundle:(NSString *)bundle timeout:(double)timeout {
  id client = ((id (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"accessibilityInterface"));
  SEL sel = [kind isEqualToString:@"anim"] ? NSSelectorFromString(@"notifyWhenNoAnimationsAreActiveForApplication:reply:")
                                           : NSSelectorFromString(@"notifyWhenEventLoopIsIdleForApplication:reply:");
  if (![client respondsToSelector:sel]) return @"no-selector";
  __block BOOL fin = NO; __block NSString *res = @"ok";
  ((void (*)(id, SEL, id, void (^)(NSError *)))objc_msgSend)(client, sel, [self app:bundle], ^(NSError *e) {
    if (e) res = e.localizedDescription ?: @"error";
    fin = YES;
  });
  if (!spinUntil(^{ return fin; }, timeout)) return @"timeout";
  return res;
}

- (NSDictionary *)handle:(NSDictionary *)req {
  NSString *cmd = req[@"cmd"];
  double t0 = nowMs();
  NSMutableDictionary *r = [NSMutableDictionary dictionaryWithDictionary:@{@"ok" : @YES}];
  static NSSet *guarded;
  if (!guarded) guarded = [NSSet setWithArray:@[ @"act", @"fasttap", @"tap", @"swipe", @"pinch", @"type", @"sendstring", @"setattr", @"dlaunch", @"launch", @"activate" ]];
  if ([guarded containsObject:cmd] && ![req[@"force"] boolValue]) {
    NSString *st = [self screenState];
    r[@"guard_ms"] = @(nowMs() - t0);
    if ([st isEqualToString:@"locked"] || [st isEqualToString:@"cover-sheet"] || [st isEqualToString:@"screen-unknown"])
      return @{@"ok" : @NO, @"error" : st, @"guard_ms" : r[@"guard_ms"]};
  }
  if ([cmd isEqualToString:@"ping"]) {
    r[@"pid"] = @(getpid());
    r[@"build"] = @S2_BUILD_ID;
  } else if ([cmd isEqualToString:@"screen_state"]) {
    r[@"state"] = [self screenState];
  } else if ([cmd isEqualToString:@"launch"]) {
    [[self app:req[@"bundle"]] launch];
  } else if ([cmd isEqualToString:@"activate"]) {
    [[self app:req[@"bundle"]] activate];
  } else if ([cmd isEqualToString:@"state"]) {
    r[@"state"] = @([self app:req[@"bundle"]].state);
  } else if ([cmd isEqualToString:@"tree"]) {
    NSError *err = nil;
    double s0 = nowMs();
    id<XCUIElementSnapshot> snap = [[self app:req[@"bundle"]] snapshotWithError:&err];
    double s1 = nowMs();
    if (!snap) return @{@"ok" : @NO, @"error" : err.localizedDescription ?: @"snapshot failed"};
    int count = 0;
    r[@"tree"] = [self node:snap depth:0 count:&count];
    r[@"nodes"] = @(count);
    r[@"snapshot_ms"] = @(s1 - s0);
    r[@"walk_ms"] = @(nowMs() - s1);
  } else if ([cmd isEqualToString:@"act"]) {
    id ax = self.axById[req[@"ax"]];
    if (!ax) return @{@"ok" : @NO, @"error" : @"unknown ax"};
    r[@"result"] = [self daemonAction:[req[@"action"] intValue] ax:ax];
  } else if ([cmd isEqualToString:@"tap"]) {
    XCUIApplication *a = [self app:req[@"bundle"]];
    XCUICoordinate *c = [[a coordinateWithNormalizedOffset:CGVectorMake(0, 0)] coordinateWithOffset:CGVectorMake([req[@"x"] doubleValue], [req[@"y"] doubleValue])];
    [c tap];
  } else if ([cmd isEqualToString:@"type"]) {
    [[self app:req[@"bundle"]] typeText:req[@"text"]];
  } else if ([cmd isEqualToString:@"home"]) {
    [[XCUIDevice sharedDevice] pressButton:XCUIDeviceButtonHome];
  } else if ([cmd isEqualToString:@"screenshot"]) {
    NSData *png = [XCUIScreen mainScreen].screenshot.PNGRepresentation;
    r[@"bytes"] = @(png.length);
    if ([req[@"data"] boolValue]) r[@"png"] = [png base64EncodedStringWithOptions:0];
  } else if ([cmd isEqualToString:@"fasttap"]) {
    gTapOrient = [req[@"iorient"] longValue];
    r[@"result"] = synthesize(@[ [NSValue valueWithCGPoint:CGPointMake([req[@"x"] doubleValue], [req[@"y"] doubleValue])] ], 0, [req[@"hold"] doubleValue]);
  } else if ([cmd isEqualToString:@"swipe"]) {
    r[@"result"] = synthesize(@[ [NSValue valueWithCGPoint:CGPointMake([req[@"x1"] doubleValue], [req[@"y1"] doubleValue])],
                                 [NSValue valueWithCGPoint:CGPointMake([req[@"x2"] doubleValue], [req[@"y2"] doubleValue])] ],
                              [req[@"duration"] ?: @0.3 doubleValue], 0);
  } else if ([cmd isEqualToString:@"hit"]) {
    __block id hitEl = nil; __block NSError *hitErr = nil; __block BOOL fin = NO;
    ((void (*)(id, SEL, CGPoint, void (^)(id, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_requestElementAtPoint:reply:"),
        CGPointMake([req[@"x"] doubleValue], [req[@"y"] doubleValue]), ^(id el, NSError *e) { hitEl = el; hitErr = e; fin = YES; });
    spinUntil(^{ return fin; }, 10);
    if (hitEl) { NSString *k = axKey(hitEl); self.axById[k] = hitEl; r[@"ax"] = k; }
    if (hitErr) r[@"error"] = hitErr.localizedDescription;
  } else if ([cmd isEqualToString:@"setattr"]) {
    id ax = self.axById[req[@"ax"]];
    if (!ax) return @{@"ok" : @NO, @"error" : @"unknown ax"};
    __block BOOL fin = NO, okv = NO; __block NSError *err2 = nil;
    ((void (*)(id, SEL, id, id, id, void (^)(BOOL, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_setAttribute:value:element:reply:"),
        req[@"attr"], req[@"value"], ax, ^(BOOL ok, NSError *e) { okv = ok; err2 = e; fin = YES; });
    spinUntil(^{ return fin; }, 10);
    r[@"result"] = fin ? (okv ? @"ok" : (err2.localizedDescription ?: @"false")) : @"timeout";
  } else if ([cmd isEqualToString:@"fetch"]) {
    id ax = self.axById[req[@"ax"]];
    if (!ax) return @{@"ok" : @NO, @"error" : @"unknown ax"};
    __block BOOL fin = NO; __block NSDictionary *vals = nil; __block NSError *err2 = nil;
    ((void (*)(id, SEL, id, id, void (^)(NSDictionary *, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_fetchAttributes:forElement:reply:"),
        req[@"attrs"], ax, ^(NSDictionary *d, NSError *e) { vals = d; err2 = e; fin = YES; });
    spinUntil(^{ return fin; }, 10);
    NSMutableDictionary *o = [NSMutableDictionary dictionary];
    for (id k in vals) {
      id v = vals[k];
      if ([NSStringFromClass([v class]) containsString:@"AccessibilityElement"]) { NSString *ak = axKey(v); self.axById[ak] = v; o[[k description]] = ak; }
      else o[[k description]] = [v description];
    }
    r[@"values"] = o;
    if (err2) r[@"error"] = err2.localizedDescription;
  } else if ([cmd isEqualToString:@"sendstring"]) {
    __block BOOL fin = NO; __block NSError *err2 = nil;
    ((void (*)(id, SEL, id, unsigned long long, void (^)(NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_sendString:maximumFrequency:completion:"),
        req[@"text"], (unsigned long long)[req[@"freq"] ?: @60 longLongValue], ^(NSError *e) { err2 = e; fin = YES; });
    spinUntil(^{ return fin; }, 60);
    r[@"result"] = fin ? (err2 ? err2.localizedDescription : @"ok") : @"timeout";
  } else if ([cmd isEqualToString:@"bundle_for_pid"]) {
    __block BOOL fin = NO; __block NSString *bid = nil;
    ((void (*)(id, SEL, int, void (^)(NSString *, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_requestBundleIDForPID:reply:"),
        [req[@"pid"] intValue], ^(NSString *b, NSError *e) { bid = b; fin = YES; });
    spinUntil(^{ return fin; }, 10);
    r[@"bundle"] = bid ?: [NSNull null];
  } else if ([cmd isEqualToString:@"snapfast"]) {
    // Daemon snapshot with explicit attributes/parameters (the path WDA and agent-device use for speed).
    XCUIApplication *a = [self app:req[@"bundle"]];
    id root = [a valueForKey:@"accessibilityElement"];
    id client = ((id (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"accessibilityInterface"));
    NSMutableDictionary *params = [[client valueForKey:@"defaultParameters"] mutableCopy] ?: [NSMutableDictionary dictionary];
    if ([req[@"show_params"] boolValue]) r[@"default_params"] = [params description];
    for (NSString *k in req[@"params"]) params[k] = req[@"params"][k];
    NSArray *attrs = req[@"attrs"] ?: @[ @"XC_kAXXCAttributeElementType", @"XC_kAXXCAttributeIdentifier", @"XC_kAXXCAttributeLabel",
                                          @"XC_kAXXCAttributeValue", @"XC_kAXXCAttributeFrame", @"XC_kAXXCAttributeIsEnabled",
                                          @"XC_kAXXCAttributeIsSelected", @"XC_kAXXCAttributeHasFocus", @"XC_kAXXCAttributePlaceholderValue" ];
    __block id snapOut = nil; __block NSError *snapErr = nil; __block BOOL fin = NO;
    double s0 = nowMs();
    ((void (*)(id, SEL, id, id, id, void (^)(id, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_requestSnapshotForElement:attributes:parameters:reply:"),
        root, attrs, params, ^(id snap, NSError *e) { snapOut = snap; snapErr = e; fin = YES; });
    spinUntil(^{ return fin; }, [req[@"timeout"] ?: @60 doubleValue]);
    double s1 = nowMs();
    if (!snapOut) return @{@"ok" : @NO, @"error" : snapErr.localizedDescription ?: @"timeout", @"snapshot_ms" : @(s1 - s0)};
    if ([snapOut respondsToSelector:NSSelectorFromString(@"elementSnapshot")]) snapOut = [snapOut valueForKey:@"elementSnapshot"];
    int count = 0;
    r[@"tree"] = [self node:snapOut depth:0 count:&count];
    r[@"nodes"] = @(count);
    r[@"snapshot_ms"] = @(s1 - s0);
    r[@"walk_ms"] = @(nowMs() - s1);
  } else if ([cmd isEqualToString:@"orientation"]) {
    if (req[@"set"]) [XCUIDevice sharedDevice].orientation = (UIDeviceOrientation)[req[@"set"] integerValue];
    r[@"orientation"] = @([XCUIDevice sharedDevice].orientation);
  } else if ([cmd isEqualToString:@"pinch"]) {
    double x = [req[@"x"] doubleValue], y = [req[@"y"] doubleValue], a = [req[@"from"] doubleValue], b = [req[@"to"] doubleValue], d = [req[@"duration"] ?: @0.5 doubleValue];
    NSArray *p1 = @[ [NSValue valueWithCGPoint:CGPointMake(x - a, y)], [NSValue valueWithCGPoint:CGPointMake(x - b, y)] ];
    NSArray *p2 = @[ [NSValue valueWithCGPoint:CGPointMake(x + a, y)], [NSValue valueWithCGPoint:CGPointMake(x + b, y)] ];
    r[@"result"] = synthesizePaths(@[ pointerPath(p1, d), pointerPath(p2, d) ]);
  } else if ([cmd isEqualToString:@"dlaunch"]) {
    __block BOOL fin = NO; __block NSError *lerr = nil;
    ((void (*)(id, SEL, id, id, id, void (^)(NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_launchApplicationWithBundleID:arguments:environment:completion:"),
        req[@"bundle"], @[], @{}, ^(NSError *e) { lerr = e; fin = YES; });
    spinUntil(^{ return fin; }, 30);
    r[@"result"] = fin ? (lerr ? lerr.localizedDescription : @"ok") : @"timeout";
  } else if ([cmd isEqualToString:@"lock"]) {
    ((void (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"pressLockButton"));
  } else if ([cmd isEqualToString:@"methods"]) {
    // Introspection: selectors + type encodings of a class (instance and class side) matching a substring.
    Class c = NSClassFromString(req[@"cls"]);
    if (!c) return @{@"ok" : @NO, @"error" : @"no class"};
    NSString *f = [req[@"filter"] lowercaseString] ?: @"";
    NSMutableArray *out = [NSMutableArray array];
    for (int meta = 0; meta < 2; meta++) {
      unsigned n = 0; Method *m = class_copyMethodList(meta ? object_getClass(c) : c, &n);
      for (unsigned i = 0; i < n; i++) {
        NSString *name = NSStringFromSelector(method_getName(m[i]));
        if (f.length && ![[name lowercaseString] containsString:f]) continue;
        [out addObject:[NSString stringWithFormat:@"%@%@ %s", meta ? @"+" : @"-", name, method_getTypeEncoding(m[i])]];
      }
      free(m);
    }
    r[@"methods"] = out;
    r[@"super"] = NSStringFromClass(class_getSuperclass(c)) ?: @"";
  } else if ([cmd isEqualToString:@"protos"]) {
    // Protocol method descriptions (names + types) for protocols whose name contains `filter`, limited to selectors containing `sel`.
    unsigned n = 0; Protocol * __unsafe_unretained *ps = objc_copyProtocolList(&n);
    NSMutableDictionary *out = [NSMutableDictionary dictionary];
    for (unsigned i = 0; i < n; i++) {
      NSString *pn = @(protocol_getName(ps[i]));
      if (![pn containsString:req[@"filter"] ?: @"XCT"]) continue;
      NSMutableArray *ms = [NSMutableArray array];
      for (int reqd = 0; reqd < 2; reqd++) {
        unsigned k = 0; struct objc_method_description *d = protocol_copyMethodDescriptionList(ps[i], reqd, YES, &k);
        for (unsigned j = 0; j < k; j++) {
          NSString *sn = NSStringFromSelector(d[j].name);
          if (req[@"sel"] && ![[sn lowercaseString] containsString:[req[@"sel"] lowercaseString]]) continue;
          [ms addObject:[NSString stringWithFormat:@"%@ %s", sn, d[j].types]];
        }
        free(d);
      }
      if (ms.count) out[pn] = ms;
    }
    free(ps);
    r[@"protos"] = out;
  } else if ([cmd isEqualToString:@"axnames"]) {
    NSMutableDictionary *o = [NSMutableDictionary dictionary];
    for (int i = [req[@"from"] intValue]; i <= [req[@"to"] intValue]; i++) { NSString *nm = axName(i); if (nm.length && ![nm hasPrefix:@"Unknown"]) o[[@(i) stringValue]] = nm; }
    r[@"names"] = o;
  } else if ([cmd isEqualToString:@"axreg"]) {
    id client = ((id (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"accessibilityInterface"));
    NSMutableDictionary *o = [NSMutableDictionary dictionary];
    for (NSNumber *n in req[@"ids"]) {
      double t = nowMs(); NSError *e = nil;
      BOOL ok = ((BOOL (*)(id, SEL, int, NSError **))objc_msgSend)(client, NSSelectorFromString(@"_registerForAXNotification:error:"), n.intValue, &e);
      o[n.stringValue] = [NSString stringWithFormat:@"%@ %.0fms %@", ok ? @"ok" : @"fail", nowMs() - t, e.localizedDescription ?: @""];
    }
    r[@"reg"] = o;
  } else if ([cmd isEqualToString:@"axevents"]) {
    @synchronized(gAX) { r[@"events"] = [gAX copy]; if ([req[@"clear"] boolValue]) [gAX removeAllObjects]; }
    r[@"now"] = @(nowMs());
  } else if ([cmd isEqualToString:@"sig"]) {
    [r addEntriesFromDictionary:[self signature:req[@"bundle"]]];
  } else if ([cmd isEqualToString:@"idle"]) {
    double t = nowMs();
    r[@"result"] = [self idle:req[@"kind"] bundle:req[@"bundle"] timeout:[req[@"timeout"] ?: @10 doubleValue]];
    r[@"idle_ms"] = @(nowMs() - t);
  } else if ([cmd isEqualToString:@"watch"]) {
    // Run an inner command, then for watch_ms record: AX notifications, screen-signature changes (polled), and
    // when the run-loop-idle / animations-idle callbacks fire. All times are ms after the inner command started.
    NSString *bundle = req[@"bundle"];
    double watch = [req[@"watch_ms"] ?: @3000 doubleValue], poll = [req[@"poll_ms"] ?: @0 doubleValue];
    NSMutableArray *prevLines = nil;
    NSDictionary *s0 = poll > 0 ? [self signature:bundle lines:&prevLines] : nil;
    @synchronized(gAX) { [gAX removeAllObjects]; }
    double t0 = nowMs();
    NSMutableDictionary *inner = [req[@"do"] mutableCopy];
    NSDictionary *ir = [self handle:inner];
    double tDone = nowMs() - t0;
    NSMutableArray *idles = [NSMutableArray array];
    id client = ((id (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"accessibilityInterface"));
    for (NSString *kind in req[@"idles"] ?: @[]) {
      // Fire and record; the callbacks arrive while the poll loop below spins the run loop.
      SEL sel = [kind isEqualToString:@"anim"] ? NSSelectorFromString(@"notifyWhenNoAnimationsAreActiveForApplication:reply:")
                                               : NSSelectorFromString(@"notifyWhenEventLoopIsIdleForApplication:reply:");
      NSMutableDictionary *rec = [@{@"kind" : kind, @"start" : @(nowMs() - t0)} mutableCopy];
      [idles addObject:rec];
      ((void (*)(id, SEL, id, void (^)(NSError *)))objc_msgSend)(client, sel, [self app:bundle], ^(NSError *e) {
        @synchronized(rec) { if (!rec[@"end"]) { rec[@"end"] = @(nowMs() - t0); rec[@"result"] = e ? (e.localizedDescription ?: @"error") : @"ok"; rec[@"main"] = @([NSThread isMainThread]); } }
      });
    }
    NSMutableArray *sigs = [NSMutableArray array];
    NSString *last = s0[@"h"];
    if (s0) [sigs addObject:@{@"t" : @0, @"h" : s0[@"h"] ?: @"", @"nodes" : s0[@"nodes"] ?: @0, @"ms" : s0[@"ms"] ?: @0, @"pre" : @YES}];
    while (nowMs() - t0 < watch) {
      if (poll > 0) {
        double ts = nowMs() - t0;
        NSMutableArray *lines = nil;
        NSDictionary *sg = [self signature:bundle lines:&lines];
        if (![sg[@"h"] isEqual:last]) {
          NSMutableArray *added = [NSMutableArray array], *removed = [NSMutableArray array];
          NSCountedSet *a = [NSCountedSet setWithArray:lines ?: @[]], *b = [NSCountedSet setWithArray:prevLines ?: @[]];
          for (NSString *l in lines) if ([a countForObject:l] > [b countForObject:l] && added.count < 4) [added addObject:l.length > 90 ? [l substringToIndex:90] : l];
          for (NSString *l in prevLines) if ([b countForObject:l] > [a countForObject:l] && removed.count < 4) [removed addObject:l.length > 90 ? [l substringToIndex:90] : l];
          [sigs addObject:@{@"t" : @(ts), @"te" : @(nowMs() - t0), @"h" : sg[@"h"] ?: @"", @"nodes" : sg[@"nodes"] ?: @0, @"ms" : sg[@"ms"] ?: @0, @"add" : added, @"rem" : removed}];
          last = sg[@"h"]; prevLines = lines;
        }
        spinUntil(^{ return NO; }, poll / 1000.0);
      } else spinUntil(^{ return NO; }, 0.05);
    }
    NSMutableArray *ev = [NSMutableArray array];
    @synchronized(gAX) { for (NSDictionary *e in gAX) { NSMutableDictionary *m = [e mutableCopy]; m[@"t"] = @([e[@"t"] doubleValue] - t0); [ev addObject:m]; } }
    r[@"t0abs"] = @(t0); r[@"inner"] = ir; r[@"inner_ms"] = @(tDone); r[@"events"] = ev; r[@"sigs"] = sigs; r[@"idles"] = idles;
  } else if ([cmd isEqualToString:@"sweep"]) {
    // Perform every AX action number in [from, to] on one element, gap_ms apart; times are device-clock ms (CFAbsoluteTime).
    id ax = self.axById[req[@"ax"]];
    if (!ax) return @{@"ok" : @NO, @"error" : @"unknown ax"};
    NSMutableArray *o = [NSMutableArray array];
    double gap = [req[@"gap_ms"] ?: @60 doubleValue] / 1000.0;
    NSSet *skip = [NSSet setWithArray:req[@"skip"] ?: @[]];
    for (int n = [req[@"from"] intValue]; n <= [req[@"to"] intValue]; n++) {
      if ([skip containsObject:@(n)]) continue;
      @autoreleasepool {
        double t = nowMs();
        NSString *res = [self daemonAction:n ax:ax timeout:[req[@"timeout_ms"] ?: @300 doubleValue] / 1000.0];
        [o addObject:@[ @(n), @(t), res ]];
        spinUntil(^{ return NO; }, gap);
      }
    }
    r[@"results"] = o;
  } else if ([cmd isEqualToString:@"settle"]) {
    // Settle rule v2 (IOS.md §13.3 plus re-arming): run the inner command, request run-loop and animations idle,
    // and once both replied wait quiet_ms without a change notification. A change notification that arrives after
    // the replies re-arms both idle requests (up to `rearm` times), because an animation that starts after the
    // request (the keyboard after setAttribute) is not covered by the earlier reply.
    NSString *bundle = req[@"bundle"];
    double quiet = [req[@"quiet_ms"] ?: @100 doubleValue], cap = [req[@"timeout_ms"] ?: @3000 doubleValue];
    int maxRearm = [req[@"rearm"] ?: @5 intValue];
    static NSSet *changeN;
    if (!changeN) changeN = [NSSet setWithArray:@[ @1000, @1001, @1006, @1008, @1009, @1017, @1018, @1020, @1021, @1025, @1031, @3031 ]];
    BOOL (^isChange)(NSDictionary *) = ^BOOL(NSDictionary *e) {
      if ([changeN containsObject:e[@"n"]]) return YES;
      if ([e[@"n"] intValue] != 4002) return NO;
      NSString *p = e[@"p"];
      return [p containsString:@"ViewDidAppear"] || [p containsString:@"ViewDidDisappear"] || [p containsString:@"KeyEventComplete"];
    };
    @synchronized(gAX) { [gAX removeAllObjects]; }
    double t0 = nowMs();
    NSDictionary *ir = [self handle:[req[@"do"] mutableCopy]];
    double tDone = nowMs() - t0;
    id client = ((id (*)(id, SEL))objc_msgSend)([XCUIDevice sharedDevice], NSSelectorFromString(@"accessibilityInterface"));
    NSMutableArray *rounds = [NSMutableArray array];
    BOOL settled = NO; int rearms = 0;
    double (^lastChange)(void) = ^double {
      double l = -1;
      @synchronized(gAX) { for (NSDictionary *e in gAX) if (isChange(e)) l = MAX(l, [e[@"t"] doubleValue] - t0); }
      return l;
    };
    // rearm_on: only these notifications re-arm (1031 only when the keyboard is appearing); default: any change.
    NSSet *rearmOn = req[@"rearm_on"] ? [NSSet setWithArray:req[@"rearm_on"]] : nil;
    double (^lastTrigger)(void) = ^double {
      if (!rearmOn) return lastChange();
      double l = -1;
      @synchronized(gAX) {
        for (NSDictionary *e in gAX) {
          if (![rearmOn containsObject:e[@"n"]]) continue;
          if ([e[@"n"] intValue] == 1031 && ![e[@"p"] containsString:@"visible = 1"]) continue;
          l = MAX(l, [e[@"t"] doubleValue] - t0);
        }
      }
      return l;
    };
    while (!settled && nowMs() - t0 < cap) {
      double armed = nowMs() - t0;
      __block int left = 2;
      for (NSString *kind in @[ @"runloop", @"anim" ]) {
        SEL sel = [kind isEqualToString:@"anim"] ? NSSelectorFromString(@"notifyWhenNoAnimationsAreActiveForApplication:reply:")
                                                 : NSSelectorFromString(@"notifyWhenEventLoopIsIdleForApplication:reply:");
        ((void (*)(id, SEL, id, void (^)(NSError *)))objc_msgSend)(client, sel, [self app:bundle], ^(NSError *e) { left--; });
      }
      spinUntil(^{ return (BOOL)(left <= 0); }, MAX(0, (cap - (nowMs() - t0)) / 1000.0));
      double replied = nowMs() - t0;
      [rounds addObject:@[ @(round(armed)), @(round(replied)) ]];
      if (left > 0) break;
      BOOL rearm = NO;
      while (nowMs() - t0 < cap) {
        spinUntil(^{ return NO; }, 0.005);
        double l = lastChange(), now = nowMs() - t0;
        if (lastTrigger() > replied && rearms < maxRearm) { rearms++; rearm = YES; break; }
        if (now - MAX(replied, l) >= quiet) { settled = YES; break; }
      }
      if (!rearm && !settled) break;
    }
    NSMutableArray *ev = [NSMutableArray array];
    @synchronized(gAX) { for (NSDictionary *e in gAX) [ev addObject:@[ @(round([e[@"t"] doubleValue] - t0)), e[@"n"], e[@"el"], [e[@"p"] length] > 60 ? [e[@"p"] substringToIndex:60] : e[@"p"] ]]; }
    r[@"inner"] = ir; r[@"inner_ms"] = @(tDone); r[@"settle_ms"] = @(nowMs() - t0); r[@"idle"] = @(settled);
    r[@"rearms"] = @(rearms); r[@"rounds"] = rounds; r[@"events"] = ev;
  } else if ([cmd isEqualToString:@"syms"]) {
    // Read uint32 globals by symbol name (UIKit's UIAccessibility*Notification constants), to name the AX notification numbers.
    NSMutableDictionary *o = [NSMutableDictionary dictionary];
    for (NSString *name in req[@"names"]) {
      void *p = dlsym(RTLD_DEFAULT, name.UTF8String);
      o[name] = p ? @(*(uint32_t *)p) : [NSNull null];
    }
    r[@"values"] = o;
  } else if ([cmd isEqualToString:@"appax"]) {
    // Register the application element so fetch/setattr can address it (ResponderElement, ActiveKeyboard).
    id ax = [[self app:req[@"bundle"]] valueForKey:@"accessibilityElement"];
    NSString *k = axKey(ax); self.axById[k] = ax; r[@"ax"] = k;
  } else if ([cmd isEqualToString:@"stats"]) {
    r[@"rss_mb"] = @(residentMB());
    r[@"ax_cache"] = @(self.axById.count);
  } else if ([cmd isEqualToString:@"forget"]) {
    [self.axById removeAllObjects];
  } else if ([cmd isEqualToString:@"quit"]) {
    self.done = YES;
  } else {
    return @{@"ok" : @NO, @"error" : [NSString stringWithFormat:@"unknown cmd %@", cmd]};
  }
  r[@"ms"] = @(nowMs() - t0);
  return r;
}

- (void)serveClient:(int)fd {
  FILE *in = fdopen(dup(fd), "r");
  char *line = NULL; size_t cap = 0; ssize_t len;
  while ((len = getline(&line, &cap, in)) > 0) @autoreleasepool {
    NSData *data = [NSData dataWithBytes:line length:len];
    NSDictionary *req = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    __block NSDictionary *resp;
    if (![req isKindOfClass:NSDictionary.class]) resp = @{@"ok" : @NO, @"error" : @"bad json"};
    else if (![req[@"token"] isEqual:self.token]) resp = @{@"ok" : @NO, @"error" : @"bad token"};
    else dispatch_sync(dispatch_get_main_queue(), ^{
      @try { @autoreleasepool {
        [self.issues removeAllObjects];
        resp = [self handle:req];
        if (self.issues.count) { NSMutableDictionary *m = [resp mutableCopy]; m[@"ok"] = @NO; m[@"issues"] = [self.issues copy]; resp = m; }
      } } @catch (NSException *e) { resp = @{@"ok" : @NO, @"error" : [NSString stringWithFormat:@"%@: %@", e.name, e.reason]}; }
    });
    if (req[@"id"]) { NSMutableDictionary *m = [resp mutableCopy]; m[@"id"] = req[@"id"]; resp = m; }
    NSMutableData *out = [[NSJSONSerialization dataWithJSONObject:resp options:0 error:nil] mutableCopy];
    [out appendBytes:"\n" length:1];
    write(fd, out.bytes, out.length);
  }
  free(line); fclose(in); close(fd);
}

- (void)listenOn:(uint16_t)port loopback:(BOOL)loopback {
  int s = socket(AF_INET, SOCK_STREAM, 0);
  int one = 1; setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  struct sockaddr_in a = {.sin_len = sizeof a, .sin_family = AF_INET, .sin_port = htons(port)};
  a.sin_addr.s_addr = htonl(loopback ? INADDR_LOOPBACK : INADDR_ANY);
  if (bind(s, (struct sockaddr *)&a, sizeof a) != 0 || listen(s, 4) != 0) { NSLog(@"S2 listen %d failed errno=%d", port, errno); return; }
  NSLog(@"S2 listening port=%d loopback=%d", port, loopback);
  [NSThread detachNewThreadWithBlock:^{
    while (!self.done) {
      int c = accept(s, NULL, NULL);
      if (c < 0) continue;
      [NSThread detachNewThreadWithBlock:^{ [self serveClient:c]; }];
    }
  }];
}

- (void)listenOn6:(uint16_t)port {
  int s = socket(AF_INET6, SOCK_STREAM, 0);
  int one = 1; setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  struct sockaddr_in6 a = {.sin6_len = sizeof a, .sin6_family = AF_INET6, .sin6_port = htons(port), .sin6_addr = in6addr_any};
  if (bind(s, (struct sockaddr *)&a, sizeof a) != 0 || listen(s, 4) != 0) { NSLog(@"S2 listen6 %d failed errno=%d", port, errno); return; }
  NSLog(@"S2 listening port=%d ipv6 any", port);
  [NSThread detachNewThreadWithBlock:^{
    while (!self.done) { int c = accept(s, NULL, NULL); if (c >= 0) [NSThread detachNewThreadWithBlock:^{ [self serveClient:c]; }]; }
  }];
}

// XCTest failures (failed launch/activate/typeText, ...) must become error replies, never end the test.
- (void)recordIssue:(XCTIssue *)issue {
  NSString *d = issue.compactDescription ?: @"issue";
  NSLog(@"S2 issue swallowed: %@", d);
  [self.issues addObject:d];
}

- (void)testServe {
  self.continueAfterFailure = YES;
  self.issues = [NSMutableArray array];
  self.token = NSProcessInfo.processInfo.environment[@"S2_TOKEN"] ?: @"";
  XCTAssert(self.token.length >= 16, @"S2_TOKEN missing");
  self.axById = [NSMutableDictionary dictionary];
  installAXHooks();
  self.apps = [NSMutableDictionary dictionary];
  [self listenOn:18100 loopback:YES];
  [self listenOn:18101 loopback:NO];
  if (NSProcessInfo.processInfo.environment[@"S2_IPV6"]) [self listenOn6:18102];
  NSLog(@"S2 ready pid=%d", getpid());
  while (!self.done) [[NSRunLoop mainRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];
}

@end
