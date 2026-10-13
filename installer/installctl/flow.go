package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	productID    = "CodexMultiKey"
	launcherFile = "CodexMultiKey.exe"
	markerFile   = ".codexmk-install.json"
	manifestFile = ".codexmk-files.json"
	residualFile = ".codexmk-uninstalled.json"
)

type marker struct {
	Product        string `json:"product"`
	Location       string `json:"location"`
	Version        string `json:"version"`
	ManifestSHA256 string `json:"manifest_sha256,omitempty"`
}

type manifest struct {
	Product string            `json:"product"`
	Files   map[string]string `json:"files"`
}

func samePath(a, b string) bool {
	return strings.EqualFold(filepath.Clean(a), filepath.Clean(b))
}

func pathWithin(path, root string) bool {
	path, root = filepath.Clean(path), filepath.Clean(root)
	return strings.EqualFold(path, root) || strings.HasPrefix(strings.ToLower(path), strings.ToLower(root+string(os.PathSeparator)))
}

func randomSibling(path, label string) (string, error) {
	var bytes [8]byte
	if _, err := rand.Read(bytes[:]); err != nil {
		return "", err
	}
	return filepath.Join(filepath.Dir(path), "."+filepath.Base(path)+"."+label+"-"+hex.EncodeToString(bytes[:])), nil
}

func copyTree(source, destination string) error {
	return filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("reparse path in install tree: %s", path)
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		target := filepath.Join(destination, relative)
		if entry.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		input, err := os.Open(path)
		if err != nil {
			return err
		}
		output, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
		if err != nil {
			input.Close()
			return err
		}
		_, copyErr := io.Copy(output, input)
		closeErr := output.Close()
		inputErr := input.Close()
		if copyErr != nil {
			return copyErr
		}
		if closeErr != nil {
			return closeErr
		}
		return inputErr
	})
}

func writeInstallRecords(stage, source, target, version string) error {
	files := make(map[string]string)
	err := filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("reparse path in payload: %s", path)
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		if strings.EqualFold(relative, "uninstall.exe") {
			// The helper backs up and removes the uninstaller separately.
			return nil
		}
		hash, err := hashFile(path)
		if err != nil {
			return err
		}
		files[filepath.ToSlash(relative)] = hash
		return nil
	})
	if err != nil {
		return err
	}
	manifestData, err := json.MarshalIndent(manifest{Product: productID, Files: files}, "", "  ")
	if err != nil {
		return err
	}
	manifestHash := sha256.Sum256(manifestData)
	markerData, err := json.MarshalIndent(marker{
		Product: productID, Location: target, Version: version,
		ManifestSHA256: hex.EncodeToString(manifestHash[:]),
	}, "", "  ")
	if err != nil {
		return err
	}
	for name, data := range map[string][]byte{manifestFile: manifestData, markerFile: markerData} {
		if err := os.WriteFile(filepath.Join(stage, name), data, 0o644); err != nil {
			return err
		}
	}
	return nil
}

func validateResidual(root string) error {
	content, err := os.ReadFile(filepath.Join(root, residualFile))
	if err != nil {
		return err
	}
	var stored marker
	if err := json.Unmarshal(content, &stored); err != nil {
		return err
	}
	if stored.Product != productID || !samePath(stored.Location, root) {
		return fmt.Errorf("uninstalled directory marker does not match %s", root)
	}
	return nil
}

// preserveCollision keeps a residual user file when the new payload needs the
// same path. The preserved copy remains inside the installed directory.
func preserveCollision(root, path, preserved string) error {
	relative, err := filepath.Rel(root, path)
	if err != nil || relative == "." || strings.HasPrefix(relative, "..") {
		return fmt.Errorf("cannot preserve path outside install: %s", path)
	}
	target := filepath.Join(preserved, relative)
	if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
		return err
	}
	return os.Rename(path, target)
}

func legacyProgramPath(relative string) bool {
	relative = filepath.ToSlash(relative)
	if strings.HasPrefix(relative, "runtime/python/") {
		return true
	}
	switch relative {
	case "CodexMultiKey.exe", "uninstall.exe", "\u4f7f\u7528\u8bf4\u660e.txt",
		"runtime/app/admin.py", "runtime/app/autostart.py", "runtime/app/keys_store.py",
		"runtime/app/local-models.json", "runtime/app/router.py", "runtime/app/toolctl.py",
		"runtime/app/zstd.exe", "runtime/ui/index.html", "runtime/ui/logo.png":
		return true
	}
	return false
}

