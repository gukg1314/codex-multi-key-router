// Minimal WKWebView window plus a menu bar item for the Codex multi key
// router launcher. Objective-C is used only here; the shared logic lives in
// main.go, which also owns the close flow (stop the router and admin service,
// then leave while keeping the Codex provider configuration).
//
// Closing the window only hides it: the menu bar item keeps the tool alive and
// can bring the window back. With the Dock hidden, Cmd+Q only hides the window;
// the status item "Quit" still runs the shared close flow. Labels come from Go, so this file
// stays ASCII and never needs an encoding dependent string literal.

#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#import <dispatch/dispatch.h>
#import <stdio.h>

// Implemented in export_darwin.go; runs the shared close flow in Go.
extern void codexmkQuitRequested(void);

static void codexmk_log(const char *message) {
    fprintf(stderr, "[codexmk] %s\n", message != NULL ? message : "");
    fflush(stderr);
}

void codexmk_show_window(void);
void codexmk_finish_quit(void);

@interface CodexMKDelegate : NSObject <NSWindowDelegate, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler>
@end

static NSWindow *gWindow = nil;
static WKWebView *gWebView = nil;
static CodexMKDelegate *gDelegate = nil;
static NSStatusItem *gStatusItem = nil;
static BOOL gHideDockIcon = YES;
static BOOL gTerminationPending = NO;

static void codexmk_apply_dock_visibility(BOOL hide) {
    NSApplicationActivationPolicy policy = hide ? NSApplicationActivationPolicyAccessory : NSApplicationActivationPolicyRegular;
    if (gHideDockIcon == hide && [NSApp activationPolicy] == policy) {
        return;
    }
    if (![NSApp setActivationPolicy:policy]) {
        codexmk_log("dock visibility change rejected");
        return;
    }
    gHideDockIcon = hide;
    codexmk_log(hide ? "dock hidden; accessory activation policy" : "dock visible; regular activation policy");
    if (gWindow != nil && [gWindow isVisible]) {
        [NSApp activateIgnoringOtherApps:YES];
        [gWindow makeKeyAndOrderFront:nil];
    }
}

// JavaScript confirm()/alert() flow through the WKWebView UIDelegate. Without
// one WebKit answers the panels with false/undefined without showing anything,
// which is why the delete confirmation was silently ignored on macOS. The
// titles stay ASCII in this file: Chinese labels are written as escapes.
static NSString *codexmk_panel_title(void) {
    NSBundle *bundle = [NSBundle mainBundle];
    NSString *name = [bundle objectForInfoDictionaryKey:@"CFBundleDisplayName"];
    if ([name length] == 0) {
        name = [bundle objectForInfoDictionaryKey:@"CFBundleName"];
    }
    if ([name length] == 0) {
        name = [[NSProcessInfo processInfo] processName];
    }
    return name;
}

static NSString *codexmk_ok_title(void) {
    NSString *language = [[NSLocale preferredLanguages] firstObject];
    if (language != nil && [language hasPrefix:@"zh-Hant"]) {
        return @"\u78ba\u5b9a";
    }
    if (language != nil && [language hasPrefix:@"zh"]) {
        return @"\u786e\u5b9a";
    }
    return @"OK";
}

static NSString *codexmk_cancel_title(void) {
    NSString *language = [[NSLocale preferredLanguages] firstObject];
    if (language != nil && [language hasPrefix:@"zh"]) {
        return @"\u53d6\u6d88";
    }
    return @"Cancel";
}

// Presents the panel as a sheet when the window is on screen and falls back to
// a modal dialog otherwise. AppKit retains the alert until the sheet is done,
// so the local reference is released right away.
static void codexmk_present_js_panel(NSAlert *alert, void (^handler)(NSModalResponse response)) {
    if (gWindow != nil && [gWindow isVisible]) {
        [alert beginSheetModalForWindow:gWindow completionHandler:handler];
        [alert release];
        return;
    }
    NSModalResponse response = [alert runModal];
    [alert release];
    handler(response);
}

@implementation CodexMKDelegate

// Hiding instead of exiting keeps the menu bar item (and the running router)
// alive, which is what "close the window" means for this tool.
- (void)windowWillClose:(NSNotification *)notification {
    (void)notification;
    codexmk_log("window hidden; menu bar item stays");
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
    (void)sender;
    return NO;
}

// System logout/shutdown must still stop the services even with the Dock hidden.
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    (void)sender;
    codexmk_log("quit requested; running the shared close flow");
    gTerminationPending = YES;
    codexmkQuitRequested();
    return NSTerminateLater;
}

