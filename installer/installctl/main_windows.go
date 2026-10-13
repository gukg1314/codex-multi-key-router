//go:build windows

// Command installctl performs the filesystem and registry changes behind the
// NSIS wizard. It is embedded in setup and uninstaller, never installed.
package main

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

const (
	uninstallKey = `Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexMultiKey`
	runKey       = `Software\Microsoft\Windows\CurrentVersion\Run`
	displayName  = "Codex \u8def\u7531"
	publisher    = "Codex Router"
	shortcutName = "Codex \u8def\u7531.lnk"
)

var logFile *os.File
var queryImageName = syscall.NewLazyDLL("kernel32.dll").NewProc("QueryFullProcessImageNameW")

func main() {
	if len(os.Args) != 6 {
		os.Exit(2)
	}
	mode, source, target, version, logPath := os.Args[1], os.Args[2], os.Args[3], os.Args[4], os.Args[5]
	if err := openLog(logPath); err != nil {
		os.Exit(2)
	}
	defer logFile.Close()
	logf("action=%s target=%s version=%s", mode, target, version)
	var err error
	switch mode {
	case "install":
		err = install(source, target, version)
	case "uninstall":
		err = uninstall(target)
	default:
		err = errors.New("unknown action")
	}
	if err != nil {
		logf("action failed error=%v", err)
		fmt.Fprintln(os.Stdout, err)
		os.Exit(1)
	}
	logf("action complete")
	fmt.Fprintln(os.Stdout, "OK")
}

func openLog(path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	var err error
	logFile, err = os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	return err
}

func logf(format string, args ...interface{}) {
	if logFile == nil {
		return
	}
	_, _ = fmt.Fprintf(logFile, "%s %s\r\n", time.Now().Format("2006-01-02 15:04:05"), fmt.Sprintf(format, args...))
	_ = logFile.Sync()
}

func normalizeRoot(raw string) (string, error) {
	if !filepath.IsAbs(raw) {
		return "", fmt.Errorf("install path is not absolute: %s", raw)
	}
	path, err := filepath.Abs(raw)
	if err != nil {
		return "", err
	}
	path = filepath.Clean(path)
	if samePath(path, filepath.VolumeName(path)+string(os.PathSeparator)) {
		return "", fmt.Errorf("install path is a volume root: %s", path)
	}
	return path, nil
}

func checkNoReparse(path string, walk bool) error {
	for current := path; current != ""; current = filepath.Dir(current) {
		if current == filepath.Dir(current) {
			break
		}
		if _, err := os.Lstat(current); errors.Is(err, os.ErrNotExist) {
			continue
		} else if err != nil {
			return err
		}
		if err := checkOnePath(current); err != nil {
			return err
		}
	}
	if !walk {
		return nil
	}
	if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
		return nil
	} else if err != nil {
		return err
	}
	return filepath.WalkDir(path, func(child string, _ fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		return checkOnePath(child)
	})
}

func checkOnePath(path string) error {
	wide, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	attributes, err := windows.GetFileAttributes(wide)
	if err != nil {
		return err
	}
	if attributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 {
		return fmt.Errorf("reparse point is not allowed in install tree: %s", path)
	}
	return nil
}

