//go:build darwin && cgo

package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestHideDockSettingDefaultsAndSavedValue(t *testing.T) {
	cases := []struct {
		name   string
		body   string
		status int
		want   bool
	}{
		{"missing defaults on", `{"ok":true,"settings":{}}`, 200, true},
		{"saved off", `{"ok":true,"settings":{"hide_dock_icon":false}}`, 200, false},
		{"saved on", `{"ok":true,"settings":{"hide_dock_icon":true}}`, 200, true},
		{"invalid value defaults on", `{"ok":true,"settings":{"hide_dock_icon":"false"}}`, 200, true},
		{"failed request defaults on", `{"ok":false}`, 500, true},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				if request.URL.Path != "/api/settings" {
					t.Errorf("unexpected settings path %s", request.URL.Path)
				}
				writer.WriteHeader(item.status)
				_, _ = io.WriteString(writer, item.body)
			}))
			defer server.Close()
			if got := readHideDockIcon(server.URL); got != item.want {
				t.Fatalf("hide Dock = %t, want %t", got, item.want)
			}
		})
	}
}

// Exercise the actual Cocoa callbacks with fake application/window objects;
// no window or status item is created on the user's desktop.
func TestNativeDockAndQuitBehavior(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	binary := filepath.Join(t.TempDir(), "dock-visibility-test")
	compile := exec.CommandContext(ctx, "clang", "-fblocks", "-framework", "Cocoa", "-framework", "WebKit",
		"testdata/dock_visibility_test.m", "-o", binary)
	if output, err := compile.CombinedOutput(); err != nil {
		t.Fatalf("compile native behavior test: %v\n%s", err, output)
	}
	if output, err := exec.CommandContext(ctx, binary).CombinedOutput(); err != nil {
		t.Fatalf("native behavior test: %v\n%s", err, output)
	}
}
