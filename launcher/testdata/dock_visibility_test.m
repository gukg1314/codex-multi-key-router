#import <Cocoa/Cocoa.h>
#import <assert.h>
#import <stdlib.h>
#include "../window_darwin.m"

static int quitRequests = 0;
void codexmkQuitRequested(void) { quitRequests++; }

@interface DockTestApplication : NSObject {
@public
    NSApplicationActivationPolicy policy;
    int terminateCount;
    int activateCount;
    BOOL terminationAllowed;
}
@end
@implementation DockTestApplication
- (NSApplicationActivationPolicy)activationPolicy { return policy; }
- (BOOL)setActivationPolicy:(NSApplicationActivationPolicy)value { policy = value; return YES; }
- (void)activateIgnoringOtherApps:(BOOL)value { (void)value; activateCount++; }
- (void)terminate:(id)sender { (void)sender; terminateCount++; }
- (void)replyToApplicationShouldTerminate:(BOOL)allowed { terminationAllowed = allowed; }
@end

@interface DockTestWindow : NSObject {
@public
    BOOL visible;
    BOOL miniaturized;
    int showCount;
}
@end
@implementation DockTestWindow
- (BOOL)isVisible { return visible; }
- (BOOL)isMiniaturized { return miniaturized; }
- (void)deminiaturize:(id)sender { (void)sender; miniaturized = NO; }
- (void)makeKeyAndOrderFront:(id)sender { (void)sender; visible = YES; showCount++; }
- (void)orderOut:(id)sender { (void)sender; visible = NO; }
@end

int main(void) {
    @autoreleasepool {
        DockTestApplication *app = [[DockTestApplication alloc] init];
        DockTestWindow *window = [[DockTestWindow alloc] init];
        NSApp = (NSApplication *)app;
        gWindow = (NSWindow *)window;
        gDelegate = [[CodexMKDelegate alloc] init];
        app->policy = NSApplicationActivationPolicyRegular;

        codexmk_apply_dock_visibility(YES);
        assert(gHideDockIcon && app->policy == NSApplicationActivationPolicyAccessory);
        assert(!window->visible); // A silent window stays hidden when policy changes.
        window->visible = YES;
        codexmk_apply_dock_visibility(NO);
        assert(!gHideDockIcon && app->policy == NSApplicationActivationPolicyRegular);
        assert(window->visible && window->showCount == 1);
        [gDelegate quitFromApplicationMenu:nil];
        assert(app->terminateCount == 1 && quitRequests == 0);

        codexmk_apply_dock_visibility(YES);
        [gDelegate quitFromApplicationMenu:nil];
        assert(!window->visible && app->terminateCount == 1 && quitRequests == 0);
        assert(![gDelegate applicationShouldTerminateAfterLastWindowClosed:NSApp]);
        [gDelegate windowWillClose:nil];
        assert(quitRequests == 0);
        [gDelegate quitFromMenuBar:nil];
        assert(quitRequests == 1);
        assert([gDelegate applicationShouldTerminate:NSApp] == NSTerminateLater);
        assert(quitRequests == 2); // System termination still runs service shutdown.

        window->miniaturized = YES;
        codexmk_show_window();
        codexmk_finish_quit();
        dispatch_async(dispatch_get_main_queue(), ^{
            assert(window->visible && !window->miniaturized);
            assert(gHideDockIcon && app->policy == NSApplicationActivationPolicyAccessory);
            assert(app->terminationAllowed); // Pending logout/shutdown is allowed after cleanup.
            fprintf(stdout, "Native Dock behavior checks passed\n");
            exit(0);
        });
        dispatch_main();
    }
}
