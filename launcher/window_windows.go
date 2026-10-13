//go:build windows

// Window lifecycle for the Windows launcher: the WebView2 window is
// subclassed so WM_CLOSE hides the tool in the notification area instead of
// ending the process, and the notification icon can bring it back or run the
// shared close flow. Every Win32 call here uses the wide character (W) variant
// so Chinese paths and titles can never hit a code page problem.

package main

import (
	"errors"
	"os"
	"path/filepath"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	webview2 "github.com/jchv/go-webview2"
	"golang.org/x/sys/windows"
)

const (
	// closeConfirmTimeout is how long the page has to show the close dialog.
	// A page that cannot answer (hung or not loaded yet) is minimized instead,
	// which keeps the tool running and never traps the user.
	closeConfirmTimeout = 1500 * time.Millisecond

	// closeChoiceBinding is the JavaScript bridge the UI uses to answer the
	// close dialog: "shown", "minimize" or "quit".
	closeChoiceBinding = "codexmkCloseChoice"

	appIconResourceID = 1

	wmClose         = 0x0010
	wmRButtonUp     = 0x0205
	wmLButtonDblClk = 0x0203

	// Window messages handled by the subclassed procedure. go-webview2 owns
	// WM_APP (0x8000) for its dispatch queue, so these start one above it.
	wmAppConfirm  = 0x8001
	wmAppMinimize = 0x8002
	wmAppLeave    = 0x8003
	wmAppShowWin  = 0x8004
	wmAppTray     = 0x8005

	gwlWndProc = ^uintptr(3) // GWL_WNDPROC (-4)

	swHide    = 0
	swShow    = 5
	swRestore = 9

	trayIconID = 1

	nimAdd    = 0x00000000
	nimDelete = 0x00000002

	nifMessage = 0x00000001
	nifIcon    = 0x00000002
	nifTip     = 0x00000004

	tpmRightButton = 0x0002
	tpmReturnCmd   = 0x0100

	trayCommandOpen = 1
	trayCommandQuit = 2
)

var (
	shell32 = syscall.NewLazyDLL("shell32.dll")

	procSetWindowLongPtrW = user32.NewProc("SetWindowLongPtrW")
	procCallWindowProcW   = user32.NewProc("CallWindowProcW")
	procPostMessageW      = user32.NewProc("PostMessageW")
	procShowWindow        = user32.NewProc("ShowWindow")
	procSetForegroundWnd  = user32.NewProc("SetForegroundWindow")
	procCreatePopupMenu   = user32.NewProc("CreatePopupMenu")
	procAppendMenuW       = user32.NewProc("AppendMenuW")
	procTrackPopupMenu    = user32.NewProc("TrackPopupMenu")
	procDestroyMenu       = user32.NewProc("DestroyMenu")
	procGetCursorPos      = user32.NewProc("GetCursorPos")
	procCreateIconRes     = user32.NewProc("CreateIconFromResourceEx")

	procShellNotifyIconW = shell32.NewProc("Shell_NotifyIconW")
)

// notifyIconData mirrors NOTIFYICONDATAW from shellapi.h. unsafe.Sizeof gives
// the size Windows expects for this structure version.
type notifyIconData struct {
	CbSize            uint32
	HWnd              windows.Handle
	UID               uint32
	UFlags            uint32
	UCallbackMessage  uint32
	HIcon             windows.Handle
	SzTip             [128]uint16
	DwState           uint32
	DwStateMask       uint32
	SzInfo            [256]uint16
	UTimeoutOrVersion uint32
	SzInfoTitle       [64]uint16
	DwInfoFlags       uint32
	GuidItem          windows.GUID
	HBalloonIcon      windows.Handle
}

type winPoint struct {
	X int32
	Y int32
}

var (
	mainView        webview2.WebView
	mainHWND        windows.Handle
	previousWndProc uintptr
	wndProcCallback uintptr
	trayIcon        windows.Handle
	trayVisible     bool
	closePending    atomic.Bool
	leaving         atomic.Bool
)