func registeredRoot() (string, error) {
	key, err := registry.OpenKey(registry.CURRENT_USER, uninstallKey, registry.QUERY_VALUE)
	if errors.Is(err, registry.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	defer key.Close()
	name, _, err := key.GetStringValue("DisplayName")
	if err != nil || name != displayName {
		return "", errors.New("existing uninstall entry has an unexpected product name")
	}
	vendor, _, err := key.GetStringValue("Publisher")
	if err != nil || vendor != publisher {
		return "", errors.New("existing uninstall entry has an unexpected publisher")
	}
	location, _, err := key.GetStringValue("InstallLocation")
	if err != nil {
		return "", fmt.Errorf("existing install location is missing: %w", err)
	}
	root, err := normalizeRoot(location)
	if err != nil {
		return "", err
	}
	return root, nil
}

func validateOwnedRoot(root string, markerRequired bool) error {
	if err := checkNoReparse(root, true); err != nil {
		return err
	}
	for _, name := range []string{launcherFile, "uninstall.exe", filepath.Join("runtime", "app", "admin.py")} {
		info, err := os.Stat(filepath.Join(root, name))
		if err != nil || info.IsDir() {
			return fmt.Errorf("registered install is incomplete: %s", filepath.Join(root, name))
		}
	}
	return validateMarker(root, markerRequired)
}

func emptyDirectory(path string) (bool, error) {
	entries, err := os.ReadDir(path)
	if errors.Is(err, os.ErrNotExist) {
		return true, nil
	}
	return len(entries) == 0, err
}

func install(source, destination, version string) error {
	if version == "" || strings.ContainsAny(version, `\/:*?"<>|`) {
		return errors.New("invalid setup version")
	}
	target, err := normalizeRoot(destination)
	if err != nil {
		return err
	}
	old, err := registeredRoot()
	if err != nil {
		return err
	}
	residual := false
	if old != "" {
		if err := validateOwnedRoot(old, false); err != nil {
			return err
		}
	} else if _, err := os.Stat(target); err == nil {
		empty, err := emptyDirectory(target)
		if err != nil {
			return err
		}
		if !empty {
			if err := checkNoReparse(target, true); err != nil {
				return err
			}
			if err := validateResidual(target); err != nil {
				return fmt.Errorf("target folder is not empty and has no matching uninstall marker: %w", err)
			}
			old, residual = target, true
			logf("verified residual directory adopted root=%s", target)
		}
	}
	if old != "" && (pathWithin(target, old) && !samePath(target, old) || pathWithin(old, target) && !samePath(old, target)) {
		return errors.New("new and old install directories overlap")
	}
	if err := checkNoReparse(target, old != "" && samePath(target, old)); err != nil {
		return err
	}
	if old == "" || !samePath(old, target) {
		empty, err := emptyDirectory(target)
		if err != nil {
			return err
		}
		if !empty {
			return fmt.Errorf("target folder is not empty and is not the registered install: %s", target)
		}
	}
	for _, name := range []string{launcherFile, "uninstall.exe", filepath.Join("runtime", "app", "admin.py"), filepath.Join("runtime", "python", "python.exe")} {
		if info, err := os.Stat(filepath.Join(source, name)); err != nil || info.IsDir() {
			return fmt.Errorf("setup payload is incomplete: %s", name)
		}
	}
	if old != "" && !residual {
		if err := checkPayloadCollisions(old, source); err != nil {
			return err
		}
	}
	if old != "" && !residual {
		logf("upgrade detected old=%s new=%s", old, target)
		if err := stopOwnedProcesses(old); err != nil {
			return err
		}
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	stage, err := randomSibling(target, "stage")
	if err != nil {
		return err
	}
	if err := os.Mkdir(stage, 0o755); err != nil {
		return err
	}
	defer os.RemoveAll(stage)
	if err := protectStage(stage); err != nil {
		return fmt.Errorf("protect staging directory: %w", err)
	}
	if old != "" {
		if err := copyTree(old, stage); err != nil {
			return fmt.Errorf("copy old install: %w", err)
		}
	}
	if residual {
		preserved, err := randomSibling(filepath.Join(stage, "preserved"), "user-files")
		if err != nil {
			return err
		}
		preserved = filepath.Join(stage, filepath.Base(preserved))
		err = filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
			if walkErr != nil || entry.IsDir() {
				return walkErr
			}
			relative, err := filepath.Rel(source, path)
			if err != nil {
				return err
			}
			collision := filepath.Join(stage, relative)
			if _, err := os.Stat(collision); errors.Is(err, os.ErrNotExist) {
				return nil
			} else if err != nil {
				return err
			}
			return preserveCollision(stage, collision, preserved)
		})
		if err != nil {
			return fmt.Errorf("preserve residual file before overwrite: %w", err)
		}
		if err := os.Remove(filepath.Join(stage, residualFile)); err != nil {
			return err
		}
		logf("residual collision files preserved path=%s", preserved)
	}
	if err := copyTree(source, stage); err != nil {
		return fmt.Errorf("copy new payload: %w", err)
	}
	if err := writeInstallRecords(stage, source, target, version); err != nil {
		return err
	}
	if err := checkNoReparse(stage, true); err != nil {
		return err
	}
	logf("stage prepared path=%s", stage)
	regBefore, err := snapshotUninstallRegistry()
	if err != nil {
		return err
	}
	runPlan, err := prepareRunMigration(old, target, func() (map[string]registryValue, error) {
		state, err := snapshotRunRegistry()
		return state.values, err
	})
	if err != nil {
		logf("autostart preflight failed step=read key=%s error=%v", runKey, err)
		return fmt.Errorf("read autostart entries: %w", err)
	}
	if len(runPlan) != 0 {
		if err := preflightRunWrite(runPlan); err != nil {
			return err
		}
	}
	runChanges := make(map[string]runChange)
	preexistingEmpty := false
	if !samePath(target, old) {
		if _, err := os.Stat(target); err == nil {
			if err := os.Remove(target); err != nil {
				return err
			}
			preexistingEmpty = true
		}
	}
	restoreMetadata := func(cause error) error {
		var failures []string
		if err := restoreRunChanges(runChanges); err != nil {
			failures = append(failures, "autostart: "+err.Error())
		}
		if err := restoreUninstallRegistry(regBefore); err != nil {
			failures = append(failures, "uninstall entry: "+err.Error())
		}
		if len(failures) != 0 {
			logf("metadata rollback failed details=%s", strings.Join(failures, "; "))
			return fmt.Errorf("%v; metadata rollback failed: %s", cause, strings.Join(failures, "; "))
		}
		return cause
	}
	metadataStarted := false
	commit := func(_ string) error {
		metadataStarted = true
		if err := writeUninstallRegistry(target, version); err != nil {
			return restoreMetadata(err)
		}
		if err := migrateRunValues(old, target, runPlan, runChanges); err != nil {
			return restoreMetadata(err)
		}
		if err := updateShortcuts(old, target); err != nil {
			return restoreMetadata(err)
		}
		return nil
	}
	backup, err := replaceTree(old, target, stage, commit)
	if err != nil {
		if metadataStarted {
			if restoreErr := restoreUninstallRegistry(regBefore); restoreErr != nil {
				logf("uninstall registry rollback retry failed error=%v", restoreErr)
				err = errors.Join(err, fmt.Errorf("uninstall registry restore: %w", restoreErr))
			}
			if restoreErr := restoreRunChanges(runChanges); restoreErr != nil {
				logf("autostart rollback retry failed error=%v", restoreErr)
				err = errors.Join(err, fmt.Errorf("autostart restore: %w", restoreErr))
			}
		} else {
			logf("metadata rollback skipped reason=commit-not-started")
		}
		if preexistingEmpty {
			_ = os.Mkdir(target, 0o755)
		}
		return err
	}
	logf("install committed path=%s", target)
	if backup != "" {
		if err := os.Remove(filepath.Join(backup, "uninstall.exe")); err != nil && !errors.Is(err, os.ErrNotExist) {
			logf("old backup uninstaller retained path=%s error=%v", filepath.Join(backup, "uninstall.exe"), err)
		}
		if err := os.RemoveAll(backup); err != nil {
			logf("old backup retained path=%s error=%v", backup, err)
		}
	}
	return nil
}

