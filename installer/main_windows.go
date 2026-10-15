//go:build windows

// Command installer installs or removes the Codex multi key router on Windows.
//
// The setup executable embeds the whole runtime as a zip, so it can be cross
// built on macOS. User visible text is written with unicode escapes to keep
// this source pure ASCII (plan section 5), and every PowerShell call goes
// through -EncodedCommand (UTF-16LE + Base64) for the same reason.
package main

import (
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"
	"unsafe"

	"golang.org/x/sys/windows/registry"
)

const (
	setupVersion   = "1.2.26"
	publisher      = "Codex Router"
	launcherName   = "CodexMultiKey.exe"
	uninstallName  = "uninstall.exe"
	installDirName = "CodexMultiKey"
	uninstallKey   = `Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexMultiKey`

	createNewProcessGroup = 0x00000200
	createNoWindow        = 0x08000000
	mbIconError           = 0x00000010
	mbIconInfo            = 0x00000040

	appDisplayName = "Codex \u8def\u7531"
	shortcutName   = "Codex \u8def\u7531.lnk"
	shortcutDesc   = "Codex \u8def\u7531 - \u591a\u8d26\u53f7\u6a21\u578b\u8def\u7531\u4e0e\u5b98\u65b9\u6a21\u578b\u5171\u5b58"

	textInstallDone      = "\u5b89\u88c5\u5b8c\u6210\u3002"
	textInstallFailed    = "\u5b89\u88c5\u5931\u8d25"
	textUninstallDone    = "\u5378\u8f7d\u5b8c\u6210\u3002"
	textUninstallFailed  = "\u5378\u8f7d\u5931\u8d25"
	textInstallLocation  = "\u5b89\u88c5\u4f4d\u7f6e\uff1a"
	textShortcutsCreated = "\u5df2\u521b\u5efa\u684c\u9762\u548c\u5f00\u59cb\u83dc\u5355\u5feb\u6377\u65b9\u5f0f\uff0c\u73b0\u5728\u53ef\u4ee5\u53cc\u51fb\u56fe\u6807\u4f7f\u7528\u3002"
	textFilesRemoved     = "\u7a0b\u5e8f\u6587\u4ef6\u5df2\u5220\u9664\u3002"
	textSeeLog           = "\u8be6\u60c5\u89c1\u65e5\u5fd7\uff1a"
)

var (
	user32          = syscall.NewLazyDLL("user32.dll")
	procMessageBoxW = user32.NewProc("MessageBoxW")
	logFile         *os.File
	logPath         string
)

func main() {
	args := os.Args[1:]
	if hasFlag(args, "--uninstall") {
		os.Exit(runUninstall(hasFlag(args, "--worker")))
	}
	if len(payload) == 0 {
		// This is the copy that lives in the install folder; double clicking it
		// means "remove the app". The setup build embeds a payload instead.
		os.Exit(runUninstall(false))
	}
	os.Exit(runInstall())
}

