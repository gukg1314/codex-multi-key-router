//go:build windows

package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"

	"golang.org/x/sys/windows"
)

// renameDiagnostic only reads filesystem and security state. It runs before
// rollback so the log reflects the failed rename rather than its aftermath.
func renameDiagnostic(phase, source, target, other string, renameErr error) {
	logf("rename failed phase=%s win32=%d error=%v", phase, win32Code(renameErr), renameErr)
	var currentUser *windows.SID
	if user, err := windows.GetCurrentProcessToken().GetTokenUser(); err == nil {
		currentUser = user.User.Sid
	} else {
		logf("rename diagnostic current-user lookup win32=%d error=%v", win32Code(err), err)
	}
	logRenamePath("source", source, windows.DELETE, currentUser)
	logRenamePath("target", target, windows.DELETE, currentUser)
	logRenamePath("parent", filepath.Dir(target), windows.FILE_APPEND_DATA, currentUser)
	if other != "" {
		logRenamePath("other", other, windows.DELETE, currentUser)
	}
}

func logRenamePath(role, path string, desiredAccess uint32, currentUser *windows.SID) {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		logf("rename path role=%s state=missing", role)
		return
	}
	if err != nil {
		logf("rename path role=%s state=unknown stat_win32=%d stat_error=%v", role, win32Code(err), err)
		return
	}
	kind := "file"
	if info.IsDir() {
		kind = "directory"
	} else if info.Mode()&os.ModeSymlink != 0 {
		kind = "reparse"
	}
	attributes := "unknown"
	if wide, err := windows.UTF16PtrFromString(path); err == nil {
		if flags, err := windows.GetFileAttributes(wide); err == nil {
			attributes = fmt.Sprintf("0x%X,readonly=%t,reparse=%t", flags,
				flags&windows.FILE_ATTRIBUTE_READONLY != 0,
				flags&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0)
		} else {
			attributes = fmt.Sprintf("error-win32-%d", win32Code(err))
		}
	}
	owner, dacl := securitySummary(path, currentUser)
	probe := accessProbe(path, desiredAccess)
	accessName := "delete"
	if desiredAccess == windows.FILE_APPEND_DATA {
		accessName = "add-subdirectory"
	}
	logf("rename path role=%s kind=%s attributes=%s owner=%s dacl=%s access=%s probe=%s",
		role, kind, attributes, owner, dacl, accessName, probe)
}

func securitySummary(path string, currentUser *windows.SID) (owner, dacl string) {
	descriptor, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return "unknown", fmt.Sprintf("query-win32-%d", win32Code(err))
	}
	owner = "unknown"
	if sid, _, err := descriptor.Owner(); err == nil && sid != nil {
		switch {
		case currentUser != nil && sid.Equals(currentUser):
			owner = "current-user"
		case sid.IsWellKnown(windows.WinLocalSystemSid):
			owner = "system"
		case sid.IsWellKnown(windows.WinBuiltinAdministratorsSid):
			owner = "administrators"
		default:
			owner = "other"
		}
	}
	control, _, err := descriptor.Control()
	if err != nil {
		return owner, fmt.Sprintf("control-win32-%d", win32Code(err))
	}
	dacl = fmt.Sprintf("present=%t,protected=%t", control&windows.SE_DACL_PRESENT != 0,
		control&windows.SE_DACL_PROTECTED != 0)
	return owner, dacl
}

// Opening a directory with DELETE or FILE_ADD_SUBDIRECTORY is an access check,
// not a mutation. A sharing violation here is evidence of an open handle that
// does not allow the rename; a granted probe does not exclude a child lock.
func accessProbe(path string, access uint32) string {
	wide, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return "invalid-path"
	}
	handle, err := windows.CreateFile(wide, access,
		windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE,
		nil, windows.OPEN_EXISTING,
		windows.FILE_FLAG_BACKUP_SEMANTICS|windows.FILE_FLAG_OPEN_REPARSE_POINT, 0)
	if err != nil {
		return fmt.Sprintf("denied-win32-%d", win32Code(err))
	}
	_ = windows.CloseHandle(handle)
	return "granted"
}

func win32Code(err error) uint32 {
	var errno syscall.Errno
	if errors.As(err, &errno) {
		return uint32(errno)
	}
	return 0
}