// checkPayloadCollisions rejects an unknown old file at a path the new build
// must occupy. A legacy Go install has no manifest, so its fixed shipped layout
// is the only allowed collision set.
func checkPayloadCollisions(old, source string) error {
	var known map[string]string
	if _, err := os.Stat(filepath.Join(old, markerFile)); err == nil {
		content, err := os.ReadFile(filepath.Join(old, manifestFile))
		if err != nil {
			return err
		}
		var record manifest
		if err := json.Unmarshal(content, &record); err != nil || record.Product != productID {
			return errors.New("old install manifest is invalid")
		}
		known = record.Files
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil || entry.IsDir() {
			return walkErr
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		if _, err := os.Stat(filepath.Join(old, relative)); errors.Is(err, os.ErrNotExist) {
			return nil
		} else if err != nil {
			return err
		}
		name := filepath.ToSlash(relative)
		if known != nil {
			if name == "uninstall.exe" {
				return nil
			}
			if _, owned := known[name]; owned {
				return nil
			}
		} else if legacyProgramPath(name) {
			return nil
		}
		return fmt.Errorf("old install contains an unknown file at new program path: %s", filepath.Join(old, relative))
	})
}

func hashFile(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func retryRename(source, target string, rename func(string, string) error, retryable func(error) bool, maxAttempts int, delay time.Duration) (int, error) {
	for attempt := 1; ; attempt++ {
		err := rename(source, target)
		if err == nil || attempt >= maxAttempts || !retryable(err) {
			return attempt, err
		}
		time.Sleep(delay)
	}
}

// replaceTree moves the old installation aside before publishing the prepared
// tree. A failed registry/shortcut commit restores the old directory in place.
func replaceTree(old, target, stage string, commit func(backup string) error) (string, error) {
	backup := ""
	if old != "" {
		var err error
		backup, err = randomSibling(old, "backup")
		if err != nil {
			return "", err
		}
		if err := os.Rename(old, backup); err != nil {
			renameDiagnostic("backup-old", old, backup, "", err)
			return "", fmt.Errorf("move old install to backup: %w", err)
		}
	}
	if err := publishStage(stage, target); err != nil {
		renameDiagnostic("publish", stage, target, backup, err)
		if backup != "" {
			if restoreErr := os.Rename(backup, old); restoreErr != nil {
				renameDiagnostic("restore-old-after-publish", backup, old, stage, restoreErr)
				return backup, fmt.Errorf("publish failed: %v; old install remains at %s: %w", err, backup, restoreErr)
			}
		}
		return "", fmt.Errorf("publish new install: %w", err)
	}
	if err := commit(backup); err != nil {
		failed, nameErr := randomSibling(target, "failed")
		if nameErr != nil {
			return backup, fmt.Errorf("commit failed: %v; cannot reserve rollback path: %w", err, nameErr)
		}
		if moveErr := os.Rename(target, failed); moveErr != nil {
			renameDiagnostic("move-failed-install", target, failed, backup, moveErr)
			return backup, fmt.Errorf("commit failed: %v; new install remains at %s; old backup at %s: %w", err, target, backup, moveErr)
		}
		if backup != "" {
			if restoreErr := os.Rename(backup, old); restoreErr != nil {
				renameDiagnostic("restore-old-after-commit", backup, old, failed, restoreErr)
				return backup, fmt.Errorf("commit failed: %v; old backup remains at %s; new tree at %s: %w", err, backup, failed, restoreErr)
			}
		}
		_ = os.RemoveAll(failed)
		return "", err
	}
	return backup, nil
}

func validateMarker(root string, required bool) error {
	content, err := os.ReadFile(filepath.Join(root, markerFile))
	if errors.Is(err, os.ErrNotExist) && !required {
		return nil
	}
	if err != nil {
		return err
	}
	var stored marker
	if err := json.Unmarshal(content, &stored); err != nil {
		return err
	}
	if stored.Product != productID || !samePath(stored.Location, root) {
		return fmt.Errorf("install marker does not match %s", root)
	}
	manifestData, err := os.ReadFile(filepath.Join(root, manifestFile))
	if err != nil {
		return err
	}
	digest := sha256.Sum256(manifestData)
	if stored.ManifestSHA256 != hex.EncodeToString(digest[:]) {
		return errors.New("installed file manifest digest does not match marker")
	}
	return nil
}

func safeManifestPath(root, relative string) (string, error) {
	if relative == "" || filepath.IsAbs(relative) || filepath.VolumeName(filepath.FromSlash(relative)) != "" || strings.Contains(relative, "\\") {
		return "", fmt.Errorf("invalid manifest path %q", relative)
	}
	clean := filepath.Clean(filepath.FromSlash(relative))
	if clean == "." || clean == ".." || strings.HasPrefix(clean, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("invalid manifest path %q", relative)
	}
	result := filepath.Join(root, clean)
	if !pathWithin(result, root) || samePath(result, root) {
		return "", fmt.Errorf("manifest path outside install: %q", relative)
	}
	if pathWithin(result, filepath.Join(root, "runtime", "app", "runtime")) ||
		samePath(result, filepath.Join(root, markerFile)) ||
		samePath(result, filepath.Join(root, manifestFile)) ||
		samePath(result, filepath.Join(root, residualFile)) ||
		samePath(result, filepath.Join(root, "uninstall.exe")) {
		return "", fmt.Errorf("manifest path is reserved: %q", relative)
	}
	return result, nil
}
