// Command launcher opens the Codex multi key router desktop window.
//
// One shared main flow serves both platforms: locate the bundled runtime, make
// sure the local admin service answers on 127.0.0.1, wake an already running
// window or show a new one, and keep working in the menu bar / tray after the
// window is closed. "Quit" stops the router but leaves Codex on the local
// route; the login autostart brings the same route back after a reboot.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// appVersion must match admin.py APP_VERSION: the launcher only reuses an
	// admin service that reports this exact version.
	appVersion   = "1.2.26"
	defaultURL   = "http://127.0.0.1:8791"
	windowTitle  = "Codex \u8def\u7531"
	windowWidth  = 1180
	windowHeight = 820
	// runtimeName matches toolctl.RUNTIME_NAME; both sides must agree on the
	// macOS Application Support folder that holds keys, models and logs.
	runtimeName = "CodexOfficialLocalRouter"
	// instanceOffset moves the single instance channel one port above the admin
	// UI, so a second launch can wake the running window instead of opening a
	// second copy of the tool.
	instanceOffset = 1
	// instanceToken is echoed by the channel so an unrelated program that happens
	// to own the port can never stop a fresh launch. The version suffix keeps a
	// window from an older release from swallowing the new one, which would leave
	// the user in front of the old UI.
	instanceToken = "codexmk-window-" + appVersion
	// Menu bar and tray labels are shared by both platforms. Unicode escapes
	// keep every Go source file ASCII, so a Windows build can never trip over
	// an encoding the toolchain guesses wrong.
	menuOpenLabel = "\u6253\u5f00\u4e3b\u754c\u9762"
	menuQuitLabel = "\u9000\u51fa"
)

// config holds every path the launcher needs to start the Python service.
type config struct {
	python     string
	packageDir string
	runtimeDir string
	logsDir    string
	homeDir    string
	logPath    string
	launcher   string
}

var logger = log.New(os.Stderr, "", log.LstdFlags)

var currentLogPath string

// platformAPI lets the menu bar and tray callbacks run the same close flow.
var platformAPI apiPoster

func main() {
	// AppKit on macOS only accepts a window on the process main thread, and the
	// Go scheduler may move this goroutine off it while talking to the admin
	// service. Pinning it here keeps both platforms on one shared flow.
	runtime.LockOSThread()
	// --silent is what the login autostart uses: the tool starts with its menu
	// bar / tray icon but keeps the main window in the background.
	silent := hasArg("--silent")
	cfg, cfgErr := resolveConfig()
	setupLogging(cfg, cfgErr)
	if cfgErr != nil {
		fatal(cfgErr)
		return
	}
	logger.Printf("runtime package=%s logs=%s python=%s", cfg.packageDir, cfg.logsDir, cfg.python)

	baseURL := adminBaseURL(cfg)
	if err := ensureAdmin(cfg, baseURL); err != nil {
		fatal(fmt.Errorf("admin service did not start: %w", err))
		return
	}
	logger.Printf("admin ready url=%s", baseURL)
	platformAPI = adminClient{baseURL: baseURL, client: &http.Client{Timeout: 5 * time.Second}}
	repairAutostart(platformAPI)

	channelPort := instancePort(baseURL)
	if wakeRunningInstance(channelPort, !silent) {
		if silent {
			logger.Printf("another window is running; silent start leaves it alone")
		} else {
			logger.Printf("another window is running; asked it to show itself")
		}
		return
	}
	stopChannel, channelOwned := startInstanceChannel(channelPort, showWindow)
	if silent && !channelOwned {
		// Another copy already owns the channel: a login start must never open
		// a second window or second tray icon.
		logger.Printf("silent start found another instance port=%d; leaving it alone", channelPort)
		return
	}
	defer stopChannel()

	if err := openWindow(baseURL, windowTitle, silent); err != nil {
		logger.Printf("native window unavailable error=%v", err)
		if browserErr := openInBrowser(baseURL); browserErr != nil {
			fatal(fmt.Errorf("window failed: %v; browser failed: %v", err, browserErr))
			return
		}
	}
	logger.Printf("launcher exit")
}