func hasFlag(args []string, name string) bool {
	for _, arg := range args {
		if strings.EqualFold(arg, name) {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------- logging

func openLogger(path string) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return
	}
	logFile = file
	logPath = path
}

func logf(format string, args ...interface{}) {
	if logFile == nil {
		return
	}
	line := time.Now().Format("2006-01-02 15:04:05") + " " + fmt.Sprintf(format, args...) + "\r\n"
	_, _ = logFile.WriteString(line)
	_ = logFile.Sync()
}

func closeLogger() {
	if logFile != nil {
		_ = logFile.Close()
		logFile = nil
	}
}

func messageBox(title, message string, flags uintptr) {
	titlePtr, err := syscall.UTF16PtrFromString(title)
	if err != nil {
		return
	}
	messagePtr, err := syscall.UTF16PtrFromString(message)
	if err != nil {
		return
	}
	procMessageBoxW.Call(0, uintptr(unsafe.Pointer(messagePtr)), uintptr(unsafe.Pointer(titlePtr)), flags)
}

func fail(title string, err error) int {
	logf("fatal error=%v", err)
	message := title + "\uff1a" + err.Error()
	if logPath != "" {
		message += "\n\n" + textSeeLog + logPath
	}
	messageBox(appDisplayName, message, mbIconError)
	return 1
}

// ------------------------------------------------------------------ paths

func installRoot() (string, error) {
	if local := strings.TrimSpace(os.Getenv("LOCALAPPDATA")); local != "" {
		return filepath.Join(local, installDirName), nil
	}
	if cache, err := os.UserCacheDir(); err == nil {
		return filepath.Join(cache, installDirName), nil
	}
	return "", errors.New("LOCALAPPDATA is not available")
}

// ---------------------------------------------------------------- install

func runInstall() int {
	root, err := installRoot()
	if err != nil {
		return fail(textInstallFailed, err)
	}
	openLogger(filepath.Join(root, "install.log"))
	defer closeLogger()
	logf("setup start version=%s root=%s", setupVersion, root)
	if _, statErr := os.Stat(filepath.Join(root, launcherName)); statErr == nil {
		logf("existing install detected, stopping running app")
		killRunningApp(root)
	}

	reader, err := readPayload(payload)
	if err != nil {
		return fail(textInstallFailed, err)
	}
	if err := verifyPayload(reader); err != nil {
		return fail(textInstallFailed, err)
	}
	if err := extractPayload(reader, root); err != nil {
		return fail(textInstallFailed, err)
	}
	logf("payload extracted files=%d", len(reader.File))

	if err := createShortcuts(root); err != nil {
		logf("shortcut creation failed error=%v", err)
	}
	if err := writeUninstallEntry(root); err != nil {
		logf("uninstall entry failed error=%v", err)
	}
	if err := launchApp(root); err != nil {
		logf("launch failed error=%v", err)
	}
	messageBox(appDisplayName, textInstallDone+"\n\n"+textInstallLocation+root+"\n"+textShortcutsCreated, mbIconInfo)
	logf("setup complete")
	return 0
}

func launchApp(root string) error {
	target := filepath.Join(root, launcherName)
	if _, err := os.Stat(target); err != nil {
		return err
	}
	cmd := exec.Command(target)
	cmd.Dir = root
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNewProcessGroup}
	if err := cmd.Start(); err != nil {
		return err
	}
	logf("launcher started pid=%d", cmd.Process.Pid)
	return nil
}

func createShortcuts(root string) error {
	target := filepath.Join(root, launcherName)
	script := strings.Join([]string{
		"$ErrorActionPreference = 'Stop'",
		"$shell = New-Object -ComObject WScript.Shell",
		"$target = " + psQuote(target),
		"$root = " + psQuote(root),
		"$links = @((Join-Path ([Environment]::GetFolderPath('Desktop')) " + psQuote(shortcutName) +
			"), (Join-Path ([Environment]::GetFolderPath('Programs')) " + psQuote(shortcutName) + "))",
		"foreach ($link in $links) {",
		"  $shortcut = $shell.CreateShortcut($link)",
		"  $shortcut.TargetPath = $target",
		"  $shortcut.WorkingDirectory = $root",
		"  $shortcut.IconLocation = $target + ',0'",
		"  $shortcut.Description = " + psQuote(shortcutDesc),
		"  $shortcut.Save()",
		"}",
	}, "\r\n")
	return runPowerShell(script)
}

func removeShortcuts() {
	script := strings.Join([]string{
		"$shell = New-Object -ComObject WScript.Shell",
		"$links = @((Join-Path ([Environment]::GetFolderPath('Desktop')) " + psQuote(shortcutName) +
			"), (Join-Path ([Environment]::GetFolderPath('Programs')) " + psQuote(shortcutName) + "))",
		"foreach ($link in $links) { if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force } }",
	}, "\r\n")
	if err := runPowerShell(script); err != nil {
		logf("shortcut removal failed error=%v", err)
	}
}

func killRunningApp(root string) {
	script := strings.Join([]string{
		"$root = " + psQuote(root),
		"foreach ($item in (Get-Process -ErrorAction SilentlyContinue)) {",
		"  $path = $null",
		"  try { $path = $item.Path } catch { $path = $null }",
		"  if ($path -and $path.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {",
		"    Stop-Process -Id $item.Id -Force -ErrorAction SilentlyContinue",
		"  }",
		"}",
	}, "\r\n")
	if err := runPowerShell(script); err != nil {
		logf("process cleanup failed error=%v", err)
	}
	time.Sleep(800 * time.Millisecond)
}

