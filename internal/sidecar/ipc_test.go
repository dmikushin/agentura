package sidecar

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestIPCRoundTrip(t *testing.T) {
	sockPath := filepath.Join(os.TempDir(), "agentura-test-ipc.sock")
	defer os.Remove(sockPath)

	ln, err := NewListener(sockPath)
	if err != nil {
		t.Fatalf("NewListener: %v", err)
	}
	defer ln.Close()

	// Proxy function that echoes back the path
	proxyFn := func(method, path string, data map[string]interface{}) (map[string]interface{}, error) {
		return map[string]interface{}{
			"echo_method": method,
			"echo_path":   path,
			"echo_data":   data,
		}, nil
	}

	// Start listener in background
	done := make(chan struct{})
	go func() {
		ln.ProcessPending(proxyFn, 2*time.Second)
		close(done)
	}()

	// Give listener time to start
	time.Sleep(50 * time.Millisecond)

	// Send request
	resp, err := Request(sockPath, "GET", "/agents", nil)
	if err != nil {
		t.Fatalf("Request: %v", err)
	}

	if resp["echo_method"] != "GET" {
		t.Errorf("method: got %v, want GET", resp["echo_method"])
	}
	if resp["echo_path"] != "/agents" {
		t.Errorf("path: got %v, want /agents", resp["echo_path"])
	}

	<-done
}

func TestIPCWithData(t *testing.T) {
	sockPath := filepath.Join(os.TempDir(), "agentura-test-ipc2.sock")
	defer os.Remove(sockPath)

	ln, err := NewListener(sockPath)
	if err != nil {
		t.Fatalf("NewListener: %v", err)
	}
	defer ln.Close()

	proxyFn := func(method, path string, data map[string]interface{}) (map[string]interface{}, error) {
		return map[string]interface{}{"received": data}, nil
	}

	done := make(chan struct{})
	go func() {
		ln.ProcessPending(proxyFn, 2*time.Second)
		close(done)
	}()

	time.Sleep(50 * time.Millisecond)

	resp, err := Request(sockPath, "POST", "/sidecar/heartbeat", map[string]interface{}{
		"agent_id":    "test@host:123",
		"child_alive": true,
	})
	if err != nil {
		t.Fatalf("Request: %v", err)
	}

	received, ok := resp["received"].(map[string]interface{})
	if !ok {
		t.Fatalf("received not a map: %T", resp["received"])
	}
	if received["agent_id"] != "test@host:123" {
		t.Errorf("agent_id: got %v, want test@host:123", received["agent_id"])
	}

	<-done
}

func TestIPCUnavailable(t *testing.T) {
	_, err := Request("/tmp/nonexistent-agentura-sock.sock", "GET", "/test", nil)
	if err == nil {
		t.Error("expected error for nonexistent socket")
	}
}
