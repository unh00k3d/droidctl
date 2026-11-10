// S5 spike: an in-simulator helper (run with `simctl spawn`) that reads and acts on accessibility
// without XCTest. Design idea from idb's SimulatorFrameworkBridge (MIT); code written fresh.
//   simax automation            -> prints _AXSAutomationEnabled
//   simax hit X Y               -> element at point: pid + attributes
//   simax press X Y [action]    -> AXUIElementPerformAction on the element at point (default Press 2010)
//   simax setvalue X Y TEXT     -> AXUIElementSetAttributeValue(value 2006)
//   simax tree PID [reps]       -> userTestingSnapshotForElement on the app, node count + ms
#import <Foundation/Foundation.h>
#import <dlfcn.h>
#import <objc/message.h>

typedef void *AXRef;
static AXRef (*CreateSystemWide)(void);
static int32_t (*CopyElementAtPosition)(AXRef, float, float, AXRef *);
static int32_t (*GetPid)(AXRef, pid_t *);
static int32_t (*PerformAction)(AXRef, uint32_t);
static int32_t (*SetAttributeValue)(AXRef, uint32_t, const void *);
static AXRef (*CreateApplication)(pid_t);
static bool (*AutomationEnabled)(void);
static NSDictionary *(*DefaultSnapshotParameters)(void);
static NSArray *(*AttributeNumbersForNames)(NSArray *);

static double now(void) { return CFAbsoluteTimeGetCurrent() * 1000; }

static NSArray *kNames;

static id framework(void) {
  static id fw;
  if (!fw) fw = ((id (*)(id, SEL))objc_msgSend)([NSClassFromString(@"XCTAccessibilityFramework") alloc], NSSelectorFromString(@"initForRemoteAccess"));
  return fw;
}

static id wrap(AXRef el) {
  return ((id (*)(id, SEL, AXRef))objc_msgSend)(NSClassFromString(@"XCAccessibilityElement"), NSSelectorFromString(@"elementWithAXUIElement:"), el);
}

static NSDictionary *attrs(AXRef el) {
  NSError *err = nil;
  id r = ((id (*)(id, SEL, id, id, NSError **))objc_msgSend)(framework(), NSSelectorFromString(@"attributesForElement:attributes:error:"), wrap(el), kNames, &err);
  return r ?: @{@"error" : err.localizedDescription ?: @"nil"};
}

static AXRef hit(float x, float y) {
  AXRef sys = CreateSystemWide(), el = NULL;
  int32_t e = CopyElementAtPosition(sys, x, y, &el);
  if (e) printf("CopyElementAtPosition error %d\n", e);
  return el;
}

static NSUInteger countNodes(id snap) {
  NSUInteger n = 1;
  id kids = nil;
  if ([snap isKindOfClass:NSDictionary.class]) {
    static BOOL shown;
    if (!shown) { shown = YES; NSMutableArray *ks = [NSMutableArray array]; for (id k in snap) [ks addObject:[NSString stringWithFormat:@"%@:%@", k, NSStringFromClass([snap[k] class])]]; printf("keys %s\n", [ks componentsJoinedByString:@" "].UTF8String); }
    for (id k in snap) if ([snap[k] isKindOfClass:NSArray.class]) kids = snap[k];
  } else {
    @try { kids = [snap valueForKey:@"children"]; } @catch (__unused NSException *e) {}
  }
  for (id c in kids) n += countNodes(c);
  return n;
}