// A private, inheritable DACL prevents a custom shared parent directory from
// broadening access to copied keys and secrets during an upgrade or move.
func protectStage(path string) error {
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return err
	}
	sddl := "D:P(A;OICI;FA;;;" + user.User.Sid.String() + ")(A;OICI;FA;;;SY)"
	descriptor, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return err
	}
	dacl, _, err := descriptor.DACL()
	if err != nil {
		return err
	}
	if err := windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, dacl, nil); err != nil {
		return err
	}
	logf("stage DACL restricted to current user and SYSTEM path=%s", path)
	return nil
}

func stopOwnedProcesses(root string) error {
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(snapshot)
	var entry windows.ProcessEntry32
	entry.Size = uint32(unsafe.Sizeof(entry))
	if err := windows.Process32First(snapshot, &entry); err != nil {
		return err
	}
	for {
		if entry.ProcessID != uint32(os.Getpid()) {
			handle, openErr := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION|windows.PROCESS_TERMINATE|windows.SYNCHRONIZE, false, entry.ProcessID)
			if openErr == nil {
				var buffer [32768]uint16
				size := uint32(len(buffer))
				if queryErr := processImageName(handle, &buffer[0], &size); queryErr == nil {
					image := windows.UTF16ToString(buffer[:size])
					if pathWithin(image, root) {
						logf("stopping owned process pid=%d image=%s", entry.ProcessID, image)
						if err := windows.TerminateProcess(handle, 1); err != nil {
							windows.CloseHandle(handle)
							return err
						}
						result, err := windows.WaitForSingleObject(handle, 10000)
						if err != nil || result != windows.WAIT_OBJECT_0 {
							windows.CloseHandle(handle)
							return fmt.Errorf("process did not stop pid=%d result=%d error=%v", entry.ProcessID, result, err)
						}
					}
				}
				windows.CloseHandle(handle)
			}
		}
		if err := windows.Process32Next(snapshot, &entry); errors.Is(err, windows.ERROR_NO_MORE_FILES) {
			break
		} else if err != nil {
			return err
		}
	}
	return nil
}