// Clicking the Dock icon after the window was hidden shows it again.
- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)flag {
    (void)sender;
    (void)flag;
    codexmk_show_window();
    return YES;
}

- (void)showFromMenuBar:(id)sender {
    (void)sender;
    codexmk_show_window();
}

- (void)quitFromMenuBar:(id)sender {
    (void)sender;
    codexmk_log("quit requested from the menu bar");
    codexmkQuitRequested();
}

- (void)quitFromApplicationMenu:(id)sender {
    (void)sender;
    if (gHideDockIcon) {
        [gWindow orderOut:nil];
        codexmk_log("Cmd+Q hid window; menu bar item stays");
    } else {
        [NSApp terminate:nil];
    }
}

- (void)userContentController:(WKUserContentController *)controller didReceiveScriptMessage:(WKScriptMessage *)message {
    (void)controller;
    if (![message.name isEqualToString:@"codexmkDockVisibility"] || !message.frameInfo.isMainFrame ||
        ![message.body isKindOfClass:[NSNumber class]] || CFGetTypeID((CFTypeRef)message.body) != CFBooleanGetTypeID()) {
        codexmk_log("dock visibility message rejected");
        return;
    }
    BOOL hide = [message.body boolValue];
    dispatch_async(dispatch_get_main_queue(), ^{
        codexmk_apply_dock_visibility(hide);
    });
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    (void)webView;
    (void)navigation;
    codexmk_log("webview finished loading");
}

- (void)webView:(WKWebView *)webView didFailNavigation:(WKNavigation *)navigation withError:(NSError *)error {
    (void)webView;
    (void)navigation;
    codexmk_log([[[NSString stringWithFormat:@"webview failed: %@", [error localizedDescription]] stringByReplacingOccurrencesOfString:@"\n" withString:@" "] UTF8String]);
}

- (void)webView:(WKWebView *)webView didFailProvisionalNavigation:(WKNavigation *)navigation withError:(NSError *)error {
    (void)webView;
    (void)navigation;
    codexmk_log([[[NSString stringWithFormat:@"webview provisional failed: %@", [error localizedDescription]] stringByReplacingOccurrencesOfString:@"\n" withString:@" "] UTF8String]);
}

// window.alert(): acknowledge only, so the handler takes no result.
- (void)webView:(WKWebView *)webView runJavaScriptAlertPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(void))completionHandler {
    (void)webView;
    (void)frame;
    codexmk_log("javascript alert panel shown");
    NSAlert *alert = [[NSAlert alloc] init];
    [alert setMessageText:codexmk_panel_title()];
    [alert setInformativeText:(message != nil ? message : @"")];
    [alert addButtonWithTitle:codexmk_ok_title()];
    codexmk_present_js_panel(alert, ^(NSModalResponse response) {
        (void)response;
        codexmk_log("javascript alert dismissed");
        completionHandler();
    });
}

// window.confirm(): the page keeps running only when the user accepts, which
// is exactly what the UI expects before deleting a key.
- (void)webView:(WKWebView *)webView runJavaScriptConfirmPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(BOOL result))completionHandler {
    (void)webView;
    (void)frame;
    codexmk_log("javascript confirm panel shown");
    NSAlert *alert = [[NSAlert alloc] init];
    [alert setMessageText:codexmk_panel_title()];
    [alert setInformativeText:(message != nil ? message : @"")];
    [alert addButtonWithTitle:codexmk_ok_title()];
    [alert addButtonWithTitle:codexmk_cancel_title()];
    codexmk_present_js_panel(alert, ^(NSModalResponse response) {
        BOOL accepted = response == NSAlertFirstButtonReturn;
        codexmk_log(accepted ? "javascript confirm accepted" : "javascript confirm dismissed");
        completionHandler(accepted);
    });
}

@end