// openWindow creates the WebView2 window and runs its message loop. It returns
// when the shared close flow ends the loop. In silent mode (login autostart)
// the window is hidden right away and only the notification area icon stays.
func openWindow(url, title string, silent bool) error {
	dataPath := ""
	if dir, err := os.UserCacheDir(); err == nil {
		dataPath = filepath.Join(dir, "CodexMultiKey", "webview2")
	}
	window := webview2.NewWithOptions(webview2.WebViewOptions{
		Debug:     false,
		AutoFocus: true,
		DataPath:  dataPath,
		WindowOptions: webview2.WindowOptions{
			Title:  title,
			Width:  windowWidth,
			Height: windowHeight,
			IconId: appIconResourceID,
			Center: true,
		},
	})
	if window == nil {
		return errors.New("WebView2 runtime is not available")
	}
	if err := window.Bind(closeChoiceBinding, onCloseChoice); err != nil {
		logger.Printf("close dialog bridge failed error=%v", err)
	}
	mainView = window
	mainHWND = windows.Handle(uintptr(window.Window()))
	installWindowHook()
	if silent {
		// go-webview2 shows the window while creating it, so hide it again and
		// park the tool in the notification area without stealing focus.
		procShowWindow.Call(uintptr(mainHWND), swHide)
		addTrayIcon()
		logger.Printf("silent start; window hidden in the notification area")
	}
	defer window.Destroy()
	window.Navigate(url)
	window.Run()
	logger.Printf("window loop finished")
	return nil
}

// installWindowHook chains our procedure in front of go-webview2's own
// procedure; without it WM_CLOSE would destroy the window and end the loop.
func installWindowHook() {
	if mainHWND == 0 {
		logger.Printf("window hook skipped; no window handle")
		return
	}
	if wndProcCallback == 0 {
		wndProcCallback = windows.NewCallback(codexmkWndProc)
		if wndProcCallback == 0 {
			logger.Printf("window hook failed error=NewCallback")
			return
		}
	}
	previous, _, err := procSetWindowLongPtrW.Call(uintptr(mainHWND), gwlWndProc, wndProcCallback)
	if previous == 0 {
		logger.Printf("window hook failed error=%v", err)
		return
	}
	previousWndProc = previous
	logger.Printf("window hook installed hwnd=%d", mainHWND)
}

func codexmkWndProc(hwnd, msg, wparam, lparam uintptr) uintptr {
	switch uint32(msg) {
	case wmClose:
		if leaving.Load() {
			return callWindowProc(hwnd, msg, wparam, lparam)
		}
		postWindowMessage(wmAppConfirm)
		return 0
	case wmAppConfirm:
		confirmClose()
		return 0
	case wmAppMinimize:
		minimizeToTray()
		return 0
	case wmAppLeave:
		leaveApplication()
		return 0
	case wmAppShowWin:
		restoreFromTray()
		return 0
	case wmAppTray:
		handleTrayEvent(lparam)
		return 0
	default:
		return callWindowProc(hwnd, msg, wparam, lparam)
	}
}

func callWindowProc(hwnd, msg, wparam, lparam uintptr) uintptr {
	result, _, _ := procCallWindowProcW.Call(previousWndProc, hwnd, msg, wparam, lparam)
	return result
}

func postWindowMessage(message uint32) {
	if mainHWND == 0 {
		logger.Printf("window message dropped message=%#x; no window handle", message)
		return
	}
	procPostMessageW.Call(uintptr(mainHWND), uintptr(message), 0, 0)
}

// ----------------------------------------------------------------- close flow

// onCloseChoice is the JavaScript bridge the close dialog answers on. It runs
// on the UI thread, so it only posts messages and never blocks.
func onCloseChoice(action string) string {
	logger.Printf("close dialog answered choice=%q", action)
	switch action {
	case "shown":
		closePending.Store(false)
	case "minimize":
		closePending.Store(false)
		postWindowMessage(wmAppMinimize)
	case "quit":
		closePending.Store(false)
		go quitApp()
	default:
		logger.Printf("close dialog ignored unknown choice=%q", action)
	}
	return "ok"
}

// confirmClose asks the page for a decision. A page that stays silent within
// closeConfirmTimeout is minimized, so the window can never become unclosable.
func confirmClose() {
	if mainView == nil {
		logger.Printf("close dialog unavailable; minimizing instead")
		minimizeToTray()
		return
	}
	closePending.Store(true)
	mainView.Eval("window.codexmkConfirmClose && window.codexmkConfirmClose()")
	time.AfterFunc(closeConfirmTimeout, func() {
		if !closePending.CompareAndSwap(true, false) {
			return
		}
		logger.Printf("close dialog silent for %s; minimizing instead", closeConfirmTimeout)
		postWindowMessage(wmAppMinimize)
	})
}

func minimizeToTray() {
	closePending.Store(false)
	if mainHWND == 0 {
		return
	}
	procShowWindow.Call(uintptr(mainHWND), swHide)
	addTrayIcon()
	logger.Printf("window minimized to the notification area")
}

func restoreFromTray() {
	if mainHWND == 0 {
		return
	}
	removeTrayIcon()
	procShowWindow.Call(uintptr(mainHWND), swShow)
	procShowWindow.Call(uintptr(mainHWND), swRestore)
	procSetForegroundWnd.Call(uintptr(mainHWND))
	logger.Printf("window restored from the notification area")
}

