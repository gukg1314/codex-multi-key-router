//go:build darwin && cgo

package main

/*
#cgo CFLAGS: -x objective-c
#cgo LDFLAGS: -framework Cocoa -framework WebKit
#include <stdlib.h>

int codexmk_open_window(const char *url, const char *title, const char *open_label, const char *quit_label, int silent, int hide_dock);
void codexmk_show_window(void);
void codexmk_finish_quit(void);
*/
import "C"

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os/exec"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

func configureCmd(cmd *exec.Cmd, detached bool) {
	if detached {
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	}
}

func openWindow(url, title string, silent bool) error {
	cURL := C.CString(url)
	defer C.free(unsafe.Pointer(cURL))
	cTitle := C.CString(title)
	defer C.free(unsafe.Pointer(cTitle))
	cOpen := C.CString(menuOpenLabel)
	defer C.free(unsafe.Pointer(cOpen))
	cQuit := C.CString(menuQuitLabel)
	defer C.free(unsafe.Pointer(cQuit))
	silentFlag := C.int(0)
	if silent {
		silentFlag = 1
	}
	hideDockFlag := C.int(0)
	if readHideDockIcon(url) {
		hideDockFlag = 1
	}
	if C.codexmk_open_window(cURL, cTitle, cOpen, cQuit, silentFlag, hideDockFlag) != 0 {
		return errors.New("AppKit refused to open a window off the main thread")
	}
	return nil
}

func readHideDockIcon(baseURL string) bool {
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Get(baseURL + "/api/settings")
	if err != nil {
		logger.Printf("dock setting unavailable; default hidden=true error=%v", err)
		return true
	}
	defer response.Body.Close()
	var payload struct {
		OK       bool `json:"ok"`
		Settings struct {
			HideDockIcon *bool `json:"hide_dock_icon"`
		} `json:"settings"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 1024*1024)).Decode(&payload); err != nil || response.StatusCode != http.StatusOK || !payload.OK {
		logger.Printf("dock setting invalid; default hidden=true status=%d error=%v", response.StatusCode, err)
		return true
	}
	hide := payload.Settings.HideDockIcon == nil || *payload.Settings.HideDockIcon
	logger.Printf("dock setting loaded hidden=%t", hide)
	return hide
}

// showWindow brings the hidden window back; the single instance channel calls
// it when a second launch asks the running copy to show itself.
func showWindow() {
	C.codexmk_show_window()
}

// finishQuit removes the menu bar item and leaves the AppKit event loop.
func finishQuit() {
	C.codexmk_finish_quit()
}

func openInBrowser(url string) error {
	return exec.Command("open", url).Start()
}

func showErrorDialog(title, message string) {
	script := "display alert " + appleScriptQuote(title) + " message " + appleScriptQuote(message) + " as critical"
	_ = exec.Command("osascript", "-e", script).Run()
}

func appleScriptQuote(text string) string {
	replacer := strings.NewReplacer("\\", "\\\\", "\"", "\\\"", "\r", "", "\n", "\\n")
	return "\"" + replacer.Replace(text) + "\""
}
