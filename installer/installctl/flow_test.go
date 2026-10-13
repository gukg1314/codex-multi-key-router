package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestRetryRenameAfterTransientPermissionFailure(t *testing.T) {
	parent := t.TempDir()
	stage := filepath.Join(parent, "stage")
	target := filepath.Join(parent, "installed")
	if err := os.Mkdir(stage, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stage, "CodexMultiKey.exe"), []byte("new"), 0o644); err != nil {
		t.Fatal(err)
	}
	calls := 0
	attempts, err := retryRename(stage, target, func(source, destination string) error {
		calls++
		if calls <= 2 {
			return &os.LinkError{Op: "rename", Old: source, New: destination, Err: os.ErrPermission}
		}
		return os.Rename(source, destination)
	}, func(err error) bool { return errors.Is(err, os.ErrPermission) }, 4, 0)
	if err != nil || attempts != 3 || calls != 3 {
		t.Fatalf("transient rename was not retried: attempts=%d calls=%d error=%v", attempts, calls, err)
	}
	if _, err := os.Stat(filepath.Join(target, "CodexMultiKey.exe")); err != nil {
		t.Fatalf("staged program was not published: %v", err)
	}
}

func TestRetryRenameStopsAfterLimit(t *testing.T) {
	permissionErr := &os.LinkError{Op: "rename", Err: os.ErrPermission}
	calls := 0
	attempts, err := retryRename("stage", "target", func(string, string) error {
		calls++
		return permissionErr
	}, func(err error) bool { return errors.Is(err, os.ErrPermission) }, 3, 0)
	if attempts != 3 || calls != 3 || !errors.Is(err, os.ErrPermission) {
		t.Fatalf("persistent failure was not bounded: attempts=%d calls=%d error=%v", attempts, calls, err)
	}
}

func TestRetryRenameDoesNotRetryOtherErrors(t *testing.T) {
	otherError := errors.New("target already exists")
	calls := 0
	attempts, err := retryRename("stage", "target", func(string, string) error {
		calls++
		return otherError
	}, func(err error) bool { return errors.Is(err, os.ErrPermission) }, 31, 0)
	if attempts != 1 || calls != 1 || !errors.Is(err, otherError) {
		t.Fatalf("non-transient failure was retried: attempts=%d calls=%d error=%v", attempts, calls, err)
	}
}

