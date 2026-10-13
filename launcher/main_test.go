package main

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestDataDirFollowsThePythonRuntimeLayout(t *testing.T) {
	home := "/Users/example"
	macPackage := filepath.Join("/Applications", "CodexMultiKey.app", "Contents", "Resources", "runtime", "app")
	macWant := filepath.Join(home, "Library", "Application Support", runtimeName)
	if got := dataDir("darwin", macPackage, home); got != macWant {
		t.Fatalf("darwin data dir = %q, want %q", got, macWant)
	}

	// toolctl.paths() puts the data directory at package_dir/runtime on Windows,
	// and the launcher hands the Python side the packaged runtime/app folder.
	winPackage := filepath.Join("C:\\Users\\example\\AppData\\Local\\CodexMultiKey", "runtime", "app")
	winWant := filepath.Join(winPackage, "runtime")
	if got := dataDir("windows", winPackage, `C:\Users\example`); got != winWant {
		t.Fatalf("windows data dir = %q, want the python runtime %q", got, winWant)
	}
}

// The launcher must never keep data inside the read only macOS bundle.
func TestDataDirFallsBackBesideTheAppWithoutAHomeDirectory(t *testing.T) {
	packageDir := filepath.Join("/Applications", "CodexMultiKey.app", "Contents", "Resources", "runtime", "app")
	if got := dataDir("darwin", packageDir, ""); got != filepath.Join(packageDir, "runtime") {
		t.Fatalf("darwin data dir without home = %q, want the package data dir", got)
	}
}

// ----------------------------------------------------------------- instance

// A running admin may only be reused when it reports this exact release: an
// older service answers the new UI with its own, smaller API.
func TestAdminHealthReadsTheReleaseFromHealthz(t *testing.T) {
	cases := []struct {
		name      string
		body      string
		status    int
		wantOK    bool
		wantApp   string
		wantBuild string
	}{
		{"current release", "{\"ok\":true,\"version\":\"6\",\"app_version\":\"" + appVersion + "\",\"build\":\"abc123abc123\",\"service\":\"admin\"}", http.StatusOK, true, appVersion, "abc123abc123"},
		{"older release without the fields", "{\"ok\":true,\"version\":\"6\",\"service\":\"admin\"}", http.StatusOK, true, "", ""},
		{"foreign body", "<html>not our service</html>", http.StatusOK, true, "", ""},
		{"error status", "{}", http.StatusInternalServerError, false, "", ""},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
				if request.URL.Path != "/healthz" {
					writer.WriteHeader(http.StatusNotFound)
					return
				}
				writer.WriteHeader(item.status)
				_, _ = io.WriteString(writer, item.body)
			}))
			defer server.Close()
			ok, release, build := adminHealth(server.URL, 2*time.Second)
			if ok != item.wantOK || release != item.wantApp || build != item.wantBuild {
				t.Fatalf("adminHealth = (%v, %q, %q), want (%v, %q, %q)", ok, release, build, item.wantOK, item.wantApp, item.wantBuild)
			}
		})
	}

	// Nothing listening at all is the normal "start the service" path.
	if ok, release, build := adminHealth("http://127.0.0.1:1", 500*time.Millisecond); ok || release != "" || build != "" {
		t.Fatalf("a dead port reported (%v, %q, %q), want a clean miss", ok, release, build)
	}
}

// A same-version hot fix must still replace the running service, so the launcher
// reads the shipped admin.py and compares it with what /healthz reports.
func TestAdminBuildFingerprintFollowsTheShippedAdminScript(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "admin.py")
	if err := os.WriteFile(path, []byte("print('one')\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	one := adminBuildFingerprint(dir)
	if len(one) != 12 {
		t.Fatalf("fingerprint = %q, want 12 hex characters", one)
	}
	if err := os.WriteFile(path, []byte("print('two')\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if two := adminBuildFingerprint(dir); two == one {
		t.Fatalf("a rebuilt admin.py kept fingerprint %q", one)
	}
	if missing := adminBuildFingerprint(t.TempDir()); missing != "" {
		t.Fatalf("missing admin.py fingerprint = %q, want empty", missing)
	}
}

func TestAdminReusableRequiresTheSameReleaseAndBuild(t *testing.T) {
	cases := []struct {
		name     string
		release  string
		build    string
		expected string
		want     bool
	}{
		{"same release and build", appVersion, "abc123abc123", "abc123abc123", true},
		{"same version rebuilt", appVersion, "old000old000", "abc123abc123", false},
		{"older release without a build", "1.1.1", "", "abc123abc123", false},
		{"unreadable local copy falls back to the release", appVersion, "abc123abc123", "", true},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			if got := adminReusable(item.release, item.build, item.expected); got != item.want {
				t.Fatalf("adminReusable(%q, %q, %q) = %v, want %v", item.release, item.build, item.expected, got, item.want)
			}
		})
	}
}

func TestInstancePortFollowsTheAdminPort(t *testing.T) {
	cases := []struct {
		url  string
		want int
	}{
		{"http://127.0.0.1:8791", 8792},
		{"http://127.0.0.1:9000/", 9001},
		{"http://127.0.0.1:65535", 0},
		{"http://127.0.0.1", 0},
		{"not a url", 0},
	}
	for _, item := range cases {
		if got := instancePort(item.url); got != item.want {
			t.Fatalf("instancePort(%q) = %d, want %d", item.url, got, item.want)
		}
	}
}

