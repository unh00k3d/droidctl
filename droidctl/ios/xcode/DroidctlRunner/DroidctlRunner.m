// droidctl's iOS runner: one XCUITest that never returns and serves droidctl's agent
// protocol (android/agent/PROTOCOL.md: NDJSON JSON-RPC 2.0) on 127.0.0.1:$DROIDCTL_PORT.
// The host reaches it through usbmuxd. Design and the rules R1-R12 cited below: IOS.md §6-§13.
//
// Every private call is reached by selector and guarded with respondsToSelector:, so a
// missing one fails as "unsupported" instead of crashing (IOS.md §7).
#import <XCTest/XCTest.h>
#import <objc/message.h>
#import <objc/runtime.h>
#import <dlfcn.h>
#import <arpa/inet.h>
#import <netinet/in.h>
#import <netinet/tcp.h>
#import <sys/socket.h>
#import <sys/sysctl.h>
#import <unistd.h>

#ifndef DROIDCTL_BUILD_ID
#define DROIDCTL_BUILD_ID "dev"
#endif
static NSString *const kVersion = @"0.2.0";
static const int kProtocol = 3;
static const uint16_t kDefaultPort = 27100;
static const NSUInteger kMaxLine = 1 << 20;

// JSON-RPC error codes (PROTOCOL.md "Error codes")
enum { EParse = -32700, EInvalid = -32600, ENoMethod = -32601, EParams = -32602, EInternal = -32603,
       EStale = -32002, EGone = -32003, EDisabled = -32004, ENotVisible = -32005, EUnsupported = -32006,
       ETimeout = -32007, EScreen = -32010, EOccluded = -32011, ENotFocused = -32012 };

static NSError *rpcError(int code, NSString *msg) {
  return [NSError errorWithDomain:@"droidctl" code:code userInfo:@{NSLocalizedDescriptionKey : msg ?: @"error"}];
}

static double nowMs(void) { return CFAbsoluteTimeGetCurrent() * 1000.0; }
static double gStartMs;

// Wait by spinning the main run loop: daemon replies can arrive on the main queue (R8).
static BOOL spinUntil(BOOL (^done)(void), double seconds) {
  NSDate *end = [NSDate dateWithTimeIntervalSinceNow:seconds];
  while (!done() && [end timeIntervalSinceNow] > 0)
    [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.002]];
  return done();
}

static id msg0(id target, NSString *sel) {
  SEL s = NSSelectorFromString(sel);
  if (!target || ![target respondsToSelector:s]) return nil;
  return ((id (*)(id, SEL))objc_msgSend)(target, s);
}

static id daemonProxy(void) {
  id session = msg0(NSClassFromString(@"XCTRunnerDaemonSession"), @"sharedSession");
  @try { return [session valueForKey:@"daemonProxy"]; } @catch (__unused NSException *e) { return nil; }
}

static id appElement(int pid);
static id axInterface(void) { return msg0([XCUIDevice sharedDevice], @"accessibilityInterface"); }

static BOOL proxyHas(NSString *sel) { return [daemonProxy() respondsToSelector:NSSelectorFromString(sel)]; }

// "AX element pid: 757, elementOrHash.elementID: 4385397904.10" -> "757/4385397904.10"
static NSString *axKey(id ax) {
  NSString *d = [ax description];
  NSRange p = [d rangeOfString:@"pid: "], e = [d rangeOfString:@"elementID: "];
  if (p.location != NSNotFound && e.location == NSNotFound)  // the application element: "AX element pid: 757"
    return [NSString stringWithFormat:@"%d/app", [[d substringFromIndex:NSMaxRange(p)] intValue]];
  if (p.location == NSNotFound || e.location == NSNotFound) return d;
  NSString *pid = [[d substringFromIndex:NSMaxRange(p)] componentsSeparatedByString:@","][0];
  return [NSString stringWithFormat:@"%@/%@", pid, [d substringFromIndex:NSMaxRange(e)]];
}
static int axPid(id ax) { return [[axKey(ax) componentsSeparatedByString:@"/"][0] intValue]; }

// ---------------------------------------------------------------------------------------
// Connections
@interface DRConn : NSObject
@property(nonatomic) int fd;
@property(nonatomic, strong) NSLock *wlock;
@property(atomic, strong) NSSet<NSString *> *subscribed;  // nil = not subscribed; empty = all types
@end
@implementation DRConn
- (BOOL)send:(NSDictionary *)obj {
  NSData *json = [NSJSONSerialization dataWithJSONObject:obj options:0 error:nil];
  if (!json) json = [NSJSONSerialization dataWithJSONObject:@{@"jsonrpc" : @"2.0", @"id" : obj[@"id"] ?: [NSNull null],
                                                              @"error" : @{@"code" : @(EInternal), @"message" : @"unserializable reply"}}
                                                    options:0 error:nil];
  NSMutableData *out = [json mutableCopy];
  [out appendBytes:"\n" length:1];
  [self.wlock lock];
  const uint8_t *b = out.bytes; size_t left = out.length; BOOL ok = YES;
  while (left > 0) {
    ssize_t n = write(self.fd, b, left);
    if (n <= 0) { ok = NO; break; }
    b += n; left -= n;
  }
  [self.wlock unlock];
  return ok;
}
@end

// ---------------------------------------------------------------------------------------
// AX notifications -> gen and events (IOS.md §13.1; research/ios-decision.md §15.1-15.2)
@class DroidctlRunner;
static __weak DroidctlRunner *gRunner;
static IMP gOrigRecv3;
static void hookRecv3(id self, SEL _cmd, int n, id el, id payload);

@interface DroidctlRunner : XCTestCase
@property(atomic) BOOL done;
@property(nonatomic, strong) NSMutableArray<NSString *> *issues;
@property(nonatomic, strong) NSMutableDictionary<NSString *, XCUIApplication *> *apps;
@property(nonatomic, strong) NSMutableDictionary<NSNumber *, NSString *> *bundleForPid;
@property(nonatomic, strong) NSMutableDictionary<NSString *, NSNumber *> *pidForBundle;
@property(nonatomic, strong) NSMutableArray *handles;     // the latest dump's elements, index = handle (R11)
@property(nonatomic, strong) NSMutableArray<NSMutableDictionary *> *nodes;  // the latest dump's nodes, same index
@property(nonatomic, strong) NSMutableArray<NSArray *> *axlog;  // [t, isChange, isRearm] for the settle rule
@property(nonatomic) long long dump;
@property(atomic) long long gen;
@property(nonatomic) long long seq;
@property(nonatomic, strong) NSMutableArray<NSMutableDictionary *> *ring;
@property(nonatomic, strong) NSMutableArray<NSArray *> *recent;  // [t, key] for dedupe
@property(nonatomic, strong) NSMutableArray<DRConn *> *conns;
@property(nonatomic, copy) NSString *lastApp;               // the last front app that wasn't SpringBoard
@property(atomic) BOOL keyboard;
@property(nonatomic) BOOL treeKeyboard;  // the tree being built has a Keyboard node
@property(nonatomic) long rotation;  // 0 portrait, 1/3 landscape, 2 upside down (the last real one)
@property(nonatomic) int frontPid;  // the pid at the screen centre, from the latest hit-test
@property(nonatomic, copy) NSString *hitNote;  // why the latest hit-test assertion passed without a match
@property(nonatomic, strong) NSArray *attrsOverride;  // tree {attrs:[…]} for measurements
@end

static NSString *const kSpringBoard = @"com.apple.springboard";

static NSString *payloadText(id payload) {
  if (!payload) return @"";
  id obj = payload;
  if ([payload isKindOfClass:NSData.class]) {
    @try {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
      obj = [NSKeyedUnarchiver unarchiveObjectWithData:payload] ?: payload;
#pragma clang diagnostic pop
    } @catch (__unused NSException *e) {}
  }
  return [[obj description] stringByReplacingOccurrencesOfString:@"\n" withString:@" "];
}

