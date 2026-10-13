// Package main holds the Windows setup and uninstall helper. The zip helpers
// below stay platform neutral so they can be unit tested on the build machine.
package main

import (
	"archive/zip"
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
)

var errUnsafePath = errors.New("payload entry escapes the install directory")

// readPayload opens the embedded payload archive.
func readPayload(data []byte) (*zip.Reader, error) {
	if len(data) == 0 {
		return nil, errors.New("payload is empty; rebuild with the installer payload tag")
	}
	return zip.NewReader(bytes.NewReader(data), int64(len(data)))
}

// extractPayload writes every entry below destination, rejecting any path that
// tries to escape it.
func extractPayload(reader *zip.Reader, destination string) error {
	root, err := filepath.Abs(destination)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		return err
	}
	for _, entry := range reader.File {
		target, err := safeJoin(root, entry.Name)
		if err != nil {
			return err
		}
		if entry.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		if err := writeEntry(entry, target); err != nil {
			return err
		}
	}
	return nil
}

func safeJoin(root, name string) (string, error) {
	cleaned := filepath.Clean(filepath.Join(root, filepath.FromSlash(name)))
	if cleaned != root && !strings.HasPrefix(cleaned, root+string(os.PathSeparator)) {
		return "", errUnsafePath
	}
	return cleaned, nil
}

func writeEntry(entry *zip.File, target string) error {
	source, err := entry.Open()
	if err != nil {
		return err
	}
	defer source.Close()
	destination, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	if _, err := io.Copy(destination, source); err != nil {
		destination.Close()
		return err
	}
	return destination.Close()
}

// verifyPayload makes sure the archive really contains a runnable app.
func verifyPayload(reader *zip.Reader) error {
	required := []string{
		"CodexMultiKey.exe",
		"uninstall.exe",
		"runtime/app/admin.py",
		"runtime/app/autostart.py",
		"runtime/app/keys_store.py",
		"runtime/app/local-models.json",
		"runtime/app/router.py",
		"runtime/app/toolctl.py",
		"runtime/app/zstd.exe",
		"runtime/ui/index.html",
		"runtime/python/python.exe",
		"runtime/python/pythonw.exe",
		"runtime/python/python312.zip",
		"runtime/python/python312._pth",
	}
	present := map[string]bool{}
	for _, entry := range reader.File {
		present[entry.Name] = true
	}
	var missing []string
	for _, name := range required {
		if !present[name] {
			missing = append(missing, name)
		}
	}
	if len(missing) > 0 {
		return errors.New("payload is incomplete: " + strings.Join(missing, ", "))
	}
	return nil
}