int main(int argc, char **argv) {
  @autoreleasepool {
    dlopen("/System/Library/PrivateFrameworks/AXRuntime.framework/AXRuntime", RTLD_NOW);
    dlopen("/System/Library/PrivateFrameworks/AccessibilityUtilities.framework/AccessibilityUtilities", RTLD_NOW);
    if (!dlopen("/Developer/Library/PrivateFrameworks/XCTAutomationSupport.framework/XCTAutomationSupport", RTLD_NOW)) printf("dlopen XCTAutomationSupport: %s\n", dlerror());
    CreateSystemWide = dlsym(RTLD_DEFAULT, "AXUIElementCreateSystemWide");
    CopyElementAtPosition = dlsym(RTLD_DEFAULT, "AXUIElementCopyElementAtPosition");
    GetPid = dlsym(RTLD_DEFAULT, "AXUIElementGetPid");
    PerformAction = dlsym(RTLD_DEFAULT, "AXUIElementPerformAction");
    SetAttributeValue = dlsym(RTLD_DEFAULT, "AXUIElementSetAttributeValue");
    CreateApplication = dlsym(RTLD_DEFAULT, "AXUIElementCreateApplication");
    AutomationEnabled = dlsym(RTLD_DEFAULT, "_AXSAutomationEnabled");
    DefaultSnapshotParameters = dlsym(RTLD_DEFAULT, "XCTDefaultSnapshotParameters");
    AttributeNumbersForNames = dlsym(RTLD_DEFAULT, "XCAXAccessibilityAttributesForStringAttributes");
    kNames = @[ @"XC_kAXXCAttributeElementType", @"XC_kAXXCAttributeLabel", @"XC_kAXXCAttributeIdentifier",
                @"XC_kAXXCAttributeValue", @"XC_kAXXCAttributeFrame" ];
    if (argc < 2) return 2;
    NSString *cmd = @(argv[1]);
    if ([cmd isEqualToString:@"setautomation"]) {
      id settings = ((id (*)(id, SEL))objc_msgSend)(NSClassFromString(@"AXSettings"), NSSelectorFromString(@"sharedInstance"));
      ((void (*)(id, SEL, BOOL))objc_msgSend)(settings, NSSelectorFromString(@"setAutomationEnabled:"), atoi(argv[2]) != 0);
      [NSThread sleepForTimeInterval:0.5];
      printf("automation now=%d\n", AutomationEnabled ? AutomationEnabled() : -1);
    } else if ([cmd isEqualToString:@"automation"]) {
      printf("automation=%d\n", AutomationEnabled ? AutomationEnabled() : -1);
    } else if ([cmd isEqualToString:@"hit"] || [cmd isEqualToString:@"press"] || [cmd isEqualToString:@"setvalue"]) {
      double t0 = now();
      AXRef el = hit(atof(argv[2]), atof(argv[3]));
      double t1 = now();
      if (!el) return 1;
      pid_t pid = 0; GetPid(el, &pid);
      NSDictionary *a = attrs(el);
      double t2 = now();
      printf("pid=%d hit_ms=%.1f attrs_ms=%.1f attrs=%s\n", pid, t1 - t0, t2 - t1, [[a description] stringByReplacingOccurrencesOfString:@"\n" withString:@" "].UTF8String);
      if ([cmd isEqualToString:@"press"]) {
        uint32_t action = argc > 4 ? (uint32_t)atoi(argv[4]) : 2010;
        double t3 = now();
        int32_t e = PerformAction(el, action);
        printf("perform action=%u err=%d ms=%.1f\n", action, e, now() - t3);
      } else if ([cmd isEqualToString:@"setvalue"]) {
        double t3 = now();
        int32_t e = SetAttributeValue(el, 2006, (__bridge const void *)@(argv[4]));
        printf("setvalue err=%d ms=%.1f\n", e, now() - t3);
      }
    } else if ([cmd isEqualToString:@"tree"]) {
      pid_t pid = atoi(argv[2]);
      int reps = argc > 3 ? atoi(argv[3]) : 1;
      id appEl = ((id (*)(id, SEL, int))objc_msgSend)(NSClassFromString(@"XCAccessibilityElement"), NSSelectorFromString(@"elementWithProcessIdentifier:"), pid);
      AXRef app = ((AXRef (*)(id, SEL))objc_msgSend)(appEl, NSSelectorFromString(@"AXUIElement"));
      printf("CreateApplication=%p DefaultSnapshotParameters=%p AttributeNumbersForNames=%p app=%p\n", CreateApplication, DefaultSnapshotParameters, AttributeNumbersForNames, app);
      if (!app || !DefaultSnapshotParameters || !AttributeNumbersForNames) return 1;
      NSMutableDictionary *opts = [DefaultSnapshotParameters() mutableCopy] ?: [NSMutableDictionary dictionary];
      opts[@"attributes"] = AttributeNumbersForNames(kNames);
      for (int i = 0; i < reps; i++) {
        double t0 = now();
        NSError *err = nil;
        id snap = ((id (*)(id, SEL, id, id, NSError **))objc_msgSend)(framework(), NSSelectorFromString(@"userTestingSnapshotForElement:options:error:"), (__bridge id)app, opts, &err);
        double t1 = now();
        if (!snap) { printf("snapshot error %s\n", err.localizedDescription.UTF8String); return 1; }
        printf("tree class=%s nodes=%lu ms=%.1f\n", NSStringFromClass([snap class]).UTF8String, (unsigned long)countNodes(snap), t1 - t0);
      }
    }
  }
  return 0;
}
