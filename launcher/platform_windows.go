//go:build windows

package main

import (
	"os/exec"
	"syscall"
	"unsafe"
)

const (
	createNoWindow      = 0x08000000
	createNewProcessGrp = 0x00000200
	messageBoxIconError = 0x00000010
)

var (
	user32          = syscall.NewLazyDLL("user32.dll")
	procMessageBoxW = user32.NewProc("MessageBoxW")
)

// configureCmd keeps every child process windowless; the launcher itself is a
// GUI binary, so no console should ever flash on screen.
func configureCmd(cmd *exec.Cmd, detached bool) {
	flags := uint32(createNoWindow)
	if detached {
		flags |= createNewProcessGrp
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: flags}
}

func openInBrowser(url string) error {
	cmd := exec.Command("rundll32.exe", "url.dll,FileProtocolHandler", url)
	configureCmd(cmd, true)
	return cmd.Start()
}

// showErrorDialog uses the UTF-16 MessageBoxW API, so non ASCII text is safe.
func showErrorDialog(title, message string) {
	titlePtr, err := syscall.UTF16PtrFromString(title)
	if err != nil {
		return
	}
	messagePtr, err := syscall.UTF16PtrFromString(message)
	if err != nil {
		return
	}
	procMessageBoxW.Call(
		0,
		uintptr(unsafe.Pointer(messagePtr)),
		uintptr(unsafe.Pointer(titlePtr)),
		uintptr(messageBoxIconError),
	)
}