// leaveApplication ends the message loop after the shared close flow finished.
func leaveApplication() {
	leaving.Store(true)
	removeTrayIcon()
	if mainView != nil {
		mainView.Terminate()
	}
}

// showWindow is used by the single instance channel: a second launch asks the
// running copy to show itself instead of starting twice.
func showWindow() {
	postWindowMessage(wmAppShowWin)
}

// finishQuit ends the process once the router has been stopped. The window
// message is the graceful path; the exit call is the safety net.
func finishQuit() {
	leaving.Store(true)
	removeTrayIcon()
	if mainHWND != 0 {
		postWindowMessage(wmAppLeave)
		time.Sleep(300 * time.Millisecond)
	}
	logger.Printf("leaving the application")
	os.Exit(0)
}

// ----------------------------------------------------------------- notification

func handleTrayEvent(lparam uintptr) {
	switch uint32(lparam) & 0xFFFF {
	case wmRButtonUp:
		showTrayMenu()
	case wmLButtonDblClk:
		restoreFromTray()
	}
}

func showTrayMenu() {
	menu, _, _ := procCreatePopupMenu.Call()
	if menu == 0 {
		logger.Printf("tray menu could not be created")
		return
	}
	defer procDestroyMenu.Call(menu)
	appendTrayMenu(menu, menuOpenLabel, trayCommandOpen)
	appendTrayMenu(menu, menuQuitLabel, trayCommandQuit)

	var cursor winPoint
	procGetCursorPos.Call(uintptr(unsafe.Pointer(&cursor)))
	// The foreground calls are required so the menu closes when the user
	// clicks somewhere else.
	procSetForegroundWnd.Call(uintptr(mainHWND))
	command, _, _ := procTrackPopupMenu.Call(
		menu,
		tpmRightButton|tpmReturnCmd,
		uintptr(int(cursor.X)),
		uintptr(int(cursor.Y)),
		0,
		uintptr(mainHWND),
		0,
	)
	switch int32(command) {
	case trayCommandOpen:
		restoreFromTray()
	case trayCommandQuit:
		logger.Printf("tray quit requested")
		go quitApp()
	}
}

func appendTrayMenu(menu uintptr, label string, command uintptr) {
	text, err := windows.UTF16PtrFromString(label)
	if err != nil {
		return
	}
	procAppendMenuW.Call(menu, 0, command, uintptr(unsafe.Pointer(text)))
}

func addTrayIcon() {
	if trayVisible || mainHWND == 0 {
		return
	}
	if trayIcon == 0 {
		trayIcon = createTrayIcon()
	}
	data := notifyIconData{
		HWnd:             mainHWND,
		UID:              trayIconID,
		UFlags:           nifMessage | nifIcon | nifTip,
		UCallbackMessage: wmAppTray,
		HIcon:            trayIcon,
	}
	data.CbSize = uint32(unsafe.Sizeof(data))
	copyUTF16(data.SzTip[:], windowTitle)
	result, _, err := procShellNotifyIconW.Call(nimAdd, uintptr(unsafe.Pointer(&data)))
	if result == 0 {
		logger.Printf("tray icon add failed error=%v", err)
		return
	}
	trayVisible = true
	logger.Printf("tray icon added")
}

func removeTrayIcon() {
	if !trayVisible || mainHWND == 0 {
		return
	}
	data := notifyIconData{HWnd: mainHWND, UID: trayIconID}
	data.CbSize = uint32(unsafe.Sizeof(data))
	procShellNotifyIconW.Call(nimDelete, uintptr(unsafe.Pointer(&data)))
	trayVisible = false
	logger.Printf("tray icon removed")
}

// createTrayIcon turns the embedded ICO into a HICON. The 32 pixel frame is
// picked so the icon stays sharp on scaled displays.
func createTrayIcon() windows.Handle {
	offset, size, ok := icoFrame(trayIconBytes, 2)
	if !ok {
		offset, size, ok = icoFrame(trayIconBytes, 0)
	}
	if !ok {
		logger.Printf("tray icon data unreadable; keeping the default icon")
		return 0
	}
	icon, _, err := procCreateIconRes.Call(
		uintptr(unsafe.Pointer(&trayIconBytes[offset])),
		uintptr(size),
		1,
		0x00030000,
		32,
		32,
		0,
	)
	if icon == 0 {
		logger.Printf("tray icon creation failed error=%v", err)
		return 0
	}
	return windows.Handle(icon)
}

func copyUTF16(target []uint16, text string) {
	encoded, err := windows.UTF16FromString(text)
	if err != nil {
		return
	}
	if len(encoded) > len(target) {
		encoded = encoded[:len(target)]
		encoded[len(encoded)-1] = 0
	}
	copy(target, encoded)
}