// hasArg reads a bare launcher flag without the flag package: an unknown
// argument must never abort a desktop app that only has a message box to
// report the failure with.
func hasArg(want string) bool {
	for _, arg := range os.Args[1:] {
		if strings.TrimSpace(arg) == want {
			return true
		}
	}
	return false
}

// resolveConfig finds the bundled runtime (or, in development, the checkout).
func resolveConfig() (*config, error) {
	exe, err := os.Executable()
	if err != nil {
		return nil, fmt.Errorf("cannot locate executable: %w", err)
	}
	exeDir := filepath.Dir(exe)
	workDir, _ := os.Getwd()
	home, _ := os.UserHomeDir()
	if override := strings.TrimSpace(os.Getenv("CODEXMK_HOME")); override != "" {
		home = override
	}

	var candidates []string
	if root := strings.TrimSpace(os.Getenv("CODEXMK_ROOT")); root != "" {
		candidates = append(candidates, filepath.Join(root, "app"))
	}
	candidates = append(candidates,
		filepath.Join(exeDir, "runtime", "app"),
		filepath.Join(exeDir, "..", "Resources", "runtime", "app"),
		filepath.Join(exeDir, "..", "..", "Resources", "runtime", "app"),
		filepath.Join(exeDir, "app"),
		filepath.Join(workDir, "app"),
	)
	for _, dir := range candidates {
		abs, absErr := filepath.Abs(dir)
		if absErr != nil {
			continue
		}
		if !fileExists(filepath.Join(abs, "admin.py")) {
			continue
		}
		runtimeDir := filepath.Dir(abs)
		python := findPython(runtimeDir)
		if python == "" {
			return nil, errors.New("no Python runtime found; reinstall the app")
		}
		return &config{
			python:     python,
			packageDir: abs,
			runtimeDir: runtimeDir,
			logsDir:    filepath.Join(dataDir(runtime.GOOS, abs, home), "logs"),
			homeDir:    home,
			launcher:   exe,
		}, nil
	}
	return nil, errors.New("app files not found; reinstall Codex multi key router")
}

// dataDir mirrors toolctl.paths so both sides write logs to the same place.
// Windows keeps every writable file inside the installed package, exactly like
// toolctl.paths; macOS keeps them under Application Support because the app
// bundle itself is read only.
func dataDir(goos, packageDir, home string) string {
	if goos == "darwin" && home != "" {
		return filepath.Join(home, "Library", "Application Support", runtimeName)
	}
	return filepath.Join(packageDir, "runtime")
}

func findPython(runtimeDir string) string {
	bundled := []string{
		filepath.Join(runtimeDir, "python", "bin", "python3"),
		filepath.Join(runtimeDir, "python", "python.exe"),
		filepath.Join(runtimeDir, "python", "python3.exe"),
		filepath.Join(runtimeDir, "python", "python3"),
	}
	for _, candidate := range bundled {
		if fileExists(candidate) {
			return candidate
		}
	}
	for _, name := range []string{"python3", "python"} {
		if found, err := exec.LookPath(name); err == nil {
			return found
		}
	}
	return ""
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func setupLogging(cfg *config, cfgErr error) {
	writers := []io.Writer{os.Stderr}
	var targets []string
	if cfg != nil && cfg.logsDir != "" {
		targets = append(targets, filepath.Join(cfg.logsDir, "launcher.log"))
	}
	if dir, err := os.UserCacheDir(); err == nil {
		targets = append(targets, filepath.Join(dir, "CodexMultiKey", "launcher.log"))
	}
	for _, target := range targets {
		file := openLogFile(target)
		if file == nil {
			continue
		}
		writers = append(writers, file)
		currentLogPath = target
		if cfg != nil {
			cfg.logPath = target
		}
		break
	}
	logger = log.New(io.MultiWriter(writers...), "", log.LstdFlags)
	logger.Printf("launcher start version=%s os=%s arch=%s", appVersion, runtime.GOOS, runtime.GOARCH)
	if cfgErr != nil {
		logger.Printf("config error=%v", cfgErr)
	}
}

func openLogFile(path string) *os.File {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil
	}
	return file
}

