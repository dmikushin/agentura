// Thin launcher for agentura binaries.
//
// Behavior is determined by argv[0]: agentura-run, agentura-mcp, etc.
// Downloads the implementation binary from the agentura server on first use,
// caches it in .agentura/bin/ in the current directory, and execs it.
//
// If the cached binary already exists, execs it directly (no download).
// Delete .agentura/bin/ to force re-download.
package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
)

// DefaultMonitorURL is set at build time via ldflags, same as the implementation.
var DefaultMonitorURL string

func main() {
	// Determine which binary we are (based on argv[0])
	name := filepath.Base(os.Args[0])
	// Strip platform suffixes if present (e.g., agentura-run-linux-amd64 → agentura-run)
	for _, suffix := range []string{"-linux-amd64", "-linux-arm64", "-darwin-amd64", "-darwin-arm64"} {
		name = strings.TrimSuffix(name, suffix)
	}
	implName := name + "-impl"

	// Determine server URL
	serverURL := os.Getenv("AGENTURA_URL")
	if serverURL == "" {
		serverURL = loadDotenvURL()
	}
	if serverURL == "" {
		serverURL = DefaultMonitorURL
	}
	if serverURL == "" {
		fatal("AGENTURA_URL not set and no default compiled in")
	}

	// Cache directory
	cacheDir := filepath.Join(".", ".agentura", "bin")
	implPath := filepath.Join(cacheDir, implName)

	// If cached impl exists, exec it directly
	if _, err := os.Stat(implPath); err == nil {
		execImpl(implPath)
	}

	// Download from server
	os.MkdirAll(cacheDir, 0755)

	// Use flock to prevent concurrent downloads
	lockPath := filepath.Join(cacheDir, ".lock")
	lockFile, err := os.OpenFile(lockPath, os.O_CREATE|os.O_WRONLY, 0644)
	if err == nil {
		syscall.Flock(int(lockFile.Fd()), syscall.LOCK_EX)
		defer syscall.Flock(int(lockFile.Fd()), syscall.LOCK_UN)
		defer lockFile.Close()
	}

	// Check again after acquiring lock (another process may have downloaded)
	if _, err := os.Stat(implPath); err == nil {
		execImpl(implPath)
	}

	// Download
	url := fmt.Sprintf("%s/bin/%s/%s/%s", serverURL, name, runtime.GOOS, runtime.GOARCH)
	fmt.Fprintf(os.Stderr, "[launcher] Downloading %s from %s\n", name, serverURL)

	client := &http.Client{Timeout: 120 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		fatal("download failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
		fatal("download failed: HTTP %d: %s", resp.StatusCode, string(body))
	}

	// Write to temp file then rename (atomic)
	tmpPath := implPath + ".tmp"
	f, err := os.OpenFile(tmpPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0755)
	if err != nil {
		fatal("create temp file: %v", err)
	}

	if _, err := io.Copy(f, resp.Body); err != nil {
		f.Close()
		os.Remove(tmpPath)
		fatal("download write: %v", err)
	}
	f.Close()

	if err := os.Rename(tmpPath, implPath); err != nil {
		os.Remove(tmpPath)
		fatal("rename: %v", err)
	}

	fmt.Fprintf(os.Stderr, "[launcher] Cached %s → %s\n", name, implPath)
	execImpl(implPath)
}

func execImpl(path string) {
	abs, err := filepath.Abs(path)
	if err != nil {
		abs = path
	}
	// Replace current process with the implementation
	err = syscall.Exec(abs, os.Args, os.Environ())
	// If exec fails, try via exec.Command (handles shebang etc.)
	cmd := exec.Command(abs, os.Args[1:]...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = os.Environ()
	if err := cmd.Run(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			os.Exit(exitErr.ExitCode())
		}
		os.Exit(1)
	}
	os.Exit(0)
}

func loadDotenvURL() string {
	data, err := os.ReadFile(".env")
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "AGENTURA_URL=") {
			return strings.TrimPrefix(line, "AGENTURA_URL=")
		}
	}
	return ""
}

func fatal(format string, args ...interface{}) {
	fmt.Fprintf(os.Stderr, "[launcher] Error: "+format+"\n", args...)
	os.Exit(1)
}