static NSString *field(NSString *text, NSString *key) {
  // `key = value;` inside an NSDictionary description
  NSRange r = [text rangeOfString:[key stringByAppendingString:@" = "]];
  if (r.location == NSNotFound) return nil;
  NSString *rest = [text substringFromIndex:NSMaxRange(r)];
  NSRange end = [rest rangeOfString:@";"];
  NSString *v = end.location == NSNotFound ? rest : [rest substringToIndex:end.location];
  v = [v stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
  if ([v hasPrefix:@"\""] && [v hasSuffix:@"\""] && v.length >= 2) v = [v substringWithRange:NSMakeRange(1, v.length - 2)];
  return v;
}

@implementation DroidctlRunner

// R2: an XCTest failure becomes an error reply, never the end of the test.
- (void)recordIssue:(XCTIssue *)issue {
  NSString *d = issue.compactDescription ?: @"issue";
  NSLog(@"droidctl: issue swallowed: %@", d);
  @synchronized(self) { [self.issues addObject:d]; }
}

- (XCUIApplication *)app:(NSString *)bundle {
  XCUIApplication *a = self.apps[bundle];
  if (!a) { a = [[XCUIApplication alloc] initWithBundleIdentifier:bundle]; self.apps[bundle] = a; }
  return a;
}

// ---- events. Notifications arrive on XCTest's own queues, not the main thread (a settle reading the log
// raised "mutated while being enumerated", 2026-10-21): the ring, the settle log and the dedupe list are
// only touched under @synchronized(self.ring), and pushes are written after it is released.
// Returns the event to push to subscribers, or nil.
- (NSDictionary *)ax:(int)n element:(id)el payload:(id)payload {
  double t = nowMs();
  NSString *p = payloadText(payload);
  NSString *elKey = el ? axKey(el) : @"";
  NSString *key = [NSString stringWithFormat:@"%d|%@|%@", n, elKey, p];
  // true duplicates arrive within 3 ms (XCTest registers too): drop identical ones within 5 ms
  NSMutableArray *keep = [NSMutableArray array];
  for (NSArray *r in self.recent) {
    if (t - [r[0] doubleValue] > 5) continue;
    if ([r[1] isEqualToString:key]) return nil;
    [keep addObject:r];
  }
  [keep addObject:@[ @(t), key ]];
  self.recent = keep;

  NSString *type = nil;
  NSMutableDictionary *ev = [NSMutableDictionary dictionary];
  NSString *ut = n == 4002 ? field(p, @"event") : nil;
  switch (n) {
    case 1000: case 1006: type = @"window_state"; if (n == 1006) ev[@"alert"] = @YES; break;
    case 1001: case 1025: type = @"window_content"; break;
    case 1009: type = @"scrolled"; if (p.length) ev[@"text"] = field(p, @"data") ?: p; break;
    case 1017: type = @"text_changed"; break;
    case 1018: type = @"focused"; break;
    case 1008: type = @"announcement"; ev[@"text"] = field(p, @"data") ?: p; break;
    case 1031: {
      NSString *v = field(p, @"visible");
      if (v) { type = @"ime"; ev[@"shown"] = @([v intValue] != 0); self.keyboard = [v intValue] != 0; }
      break;
    }
    case 3031: type = @"app_ready"; ev[@"pkg"] = field(p, @"bundleID") ?: @""; break;
    case 4002:
      if ([ut isEqualToString:@"ViewDidAppear"] || [ut isEqualToString:@"ViewDidDisappear"]) {
        type = @"window_state"; ev[@"changes"] = ut;
        NSString *c = field(p, @"controllerClass"), *ti = field(p, @"controllerTitle");
        if (c) ev[@"class"] = c;
        if (ti.length && ![ti isEqualToString:@"<null>"]) ev[@"title"] = ti;
      }
      break;
    default: break;
  }
  // gen counts every change notification the settle rule counts (IOS.md §13.3)
  BOOL change = type != nil || n == 1020 || n == 1021 ||
                (n == 4002 && ([ut isEqualToString:@"KeyEventComplete"] || [ut isEqualToString:@"TouchEventsCompleted"]));
  if (change && ![type isEqualToString:@"announcement"]) self.gen++;
  if (change) {  // the settle rule's input (§13.3): change notifications, and which of them re-arm
    BOOL rearm = n == 1018 || (n == 1031 && [ev[@"shown"] boolValue]);
    [self.axlog addObject:@[ @(t), @(rearm) ]];
    if (self.axlog.count > 400) [self.axlog removeObjectsInRange:NSMakeRange(0, 200)];
  }
  // The keyboard's own view controllers and sounds are noise to an agent (the `ime` event says it): they
  // still count for the settle rule and gen above, but are not reported.
  static NSArray *kbParts;
  if (!kbParts) kbParts = @[ @"InputWindowController", @"InputViewController", @"InputAssistant", @"Prediction",
                             @"KeyboardDock", @"CursorAccessory", @"UIKeyboard" ];
  NSString *cls = ev[@"class"] ?: @"";
  for (NSString *k in kbParts) if ([cls containsString:k]) { type = nil; break; }
  if ([type isEqualToString:@"announcement"] && [ev[@"text"] hasPrefix:@"AXAnnouncementTypeKeyboard"]) type = nil;
  if (!type) return nil;
  int pid = el ? axPid(el) : [field(p, @"pid") intValue];
  if (pid > 0) {
    ev[@"pid"] = @(pid);
    NSString *b;
    @synchronized(self.bundleForPid) { b = self.bundleForPid[@(pid)]; }  // written on the main thread
    if (b && !ev[@"pkg"]) ev[@"pkg"] = b;
    if (!b) [self lookupBundle:pid];  // fills the cache for later events; never waits here
  }
  ev[@"type"] = type; ev[@"ax"] = @(n);
  ev[@"t"] = @((long long)(t - gStartMs)); ev[@"gen"] = @(self.gen);
  // compaction (PROTOCOL.md Events): content bursts from one pid within 250 ms become one event
  NSMutableDictionary *last = self.ring.lastObject;
  if ([type isEqualToString:@"window_content"] && [last[@"type"] isEqualToString:@"window_content"] &&
      [last[@"pid"] isEqual:ev[@"pid"]] && [ev[@"t"] longLongValue] - [last[@"t_last"] ?: last[@"t"] longLongValue] <= 250) {
    last[@"n"] = @([last[@"n"] ?: @1 intValue] + 1);
    last[@"t_last"] = ev[@"t"]; last[@"gen"] = ev[@"gen"];
    return nil;
  }
  ev[@"seq"] = @(++self.seq);
  [self.ring addObject:ev];
  if (self.ring.count > 500) [self.ring removeObjectAtIndex:0];
  return [ev copy];
}

- (void)push:(NSDictionary *)ev {
  NSString *type = ev[@"type"];
  NSArray *conns; @synchronized(self.conns) { conns = [self.conns copy]; }
  for (DRConn *c in conns) {
    NSSet *want = c.subscribed;
    if (want && (!want.count || [want containsObject:type]))
      [c send:@{@"jsonrpc" : @"2.0", @"method" : @"event", @"params" : ev}];
  }
}

- (void)installAXHooks {
  Class c = NSClassFromString(@"XCTRunnerDaemonSession");
  Method m3 = class_getInstanceMethod(c, NSSelectorFromString(@"_XCT_receivedAccessibilityNotification:fromElement:payload:"));
  if (m3) gOrigRecv3 = method_setImplementation(m3, (IMP)hookRecv3);
  // Register once per process: each extra registration adds another delivery (§15.2).
  id client = axInterface();
  SEL reg = NSSelectorFromString(@"_registerForAXNotification:error:");
  if (![client respondsToSelector:reg]) { NSLog(@"droidctl: no AX notification registration"); return; }
  NSMutableArray *ids = [NSMutableArray array];
  for (int i = 1000; i <= 1060; i++) [ids addObject:@(i)];
  [ids addObjectsFromArray:@[ @3031, @4002 ]];
  for (NSNumber *n in ids) {
    NSError *e = nil;
    ((BOOL (*)(id, SEL, int, NSError **))objc_msgSend)(client, reg, n.intValue, &e);
  }
  NSLog(@"droidctl: ax hooks %d", m3 != NULL);
}

// ---- device helpers (all with completion blocks and our own timeouts: R3/R4)
- (id)elementAtPoint:(CGPoint)pt {
  if (!proxyHas(@"_XCT_requestElementAtPoint:reply:")) return nil;
  __block id hit = nil; __block BOOL fin = NO;
  ((void (*)(id, SEL, CGPoint, void (^)(id, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_requestElementAtPoint:reply:"),
      pt, ^(id el, NSError *e) { hit = el; fin = YES; });
  spinUntil(^{ return fin; }, 5);
  return hit;
}

- (void)lookupBundle:(int)pid {
  static NSMutableSet *asked;
  if (!asked) asked = [NSMutableSet set];
  if ([asked containsObject:@(pid)] || !proxyHas(@"_XCT_requestBundleIDForPID:reply:")) return;
  [asked addObject:@(pid)];
  ((void (*)(id, SEL, int, void (^)(NSString *, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_requestBundleIDForPID:reply:"),
      pid, ^(NSString *x, NSError *e) {
        if (!x) return;
        dispatch_async(dispatch_get_main_queue(), ^{
          @synchronized(self.bundleForPid) { self.bundleForPid[@(pid)] = x; }
          self.pidForBundle[x] = @(pid);
        });
      });
}

- (NSString *)bundleFor:(int)pid {
  NSString *b = self.bundleForPid[@(pid)];
  if (b || !proxyHas(@"_XCT_requestBundleIDForPID:reply:")) return b;
  __block NSString *bid = nil; __block BOOL fin = NO;
  ((void (*)(id, SEL, int, void (^)(NSString *, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(@"_XCT_requestBundleIDForPID:reply:"),
      pid, ^(NSString *x, NSError *e) { bid = x; fin = YES; });
  spinUntil(^{ return fin; }, 5);
  if (bid) {
    @synchronized(self.bundleForPid) { self.bundleForPid[@(pid)] = bid; }
    self.pidForBundle[bid] = @(pid);
  }
  return bid;
}

// The runner app runs in a compatibility mode (UIScreen says 320x480), so take the size from an
// application element's frame: the latest tree's root, else a 3 ms attribute fetch.
static CGSize gScreen;
// A view service's remote view (the location prompt's map, 2026-10-26) is an application element too, with
// the frame of its slot in a SpringBoard alert (270x180): only a frame at the origin, at least an iPhone's
// width on both sides, is the screen.
static BOOL fillsScreen(CGRect r) { return r.origin.x == 0 && r.origin.y == 0 && MIN(r.size.width, r.size.height) >= 320; }

- (CGSize)screenSize {
  if (gScreen.width > 0) return gScreen;
  int pid = self.frontPid;
  NSString *sel = @"_XCT_fetchAttributes:forElement:reply:";
  id el = appElement(pid);
  if (el && proxyHas(sel)) {
    __block NSDictionary *vals = nil; __block BOOL fin = NO;
    ((void (*)(id, SEL, id, id, void (^)(NSDictionary *, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(sel),
        @[ @"XC_kAXXCAttributeFrame" ], el, ^(NSDictionary *d, NSError *e) { vals = d; fin = YES; });
    spinUntil(^{ return fin; }, 3);
    for (id k in vals) {
      id v = vals[k];
      CGRect r = CGRectZero;
      if ([v isKindOfClass:NSDictionary.class]) CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)v, &r);
      else if ([v isKindOfClass:NSValue.class]) r = [v CGRectValue];
      if (fillsScreen(r)) gScreen = r.size;
    }
  }
  return gScreen.width > 0 ? gScreen : UIScreen.mainScreen.bounds.size;
}

// Who owns the screen centre: {pid, bundle} or nil (seen with the screen off).
- (NSDictionary *)front {
  CGSize z = [self screenSize];
  id hit = [self elementAtPoint:[self toDevice:CGPointMake(z.width / 2, z.height / 2)]];
  if (!hit) return nil;
  int pid = axPid(hit);
  NSString *b = [self bundleFor:pid];
  // Web content answers with WebKit's WebContent pid (2026-10-21), not its app's: ask other points of the
  // screen for the host app, else keep the last app seen in front.
  if ([b hasPrefix:@"com.apple.WebKit"]) {
    NSString *host = nil;
    int hostPid = 0;
    for (NSValue *v in @[ [NSValue valueWithCGPoint:CGPointMake(z.width / 2, z.height - 12)],
                          [NSValue valueWithCGPoint:CGPointMake(6, z.height / 2)],
                          [NSValue valueWithCGPoint:CGPointMake(z.width - 6, z.height / 2)],
                          [NSValue valueWithCGPoint:CGPointMake(z.width / 2, 70)] ]) {
      id h2 = [self elementAtPoint:[self toDevice:v.CGPointValue]];
      NSString *b2 = h2 ? [self bundleFor:axPid(h2)] : nil;
      if (b2 && ![b2 hasPrefix:@"com.apple.WebKit"] && ![b2 isEqualToString:kSpringBoard]) { host = b2; hostPid = axPid(h2); break; }
    }
    if (!host && self.lastApp && self.pidForBundle[self.lastApp]) { host = self.lastApp; hostPid = [self.pidForBundle[host] intValue]; }
    if (host) { b = host; pid = hostPid; }
  } else if ([b isEqualToString:kSpringBoard]) {
    // An app whose screen centre holds nothing accessible (a story view: a bare spinner on black, a real
    // app, 2026-10-26) lets the hit-test fall through to SpringBoard. The app owns the screen if it answers at
    // two of the probe points, or at one when it is the app last seen in front; on the Home screen none do.
    NSMutableDictionary<NSString *, NSNumber *> *hits = [NSMutableDictionary dictionary];
    NSMutableDictionary<NSString *, NSNumber *> *pids = [NSMutableDictionary dictionary];
    for (NSValue *v in @[ [NSValue valueWithCGPoint:CGPointMake(z.width / 2, z.height - 80)],
                          [NSValue valueWithCGPoint:CGPointMake(6, z.height / 2)],
                          [NSValue valueWithCGPoint:CGPointMake(z.width - 6, z.height / 2)],
                          [NSValue valueWithCGPoint:CGPointMake(z.width / 2, 100)] ]) {
      id h2 = [self elementAtPoint:[self toDevice:v.CGPointValue]];
      NSString *b2 = h2 ? [self bundleFor:axPid(h2)] : nil;
      if (!b2 || [b2 isEqualToString:kSpringBoard] || [b2 hasPrefix:@"com.apple.WebKit"]) continue;
      hits[b2] = @([hits[b2] intValue] + 1);
      pids[b2] = @(axPid(h2));
    }
    for (NSString *b2 in hits) {
      if ([hits[b2] intValue] >= 2 || [b2 isEqualToString:self.lastApp]) { b = b2; pid = [pids[b2] intValue]; break; }
    }
  }
  self.frontPid = pid;
  return b ? @{@"pid" : @(pid), @"bundle" : b} : @{@"pid" : @(pid)};
}

// Daemon snapshot with explicit attributes (IOS.md §7). Retries kAXErrorServerNotFound (the app's
// window isn't registered yet, right after activation: §15.6) and one timeout (§12).
- (id)snapshot:(NSString *)bundle error:(NSError **)error {
  return [self snapshot:bundle pid:0 error:error];
}

// The application element for a pid, without XCUIApplication: an XCUIApplication for an app that
// XCTest didn't launch itself resolved to an element whose snapshot hung (2026-10-21).
static id appElement(int pid) {
  Class c = NSClassFromString(@"XCAccessibilityElement");
  SEL s = NSSelectorFromString(@"elementWithProcessIdentifier:");
  if (pid <= 0 || ![c respondsToSelector:s]) return nil;
  return ((id (*)(id, SEL, int))objc_msgSend)(c, s, pid);
}

- (id)snapshot:(NSString *)bundle pid:(int)pid error:(NSError **)error {
  static NSArray *defaultAttrs;
  if (!defaultAttrs) defaultAttrs = @[ @"XC_kAXXCAttributeElementType", @"XC_kAXXCAttributeIdentifier", @"XC_kAXXCAttributeLabel",
                         @"XC_kAXXCAttributeValue", @"XC_kAXXCAttributeFrame", @"XC_kAXXCAttributeTraits",
                         @"XC_kAXXCAttributePlaceholderValue" ];
  // Not IsVisible: requesting it for a whole app hung the snapshot (> 10 s on a 1030-node screen,
  // 2026-10-21); the host's off-screen rules work from frames (IOS.md §9.4b R1).
  NSArray *attrs = self.attrsOverride ?: defaultAttrs;
  NSString *sel = @"_XCT_requestSnapshotForElement:attributes:parameters:reply:";
  if (!proxyHas(sel)) { if (error) *error = rpcError(EUnsupported, @"no daemon snapshot selector"); return nil; }
  id root = appElement(pid);
  if (!root) { @try { root = [[self app:bundle] valueForKey:@"accessibilityElement"]; } @catch (__unused NSException *e) {} }
  NSMutableDictionary *params = [[axInterface() valueForKey:@"defaultParameters"] mutableCopy] ?: [NSMutableDictionary dictionary];
  double deadline = nowMs() + 1000;
  int timeouts = 0;
  while (YES) {
    __block id snap = nil; __block NSError *err = nil; __block BOOL fin = NO;
    ((void (*)(id, SEL, id, id, id, void (^)(id, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(sel),
        root, attrs, params, ^(id s, NSError *e) { snap = s; err = e; fin = YES; });
    if (!spinUntil(^{ return fin; }, 5)) {
      if (++timeouts < 2) continue;
      if (error) *error = rpcError(ETimeout, @"snapshot timed out twice (5 s each)");
      return nil;
    }
    if (snap) {
      if ([snap respondsToSelector:NSSelectorFromString(@"elementSnapshot")]) snap = [snap valueForKey:@"elementSnapshot"];
      return snap;
    }
    NSString *m = err.localizedDescription ?: @"snapshot failed";
    if ([m containsString:@"ServerNotFound"] && nowMs() < deadline) { spinUntil(^{ return NO; }, 0.05); continue; }
    if (error) *error = rpcError(EInternal, m);
    return nil;
  }
}

static unsigned long long snapTraits(id s) {
  @try { id v = [s valueForKey:@"traits"]; return [v unsignedLongLongValue]; } @catch (__unused NSException *e) { return 0; }
}

- (NSDictionary *)node:(id<XCUIElementSnapshot>)s count:(int *)count debug:(BOOL)debug {
  (*count)++;
  NSMutableDictionary *n = [NSMutableDictionary dictionary];
  n[@"handle"] = @(self.handles.count);
  [self.nodes addObject:n];
  id ax = nil;
  @try { ax = [(id)s valueForKey:@"accessibilityElement"]; } @catch (__unused NSException *e) {}
  [self.handles addObject:ax ?: [NSNull null]];
  if (ax) n[@"uid"] = axKey(ax);
  n[@"type"] = @(s.elementType);
  if (s.elementType == XCUIElementTypeKeyboard) self.treeKeyboard = YES;
  if (s.identifier.length) n[@"id"] = s.identifier;
  if (s.label.length) n[@"label"] = s.label;
  if (s.value && ![[s.value description] isEqualToString:@""]) n[@"value"] = [s.value description];
  if (s.placeholderValue.length) n[@"placeholder"] = s.placeholderValue;
  CGRect f = s.frame;
  n[@"frame"] = @[ @(f.origin.x), @(f.origin.y), @(f.size.width), @(f.size.height) ];
  if (!s.enabled) n[@"enabled"] = @NO;
  if (s.selected) n[@"selected"] = @YES;
  unsigned long long tr = snapTraits(s);
  if (tr) n[@"traits"] = @(tr);
  NSDictionary *more = nil;
  @try { more = [(id)s valueForKey:@"additionalAttributes"]; } @catch (__unused NSException *e) {}
  id cls = more[@5004];  // the element's UIKit/SwiftUI class, sent with every snapshot
  if ([cls isKindOfClass:NSString.class]) n[@"class"] = cls;
  if (debug) {
    @try { n[@"debug"] = [[[(id)s valueForKey:@"additionalAttributes"] description] stringByReplacingOccurrencesOfString:@"\n" withString:@" "]; }
    @catch (__unused NSException *e) {}
  }
  NSMutableArray *kids = [NSMutableArray array];
  for (id<XCUIElementSnapshot> c in s.children) [kids addObject:[self node:c count:count debug:debug]];
  if (kids.count) n[@"children"] = kids;
  return n;
}

// Lock guard (IOS.md §10): fail closed. Returns clear | springboard | locked | cover-sheet | screen-unknown.
- (NSString *)screenState:(NSDictionary *)front springboardTree:(id *)sbOut {
  if (front && ![front[@"bundle"] isEqualToString:kSpringBoard]) return @"clear";
  NSError *err = nil;
  id sb = [self snapshot:kSpringBoard pid:[self.pidForBundle[kSpringBoard] intValue] error:&err];
  if (sbOut) *sbOut = sb;
  if (!sb) return @"screen-unknown";
  BOOL cover = NO, locked = NO;
  NSMutableArray *stack = [NSMutableArray arrayWithObject:sb];
  while (stack.count) {
    id<XCUIElementSnapshot> n = stack.lastObject; [stack removeLastObject];
    if ([n.identifier isEqualToString:@"SBCoverSheetWindow"]) cover = YES;
    if ([n.label isEqualToString:@"Locked"]) locked = YES;
    [stack addObjectsFromArray:n.children];
  }
  return locked ? @"locked" : cover ? @"cover-sheet" : (front ? @"springboard" : @"screen-unknown");
}

- (NSDictionary *)screen:(NSString *)state {
  CGSize z = [self screenSize];
  long rotation = [self currentRotation];
  BOOL locked = [state isEqualToString:@"locked"] || [state isEqualToString:@"cover-sheet"];
  BOOL on = ![state isEqualToString:@"screen-unknown"];
  return @{@"w" : @(z.width), @"h" : @(z.height), @"scale" : @(UIScreen.mainScreen.scale), @"rotation" : @(rotation),
           @"on" : @(on), @"locked" : @(locked), @"state" : state};
}

// ---- input (IOS.md §7, §11.2): synthesized touches through XCUIDevice's event synthesizer
// (WDA's route; re-typed, not copied). Points are portrait device points.
static id pointerPath(NSArray<NSValue *> *pts, double duration, double hold, double holdEnd) {
  Class pc = NSClassFromString(@"XCPointerEventPath");
  if (![pc instancesRespondToSelector:NSSelectorFromString(@"initForTouchAtPoint:offset:")]) return nil;
  id path = ((id (*)(id, SEL, CGPoint, double))objc_msgSend)([pc alloc], NSSelectorFromString(@"initForTouchAtPoint:offset:"), pts[0].CGPointValue, 0.0);
  double t = hold;
  if (hold > 0) ((void (*)(id, SEL, CGPoint, double))objc_msgSend)(path, NSSelectorFromString(@"moveToPoint:atOffset:"), pts[0].CGPointValue, hold);
  for (NSUInteger i = 1; i < pts.count; i++) {
    t += duration / (pts.count - 1);
    ((void (*)(id, SEL, CGPoint, double))objc_msgSend)(path, NSSelectorFromString(@"moveToPoint:atOffset:"), pts[i].CGPointValue, t);
  }
  if (holdEnd > 0) {  // rest at the last point before lifting: no fling, the content stops where the finger did
    t += holdEnd;
    ((void (*)(id, SEL, CGPoint, double))objc_msgSend)(path, NSSelectorFromString(@"moveToPoint:atOffset:"), pts.lastObject.CGPointValue, t);
  }
  ((void (*)(id, SEL, double))objc_msgSend)(path, NSSelectorFromString(@"liftUpAtOffset:"), t + 0.05);
  return path;
}

- (NSError *)synthesize:(NSArray *)paths {
  Class rc = NSClassFromString(@"XCSynthesizedEventRecord");
  id synth = msg0([XCUIDevice sharedDevice], @"eventSynthesizer");
  SEL go = NSSelectorFromString(@"synthesizeEvent:completion:");
  if (!rc || ![synth respondsToSelector:go] || [paths containsObject:[NSNull null]]) return rpcError(EUnsupported, @"no event synthesizer");
  id rec = [rc instancesRespondToSelector:NSSelectorFromString(@"initWithName:displayID:interfaceOrientation:")]
    ? ((id (*)(id, SEL, id, unsigned long long, long))objc_msgSend)([rc alloc], NSSelectorFromString(@"initWithName:displayID:interfaceOrientation:"), @"droidctl", 0ULL, (long)UIInterfaceOrientationPortrait)
    : ((id (*)(id, SEL, id, long))objc_msgSend)([rc alloc], NSSelectorFromString(@"initWithName:interfaceOrientation:"), @"droidctl", (long)UIInterfaceOrientationPortrait);
  for (id p in paths) ((void (*)(id, SEL, id))objc_msgSend)(rec, NSSelectorFromString(@"addPointerEventPath:"), p);
  __block BOOL fin = NO; __block NSError *err = nil;
  ((void (*)(id, SEL, id, void (^)(BOOL, NSError *)))objc_msgSend)(synth, go, rec, ^(BOOL ok, NSError *e) { err = e; fin = YES; });
  if (!spinUntil(^{ return fin; }, 20)) return rpcError(ETimeout, @"the touch was not delivered within 20 s");
  return err ? rpcError(EInternal, err.localizedDescription) : nil;
}

// R6: no input while locked, on the cover sheet, or with the screen in an unknown state (fail closed).
- (NSError *)guard {
  NSString *st = [self screenState:[self front] springboardTree:NULL];
  if ([st isEqualToString:@"clear"] || [st isEqualToString:@"springboard"]) return nil;
  return [NSError errorWithDomain:@"droidctl" code:EScreen userInfo:@{NSLocalizedDescriptionKey : [NSString stringWithFormat:@"screen %@", st]}];
}

- (id)gesture:(NSDictionary *)p error:(NSError **)error {
  NSString *type = p[@"type"] ?: @"tap";
  NSArray *pts = p[@"points"];
  if (![pts isKindOfClass:NSArray.class] || !pts.count) { *error = rpcError(EParams, @"points: [[x, y], ...] required"); return nil; }
  NSMutableArray<NSValue *> *v = [NSMutableArray array];
  for (NSArray *xy in pts) {
    if (![xy isKindOfClass:NSArray.class] || xy.count < 2) { *error = rpcError(EParams, @"each point is [x, y]"); return nil; }
    [v addObject:[NSValue valueWithCGPoint:CGPointMake([xy[0] doubleValue], [xy[1] doubleValue])]];
  }
  if ((*error = [self guard])) return nil;
  for (NSUInteger i = 0; i < v.count; i++) v[i] = [NSValue valueWithCGPoint:[self toDevice:v[i].CGPointValue]];
  long long seq0 = self.seq;
  double t0 = nowMs();
  double ms = [p[@"ms"] ?: @0 doubleValue] / 1000.0;
  id path;
  if ([type isEqualToString:@"tap"]) path = pointerPath(@[ v[0] ], 0, 0, 0);
  else if ([type isEqualToString:@"long"]) path = pointerPath(@[ v[0] ], 0, ms > 0 ? ms : 0.6, 0);
  else if ([type isEqualToString:@"swipe"] || [type isEqualToString:@"path"]) {
    if (v.count < 2) { *error = rpcError(EParams, @"a swipe needs two points"); return nil; }
    path = pointerPath(v, ms > 0 ? ms : 0.3, 0, [p[@"hold_end_ms"] doubleValue] / 1000.0);
  } else { *error = rpcError(EUnsupported, [NSString stringWithFormat:@"gesture type %@", type]); return nil; }
  NSError *e = [self synthesize:@[ path ?: [NSNull null] ]];
  if (e) { *error = e; return nil; }
  NSMutableDictionary *r = [@{@"performed" : @YES, @"type" : type, @"ms" : @((long long)(nowMs() - t0))} mutableCopy];
  return [self finish:r bundle:[self bundleFor:self.frontPid] t0:t0 seq:seq0 floorAt:0 params:p];
}

- (id)launch:(NSDictionary *)p error:(NSError **)error {
  NSString *bundle = p[@"pkg"] ?: p[@"bundle"];
  if (![bundle isKindOfClass:NSString.class]) { *error = rpcError(EParams, @"pkg required"); return nil; }
  NSString *sel = @"_XCT_launchApplicationWithBundleID:arguments:environment:completion:";
  if (!proxyHas(sel)) { *error = rpcError(EUnsupported, @"no launch selector"); return nil; }
  if ((*error = [self guard])) return nil;
  long long seq0 = self.seq;
  double t0 = nowMs();
  __block BOOL fin = NO; __block NSError *lerr = nil;
  ((void (*)(id, SEL, id, id, id, void (^)(NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(sel),
      bundle, @[], @{}, ^(NSError *e) { lerr = e; fin = YES; });
  if (!spinUntil(^{ return fin; }, 30)) { *error = rpcError(ETimeout, @"launch timed out"); return nil; }
  if (lerr) { *error = rpcError(EInternal, lerr.localizedDescription); return nil; }
  // The launch call returns before the switch is done: from another app, a settle started now ended on
  // SpringBoard's transition (202 ms) or ran into the cap (2026-10-21). Wait until the app owns the screen.
  // A permission prompt can come up over the app before it is ever seen in front (2026-10-26: the location
  // prompt's view service owned the screen centre): after 1 s, a system alert in front counts too.
  __block BOOL alert = NO;
  BOOL front = [self waitFront:^BOOL(NSString *b) {
    if ([b isEqualToString:bundle]) return YES;
    return (BOOL)(nowMs() - t0 > 1000 && (alert = [self systemAlertInFront]));
  } seconds:10];
  if (!front) { *error = rpcError(ETimeout, [NSString stringWithFormat:@"%@ did not come to the front within 10 s", bundle]); return nil; }
  NSMutableDictionary *r = [@{@"launched" : bundle, @"performed" : @YES} mutableCopy];
  if (alert) r[@"system_alert"] = @YES;
  return [self finish:r bundle:alert ? nil : bundle t0:t0 seq:seq0 floorAt:0 params:p];
}


// ---- attributes, AX actions and text (IOS.md §11): every private call guarded, every wait bounded
static NSString *const kAttr = @"XC_kAXXCAttribute";

static BOOL rectOf(id v, CGRect *r) {
  if ([v isKindOfClass:NSDictionary.class]) return CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)v, r);
  if ([v isKindOfClass:NSValue.class] && strcmp([v objCType], @encode(CGRect)) == 0) { *r = [v CGRectValue]; return YES; }
  return NO;
}

static BOOL pointOf(id v, CGPoint *p) {
  if ([v isKindOfClass:NSDictionary.class]) return CGPointMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)v, p);
  if ([v isKindOfClass:NSValue.class] && strcmp([v objCType], @encode(CGPoint)) == 0) { *p = [v CGPointValue]; return YES; }
  if ([v isKindOfClass:NSString.class] && [v hasPrefix:@"{"]) { *p = CGPointFromString(v); return YES; }
  return NO;
}

static CGRect frameOf(NSDictionary *n) {
  NSArray *f = n[@"frame"];
  return f.count == 4 ? CGRectMake([f[0] doubleValue], [f[1] doubleValue], [f[2] doubleValue], [f[3] doubleValue]) : CGRectZero;
}

static BOOL isEditable(int type) { return type == 45 || type == 49 || type == 50 || type == 52; }

// Attribute values by short name ("Frame", "IsValid", ...).
- (NSDictionary *)fetch:(NSArray *)attrs of:(id)el error:(NSError **)error {
  NSString *sel = @"_XCT_fetchAttributes:forElement:reply:";
  if (!el || !proxyHas(sel)) { if (error) *error = rpcError(EUnsupported, @"no attribute fetch"); return nil; }
  __block NSDictionary *vals = nil; __block NSError *err = nil; __block BOOL fin = NO;
  ((void (*)(id, SEL, id, id, void (^)(NSDictionary *, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(sel),
      attrs, el, ^(NSDictionary *d, NSError *e) { vals = d; err = e; fin = YES; });
  if (!spinUntil(^{ return fin; }, 5)) { if (error) *error = rpcError(ETimeout, @"attribute fetch timed out"); return nil; }
  if (!vals) { if (error) *error = rpcError(EInternal, err.localizedDescription ?: @"attribute fetch failed"); return nil; }
  NSMutableDictionary *o = [NSMutableDictionary dictionary];
  for (id k in vals) o[[[k description] stringByReplacingOccurrencesOfString:kAttr withString:@""]] = vals[k];
  return o;
}

// "ok" | "refused" (kAXErrorCannotComplete: nothing ran) | an error text. Only allowlisted numbers:
// 2025, 2042 and 2043 kill the app (research/ios-decision.md §13b).
- (NSString *)perform:(int)action on:(id)ax {
  static NSSet *allowed;
  if (!allowed) allowed = [NSSet setWithArray:@[ @2003, @2004, @2005, @2006, @2007, @2010, @2014, @2015 ]];
  if (![allowed containsObject:@(action)]) return @"not allowlisted";
  NSString *sel = @"_XCT_performAccessibilityAction:onElement:withValue:reply:";
  if (!proxyHas(sel)) return @"unsupported";
  __block NSString *res = nil;
  ((void (*)(id, SEL, int, id, id, void (^)(NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(sel), action, ax, nil, ^(NSError *e) {
    res = !e ? @"ok" : [e.localizedDescription containsString:@"CannotComplete"] ? @"refused" : (e.localizedDescription ?: @"error");
  });
  if (!spinUntil(^{ return (BOOL)(res != nil); }, 5)) return @"timeout";
  return res;
}

// setAttribute value (2006): replaces a field's text without a touch, input masks included (§11.3).
- (NSString *)setText:(NSString *)text on:(id)ax {
  NSString *sel = @"_XCT_setAttribute:value:element:reply:";
  if (!proxyHas(sel)) return @"unsupported";
  __block NSString *res = nil;
  ((void (*)(id, SEL, id, id, id, void (^)(BOOL, NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(sel), @2006, text, ax,
      ^(BOOL ok, NSError *e) { res = ok ? @"ok" : (e.localizedDescription ?: @"refused"); });
  if (!spinUntil(^{ return (BOOL)(res != nil); }, 5)) return @"timeout";
  return res;
}

// Keys into whatever has the keyboard focus (callers check that something has it first).
- (NSString *)sendString:(NSString *)text {
  NSString *sel = @"_XCT_sendString:maximumFrequency:completion:";
  if (!proxyHas(sel)) return @"unsupported";
  __block NSString *res = nil;
  ((void (*)(id, SEL, id, unsigned long long, void (^)(NSError *)))objc_msgSend)(daemonProxy(), NSSelectorFromString(sel), text, 60ULL,
      ^(NSError *e) { res = e ? (e.localizedDescription ?: @"error") : @"ok"; });
  if (!spinUntil(^{ return (BOOL)(res != nil); }, 5 + text.length * 0.1)) return @"timeout";
  return res;
}

- (long)currentRotation {
  long o = (long)[XCUIDevice sharedDevice].orientation;  // 5/6 (face up/down) keep the last rotation
  if (o == 1) self.rotation = 0; else if (o == 2) self.rotation = 2; else if (o == 3) self.rotation = 1; else if (o == 4) self.rotation = 3;
  return self.rotation;
}

// Tree (interface) points -> portrait device points, for hit-tests and touches (IOS.md §12).
- (CGPoint)toDevice:(CGPoint)p {
  CGSize z = [self screenSize];  // the app frame in the current orientation
  switch ([self currentRotation]) {
    case 2: return CGPointMake(z.width - p.x, z.height - p.y);
    case 1: return CGPointMake(z.height - p.y, p.x);
    case 3: return CGPointMake(p.y, z.width - p.x);
    default: return p;
  }
}

// The element with the keyboard focus in an app (the field, or the cell holding it), or nil (~106 ms then).
- (id)responder:(int)pid {
  NSDictionary *v = [self fetch:@[ @"XC_kAXXCAttributeResponderElement" ] of:appElement(pid) error:NULL];
  id r = v[@"ResponderElement"];
  return [NSStringFromClass([r class]) containsString:@"AccessibilityElement"] ? r : nil;
}

- (NSDictionary *)nodeForUid:(NSString *)k {
  for (NSDictionary *n in self.nodes) if ([n[@"uid"] isEqualToString:k]) return n;
  return nil;
}

// Whether `n` holds the keyboard focus: the responder is `n`, or the cell that contains it.
- (BOOL)hasFocus:(NSDictionary *)n pid:(int)pid {
  id cur = [self responder:pid];
  if (!cur || !n[@"uid"]) return NO;
  NSString *k = axKey(cur);
  if ([k isEqualToString:n[@"uid"]]) return YES;
  NSDictionary *holder = [self nodeForUid:k];
  return holder && [self uid:n[@"uid"] under:holder];
}

- (BOOL)node:(NSDictionary *)n hasChildAt:(CGPoint)pt {
  for (NSDictionary *c in n[@"children"])
    if (CGRectContainsPoint(frameOf(c), pt) || [self node:c hasChildAt:pt]) return YES;
  return NO;
}

- (BOOL)uid:(NSString *)k under:(NSDictionary *)n {
  for (NSDictionary *c in n[@"children"])
    if ([c[@"uid"] isEqualToString:k] || [self uid:k under:c]) return YES;
  return NO;
}

// The enforced hit-test (IOS.md §11.1 step 4): nil when the element at `pt` is the target, a descendant of
// it, or lies inside its frame (a UISwitch answers with an internal view); else what is there instead.
- (NSString *)coveredAt:(CGPoint)pt target:(NSDictionary *)tn {
  self.hitNote = nil;
  id hit = [self elementAtPoint:[self toDevice:pt]];
  if (!hit) return @"nothing (the hit-test found no element)";
  NSString *hk = axKey(hit);
  int tpid = [[tn[@"uid"] componentsSeparatedByString:@"/"][0] intValue];
  if (axPid(hit) != tpid) return [NSString stringWithFormat:@"another app (%@)", [self bundleFor:axPid(hit)] ?: @(axPid(hit))];
  if ([hk isEqualToString:tn[@"uid"]] || [self uid:hk under:tn]) return nil;
  // the target's own container answers for the point (a text inside a cell of several targets): nothing else is there
  NSDictionary *hn0 = [self nodeForUid:hk];
  if (hn0 && [self uid:tn[@"uid"] under:hn0]) { self.hitNote = @"container"; return nil; }
  NSDictionary *v = [self fetch:@[ @"XC_kAXXCAttributeFrame", @"XC_kAXXCAttributeLabel" ] of:hit error:NULL];
  CGRect hr = CGRectZero;
  if (rectOf(v[@"Frame"], &hr) && CGRectContainsRect(CGRectInset(frameOf(tn), -1, -1), hr)) return nil;
  // An answer that isn't at the point by its own frame is no evidence of anything: an app's tab bar answered
  // with the neighbouring tab, deterministically, and a strip 57 pt above it (2026-10-26).
  if (hr.size.width > 0 && !CGRectContainsPoint(CGRectInset(hr, -1, -1), pt)) { self.hitNote = @"inconsistent"; return nil; }
  // The AX hit-test walks elements in reading order, not z-order: an empty table behind a button (a sibling
  // laid out under it) answered for the button's point, and a touch there pressed the button (2026-10-26).
  // A table, collection or scroll view with none of its own elements at the point is only its empty space.
  NSDictionary *hn = [self nodeForUid:hk];
  int ht = [hn[@"type"] intValue];
  if (hn && (ht == 26 || ht == 32 || ht == 46) && ![self node:hn hasChildAt:pt]) { self.hitNote = @"empty-container"; return nil; }
  NSString *lab = [v[@"Label"] isKindOfClass:NSString.class] ? v[@"Label"] : @"";
  return lab.length ? [NSString stringWithFormat:@"\"%@\"", lab] : @"another element";
}

// R8: a labelled switch wrapping an unlabelled one is touched on the inner one (the outer one's centre is its label).
- (NSInteger)touchTarget:(NSInteger)h {
  NSDictionary *n = self.nodes[h];
  int t = [n[@"type"] intValue];
  if (t != 40 && t != 41) return h;
  NSMutableArray *st = [NSMutableArray arrayWithArray:n[@"children"] ?: @[]];
  while (st.count) {
    NSDictionary *c = st.lastObject; [st removeLastObject];
    int ct = [c[@"type"] intValue];
    NSInteger ch = [c[@"handle"] integerValue];
    if ((ct == 40 || ct == 41) && !c[@"label"] && self.handles[ch] != NSNull.null) return ch;
    [st addObjectsFromArray:c[@"children"] ?: @[]];
  }
  return h;
}

// One touch at the element's own VisiblePoint, only after the hit-test assertion passed (§11.1).
- (BOOL)touch:(NSInteger)h hold:(double)hold into:(NSMutableDictionary *)r error:(NSError **)error {
  NSInteger th = [self touchTarget:h];
  NSDictionary *tn = self.nodes[th];
  CGSize z = [self screenSize];
  CGRect screen = CGRectMake(0, 0, z.width, z.height);
  CGPoint pt;
  NSDictionary *v = [self fetch:@[ @"XC_kAXXCAttributeVisiblePoint" ] of:self.handles[th] error:NULL];
  if (pointOf(v[@"VisiblePoint"], &pt) && CGRectContainsPoint(screen, pt)) {
    r[@"point_from"] = @"visible_point";
  } else {
    CGRect vis = CGRectIntersection(frameOf(tn), screen);
    if (CGRectIsNull(vis) || vis.size.width < 1 || vis.size.height < 1) { *error = rpcError(ENotVisible, @"the element is not on screen"); return NO; }
    pt = CGPointMake(CGRectGetMidX(vis), CGRectGetMidY(vis));
    r[@"point_from"] = @"frame";
  }
  r[@"point"] = @[ @(round(pt.x)), @(round(pt.y)) ];
  NSString *cover = [self coveredAt:pt target:tn];
  if (self.hitNote) r[@"hit_test"] = self.hitNote;
  if (cover) {
    *error = rpcError(EOccluded, [NSString stringWithFormat:@"not touched: the point %.0f,%.0f shows %@", pt.x, pt.y, cover]);
    return NO;
  }
  NSError *e = [self synthesize:@[ pointerPath(@[ [NSValue valueWithCGPoint:[self toDevice:pt]] ], 0, hold, 0) ?: [NSNull null] ]];
  if (e) { *error = e; return NO; }
  return YES;
}

// ---- settle (IOS.md §13.3): run-loop idle + animations idle, re-armed by a late focus or keyboard-show
// notification, then 100 ms without a change notification; `floorAt` delays that window (AX scrolls animate
// ~170 ms with no signal); capped at timeout_ms.
- (NSDictionary *)settle:(NSString *)bundle from:(double)t0 floorAt:(double)floorAt spec:(NSDictionary *)spec {
  double quiet = [spec[@"quiet_ms"] ?: @100 doubleValue], cap = [spec[@"timeout_ms"] ?: @3000 doubleValue];
  id client = axInterface();
  SEL sRun = NSSelectorFromString(@"notifyWhenEventLoopIsIdleForApplication:reply:");
  SEL sAnim = NSSelectorFromString(@"notifyWhenNoAnimationsAreActiveForApplication:reply:");
  XCUIApplication *app = bundle ? [self app:bundle] : nil;
  BOOL idleCalls = app && [client respondsToSelector:sRun] && [client respondsToSelector:sAnim];
  if (!idleCalls) quiet = MAX(quiet, 300);  // a pure 300 ms quiet window was right, only slower (§13.3)
  double (^last)(double, BOOL) = ^double(double since, BOOL rearmOnly) {
    double l = -1;
    NSArray *log;
    @synchronized(self.ring) { log = [self.axlog copy]; }
    for (NSArray *e in log) {
      double t = [e[0] doubleValue];
      if (t >= since && (!rearmOnly || [e[1] boolValue])) l = MAX(l, t);
    }
    return l;
  };
  BOOL settled = NO;
  int rearms = 0;
  __block double runMs = -1, animMs = -1;  // the first round's reply times (diagnostics)
  while (!settled && nowMs() - t0 < cap) {
    double replied = nowMs();
    if (idleCalls) {
      __block int left = 2;
      BOOL first = rearms == 0;
      ((void (*)(id, SEL, id, void (^)(NSError *)))objc_msgSend)(client, sRun, app, ^(NSError *e) {
        left--; if (first && runMs < 0) runMs = nowMs() - t0; });
      ((void (*)(id, SEL, id, void (^)(NSError *)))objc_msgSend)(client, sAnim, app, ^(NSError *e) {
        left--; if (first && animMs < 0) animMs = nowMs() - t0; });
      spinUntil(^{ return (BOOL)(left <= 0); }, MAX(0, (cap - (nowMs() - t0)) / 1000.0));
      if (left > 0) break;
      replied = nowMs();
    }
    BOOL rearm = NO;
    while (nowMs() - t0 < cap) {
      spinUntil(^{ return NO; }, 0.005);
      if (idleCalls && rearms < 5 && last(replied, YES) > replied) { rearms++; rearm = YES; break; }
      if (nowMs() - MAX(MAX(replied, last(t0, NO)), floorAt) >= quiet) { settled = YES; break; }
    }
    if (!rearm) break;
  }
  return @{@"idle" : @(settled), @"settle_ms" : @((long long)(nowMs() - t0)), @"rearms" : @(rearms),
           @"idle_ms" : @[ @((long long)runMs), @((long long)animMs) ]};
}

- (NSArray *)eventsSince:(long long)seq limit:(NSUInteger)limit {
  NSMutableArray *out = [NSMutableArray array];
  @synchronized(self.ring) {
    for (NSDictionary *e in self.ring)
      if ([e[@"seq"] longLongValue] > seq && out.count < limit) [out addObject:[e copy]];
  }
  return out;
}

// The reply of every input method: settle when asked, then the settled tree (the new latest dump) and the
// events seen since the input started (PROTOCOL.md "Settle").
- (NSMutableDictionary *)finish:(NSMutableDictionary *)r bundle:(NSString *)bundle t0:(double)t0 seq:(long long)seq0
                        floorAt:(double)floorAt params:(NSDictionary *)p {
  id spec = p[@"settle"];
  r[@"t0"] = @((long long)(t0 - gStartMs));
  if ([spec isKindOfClass:NSDictionary.class]) {
    [r addEntriesFromDictionary:[self settle:bundle from:t0 floorAt:floorAt spec:spec]];
    if (![spec[@"tree"] isEqual:@NO]) {
      NSError *e = nil;
      id t = [self tree:@{} error:&e];
      if (t) r[@"tree"] = t; else r[@"tree_error"] = e.localizedDescription ?: @"tree failed";
    }
  }
  r[@"events"] = [self eventsSince:seq0 limit:50];
  return r;
}

// ---- act (IOS.md §11.1): IsValid -> lock guard -> enabled -> an allowlisted AX action; a refused press
// becomes one hit-test-checked touch. `delivered` stands in for Android's click event (iOS has none).
- (id)act:(NSDictionary *)p error:(NSError **)error {
  if (p[@"custom"]) { *error = rpcError(EUnsupported, @"custom actions are not available on iOS"); return nil; }
  NSString *action = p[@"action"];
  if (![action isKindOfClass:NSString.class]) { *error = rpcError(EParams, @"action required"); return nil; }
  if ([p[@"dump"] longLongValue] != self.dump) { *error = rpcError(EStale, @"stale dump"); return nil; }
  NSInteger h = [p[@"handle"] integerValue];
  if (h < 0 || h >= (NSInteger)self.handles.count || self.handles[h] == NSNull.null) {
    *error = rpcError(EGone, @"no such element in this dump");
    return nil;
  }
  id ax = self.handles[h];
  NSDictionary *n = self.nodes[h];
  int type = [n[@"type"] intValue];
  // R5: an AX action on an element of a dead process retries for ~6 s; IsValid fails fast
  NSError *fe = nil;
  NSDictionary *valid = [self fetch:@[ @"XC_kAXXCAttributeIsValid" ] of:ax error:&fe];
  if (!valid || (valid[@"IsValid"] && ![valid[@"IsValid"] boolValue])) {
    *error = rpcError(EGone, fe ? [@"the element is gone: " stringByAppendingString:fe.localizedDescription] : @"the element is gone");
    return nil;
  }
  if ((*error = [self guard])) return nil;
  static NSDictionary *axFor;
  if (!axFor) axFor = @{@"scroll_forward" : @2006, @"scroll_down" : @2006, @"scroll_backward" : @2007, @"scroll_up" : @2007,
                        @"scroll_right" : @2014, @"scroll_left" : @2015, @"show_on_screen" : @2003};
  if (![p[@"force"] boolValue] && [n[@"enabled"] isEqual:@NO] && !axFor[action]) {
    *error = rpcError(EDisabled, @"the element is disabled");
    return nil;
  }
  // The front app (the guard just hit-tested it), not the element's own pid: a web element lives in WebKit's
  // WebContent process, and idle requests sent there never replied and emptied the web view's tree (2/2).
  int pid = self.frontPid;
  NSString *bundle = [self bundleFor:pid];
  long long seq0 = self.seq;
  double t0 = nowMs(), floorAt = 0;
  NSMutableDictionary *r = [@{@"action" : action, @"performed" : @NO} mutableCopy];
  NSString *res = nil;
  if ([action isEqualToString:@"click"]) {
    // a text is a touch target only inside a cell that holds several (mapping R9): no AX press, it has none
    res = type == XCUIElementTypeStaticText ? @"refused" : [self perform:2010 on:ax];
    r[@"ax"] = res;
    if ([res isEqualToString:@"ok"]) {
      r[@"method"] = @"ax";
    } else if ([res isEqualToString:@"refused"]) {  // nothing ran (standard UIKit controls): one checked touch
      if (![self touch:h hold:0 into:r error:error]) return nil;
      r[@"method"] = @"touch";
    } else {
      *error = rpcError(EInternal, [@"AX press: " stringByAppendingString:res]);
      return nil;
    }
    r[@"performed"] = @YES; r[@"delivered"] = @YES;
  } else if ([action isEqualToString:@"long_click"]) {
    if (![self touch:h hold:1.0 into:r error:error]) return nil;
    r[@"performed"] = @YES; r[@"delivered"] = @YES; r[@"method"] = @"touch";
  } else if (axFor[action] && type == 58 && ![action isEqualToString:@"show_on_screen"]) {
    // a WKWebView ignores AX page scrolls: 0/8 moved in Safari (refused, or "ok" with nothing moved,
    // 2026-10-22). Not performed: the host swipes on it instead.
    r[@"method"] = @"none"; r[@"reason"] = @"web views do not scroll by accessibility action";
  } else if (axFor[action]) {
    int num = [axFor[action] intValue];
    if (type == 33 && (num == 2006 || num == 2014)) num = 2004;       // a slider steps up / down
    else if (type == 33 && (num == 2007 || num == 2015)) num = 2005;
    res = [self perform:num on:ax];
    r[@"ax"] = res; r[@"method"] = @"ax";
    if ([res isEqualToString:@"ok"]) {
      r[@"performed"] = @YES;
      if (type != 33) floorAt = nowMs() + 200;
    } else if (![res isEqualToString:@"refused"]) {  // refused: at the end of the list, as on Android
      *error = rpcError(EInternal, [NSString stringWithFormat:@"AX action %d: %@", num, res]);
      return nil;
    }
  } else if ([action isEqualToString:@"set_text"]) {
    NSString *text = [p[@"args"] isKindOfClass:NSDictionary.class] ? p[@"args"][@"text"] : nil;
    if (![text isKindOfClass:NSString.class]) { *error = rpcError(EParams, @"args.text required"); return nil; }
    res = [self setText:text on:ax];
    r[@"ax"] = res; r[@"method"] = @"set_value";
    if ([res isEqualToString:@"unsupported"]) { *error = rpcError(EUnsupported, @"no setAttribute"); return nil; }
    r[@"performed"] = @([res isEqualToString:@"ok"]);
  } else if ([action isEqualToString:@"focus"]) {
    // a checked touch on the field: setting its value would change it (IOS-INTEGRATION.md §5)
    if ([self hasFocus:n pid:pid]) {
      r[@"performed"] = @YES; r[@"already"] = @YES; r[@"method"] = @"none";
    } else {
      if (![self touch:h hold:0 into:r error:error]) return nil;
      r[@"performed"] = @YES; r[@"method"] = @"touch";
    }
  } else if ([action isEqualToString:@"ime_enter"]) {
    if (!self.keyboard && ![self responder:pid]) { *error = rpcError(ENotFocused, @"no input has the keyboard focus"); return nil; }
    res = [self sendString:@"\n"];
    if (![res isEqualToString:@"ok"]) { *error = rpcError(EInternal, [@"return key: " stringByAppendingString:res]); return nil; }
    r[@"performed"] = @YES; r[@"method"] = @"keys";
  } else {
    *error = rpcError(EUnsupported, [NSString stringWithFormat:@"%@ is not available on iOS", action]);
    return nil;
  }
  r[@"perform_ms"] = @((long long)(nowMs() - t0));
  return [self finish:r bundle:bundle t0:t0 seq:seq0 floorAt:floorAt params:p];
}

// `type {text}`: keys into the focused input. Never without focus: XCTest's typeText then hangs for 41 s,
// and keys would go nowhere (§11.3).
- (id)typeText:(NSDictionary *)p error:(NSError **)error {
  NSString *text = p[@"text"];
  if (![text isKindOfClass:NSString.class] || !text.length) { *error = rpcError(EParams, @"text required"); return nil; }
  if ((*error = [self guard])) return nil;
  int pid = self.frontPid;
  if (!self.keyboard && ![self responder:pid]) {
    *error = rpcError(ENotFocused, @"no input has the keyboard focus");
    return nil;
  }
  long long seq0 = self.seq;
  double t0 = nowMs();
  NSString *res = [self sendString:text];
  if (![res isEqualToString:@"ok"]) { *error = rpcError(EInternal, [@"typing: " stringByAppendingString:res]); return nil; }
  NSMutableDictionary *r = [@{@"performed" : @YES, @"method" : @"keys", @"chars" : @(text.length)} mutableCopy];
  r[@"perform_ms"] = @((long long)(nowMs() - t0));
  return [self finish:r bundle:[self bundleFor:pid] t0:t0 seq:seq0 floorAt:0 params:p];
}

// `hide_keyboard`: AX press on the Return key (locale-independent identifier) or a Done-type accessory button,
// else a typed newline. A tap outside and swipes did not dismiss it (§11.3).
- (id)hideKeyboard:(NSDictionary *)p error:(NSError **)error {
  if ((*error = [self guard])) return nil;
  int pid = self.frontPid;
  NSString *bundle = [self bundleFor:pid];
  NSError *e = nil;
  id snap = [self snapshot:bundle pid:pid error:&e];
  if (!snap) { *error = e; return nil; }
  static NSSet *doneLabels;
  if (!doneLabels) doneLabels = [NSSet setWithArray:@[ @"Done", @"Go", @"Search", @"Send", @"Next", @"Join", @"Continue" ]];
  id<XCUIElementSnapshot> kb = nil, key = nil, done = nil;
  NSMutableArray *st = [NSMutableArray arrayWithObject:snap];
  while (st.count) {
    id<XCUIElementSnapshot> s = st.lastObject; [st removeLastObject];
    if (s.elementType == XCUIElementTypeKeyboard) kb = s;
    // the Return key is a Button inside the keyboard (identifier "Return", label "return"; iOS 18.7.8)
    if ((s.elementType == XCUIElementTypeKey || s.elementType == XCUIElementTypeButton) && kb &&
        [s.identifier caseInsensitiveCompare:@"Return"] == NSOrderedSame) key = s;
    if (s.elementType == XCUIElementTypeButton && !done && [doneLabels containsObject:s.label ?: @""]) done = s;
    [st addObjectsFromArray:s.children];
  }
  if (!kb) return @{@"performed" : @NO, @"already" : @YES, @"hidden" : @YES};
  long long seq0 = self.seq;
  double t0 = nowMs();
  NSMutableDictionary *r = [NSMutableDictionary dictionary];
  NSString *res = nil;
  id<XCUIElementSnapshot> target = key ?: done;
  // No Return key and no Done button (a number pad): a typed newline is no Return there. A real app's
  // amount field took it as input and cleared itself (2026-10-26). Say so; the agent taps elsewhere.
  if (!target) {
    *error = rpcError(EUnsupported, @"this keyboard has no Return key or Done button (a number pad?)");
    return nil;
  }
  {
    id ax = nil;
    @try { ax = [(id)target valueForKey:@"accessibilityElement"]; } @catch (__unused NSException *x) {}
    res = ax ? [self perform:2010 on:ax] : @"no element";
    r[@"method"] = key ? @"return_key" : @"done_button";
    if (!key) r[@"button"] = done.label;
  }
  if (![res isEqualToString:@"ok"] && key) {  // the Return key refused the press: the same key, typed
    res = [self sendString:@"\n"];
    r[@"method"] = @"newline";
  }
  r[@"performed"] = @([res isEqualToString:@"ok"]);
  r[@"perform_ms"] = @((long long)(nowMs() - t0));
  NSMutableDictionary *settleP = [p mutableCopy] ?: [NSMutableDictionary dictionary];
  if (!settleP[@"settle"]) settleP[@"settle"] = @{};
  [self finish:r bundle:bundle t0:t0 seq:seq0 floorAt:0 params:settleP];
  if (r[@"tree"]) r[@"hidden"] = @(![r[@"tree"][@"keyboard"] boolValue]);
  return r;
}


// ---- screenshot (IOS.md §7: public XCUIScreen; JPEG and scaling on the phone, 66 ms capture). The image is
// turned to the interface orientation, so it lines up with the tree's points like the screen does.
- (id)screenshot:(NSDictionary *)p error:(NSError **)error {
  double scale = MAX(0.05, MIN(1.0, [p[@"scale"] ?: @0.5 doubleValue]));
  double quality = MAX(10, MIN(100, [p[@"quality"] ?: @70 doubleValue])) / 100.0;
  UIImage *img = XCUIScreen.mainScreen.screenshot.image;
  if (!img) { *error = rpcError(EInternal, @"no screenshot"); return nil; }
  CGSize pt = [self screenSize];  // interface orientation, points
  long rot = [self currentRotation];
  CGSize px = CGSizeMake(img.size.width * img.scale, img.size.height * img.scale);  // portrait pixels
  double ppp = MAX(px.width, px.height) / MAX(pt.width, pt.height);                // pixels per point
  CGRect crop = CGRectMake(0, 0, pt.width, pt.height);
  NSArray *c = p[@"crop"];  // [l, t, r, b] in tree points
  if ([c isKindOfClass:NSArray.class] && c.count == 4)
    crop = CGRectIntersection(crop, CGRectMake([c[0] doubleValue], [c[1] doubleValue],
                                               [c[2] doubleValue] - [c[0] doubleValue], [c[3] doubleValue] - [c[1] doubleValue]));
  if (CGRectIsNull(crop) || crop.size.width < 1 || crop.size.height < 1) { *error = rpcError(EParams, @"crop is off screen"); return nil; }
  CGSize out = CGSizeMake(round(crop.size.width * ppp * scale), round(crop.size.height * ppp * scale));
  UIGraphicsImageRendererFormat *fmt = [UIGraphicsImageRendererFormat defaultFormat];
  fmt.scale = 1; fmt.opaque = YES;
  UIImage *res = [[[UIGraphicsImageRenderer alloc] initWithSize:out format:fmt] imageWithActions:^(UIGraphicsImageRendererContext *ctx) {
    CGContextRef g = ctx.CGContext;
    double k = ppp * scale;  // output pixels per point
    CGContextScaleCTM(g, k, k);
    CGContextTranslateCTM(g, -crop.origin.x, -crop.origin.y);
    // the portrait image drawn into interface coordinates (the inverse of -toDevice:)
    double W = pt.width, H = pt.height;
    // rot 1: interface (x, y) = (dy, H - dx); rot 3: (W - dy, dx), the inverses of -toDevice:
    if (rot == 1) { CGContextTranslateCTM(g, 0, H); CGContextRotateCTM(g, -M_PI_2); }
    else if (rot == 3) { CGContextTranslateCTM(g, W, 0); CGContextRotateCTM(g, M_PI_2); }
    else if (rot == 2) { CGContextTranslateCTM(g, W, H); CGContextRotateCTM(g, M_PI); }
    CGSize portrait = (rot == 1 || rot == 3) ? CGSizeMake(H, W) : CGSizeMake(W, H);
    [img drawInRect:CGRectMake(0, 0, portrait.width, portrait.height)];
  }];
  NSData *jpeg = UIImageJPEGRepresentation(res, quality);
  if (!jpeg) { *error = rpcError(EInternal, @"JPEG encoding failed"); return nil; }
  return @{@"format" : @"jpeg", @"w" : @(out.width), @"h" : @(out.height), @"scale" : @(scale), @"quality" : @(quality * 100),
           @"rotation" : @(rot), @"data" : [jpeg base64EncodedStringWithOptions:0]};
}

// open_url: the system opens it (any app's scheme, or Safari), like a tap on a link (XCUISystem, iOS 16.4+).
- (id)openURL:(NSDictionary *)p error:(NSError **)error {
  NSURL *url = [p[@"url"] isKindOfClass:NSString.class] ? [NSURL URLWithString:p[@"url"]] : nil;
  if (!url.scheme) { *error = rpcError(EParams, @"url with a scheme required"); return nil; }
  id system = msg0([XCUIDevice sharedDevice], @"system");
  SEL open = NSSelectorFromString(@"openURL:");
  if (![system respondsToSelector:open]) { *error = rpcError(EUnsupported, @"XCUISystem openURL is not available"); return nil; }
  if ((*error = [self guard])) return nil;
  long long seq0 = self.seq;
  double t0 = nowMs();
  NSString *before = [self front][@"bundle"];
  ((void (*)(id, SEL, id))objc_msgSend)(system, open, url);
  // the app that takes it: another app (wait for the switch), or the same one (a deep link: no switch)
  [self waitFront:^BOOL(NSString *b) { return b && ![b isEqualToString:before] && ![b isEqualToString:kSpringBoard]; }
          seconds:1.5];
  return [self finish:[@{@"performed" : @YES, @"url" : url.absoluteString} mutableCopy]
               bundle:[self bundleFor:self.frontPid] t0:t0 seq:seq0 floorAt:0 params:p];
}

// Hit-test the front app every 50 ms until `ok` says yes (or `seconds` pass).
// A SpringBoard alert owns the screen: a view service (not full screen) holds its centre, or SpringBoard
// itself does and its tree has an Alert.
- (BOOL)systemAlertInFront {
  NSDictionary *f = [self front];
  NSString *b = f[@"bundle"];
  if (!b) return NO;
  if (![b isEqualToString:kSpringBoard]) {
    CGRect r = CGRectZero;
    return rectOf([self fetch:@[ @"XC_kAXXCAttributeFrame" ] of:appElement([f[@"pid"] intValue]) error:NULL][@"Frame"], &r) &&
           r.size.width > 0 && !fillsScreen(r);
  }
  id sb = [self snapshot:kSpringBoard pid:[f[@"pid"] intValue] error:NULL];
  NSMutableArray *st = sb ? [NSMutableArray arrayWithObject:sb] : [NSMutableArray array];
  while (st.count) {
    id<XCUIElementSnapshot> n = st.lastObject; [st removeLastObject];
    if (n.elementType == XCUIElementTypeAlert) return YES;
    [st addObjectsFromArray:n.children];
  }
  return NO;
}

- (BOOL)waitFront:(BOOL (^)(NSString *))ok seconds:(double)seconds {
  double end = nowMs() + seconds * 1000;
  while (YES) {
    if (ok([self front][@"bundle"])) return YES;
    if (nowMs() > end) return NO;
    spinUntil(^{ return NO; }, 0.05);
  }
}

// ---- methods
- (id)tree:(NSDictionary *)p error:(NSError **)error {
  double t0 = nowMs();
  long long gen = self.gen;
  NSDictionary *front = [self front];
  id sb = nil;
  NSString *state = [self screenState:front springboardTree:&sb];
  NSMutableArray *bundles = [NSMutableArray array];
  NSString *fb = front[@"bundle"];
  if (fb && ![fb isEqualToString:kSpringBoard]) [bundles addObject:fb];
  self.handles = [NSMutableArray array];
  self.nodes = [NSMutableArray array];
  self.treeKeyboard = NO;
  self.dump++;
  BOOL debug = [p[@"debug"] boolValue];
  self.attrsOverride = [p[@"attrs"] isKindOfClass:NSArray.class] ? p[@"attrs"] : nil;
  NSMutableArray *apps = [NSMutableArray array];
  int count = 0;
  BOOL embedded = NO;  // the front is a view service inside a SpringBoard alert (a permission prompt)
  for (NSString *b in bundles) {
    NSError *e = nil;
    id s = [self snapshot:b pid:[self.pidForBundle[b] intValue] error:&e];
    if (!s) { if (error) *error = e; return nil; }
    CGRect rf = ((id<XCUIElementSnapshot>)s).frame;
    if (fillsScreen(rf)) { gScreen = rf.size; self.lastApp = b; }
    else embedded = YES;
    [apps addObject:@{@"bundle" : b, @"root" : [self node:s count:&count debug:debug]}];
  }
  // SpringBoard: the Home screen, system alerts (IOS.md §9.3) and the lock screen; with a view service in
  // front, the alert around it (its title and buttons are SpringBoard's).
  if (!sb && ((fb && [fb isEqualToString:kSpringBoard]) || embedded)) sb = [self snapshot:kSpringBoard pid:[self.pidForBundle[kSpringBoard] intValue] error:nil];
  if (sb) [apps addObject:@{@"bundle" : kSpringBoard, @"root" : [self node:sb count:&count debug:debug]}];
  // The keyboard's own presence is the truth (1031 can be missed); with it up, mark the focused input: no
  // snapshot attribute says which one has focus, ResponderElement does (the field, or its cell) (§11.3).
  self.keyboard = self.treeKeyboard;
  if (self.treeKeyboard && front[@"pid"]) {
    id cur = [self responder:[front[@"pid"] intValue]];
    NSMutableDictionary *holder = cur ? (NSMutableDictionary *)[self nodeForUid:axKey(cur)] : nil;
    NSMutableArray *st = holder ? [NSMutableArray arrayWithObject:holder] : [NSMutableArray array];
    while (st.count) {
      NSMutableDictionary *n = st.firstObject; [st removeObjectAtIndex:0];
      if (isEditable([n[@"type"] intValue])) { n[@"focused"] = @YES; break; }
      [st addObjectsFromArray:n[@"children"] ?: @[]];
    }
  }
  return @{@"dump" : @(self.dump), @"gen" : @(gen), @"nodes" : @(count), @"ms" : @((long long)(nowMs() - t0)),
           @"screen" : [self screen:state], @"front" : fb ?: [NSNull null], @"apps" : apps, @"keyboard" : @(self.keyboard)};
}

- (NSString *)model {
  size_t n = 0; sysctlbyname("hw.machine", NULL, &n, NULL, 0);
  char *buf = malloc(n); sysctlbyname("hw.machine", buf, &n, NULL, 0);
  NSString *m = buf ? @(buf) : @"?"; free(buf);
  return m;
}

- (id)call:(NSString *)method params:(NSDictionary *)p conn:(DRConn *)conn error:(NSError **)error {
  if ([method isEqualToString:@"ping"]) {
    NSDictionary *front = [self front];
    return @{@"protocol" : @(kProtocol), @"version" : kVersion, @"build" : @DROIDCTL_BUILD_ID, @"backend" : @"ios-runner",
             @"platform" : @"ios", @"release" : UIDevice.currentDevice.systemVersion, @"model" : [self model],
             @"screen" : [self screen:[self screenState:front springboardTree:NULL]], @"gen" : @(self.gen),
             @"uptime_ms" : @((long long)(NSProcessInfo.processInfo.systemUptime * 1000)), @"pid" : @(getpid()),
             @"caps" : @{@"units" : @"pt", @"node_actions" : @YES, @"has_back" : @NO, @"events" : @YES, @"type" : @YES,
                         @"hide_keyboard" : @YES, @"wait_change" : @YES,
                         @"global" : @[ @"home" ], @"screenshot" : @YES, @"open_url" : @YES, @"mark" : @YES, @"clipboard" : @NO}};
  }
  if ([method isEqualToString:@"echo"]) return p ?: @{};
  if ([method isEqualToString:@"gen"]) return @{@"gen" : @(self.gen), @"dump" : @(self.dump)};
  if ([method isEqualToString:@"hit"]) {  // diagnostics: what the AX hit-test answers at a point (screen points)
    id hit = [self elementAtPoint:[self toDevice:CGPointMake([p[@"x"] doubleValue], [p[@"y"] doubleValue])]];
    if (!hit) return @{@"hit" : [NSNull null]};
    NSDictionary *v = [self fetch:@[ @"XC_kAXXCAttributeFrame", @"XC_kAXXCAttributeLabel", @"XC_kAXXCAttributeIdentifier" ] of:hit error:NULL];
    CGRect r = CGRectZero;
    rectOf(v[@"Frame"], &r);
    return @{@"uid" : axKey(hit), @"pid" : @(axPid(hit)), @"label" : v[@"Label"] ?: [NSNull null], @"id" : v[@"Identifier"] ?: [NSNull null],
             @"frame" : @[ @(r.origin.x), @(r.origin.y), @(r.size.width), @(r.size.height) ]};
  }
  if ([method isEqualToString:@"tree"]) return [self tree:p error:error];
  if ([method isEqualToString:@"screen_state"]) return @{@"state" : [self screenState:[self front] springboardTree:NULL]};
  if ([method isEqualToString:@"current"]) {
    NSDictionary *front = [self front];
    NSMutableDictionary *r = [@{@"keyboard" : @(self.keyboard), @"gen" : @(self.gen), @"windows" : @[],
                                @"state" : [self screenState:front springboardTree:NULL]} mutableCopy];
    if (front[@"bundle"]) r[@"pkg"] = front[@"bundle"];
    if (front[@"pid"]) r[@"pid"] = front[@"pid"];
    return r;
  }
  if ([method isEqualToString:@"events"]) {
    long long since = [p[@"since"] longLongValue];
    NSMutableArray *out = [NSMutableArray array];
    @synchronized(self.ring) {
      for (NSDictionary *e in self.ring) if ([e[@"seq"] longLongValue] > since) [out addObject:[e copy]];
    }
    if (p[@"limit"] && out.count > [p[@"limit"] unsignedIntegerValue])
      out = [[out subarrayWithRange:NSMakeRange(0, [p[@"limit"] unsignedIntegerValue])] mutableCopy];
    return @{@"events" : out, @"next" : @(self.seq)};
  }
  if ([method isEqualToString:@"subscribe"]) {
    NSArray *types = [p[@"events"] isKindOfClass:NSArray.class] ? p[@"events"] : @[];
    conn.subscribed = [NSSet setWithArray:types];
    return @{@"subscribed" : types, @"next" : @(self.seq)};
  }
  if ([method isEqualToString:@"unsubscribe"]) { conn.subscribed = nil; return @{@"unsubscribed" : @YES}; }
  if ([method isEqualToString:@"quit"]) { self.done = YES; return @{@"stopping" : @YES}; }
  if ([method isEqualToString:@"gesture"]) return [self gesture:p error:error];
  if ([method isEqualToString:@"launch"]) return [self launch:p error:error];
  if ([method isEqualToString:@"global"]) {
    if (![p[@"name"] isEqual:@"home"]) { *error = rpcError(EUnsupported, [NSString stringWithFormat:@"%@ does not exist on iOS", p[@"name"]]); return nil; }
    if ((*error = [self guard])) return nil;
    long long seq0 = self.seq;
    double t0 = nowMs();
    [[XCUIDevice sharedDevice] pressButton:XCUIDeviceButtonHome];
    return [self finish:[@{@"performed" : @YES, @"name" : @"home"} mutableCopy] bundle:kSpringBoard t0:t0 seq:seq0 floorAt:0 params:p];
  }
  if ([method isEqualToString:@"act"]) return [self act:p error:error];
  if ([method isEqualToString:@"type"]) return [self typeText:p error:error];
  if ([method isEqualToString:@"hide_keyboard"]) return [self hideKeyboard:p error:error];
  if ([method isEqualToString:@"wait_idle"]) {
    NSDictionary *front = [self front];
    long long seq0 = self.seq;
    double t0 = nowMs();
    NSDictionary *d = [self settle:front[@"bundle"] from:t0 floorAt:0
                              spec:@{@"quiet_ms" : @100, @"timeout_ms" : p[@"timeout_ms"] ?: @3000}];
    return @{@"idle" : d[@"idle"], @"ms" : d[@"settle_ms"], @"events" : [self eventsSince:seq0 limit:50]};
  }
  if ([method isEqualToString:@"wait_change"]) {  // the host's wait_for loop: returns when gen moves (or at timeout_ms)
    long long since = [p[@"gen"] longLongValue];
    double cap = MIN([p[@"timeout_ms"] ?: @1000 doubleValue], 10000) / 1000.0;
    BOOL changed = spinUntil(^{ return (BOOL)(self.gen != since); }, cap);
    return @{@"gen" : @(self.gen), @"changed" : @(changed)};
  }
  if ([method isEqualToString:@"screenshot"]) return [self screenshot:p error:error];
  if ([method isEqualToString:@"open_url"]) return [self openURL:p error:error];
  if ([method isEqualToString:@"mark"]) {  // a line in the phone's log, to cut `droidctl logs` into steps
    NSString *label = [p[@"label"] isKindOfClass:NSString.class] ? p[@"label"] : @"";
    NSLog(@"droidctl-mark %@", label);
    return @{@"marked" : label};
  }
  if ([@[ @"wait_for", @"clipboard" ] containsObject:method]) {
    if (error) *error = rpcError(EUnsupported, [NSString stringWithFormat:@"%@ is not implemented by this runner yet", method]);
    return nil;
  }
  if (error) *error = rpcError(ENoMethod, [NSString stringWithFormat:@"method not found: %@", method]);
  return nil;
}

// ---- framing (PROTOCOL.md "Framing" and "Messages")
- (void)handleLine:(NSData *)line conn:(DRConn *)conn {
  id req = [NSJSONSerialization JSONObjectWithData:line options:0 error:nil];
  if (!req) { [conn send:@{@"jsonrpc" : @"2.0", @"id" : [NSNull null], @"error" : @{@"code" : @(EParse), @"message" : @"parse error"}}]; return; }
  id rid = [req isKindOfClass:NSDictionary.class] ? req[@"id"] : nil;
  NSString *method = [req isKindOfClass:NSDictionary.class] ? req[@"method"] : nil;
  if (![method isKindOfClass:NSString.class] || ![req[@"jsonrpc"] isEqual:@"2.0"]) {
    [conn send:@{@"jsonrpc" : @"2.0", @"id" : [NSNull null], @"error" : @{@"code" : @(EInvalid), @"message" : @"invalid request"}}];
    return;
  }
  id params = req[@"params"];
  if (params && ![params isKindOfClass:NSDictionary.class]) {
    if (rid) [conn send:@{@"jsonrpc" : @"2.0", @"id" : rid, @"error" : @{@"code" : @(EParams), @"message" : @"params must be an object"}}];
    return;
  }
  __block id result = nil; __block NSError *err = nil;
  dispatch_sync(dispatch_get_main_queue(), ^{
    @autoreleasepool {  // R1
      @synchronized(self) { [self.issues removeAllObjects]; }
      @try {
        result = [self call:method params:params conn:conn error:&err];
      } @catch (NSException *e) {
        err = rpcError(EInternal, [NSString stringWithFormat:@"%@: %@", e.name, e.reason]);
      }
      @synchronized(self) {
        if (self.issues.count && !err) err = rpcError(EInternal, [self.issues componentsJoinedByString:@"; "]);
      }
    }
  });
  if (!rid) return;  // a notification: no reply
  if (err) [conn send:@{@"jsonrpc" : @"2.0", @"id" : rid, @"error" : @{@"code" : @(err.code), @"message" : err.localizedDescription ?: @"error"}}];
  else [conn send:@{@"jsonrpc" : @"2.0", @"id" : rid, @"result" : result ?: @{}}];
}

- (void)serve:(int)fd {
  DRConn *conn = [DRConn new];
  conn.fd = fd; conn.wlock = [NSLock new];
  int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
  @synchronized(self.conns) { [self.conns addObject:conn]; }
  NSMutableData *buf = [NSMutableData data];
  uint8_t chunk[65536];
  while (!self.done) {
    ssize_t n = read(fd, chunk, sizeof chunk);
    if (n <= 0) break;
    [buf appendBytes:chunk length:n];
    while (YES) {
      @autoreleasepool {
        const char *b = buf.bytes;
        void *nl = memchr(b, '\n', buf.length);
        if (!nl) {
          if (buf.length > kMaxLine) {
            [conn send:@{@"jsonrpc" : @"2.0", @"id" : [NSNull null], @"error" : @{@"code" : @(EInvalid), @"message" : @"line too long"}}];
            buf = [NSMutableData data];
          }
          break;
        }
        NSUInteger len = (const char *)nl - b;
        NSData *line = [buf subdataWithRange:NSMakeRange(0, len)];
        [buf replaceBytesInRange:NSMakeRange(0, len + 1) withBytes:NULL length:0];
        NSString *trimmed = [[[NSString alloc] initWithData:line encoding:NSUTF8StringEncoding]
                             stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
        if (trimmed.length) [self handleLine:line conn:conn];
      }
    }
  }
  @synchronized(self.conns) { [self.conns removeObject:conn]; }
  close(fd);
}

- (BOOL)listenOn:(uint16_t)port {
  int s = socket(AF_INET, SOCK_STREAM, 0);
  int one = 1; setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  struct sockaddr_in a = {.sin_len = sizeof a, .sin_family = AF_INET, .sin_port = htons(port)};
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);  // loopback only: reached through usbmuxd
  if (bind(s, (struct sockaddr *)&a, sizeof a) != 0 || listen(s, 8) != 0) {
    NSLog(@"droidctl: listen on %d failed, errno %d", port, errno);
    return NO;
  }
  [NSThread detachNewThreadWithBlock:^{
    while (!self.done) {
      int c = accept(s, NULL, NULL);
      if (c >= 0) [NSThread detachNewThreadWithBlock:^{ [self serve:c]; }];
    }
    close(s);
  }];
  return YES;
}

- (void)testServe {
  self.continueAfterFailure = YES;
  gStartMs = nowMs();
  gRunner = self;
  self.issues = [NSMutableArray array];
  self.apps = [NSMutableDictionary dictionary];
  self.bundleForPid = [NSMutableDictionary dictionary];
  self.pidForBundle = [NSMutableDictionary dictionary];
  self.handles = [NSMutableArray array];
  self.ring = [NSMutableArray array];
  self.nodes = [NSMutableArray array];
  self.axlog = [NSMutableArray array];
  self.recent = [NSMutableArray array];
  self.conns = [NSMutableArray array];
  self.dump = arc4random_uniform(1u << 30);
  [self installAXHooks];
  NSString *ps = NSProcessInfo.processInfo.environment[@"DROIDCTL_PORT"];
  uint16_t port = ps.intValue > 0 ? (uint16_t)ps.intValue : kDefaultPort;
  XCTAssert([self listenOn:port], @"cannot listen on port %d", port);
  NSLog(@"droidctl: runner %@ build %s ready on 127.0.0.1:%d pid %d", kVersion, DROIDCTL_BUILD_ID, port, getpid());
  while (!self.done) [[NSRunLoop mainRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];
  NSLog(@"droidctl: runner stopped");
}

@end

static void hookRecv3(id self, SEL _cmd, int n, id el, id payload) {
  DroidctlRunner *r = gRunner;
  if (r) {
    NSDictionary *ev = nil;
    @try {
      @synchronized(r.ring) { ev = [r ax:n element:el payload:payload]; }
      if (ev) [r push:ev];
    } @catch (__unused NSException *e) {}
  }
  ((void (*)(id, SEL, int, id, id))gOrigRecv3)(self, _cmd, n, el, payload);
}