func processImageName(handle windows.Handle, buffer *uint16, size *uint32) error {
	result, _, err := queryImageName.Call(uintptr(handle), 0, uintptr(unsafe.Pointer(buffer)), uintptr(unsafe.Pointer(size)))
	if result != 0 {
		return nil
	}
	return err
}

type registrySnapshot struct {
	exists bool
	values map[string]registryValue
}

func snapshotKey(path string, names []string) (registrySnapshot, error) {
	state := registrySnapshot{values: make(map[string]registryValue)}
	key, err := registry.OpenKey(registry.CURRENT_USER, path, registry.QUERY_VALUE)
	if errors.Is(err, registry.ErrNotExist) {
		return state, nil
	}
	if err != nil {
		return state, err
	}
	defer key.Close()
	state.exists = true
	for _, name := range names {
		if value, valueType, err := key.GetStringValue(name); err == nil {
			state.values[name] = registryValue{exists: true, stringValue: value, stringType: valueType}
			continue
		}
		if value, _, err := key.GetIntegerValue(name); err == nil {
			state.values[name] = registryValue{exists: true, dwordValue: uint32(value), isDword: true}
			continue
		}
		state.values[name] = registryValue{}
	}
	return state, nil
}

func restoreKey(path string, state registrySnapshot) error {
	if !state.exists {
		if err := registry.DeleteKey(registry.CURRENT_USER, path); err != nil && !errors.Is(err, registry.ErrNotExist) {
			return err
		}
		return nil
	}
	key, _, err := registry.CreateKey(registry.CURRENT_USER, path, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer key.Close()
	for name, value := range state.values {
		if !value.exists {
			if err := key.DeleteValue(name); err != nil && !errors.Is(err, registry.ErrNotExist) {
				return err
			}
		} else if value.isDword {
			if err := key.SetDWordValue(name, value.dwordValue); err != nil {
				return err
			}
		} else if value.stringType == registry.EXPAND_SZ {
			if err := key.SetExpandStringValue(name, value.stringValue); err != nil {
				return err
			}
		} else if err := key.SetStringValue(name, value.stringValue); err != nil {
			return err
		}
	}
	return nil
}

var uninstallValues = []string{"DisplayName", "DisplayVersion", "Publisher", "InstallLocation", "DisplayIcon", "UninstallString", "QuietUninstallString", "NoModify", "NoRepair"}

func snapshotUninstallRegistry() (registrySnapshot, error) {
	return snapshotKey(uninstallKey, uninstallValues)
}
func restoreUninstallRegistry(state registrySnapshot) error { return restoreKey(uninstallKey, state) }
func snapshotRunRegistry() (registrySnapshot, error)        { return snapshotKey(runKey, runValues) }

func writeUninstallRegistry(root, version string) error {
	key, _, err := registry.CreateKey(registry.CURRENT_USER, uninstallKey, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer key.Close()
	values := map[string]string{
		"DisplayName": displayName, "DisplayVersion": version, "Publisher": publisher,
		"InstallLocation": root, "DisplayIcon": filepath.Join(root, launcherFile),
		"UninstallString":      `"` + filepath.Join(root, "uninstall.exe") + `"`,
		"QuietUninstallString": `"` + filepath.Join(root, "uninstall.exe") + `" /S`,
	}
	for name, value := range values {
		if err := key.SetStringValue(name, value); err != nil {
			return err
		}
	}
	for _, name := range []string{"NoModify", "NoRepair"} {
		if err := key.SetDWordValue(name, 1); err != nil {
			return err
		}
	}
	logf("uninstall entry committed root=%s version=%s", root, version)
	return nil
}

func preflightRunWrite(names []string) error {
	key, err := registry.OpenKey(registry.CURRENT_USER, runKey, registry.SET_VALUE)
	if err != nil {
		logf("autostart preflight failed step=open-write key=%s names=%s error=%v", runKey, strings.Join(names, ","), err)
		return fmt.Errorf("autostart preflight for %s: %w", strings.Join(names, ","), err)
	}
	_ = key.Close()
	logf("autostart preflight passed key=%s names=%s", runKey, strings.Join(names, ","))
	return nil
}

func migrateRunValues(old, target string, names []string, changes map[string]runChange) error {
	if len(names) == 0 {
		reason := "no-owned-value-change"
		if old == "" {
			reason = "fresh-install"
		} else if samePath(old, target) {
			reason = "same-directory"
		}
		logf("autostart migration skipped reason=%s", reason)
		return nil
	}
	key, err := registry.OpenKey(registry.CURRENT_USER, runKey, registry.QUERY_VALUE|registry.SET_VALUE)
	if err != nil {
		logf("autostart migration failed step=open-read-write key=%s names=%s error=%v", runKey, strings.Join(names, ","), err)
		return fmt.Errorf("open autostart key for migration: %w", err)
	}
	defer key.Close()
	for _, name := range names {
		value, valueType, err := key.GetStringValue(name)
		if errors.Is(err, registry.ErrNotExist) {
			logf("autostart migration skipped name=%s reason=value-removed", name)
			continue
		}
		if err != nil {
			logf("autostart migration failed step=read key=%s name=%s error=%v", runKey, name, err)
			return fmt.Errorf("read autostart value %s: %w", name, err)
		}
		if !runValueOwned(value, old) {
			logf("autostart migration skipped name=%s reason=foreign-path", name)
			continue
		}
		before := registryValue{exists: true, stringValue: value, stringType: valueType}
		if name == runValues[0] {
			after := registryValue{exists: true, stringValue: desiredRunValue(target), stringType: registry.SZ}
			if value == after.stringValue {
				logf("autostart migration skipped name=%s reason=already-current", name)
				continue
			}
			if err := key.SetStringValue(name, after.stringValue); err != nil {
				logf("autostart migration failed step=set key=%s name=%s error=%v", runKey, name, err)
				return fmt.Errorf("update autostart value %s: %w", name, err)
			}
			changes[name] = runChange{before: before, after: after}
			logf("autostart migrated name=%s action=set", name)
		} else {
			if err := key.DeleteValue(name); err != nil {
				logf("autostart migration failed step=delete key=%s name=%s error=%v", runKey, name, err)
				return fmt.Errorf("remove legacy autostart value %s: %w", name, err)
			}
			changes[name] = runChange{before: before}
			logf("autostart migrated name=%s action=delete", name)
		}
	}
	return nil
}

func restoreRunChanges(changes map[string]runChange) error {
	if len(changes) == 0 {
		return nil
	}
	key, err := registry.OpenKey(registry.CURRENT_USER, runKey, registry.QUERY_VALUE|registry.SET_VALUE)
	if err != nil {
		logf("autostart rollback failed step=open-read-write key=%s names=%s error=%v", runKey, strings.Join(changedRunNames(changes), ","), err)
		return fmt.Errorf("open autostart key for rollback: %w", err)
	}
	defer key.Close()
	var failures []error
	for _, name := range changedRunNames(changes) {
		change := changes[name]
		current, currentType, readErr := key.GetStringValue(name)
		currentValue := registryValue{}
		if errors.Is(readErr, registry.ErrUnexpectedType) {
			logf("autostart rollback skipped name=%s reason=changed-externally-type", name)
			delete(changes, name)
			continue
		}
		if readErr != nil && !errors.Is(readErr, registry.ErrNotExist) {
			logf("autostart rollback failed step=read key=%s name=%s error=%v", runKey, name, readErr)
			failures = append(failures, fmt.Errorf("read autostart value %s: %w", name, readErr))
			continue
		}
		if readErr == nil {
			currentValue = registryValue{exists: true, stringValue: current, stringType: currentType}
		}
		if !runRollbackAllowed(change, currentValue) {
			logf("autostart rollback skipped name=%s reason=changed-externally", name)
			delete(changes, name)
			continue
		}
		var restoreErr error
		if change.before.stringType == registry.EXPAND_SZ {
			restoreErr = key.SetExpandStringValue(name, change.before.stringValue)
		} else {
			restoreErr = key.SetStringValue(name, change.before.stringValue)
		}
		if err := restoreErr; err != nil {
			logf("autostart rollback failed step=set key=%s name=%s error=%v", runKey, name, err)
			failures = append(failures, fmt.Errorf("restore autostart value %s: %w", name, err))
			continue
		}
		delete(changes, name)
		logf("autostart rolled back name=%s action=set", name)
	}
	return errors.Join(failures...)
}

func deleteOwnedRunValues(root string) error {
	key, err := registry.OpenKey(registry.CURRENT_USER, runKey, registry.QUERY_VALUE)
	if errors.Is(err, registry.ErrNotExist) {
		return nil
	}
	if err != nil {
		logf("autostart uninstall failed step=open-read key=%s error=%v", runKey, err)
		return fmt.Errorf("read autostart key: %w", err)
	}
	var names []string
	for _, name := range runValues {
		value, _, err := key.GetStringValue(name)
		if errors.Is(err, registry.ErrNotExist) {
			continue
		}
		if err != nil {
			key.Close()
			logf("autostart uninstall failed step=read key=%s name=%s error=%v", runKey, name, err)
			return fmt.Errorf("read autostart value %s: %w", name, err)
		}
		if runValueOwned(value, root) {
			names = append(names, name)
		}
	}
	key.Close()
	if len(names) == 0 {
		logf("autostart uninstall skipped reason=no-owned-values")
		return nil
	}
	key, err = registry.OpenKey(registry.CURRENT_USER, runKey, registry.QUERY_VALUE|registry.SET_VALUE)
	if err != nil {
		logf("autostart uninstall failed step=open-read-write key=%s names=%s error=%v", runKey, strings.Join(names, ","), err)
		return fmt.Errorf("open autostart key for removal: %w", err)
	}
	defer key.Close()
	for _, name := range names {
		value, _, err := key.GetStringValue(name)
		if errors.Is(err, registry.ErrNotExist) {
			continue
		}
		if err != nil {
			logf("autostart uninstall failed step=verify key=%s name=%s error=%v", runKey, name, err)
			return fmt.Errorf("verify autostart value %s: %w", name, err)
		}
		if !runValueOwned(value, root) {
			logf("autostart uninstall skipped name=%s reason=foreign-path", name)
			continue
		}
		if err := key.DeleteValue(name); err != nil {
			logf("autostart uninstall failed step=delete key=%s name=%s error=%v", runKey, name, err)
			return fmt.Errorf("remove autostart value %s: %w", name, err)
		}
		logf("autostart removed name=%s", name)
	}
	return nil
}

func encodeScript(script string) string {
	units := utf16.Encode([]rune(script))
	data := make([]byte, len(units)*2)
	for index, unit := range units {
		data[2*index], data[2*index+1] = byte(unit), byte(unit>>8)
	}
	return base64.StdEncoding.EncodeToString(data)
}

func psQuote(value string) string { return "'" + strings.ReplaceAll(value, "'", "''") + "'" }

func runPowerShell(script string) error {
	command := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encodeScript(script))
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	output, err := command.CombinedOutput()
	if err != nil {
		logf("powershell failed output=%q error=%v", string(output), err)
		return err
	}
	return nil
}

func shortcutScript(old, target, action string) string {
	lines := []string{
		"$ErrorActionPreference = 'Stop'",
		"$shell = New-Object -ComObject WScript.Shell",
		"$old = " + psQuote(old),
		"$target = " + psQuote(filepath.Join(target, launcherFile)),
		"$root = " + psQuote(target),
		"$name = " + psQuote(shortcutName),
		"$links = @((Join-Path ([Environment]::GetFolderPath('Desktop')) $name), (Join-Path ([Environment]::GetFolderPath('Programs')) $name))",
	}
	if action == "update" {
		lines = append(lines,
			"$snapshots = @()",
			"foreach ($link in $links) {",
			"  $existed = Test-Path -LiteralPath $link",
			"  $bytes = $null",
			"  if ($existed) {",
			"    $existing = $shell.CreateShortcut($link).TargetPath",
			"    $owned = $existing.Equals($target, [StringComparison]::OrdinalIgnoreCase) -or ($old -and $existing.StartsWith($old + '\\', [StringComparison]::OrdinalIgnoreCase))",
			"    if (-not $owned) { throw ('Shortcut belongs to a different application: ' + $link) }",
			"    $bytes = [IO.File]::ReadAllBytes($link)",
			"  }",
			"  $snapshots += [pscustomobject]@{ Link = $link; Existed = $existed; Bytes = $bytes }",
			"}",
			"$attempted = @()",
			"try {",
			"  foreach ($snapshot in $snapshots) {",
			"    $attempted += $snapshot",
			"    $shortcut = $shell.CreateShortcut($snapshot.Link)",
			"    $shortcut.TargetPath = $target",
			"    $shortcut.WorkingDirectory = $root",
			"    $shortcut.IconLocation = $target + ',0'",
			"    $shortcut.Save()",
			"  }",
			"} catch {",
			"  $primary = $_.Exception.Message",
			"  $rollbackErrors = @()",
			"  for ($i = $attempted.Count - 1; $i -ge 0; $i--) {",
			"    $snapshot = $attempted[$i]",
			"    try {",
			"      if ($snapshot.Existed) { [IO.File]::WriteAllBytes($snapshot.Link, [byte[]]$snapshot.Bytes) }",
			"      elseif (Test-Path -LiteralPath $snapshot.Link) { Remove-Item -LiteralPath $snapshot.Link -Force }",
			"    } catch { $rollbackErrors += ($snapshot.Link + ': ' + $_.Exception.Message) }",
			"  }",
			"  if ($rollbackErrors.Count -gt 0) { throw ('Shortcut update failed: ' + $primary + '; rollback failed: ' + ($rollbackErrors -join '; ')) }",
			"  throw ('Shortcut update failed and was rolled back: ' + $primary)",
			"}",
		)
		return strings.Join(lines, "\r\n")
	}
	lines = append(lines,
		"foreach ($link in $links) {",
		"  if (Test-Path -LiteralPath $link) {",
		"    $existing = $shell.CreateShortcut($link).TargetPath",
		"    $owned = $existing.Equals($target, [StringComparison]::OrdinalIgnoreCase) -or ($old -and $existing.StartsWith($old + '\\', [StringComparison]::OrdinalIgnoreCase))",
		"    if (-not $owned) { throw ('Shortcut belongs to a different application: ' + $link) }",
		"  }",
		"  if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force }",
		"}",
	)
	return strings.Join(lines, "\r\n")
}

func updateShortcuts(old, target string) error {
	logf("shortcuts transaction begin old=%s target=%s", old, target)
	if err := runPowerShell(shortcutScript(old, target, "update")); err != nil {
		return fmt.Errorf("update shortcuts: %w", err)
	}
	logf("shortcuts transaction committed target=%s", target)
	return nil
}

func backupUninstaller(path string) (string, error) {
	input, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer input.Close()
	backup, err := os.CreateTemp("", "codexmk-uninstaller-*.exe")
	if err != nil {
		return "", err
	}
	name := backup.Name()
	if _, err := io.Copy(backup, input); err != nil {
		backup.Close()
		os.Remove(name)
		return "", err
	}
	if err := backup.Close(); err != nil {
		os.Remove(name)
		return "", err
	}
	return name, nil
}

func restoreUninstaller(backup, destination string) error {
	input, err := os.Open(backup)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.OpenFile(destination, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(output, input)
	closeErr := output.Close()
	if copyErr != nil {
		return copyErr
	}
	return closeErr
}

func uninstall(destination string) error {
	root, err := normalizeRoot(destination)
	if err != nil {
		return err
	}
	registered, err := registeredRoot()
	if err != nil {
		return err
	}
	if registered == "" || !samePath(registered, root) {
		return errors.New("uninstaller directory does not match the current-user install record")
	}
	if err := checkNoReparse(root, true); err != nil {
		return err
	}
	if err := validateMarker(root, true); err != nil {
		return err
	}
	if _, err := os.Stat(filepath.Join(root, "uninstall.exe")); err != nil {
		return err
	}
	if err := stopOwnedProcesses(root); err != nil {
		return err
	}
	content, err := os.ReadFile(filepath.Join(root, manifestFile))
	if err != nil {
		return err
	}
	markerContent, err := os.ReadFile(filepath.Join(root, markerFile))
	if err != nil {
		return err
	}
	var record manifest
	if err := json.Unmarshal(content, &record); err != nil || record.Product != productID {
		return errors.New("invalid installed file manifest")
	}
	paths := make([]string, 0, len(record.Files))
	for relative, expected := range record.Files {
		if _, err := safeManifestPath(root, relative); err != nil {
			return err
		}
		if len(expected) != 64 {
			return errors.New("invalid installed file hash")
		}
		paths = append(paths, relative)
	}
	sort.Strings(paths)
	preserved, err := randomSibling(filepath.Join(root, "preserved"), "user-files")
	if err != nil {
		return err
	}
	preserved = filepath.Join(root, filepath.Base(preserved))
	for _, relative := range paths {
		path, _ := safeManifestPath(root, relative)
		if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
			continue
		} else if err != nil {
			return err
		}
		actual, err := hashFile(path)
		if err != nil {
			return err
		}
		if !strings.EqualFold(actual, record.Files[relative]) {
			if err := preserveCollision(root, path, preserved); err != nil {
				return fmt.Errorf("preserve modified program file: %w", err)
			}
			logf("modified program file preserved original=%s backup=%s", path, preserved)
			continue
		}
		if err := os.Remove(path); err != nil {
			return err
		}
	}
	// The application's own writable tree is removed on explicit uninstall.
	// Upgrade never runs this branch and always copies the full tree first.
	dataDir := filepath.Join(root, "runtime", "app", "runtime")
	if err := os.RemoveAll(dataDir); err != nil {
		return fmt.Errorf("remove application data: %w", err)
	}
	if err := removeGeneratedCache(filepath.Join(root, "runtime", "app", "__pycache__")); err != nil {
		return err
	}
	if err := deleteOwnedRunValues(root); err != nil {
		return err
	}
	if err := runPowerShell(shortcutScript("", root, "remove")); err != nil {
		return err
	}
	residualContent, residualErr := os.ReadFile(filepath.Join(root, residualFile))
	if residualErr != nil && !errors.Is(residualErr, os.ErrNotExist) {
		return residualErr
	}
	remaining, err := hasResidualFiles(root)
	if err != nil {
		return err
	}
	if remaining {
		data, err := json.MarshalIndent(marker{Product: productID, Location: root}, "", "  ")
		if err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(root, residualFile), data, 0o600); err != nil {
			return err
		}
		logf("residual user files kept under %s", root)
	} else if err := os.Remove(filepath.Join(root, residualFile)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	uninstaller := filepath.Join(root, "uninstall.exe")
	backup, err := backupUninstaller(uninstaller)
	if err != nil {
		return err
	}
	keepBackup := false
	defer func() {
		if !keepBackup {
			_ = os.Remove(backup)
		}
	}()
	if err := os.Remove(uninstaller); err != nil {
		return fmt.Errorf("uninstaller is locked; registration was kept: %w", err)
	}
	restoreRecords := func(cause error) error {
		if restoreErr := restoreUninstaller(backup, uninstaller); restoreErr != nil {
			keepBackup = true
			logf("uninstaller restore failed backup=%s error=%v", backup, restoreErr)
			return fmt.Errorf("%v; uninstaller backup remains at %s: %w", cause, backup, restoreErr)
		}
		if err := os.WriteFile(filepath.Join(root, manifestFile), content, 0o644); err != nil {
			logf("manifest restore failed error=%v", err)
			return fmt.Errorf("%v; manifest restore failed: %w", cause, err)
		}
		if err := os.WriteFile(filepath.Join(root, markerFile), markerContent, 0o644); err != nil {
			logf("marker restore failed error=%v", err)
			return fmt.Errorf("%v; marker restore failed: %w", cause, err)
		}
		if errors.Is(residualErr, os.ErrNotExist) {
			_ = os.Remove(filepath.Join(root, residualFile))
		} else if err := os.WriteFile(filepath.Join(root, residualFile), residualContent, 0o600); err != nil {
			logf("residual marker restore failed error=%v", err)
			return fmt.Errorf("%v; residual marker restore failed: %w", cause, err)
		}
		return cause
	}
	if err := os.Remove(filepath.Join(root, manifestFile)); err != nil {
		return restoreRecords(err)
	}
	if err := os.Remove(filepath.Join(root, markerFile)); err != nil {
		return restoreRecords(err)
	}
	if err := registry.DeleteKey(registry.CURRENT_USER, uninstallKey); err != nil {
		return restoreRecords(err)
	}
	// Remove only empty directories. Unknown files stay.
	var directories []string
	_ = filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err == nil && entry.IsDir() {
			directories = append(directories, path)
		}
		return nil
	})
	for index := len(directories) - 1; index >= 0; index-- {
		if !samePath(directories[index], root) {
			_ = os.Remove(directories[index])
		}
	}
	logf("uninstall removed owned files and app data root=%s", root)
	return nil
}

func removeGeneratedCache(root string) error {
	if _, err := os.Stat(root); errors.Is(err, os.ErrNotExist) {
		return nil
	} else if err != nil {
		return err
	}
	var directories []string
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			directories = append(directories, path)
			return nil
		}
		if strings.EqualFold(filepath.Ext(path), ".pyc") {
			return os.Remove(path)
		}
		return nil
	})
	if err != nil {
		return err
	}
	for index := len(directories) - 1; index >= 0; index-- {
		_ = os.Remove(directories[index])
	}
	return nil
}

func hasResidualFiles(root string) (bool, error) {
	remaining := false
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		for _, reserved := range []string{markerFile, manifestFile, residualFile, "uninstall.exe"} {
			if samePath(path, filepath.Join(root, reserved)) {
				return nil
			}
		}
		remaining = true
		return filepath.SkipAll
	})
	return remaining, err
}
