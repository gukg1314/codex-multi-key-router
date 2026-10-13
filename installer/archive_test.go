package main

import (
	"archive/zip"
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func buildZip(t *testing.T, entries map[string]string) []byte {
	t.Helper()
	buffer := &bytes.Buffer{}
	writer := zip.NewWriter(buffer)
	for name, body := range entries {
		file, err := writer.Create(name)
		if err != nil {
			t.Fatalf("create entry: %v", err)
		}
		if _, err := file.Write([]byte(body)); err != nil {
			t.Fatalf("write entry: %v", err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close zip: %v", err)
	}
	return buffer.Bytes()
}

func TestExtractPayloadWritesFiles(t *testing.T) {
	data := buildZip(t, map[string]string{"runtime/app/admin.py": "# admin\n", "CodexMultiKey.exe": "MZ"})
	reader, err := readPayload(data)
	if err != nil {
		t.Fatalf("read payload: %v", err)
	}
	destination := t.TempDir()
	if err := extractPayload(reader, destination); err != nil {
		t.Fatalf("extract payload: %v", err)
	}
	body, err := os.ReadFile(filepath.Join(destination, "runtime", "app", "admin.py"))
	if err != nil {
		t.Fatalf("read extracted file: %v", err)
	}
	if string(body) != "# admin\n" {
		t.Fatalf("unexpected body %q", body)
	}
}

func TestExtractPayloadRejectsEscapingEntry(t *testing.T) {
	data := buildZip(t, map[string]string{"../escape.txt": "nope"})
	reader, err := readPayload(data)
	if err != nil {
		t.Fatalf("read payload: %v", err)
	}
	if err := extractPayload(reader, t.TempDir()); err == nil {
		t.Fatal("expected an error for an escaping entry")
	}
}

func TestVerifyPayloadReportsMissingFiles(t *testing.T) {
	data := buildZip(t, map[string]string{"CodexMultiKey.exe": "MZ"})
	reader, err := readPayload(data)
	if err != nil {
		t.Fatalf("read payload: %v", err)
	}
	err = verifyPayload(reader)
	if err == nil {
		t.Fatal("expected missing files to be reported")
	}
	if !bytes.Contains([]byte(err.Error()), []byte("runtime/app/admin.py")) {
		t.Fatalf("unexpected error %v", err)
	}
}