func writeUninstallEntry(root string) error {
	key, _, err := registry.CreateKey(registry.CURRENT_USER, uninstallKey, registry.WRITE)
	if err != nil {
		return err
	}
	defer key.Close()
	size := directorySizeKB(root)
	entries := map[string]string{
		"DisplayName":     appDisplayName,
		"DisplayVersion":  setupVersion,
		"Publisher":       publisher,
		"InstallLocation": root,
		"DisplayIcon":     filepath.Join(root, launcherName),
		"UninstallString": "\"" + filepath.Join(root, uninstallName) + "\" --uninstall",
	}
	for name, value := range entries {
		if err := key.SetStringValue(name, value); err != nil {
			return err
		}
	}
	for name, value := range map[string]uint32{"NoModify": 1, "NoRepair": 1, "EstimatedSize": size} {
		if err := key.SetDWordValue(name, value); err != nil {
			return err
		}
	}
	logf("uninstall entry written root=%s sizeKB=%d", root, size)
	return nil
}

func removeUninstallEntry() {
	err := registry.DeleteKey(registry.CURRENT_USER, uninstallKey)
	if err != nil && !errors.Is(err, syscall.ERROR_FILE_NOT_FOUND) {
		logf("uninstall entry removal failed error=%v", err)
	}
}

func directorySizeKB(root string) uint32 {
	var total int64
	err := filepath.Walk(root, func(_ string, info os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		if !info.IsDir() {
			total += info.Size()
		}
		return nil
	})
	if err != nil {
		return 0
	}
	return uint32(total / 1024)
}

// -------------------------------------------------------------- uninstall

func runUninstall(worker bool) int {
	root, err := installRoot()
	if err != nil {
		return fail(textUninstallFailed, err)
	}
	openLogger(filepath.Join(os.TempDir(), "CodexMultiKey-uninstall.log"))
	defer closeLogger()
	logf("uninstall start worker=%v root=%s", worker, root)

	if !worker {
		self, err := os.Executable()
		if err == nil && !sameFolder(self, os.TempDir()) {
			copyPath := filepath.Join(os.TempDir(), fmt.Sprintf("CodexMultiKey-uninstall-%d.exe", time.Now().UnixNano()))
			if err := copyFile(self, copyPath); err == nil {
				cmd := exec.Command(copyPath, "--uninstall", "--worker")
				cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNewProcessGroup}
				if err := cmd.Start(); err == nil {
					logf("uninstall handed over to %s pid=%d", copyPath, cmd.Process.Pid)
					return 0
				}
				logf("handover start failed error=%v", err)
			} else {
				logf("handover copy failed error=%v", err)
			}
		}
	}

	time.Sleep(1200 * time.Millisecond)
	killRunningApp(root)
	removeShortcuts()
	removeUninstallEntry()
	if err := os.RemoveAll(root); err != nil {
		logf("remove tree failed error=%v", err)
		messageBox(appDisplayName, textUninstallFailed+"\n\n"+err.Error()+"\n\n"+textSeeLog+logPath, mbIconError)
		return 1
	}
	logf("uninstall complete root=%s", root)
	messageBox(appDisplayName, textUninstallDone+"\n\n"+textFilesRemoved, mbIconInfo)
	if self, err := os.Executable(); err == nil && sameFolder(self, os.TempDir()) {
		_ = os.Remove(self)
	}
	return 0
}

func sameFolder(path, folder string) bool {
	left, err := filepath.Abs(filepath.Dir(path))
	if err != nil {
		return false
	}
	right, err := filepath.Abs(folder)
	if err != nil {
		return false
	}
	return strings.EqualFold(left, right)
}

func copyFile(source, target string) error {
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// ------------------------------------------------------------- powershell

func runPowerShell(script string) error {
	encoded := base64.StdEncoding.EncodeToString(encodeUTF16LE(script))
	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow}
	output, err := cmd.CombinedOutput()
	if err != nil {
		logf("powershell failed error=%v output=%s", err, strconv.Quote(string(output)))
		return fmt.Errorf("powershell exit: %w", err)
	}
	return nil
}

func encodeUTF16LE(script string) []byte {
	units := utf16.Encode([]rune(script))
	buffer := make([]byte, 0, len(units)*2)
	for _, unit := range units {
		buffer = append(buffer, byte(unit), byte(unit>>8))
	}
	return buffer
}

// psQuote wraps a value in single quotes for PowerShell.
func psQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "''") + "'"
}