static void codexmk_install_menu(void) {
    NSMenu *menuBar = [[NSMenu alloc] init];

    NSMenuItem *appItem = [[NSMenuItem alloc] init];
    NSMenu *appMenu = [[NSMenu alloc] initWithTitle:@"Apple"];
    [appMenu addItemWithTitle:@"Hide" action:@selector(hide:) keyEquivalent:@"h"];
    [appMenu addItem:[NSMenuItem separatorItem]];
    NSMenuItem *quitItem = [appMenu addItemWithTitle:@"Quit" action:@selector(quitFromApplicationMenu:) keyEquivalent:@"q"];
    [quitItem setTarget:gDelegate];
    [appItem setSubmenu:appMenu];
    [menuBar addItem:appItem];

    NSMenuItem *editItem = [[NSMenuItem alloc] init];
    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"Edit"];
    [editMenu addItemWithTitle:@"Undo" action:@selector(undo:) keyEquivalent:@"z"];
    [editMenu addItemWithTitle:@"Redo" action:@selector(redo:) keyEquivalent:@"Z"];
    [editMenu addItem:[NSMenuItem separatorItem]];
    [editMenu addItemWithTitle:@"Cut" action:@selector(cut:) keyEquivalent:@"x"];
    [editMenu addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
    [editMenu addItemWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"];
    [editMenu addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
    [editItem setSubmenu:editMenu];
    [menuBar addItem:editItem];

    [NSApp setMainMenu:menuBar];
}

// The status bar mark is generated by packaging/icons.py: a solid silhouette
// with the bright highlights punched out, shipped as 18 px and 36 px so Retina
// screens stay sharp. Both files live in the app bundle resources.
static NSImage *codexmk_status_image(void) {
    NSImage *image = nil;
    NSBundle *bundle = [NSBundle mainBundle];
    NSString *path = [bundle pathForResource:@"statusbar" ofType:@"png"];
    if (path == nil) {
        NSString *executable = [[NSProcessInfo processInfo] arguments][0];
        NSString *candidate = [[executable stringByDeletingLastPathComponent] stringByAppendingPathComponent:@"statusbar.png"];
        if ([[NSFileManager defaultManager] fileExistsAtPath:candidate]) {
            path = candidate;
        }
    }
    if (path != nil) {
        image = [[NSImage alloc] initWithContentsOfFile:path];
    }
    if (image != nil) {
        // Template images follow the light and dark menu bar automatically.
        [image setTemplate:YES];
        codexmk_log("menu bar icon loaded from the bundled silhouette");
        return image;
    }
    codexmk_log("menu bar icon missing; using the system default");
    return [NSImage imageNamed:NSImageNameApplicationIcon];
}

static void codexmk_install_status_item(NSString *openLabel, NSString *quitLabel) {
    NSStatusBar *bar = [NSStatusBar systemStatusBar];
    gStatusItem = [[bar statusItemWithLength:NSVariableStatusItemLength] retain];
    // button.image / button.toolTip replace the NSStatusItem setters that
    // were deprecated back in macOS 10.14.
    [gStatusItem.button setImage:codexmk_status_image()];
    [gStatusItem.button setToolTip:openLabel];

    NSMenu *menu = [[NSMenu alloc] init];
    NSMenuItem *openItem = [[NSMenuItem alloc] initWithTitle:openLabel action:@selector(showFromMenuBar:) keyEquivalent:@""];
    [openItem setTarget:gDelegate];
    [menu addItem:openItem];
    [menu addItem:[NSMenuItem separatorItem]];
    NSMenuItem *quitItem = [[NSMenuItem alloc] initWithTitle:quitLabel action:@selector(quitFromMenuBar:) keyEquivalent:@""];
    [quitItem setTarget:gDelegate];
    [menu addItem:quitItem];
    [gStatusItem setMenu:menu];
    codexmk_log("menu bar item installed");
}

// The Dock tile follows the bundle icon, but a freshly built unsigned bundle can
// still show the generic blank tile while the icon cache is cold. Setting the
// image explicitly makes the Dock and the app switcher show the real icon.
static void codexmk_install_dock_icon(void) {
    NSString *path = [[NSBundle mainBundle] pathForResource:@"AppIcon" ofType:@"icns"];
    if (path == nil) {
        NSString *executable = [[NSProcessInfo processInfo] arguments][0];
        NSString *candidate = [[executable stringByDeletingLastPathComponent] stringByAppendingPathComponent:@"AppIcon.icns"];
        if ([[NSFileManager defaultManager] fileExistsAtPath:candidate]) {
            path = candidate;
        }
    }
    if (path == nil) {
        codexmk_log("dock icon skipped; AppIcon.icns is not next to the launcher");
        return;
    }
    NSImage *icon = [[NSImage alloc] initWithContentsOfFile:path];
    if (icon == nil) {
        codexmk_log("dock icon skipped; AppIcon.icns could not be decoded");
        return;
    }
    [NSApp setApplicationIconImage:icon];
    [icon release];
    codexmk_log("dock icon set from AppIcon.icns");
}

static void codexmk_run(const char *url_utf8, const char *title_utf8, const char *open_utf8, const char *quit_utf8, int silent, int hide_dock) {
    @autoreleasepool {
        NSString *target = [NSString stringWithUTF8String:(url_utf8 != NULL ? url_utf8 : "")];
        NSString *title = [NSString stringWithUTF8String:(title_utf8 != NULL ? title_utf8 : "")];
        NSString *openLabel = [NSString stringWithUTF8String:(open_utf8 != NULL ? open_utf8 : "")];
        NSString *quitLabel = [NSString stringWithUTF8String:(quit_utf8 != NULL ? quit_utf8 : "")];

        codexmk_log("window creating");

        [NSApplication sharedApplication];
        codexmk_apply_dock_visibility(hide_dock != 0);
        codexmk_log(hide_dock ? "startup Dock hidden" : "startup Dock visible");

        if (gDelegate == nil) {
            gDelegate = [[CodexMKDelegate alloc] init];
        }
        [NSApp setDelegate:gDelegate];
        codexmk_install_menu();
        codexmk_install_dock_icon();
        codexmk_install_status_item(openLabel, quitLabel);

        // Keep in sync with windowWidth / windowHeight in main.go.
        NSRect frame = NSMakeRect(0, 0, 1180, 820);
        NSWindowStyleMask style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                                  NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable;
        gWindow = [[NSWindow alloc] initWithContentRect:frame
                                              styleMask:style
                                                backing:NSBackingStoreBuffered
                                                  defer:NO];
        [gWindow setTitle:title];
        [gWindow setReleasedWhenClosed:NO];
        [gWindow setDelegate:gDelegate];
        [gWindow center];

        WKWebViewConfiguration *configuration = [[WKWebViewConfiguration alloc] init];
        [configuration.userContentController addScriptMessageHandler:gDelegate name:@"codexmkDockVisibility"];
        gWebView = [[WKWebView alloc] initWithFrame:frame configuration:configuration];
        [gWebView setAutoresizingMask:(NSViewWidthSizable | NSViewHeightSizable)];
        [gWebView setNavigationDelegate:gDelegate];
        [gWebView setUIDelegate:gDelegate];
        NSURL *nsURL = [NSURL URLWithString:target];
        if (nsURL != nil) {
            codexmk_log("webview loading url");
            [gWebView loadRequest:[NSURLRequest requestWithURL:nsURL]];
        } else {
            codexmk_log("webview url is invalid");
        }
        [gWindow setContentView:gWebView];
        if (silent) {
            // Autostart leaves the window hidden independently of Dock visibility.
            codexmk_log("silent start; window stays hidden");
        } else {
            [gWindow makeKeyAndOrderFront:nil];
            [NSApp activateIgnoringOtherApps:YES];
            codexmk_log("window visible; running event loop");
        }
        [NSApp run];
        codexmk_log("event loop done; launcher is leaving");
    }
}

// Runs the AppKit window on the real main thread and blocks until the shared
// close flow stops the event loop. Returns 0 when the window was shown, 1 when
// the caller is on a worker thread.
int codexmk_open_window(const char *url, const char *title, const char *open_label, const char *quit_label, int silent, int hide_dock) {
    codexmk_log("open_window called");
    if (![NSThread isMainThread]) {
        codexmk_log("refusing to build a window off the main thread");
        return 1;
    }
    codexmk_run(url, title, open_label, quit_label, silent, hide_dock);
    codexmk_log("open_window finished");
    return 0;
}

// Wakes the running window from the single instance channel. The webview is
// only touched on the main queue, so this is safe from the HTTP handler.
void codexmk_show_window(void) {
    codexmk_log("show window requested");
    dispatch_async(dispatch_get_main_queue(), ^{
        if (gWindow == nil) {
            codexmk_log("show window skipped; no window yet");
            return;
        }
        if ([gWindow isMiniaturized]) {
            [gWindow deminiaturize:nil];
        }
        [NSApp activateIgnoringOtherApps:YES];
        [gWindow makeKeyAndOrderFront:nil];
        codexmk_log("window shown again");
    });
}

// Leaves the event loop after the shared close flow finished (router stopped).
void codexmk_finish_quit(void) {
    codexmk_log("quitting application");
    dispatch_async(dispatch_get_main_queue(), ^{
        if (gTerminationPending) {
            // Complete the system's pending logout/shutdown instead of cancelling it.
            [NSApp replyToApplicationShouldTerminate:YES];
            return;
        }
        if (gStatusItem != nil) {
            [[NSStatusBar systemStatusBar] removeStatusItem:gStatusItem];
            gStatusItem = nil;
        }
        [NSApp stop:nil];
        NSEvent *event = [NSEvent otherEventWithType:NSEventTypeApplicationDefined
                                            location:NSZeroPoint
                                       modifierFlags:0
                                           timestamp:0
                                        windowNumber:0
                                             context:nil
                                             subtype:0
                                               data1:0
                                               data2:0];
        [NSApp postEvent:event atStart:YES];
    });
}