func TestWakeRunningInstanceNeedsTheLauncherToken(t *testing.T) {
	token := make(chan string, 1)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/show" && request.URL.Path != "/ping" {
			writer.WriteHeader(http.StatusNotFound)
			return
		}
		token <- request.URL.Path
		writer.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(writer, "{\"ok\":true,\"app\":\""+instanceToken+"\"}")
	}))
	defer server.Close()
	port := serverPort(t, server.URL)

	if !wakeRunningInstance(port, true) {
		t.Fatal("a launcher answering with the token must be treated as running")
	}
	if got := <-token; got != "/show" {
		t.Fatalf("instance channel path = %q, want /show", got)
	}

	// A silent start (login autostart) must never pull the running window to
	// the front; it only checks that the launcher already owns the channel.
	if !wakeRunningInstance(port, false) {
		t.Fatal("a launcher answering with the token must be treated as running")
	}
	if got := <-token; got != "/ping" {
		t.Fatalf("silent instance probe path = %q, want /ping", got)
	}

	// An unrelated program on the same port must not swallow a fresh launch.
	other := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		_, _ = io.WriteString(writer, "hello from another program")
	}))
	defer other.Close()
	if wakeRunningInstance(serverPort(t, other.URL), true) {
		t.Fatal("a foreign service on the port must not be treated as a running launcher")
	}
}

func TestStartInstanceChannelSurvivesABusyPort(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("cannot reserve a port: %v", err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	stop, owned := startInstanceChannel(port, func() {})
	if owned {
		t.Fatal("a busy port must not be reported as owned")
	}
	stop()
	if err := listener.Close(); err != nil {
		t.Fatalf("cannot free the port: %v", err)
	}

	shown := make(chan struct{}, 1)
	stop2, owned2 := startInstanceChannel(port, func() { shown <- struct{}{} })
	if !owned2 {
		t.Fatal("the freed port must be owned by the launcher")
	}
	defer stop2()
	target := fmt.Sprintf("http://127.0.0.1:%d/show", port)
	response, err := http.Get(target)
	if err != nil {
		t.Fatalf("instance channel did not answer: %v", err)
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(response.Body)
	if !strings.Contains(string(body), instanceToken) {
		t.Fatalf("instance channel body = %q, want the launcher token", string(body))
	}
	select {
	case <-shown:
	case <-time.After(2 * time.Second):
		t.Fatal("instance channel did not ask the window to show itself")
	}
}

func serverPort(t *testing.T, rawURL string) int {
	t.Helper()
	address := strings.TrimPrefix(rawURL, "http://")
	_, rawPort, err := net.SplitHostPort(address)
	if err != nil {
		t.Fatalf("cannot read the port from %q: %v", rawURL, err)
	}
	port, err := strconv.Atoi(rawPort)
	if err != nil {
		t.Fatalf("cannot read the port from %q: %v", rawURL, err)
	}
	return port
}

// ----------------------------------------------------------------- close flow

type stubPoster struct {
	mutex  sync.Mutex
	paths  []string
	err    error
	delay  time.Duration
	called chan string
}

func (s *stubPoster) post(path string) error {
	s.mutex.Lock()
	s.paths = append(s.paths, path)
	s.mutex.Unlock()
	if s.called != nil {
		s.called <- path
	}
	if s.delay > 0 {
		time.Sleep(s.delay)
	}
	return s.err
}

func (s *stubPoster) recorded() []string {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	return append([]string{}, s.paths...)
}

// Quitting the tool ends the router and the background admin service, but
// leaves Codex on the local route. The old flow posted /api/router/direct
// first, which is why a reboot came back on the official models.
func TestShutdownServiceQuitsAndKeepsTheLocalRoute(t *testing.T) {
	stub := &stubPoster{}
	shutdownService(stub)
	want := "/api/service/quit"
	got := stub.recorded()
	if len(got) != 1 || got[0] != want {
		t.Fatalf("close flow called %v, want [%s]", got, want)
	}
}

func TestShutdownServiceStillLeavesTheUIWhenQuitFails(t *testing.T) {
	stub := &stubPoster{err: fmt.Errorf("quit refused")}
	shutdownService(stub)
	if got := stub.recorded(); len(got) != 1 {
		t.Fatalf("close flow calls = %v, want the single quit step", got)
	}
}

func TestQuitAppRunsTheCloseFlowOnceAndLeavesTheUI(t *testing.T) {
	// quitApp is guarded by a sync.Once, so the two calls below must share it:
	// this is the only test that runs the close flow.
	previousAPI := platformAPI
	previousFinished := quitFinished
	defer func() {
		platformAPI = previousAPI
		quitFinished = previousFinished
	}()

	stub := &stubPoster{}
	finished := make(chan struct{}, 4)
	platformAPI = stub
	quitFinished = func() { finished <- struct{}{} }

	quitApp()
	quitApp()

	if got := len(finished); got != 1 {
		t.Fatalf("teardown ran %d times, want exactly one", got)
	}
	if got := stub.recorded(); len(got) != 1 {
		t.Fatalf("close flow calls = %v, want the single quit step", got)
	}
}

// ----------------------------------------------------------------- icons

func TestIcoFrameReadsTheEmbeddedTrayIcon(t *testing.T) {
	if len(trayIconBytes) == 0 {
		t.Fatal("the tray icon data was not loaded")
	}
	offset, size, ok := icoFrame(trayIconBytes, 2)
	if !ok {
		t.Fatal("the tray icon directory could not be read")
	}
	if offset+size > len(trayIconBytes) || size <= 0 {
		t.Fatalf("frame out of range offset=%d size=%d total=%d", offset, size, len(trayIconBytes))
	}
	// An index past the end must clamp instead of failing.
	last, lastSize, ok := icoFrame(trayIconBytes, 99)
	if !ok || last != offset || lastSize != size {
		t.Fatalf("frame clamping = (%d,%d,%v), want (%d,%d,true)", last, lastSize, ok, offset, size)
	}
	if _, _, ok := icoFrame([]byte{1, 2, 3}, 0); ok {
		t.Fatal("a truncated ico file must not report a frame")
	}
}