func (c *config) adminArgs(extra ...string) []string {
	args := []string{"-X", "utf8", filepath.Join(c.packageDir, "admin.py"), "--package", c.packageDir}
	if c.homeDir != "" {
		args = append(args, "--home", c.homeDir)
	}
	if c.launcher != "" {
		args = append(args, "--launcher", c.launcher)
	}
	return append(args, extra...)
}

// env returns the child environment with UTF-8 forced on every platform.
func (c *config) env() []string {
	forced := map[string]string{
		"PYTHONUTF8":               "1",
		"PYTHONIOENCODING":         "utf-8",
		"PYTHONLEGACYWINDOWSSTDIO": "0",
		"PYTHONDONTWRITEBYTECODE":  "1",
	}
	env := make([]string, 0, len(os.Environ())+len(forced))
	for _, item := range os.Environ() {
		key := item
		if index := strings.IndexByte(item, '='); index >= 0 {
			key = item[:index]
		}
		if _, drop := forced[strings.ToUpper(key)]; drop {
			continue
		}
		env = append(env, item)
	}
	for key, value := range forced {
		env = append(env, key+"="+value)
	}
	return env
}

// adminBaseURL asks the Python side which port the UI uses.
func adminBaseURL(cfg *config) string {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, cfg.python, cfg.adminArgs("--print-url")...)
	cmd.Dir = cfg.packageDir
	cmd.Env = cfg.env()
	configureCmd(cmd, false)
	out, err := cmd.Output()
	if err != nil {
		logger.Printf("print-url failed error=%v; using %s", err, defaultURL)
		return defaultURL
	}
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "http://") || strings.HasPrefix(line, "https://") {
			return line
		}
	}
	logger.Printf("print-url produced unexpected output; using %s", defaultURL)
	return defaultURL
}

// adminBuildFingerprint mirrors admin.py _build_fingerprint: the sha256 of the
// shipped admin.py, shortened to 12 hex characters. A service that reports a
// different value runs code that does not match this bundle, so even a hot fix
// that keeps the release number replaces the process it left behind.
func adminBuildFingerprint(packageDir string) string {
	path := filepath.Join(packageDir, "admin.py")
	data, err := os.ReadFile(path)
	if err != nil {
		logger.Printf("admin fingerprint unreadable path=%s error=%v", path, err)
		return ""
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])[:12]
}

// adminReusable reports whether a service that already answers /healthz runs
// exactly this release and this admin.py build. An unknown expected build means
// the launcher cannot read its own copy, so the release number still decides.
func adminReusable(release, build, expectedBuild string) bool {
	if release != appVersion {
		return false
	}
	if expectedBuild == "" {
		return true
	}
	return build == expectedBuild
}

// adminHealth reports whether an admin service answers, plus the release and
// admin.py fingerprint it reported. Empty strings mean either a service from a
// release without the handshake, or a body the launcher could not read.
func adminHealth(baseURL string, timeout time.Duration) (bool, string, string) {
	client := &http.Client{Timeout: timeout}
	resp, err := client.Get(baseURL + "/healthz")
	if err != nil {
		return false, "", ""
	}
	defer resp.Body.Close()
	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if resp.StatusCode != http.StatusOK {
		return false, "", ""
	}
	if readErr != nil {
		return true, "", ""
	}
	var payload struct {
		Service    string `json:"service"`
		AppVersion string `json:"app_version"`
		Build      string `json:"build"`
	}
	if json.Unmarshal(body, &payload) != nil {
		return true, "", ""
	}
	return true, payload.AppVersion, payload.Build
}

func healthOK(baseURL string, timeout time.Duration) bool {
	ok, _, _ := adminHealth(baseURL, timeout)
	return ok
}