func TestReplaceTreeRollbackPreservesOldData(t *testing.T) {
	parent := t.TempDir()
	old := filepath.Join(parent, "old")
	target := filepath.Join(parent, "new")
	stage := filepath.Join(parent, "stage")
	for _, path := range []string{filepath.Join(old, "runtime", "app", "runtime"), stage} {
		if err := os.MkdirAll(path, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	secret := filepath.Join(old, "runtime", "app", "runtime", "keys.json")
	if err := os.WriteFile(secret, []byte("secret data"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := copyTree(old, stage); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stage, "new.exe"), []byte("new program"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := replaceTree(old, target, stage, func(string) error { return errors.New("registry write failed") })
	if err == nil {
		t.Fatal("expected commit failure")
	}
	if content, err := os.ReadFile(secret); err != nil || string(content) != "secret data" {
		t.Fatalf("old data not restored: %q, %v", content, err)
	}
	if _, err := os.Stat(target); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("new directory remains after rollback: %v", err)
	}
}

func TestReplaceTreeRollbackSameDirectory(t *testing.T) {
	parent := t.TempDir()
	root := filepath.Join(parent, "CodexMultiKey")
	stage := filepath.Join(parent, "stage")
	if err := os.Mkdir(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(stage, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "keys.json"), []byte("old key"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stage, "CodexMultiKey.exe"), []byte("new"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := replaceTree(root, root, stage, func(string) error { return errors.New("shortcut update failed") })
	if err == nil {
		t.Fatal("expected commit failure")
	}
	if content, err := os.ReadFile(filepath.Join(root, "keys.json")); err != nil || string(content) != "old key" {
		t.Fatalf("same-directory rollback lost old data: %q, %v", content, err)
	}
	if _, err := os.Stat(filepath.Join(root, "CodexMultiKey.exe")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("new program remains after rollback: %v", err)
	}
}

func TestReplaceTreePublishFailureRestoresOldDirectory(t *testing.T) {
	parent := t.TempDir()
	old := filepath.Join(parent, "old")
	stage := filepath.Join(parent, "stage")
	target := filepath.Join(parent, "occupied")
	for _, path := range []string{old, stage} {
		if err := os.Mkdir(path, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(old, "keys.json"), []byte("old key"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, []byte("other file"), 0o600); err != nil {
		t.Fatal(err)
	}
	committed := false
	_, err := replaceTree(old, target, stage, func(string) error {
		committed = true
		return nil
	})
	if err == nil || committed {
		t.Fatalf("publish failure must not commit: error=%v committed=%t", err, committed)
	}
	if content, err := os.ReadFile(filepath.Join(old, "keys.json")); err != nil || string(content) != "old key" {
		t.Fatalf("old install was not restored: %q, %v", content, err)
	}
	if content, err := os.ReadFile(target); err != nil || string(content) != "other file" {
		t.Fatalf("occupied target was changed: %q, %v", content, err)
	}
	if _, err := os.Stat(stage); err != nil {
		t.Fatalf("staged tree should remain for caller cleanup: %v", err)
	}
}

func TestInstallManifestExcludesRunningUninstaller(t *testing.T) {
	parent := t.TempDir()
	source := filepath.Join(parent, "payload")
	stage := filepath.Join(parent, "stage")
	if err := os.MkdirAll(filepath.Join(source, "runtime", "app"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(stage, 0o755); err != nil {
		t.Fatal(err)
	}
	for path, content := range map[string]string{
		filepath.Join(source, "uninstall.exe"):              "uninstaller",
		filepath.Join(source, "CodexMultiKey.exe"):          "launcher",
		filepath.Join(source, "runtime", "app", "admin.py"): "admin",
	} {
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := writeInstallRecords(stage, source, filepath.Join(parent, "installed"), "1.2.12"); err != nil {
		t.Fatal(err)
	}
	content, err := os.ReadFile(filepath.Join(stage, manifestFile))
	if err != nil {
		t.Fatal(err)
	}
	var record manifest
	if err := json.Unmarshal(content, &record); err != nil {
		t.Fatal(err)
	}
	if _, found := record.Files["uninstall.exe"]; found {
		t.Fatal("uninstaller must be handled separately from installed program files")
	}
	if _, found := record.Files["runtime/app/admin.py"]; !found {
		t.Fatal("installed program file missing from manifest")
	}
}

func TestPathWithinUsesDirectoryBoundary(t *testing.T) {
	root := filepath.Join("root", "CodexMultiKey")
	if !pathWithin(filepath.Join(root, "runtime", "app"), root) {
		t.Fatal("child path was not recognized")
	}
	if pathWithin(root+"2", root) {
		t.Fatal("sibling with the same prefix must not be treated as owned")
	}
}

func TestManifestCannotNameUserDataOrUninstaller(t *testing.T) {
	root := filepath.Join(t.TempDir(), "CodexMultiKey")
	for _, name := range []string{"runtime/app/runtime/keys.json", "uninstall.exe", "../other.txt"} {
		if _, err := safeManifestPath(root, name); err == nil {
			t.Fatalf("manifest path must be rejected: %s", name)
		}
	}
}

func TestManifestDigestAndUnknownCollision(t *testing.T) {
	parent := t.TempDir()
	old := filepath.Join(parent, "old")
	source := filepath.Join(parent, "payload")
	if err := os.MkdirAll(filepath.Join(old, "runtime", "app"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(source, "runtime", "app"), 0o755); err != nil {
		t.Fatal(err)
	}
	name := filepath.Join("runtime", "app", "new-module.py")
	if err := os.WriteFile(filepath.Join(old, name), []byte("user file"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(source, name), []byte("new program"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := checkPayloadCollisions(old, source); err == nil {
		t.Fatal("legacy install unknown user file would be overwritten")
	}
	if err := writeInstallRecords(old, source, old, "1.2.12"); err != nil {
		t.Fatal(err)
	}
	if err := validateMarker(old, true); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(old, manifestFile), []byte("tampered"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := validateMarker(old, true); err == nil {
		t.Fatal("tampered manifest should not be trusted")
	}
}

func TestResidualMarkerMatchesOnlyItsOriginalDirectory(t *testing.T) {
	root := t.TempDir()
	content, err := json.Marshal(marker{Product: productID, Location: root})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, residualFile), content, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := validateResidual(root); err != nil {
		t.Fatal(err)
	}
	other := filepath.Join(root, "other")
	if err := os.Mkdir(other, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(other, residualFile), content, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := validateResidual(other); err == nil {
		t.Fatal("residual marker must not authorize another directory")
	}
}
