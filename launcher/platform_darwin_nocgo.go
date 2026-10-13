//go:build darwin && !cgo

package main

import (
	"errors"
	"os/exec"
	"strings"
	"syscall"
)

func configureCmd(cmd *exec.Cmd, detached bool) {
	if detached {
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	}
}

// Without cgo there is no WKWebView binding; the caller falls back to a browser.
func openWindow(url, title string, silent bool) error {
	return errors.New("cgo is disabled; no native window available")
}

// Without the native window there is nothing to show and no menu bar item to
// remove; the browser fallback owns the session in that case.
func showWindow() {}

func finishQuit() {}

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