// stopStaleAdmin asks the Python side to retire an admin service left over from
// an older release. It never kills a process the Python side cannot identify.
func stopStaleAdmin(cfg *config) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, cfg.python, cfg.adminArgs("--stop-stale")...)
	cmd.Dir = cfg.packageDir
	cmd.Env = cfg.env()
	configureCmd(cmd, false)
	out, err := cmd.CombinedOutput()
	logger.Printf("stale admin stop done error=%v output=%s", err, strings.TrimSpace(string(out)))
}

// ensureAdmin starts the service detached when nothing answers yet, and
// replaces a service that still reports an older app version.
func ensureAdmin(cfg *config, baseURL string) error {
	expectedBuild := adminBuildFingerprint(cfg.packageDir)
	if ok, running, runningBuild := adminHealth(baseURL, 2*time.Second); ok {
		if adminReusable(running, runningBuild, expectedBuild) {
			logger.Printf("admin already running url=%s version=%s build=%s", baseURL, running, runningBuild)
			return nil
		}
		// A stale service answers the new UI with its own endpoints, which shows up
		// as missing routes or settings the old code silently ignores. Replace it
		// before the window talks to it; the router keeps running on its own port.
		logger.Printf(
			"admin build mismatch running=%q/%q expected=%s/%s; replacing",
			running, runningBuild, appVersion, expectedBuild,
		)
		stopStaleAdmin(cfg)
	}
	logFile := openLogFile(filepath.Join(cfg.logsDir, "admin-launch.log"))
	var sink io.Writer = io.Discard
	if logFile != nil {
		defer logFile.Close()
		sink = logFile
	}
	cmd := exec.Command(cfg.python, cfg.adminArgs()...)
	cmd.Dir = cfg.packageDir
	cmd.Env = cfg.env()
	cmd.Stdin = nil
	cmd.Stdout = sink
	cmd.Stderr = sink
	configureCmd(cmd, true)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("spawn failed: %w", err)
	}
	logger.Printf("admin spawned pid=%d", cmd.Process.Pid)
	go func() {
		if err := cmd.Wait(); err != nil {
			logger.Printf("admin process ended error=%v", err)
		}
	}()
	deadline := time.Now().Add(45 * time.Second)
	for time.Now().Before(deadline) {
		if healthOK(baseURL, 2*time.Second) {
			return nil
		}
		time.Sleep(400 * time.Millisecond)
	}
	return fmt.Errorf("timeout waiting for %s/healthz", baseURL)
}

func fatal(err error) {
	logger.Printf("launcher fatal error=%v", err)
	message := "\u542f\u52a8\u5931\u8d25\uff1a" + err.Error()
	if hint := logHint(); hint != "" {
		message += "\n\n\u65e5\u5fd7\uff1a" + hint
	}
	showErrorDialog(windowTitle, message)
}

func logHint() string {
	if currentLogPath != "" {
		return currentLogPath
	}
	if dir, err := os.UserCacheDir(); err == nil {
		return filepath.Join(dir, "CodexMultiKey", "launcher.log")
	}
	return ""
}

// ------------------------------------------------------------------ service

// apiPoster is the only thing the close flow needs, so tests can inject a stub.
type apiPoster interface {
	post(path string) error
}

// adminClient posts to the local admin service.
type adminClient struct {
	baseURL string
	client  *http.Client
}

func (c adminClient) post(path string) error {
	request, err := http.NewRequest(http.MethodPost, c.baseURL+path, nil)
	if err != nil {
		return err
	}
	response, err := c.client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, response.Body)
	if response.StatusCode >= http.StatusBadRequest {
		return fmt.Errorf("%s answered %d", path, response.StatusCode)
	}
	return nil
}

// quitPath is the one admin call the close flow makes. It stops the router and
// then ends the background Python service, so "quit" really means the user is
// done: nothing stays behind to lock the packaged runtime (python/_bz2.pyd)
// during the next overwrite install. Codex stays on the local provider, and the
// login autostart brings the same route back on the next boot.
const quitPath = "/api/service/quit"

// shutdownService stops the router and the admin service before the launcher
// leaves. The admin handler answers immediately and finishes the teardown in
// its own worker, so this only has to get the request accepted.
func shutdownService(api apiPoster) {
	if api == nil {
		return
	}
	if err := api.post(quitPath); err != nil {
		logger.Printf("close flow failed path=%s error=%v", quitPath, err)
		return
	}
	logger.Printf("close flow ok path=%s", quitPath)
}

var quitOnce sync.Once

// quitFinished is the platform teardown after the close flow: leaving the
// AppKit event loop on macOS, ending the window loop on Windows. Tests replace
// it so the shared flow can be verified without touching a real UI.
var quitFinished = finishQuit

// quitApp runs the shared close flow exactly once, then leaves the platform UI.
// Codex is deliberately left on the local route so the autostart can resume it.
func quitApp() {
	quitOnce.Do(func() {
		logger.Printf("close tool requested")
		shutdownService(platformAPI)
		quitFinished()
		logger.Printf("close tool finished")
	})
}

// ----------------------------------------------------------------- instance

// instancePort returns the wake up port for the given admin url, or 0 when the
// url cannot be parsed (the channel is then simply skipped).
func instancePort(baseURL string) int {
	parsed, err := url.Parse(baseURL)
	if err != nil {
		return 0
	}
	port, err := strconv.Atoi(parsed.Port())
	if err != nil || port <= 0 || port+instanceOffset > 65535 {
		return 0
	}
	return port + instanceOffset
}

// wakeRunningInstance asks an already running window to show itself. It reports
// true only when that window answers, so an unrelated program on the port can
// never swallow a fresh launch.
// requestShow is false for a silent start: the autostart must not pop the main
// window open on every login when the user already launched the tool.
func wakeRunningInstance(port int, requestShow bool) bool {
	if port == 0 {
		return false
	}
	path := "/ping"
	if requestShow {
		path = "/show"
	}
	client := &http.Client{Timeout: 2 * time.Second}
	response, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d%s", port, path))
	if err != nil {
		return false
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(response.Body, 512))
	if response.StatusCode != http.StatusOK || !strings.Contains(string(body), instanceToken) {
		return false
	}
	logger.Printf("instance channel already listening port=%d", port)
	return true
}

// startInstanceChannel listens on the wake up port. The boolean reports whether
// this launcher owns the channel; when another program holds the port a normal
// start still opens its window, while a silent start leaves the running copy
// alone. The returned stop function is always safe to call.
func startInstanceChannel(port int, show func()) (func(), bool) {
	if port == 0 {
		return func() {}, false
	}
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		logger.Printf("instance channel unavailable port=%d error=%v", port, err)
		return func() {}, false
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/show", func(writer http.ResponseWriter, request *http.Request) {
		logger.Printf("instance channel show requested remote=%s", request.RemoteAddr)
		show()
		writeInstanceToken(writer)
	})
	mux.HandleFunc("/ping", func(writer http.ResponseWriter, request *http.Request) {
		logger.Printf("instance channel ping requested remote=%s", request.RemoteAddr)
		writeInstanceToken(writer)
	})
	server := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Printf("instance channel stopped error=%v", err)
		}
	}()
	logger.Printf("instance channel listening port=%d", port)
	return func() { _ = server.Close() }, true
}

// writeInstanceToken answers the single instance channel. The token proves the
// responder is this launcher and not an unrelated program on the same port.
func writeInstanceToken(writer http.ResponseWriter) {
	writer.Header().Set("Content-Type", "application/json; charset=utf-8")
	_, _ = io.WriteString(writer, `{"ok":true,"app":"`+instanceToken+`"}`)
}

// repairAutostart points an enabled login entry at this launcher. Releases up
// to 1.2.0 registered admin.py, so a reboot restored the background service but
// never the menu bar / tray icon.
func repairAutostart(api apiPoster) {
	if api == nil {
		return
	}
	if err := api.post("/api/autostart/repair"); err != nil {
		logger.Printf("autostart repair failed path=/api/autostart/repair error=%v", err)
		return
	}
	logger.Printf("autostart entry checked path=/api/autostart/repair")
}
