package sidecar

import (
	"errors"
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

	proxyFn := func(method, path string, data map[string]interface{}) (map[string]interface{}, error) {
		return map[string]interface{}{
			"echo_method": method,
			"echo_path":   path,
			"echo_data":   data,
		}, nil
	}

	done := make(chan struct{})
	go func() {
		ln.ProcessPending(proxyFn, 2*time.Second)
		close(done)
	}()
	time.Sleep(50 * time.Millisecond)

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
	if !errors.Is(err, ErrSidecarUnavailable) {
		t.Errorf("expected ErrSidecarUnavailable, got: %v", err)
	}
}

// --- Ported from test_sidecar_ipc.py ---

func TestIPCInjectAgentToken(t *testing.T) {
	sockPath := filepath.Join(os.TempDir(), "agentura-test-inject.sock")
	defer os.Remove(sockPath)

	ln, err := NewListener(sockPath)
	if err != nil {
		t.Fatalf("NewListener: %v", err)
	}
	defer ln.Close()

	var receivedData map[string]interface{}

	// Simulate sidecar's proxy which handles _inject_agent_token
	proxyFn := func(method, path string, data map[string]interface{}) (map[string]interface{}, error) {
		if data != nil {
			if _, ok := data["_inject_agent_token"]; ok {
				delete(data, "_inject_agent_token")
				data["agent_token"] = "REAL_TOKEN_123"
			}
		}
		receivedData = data
		return map[string]interface{}{"status": "ok"}, nil
	}

	done := make(chan struct{})
	go func() {
		ln.ProcessPending(proxyFn, 2*time.Second)
		close(done)
	}()
	time.Sleep(50 * time.Millisecond)

	_, err = Request(sockPath, "POST", "/teams", map[string]interface{}{
		"name":                "my-team",
		"_inject_agent_token": true,
	})
	if err != nil {
		t.Fatalf("Request: %v", err)
	}

	<-done

	if receivedData["agent_token"] != "REAL_TOKEN_123" {
		t.Errorf("token not injected: got %v", receivedData["agent_token"])
	}
	if _, exists := receivedData["_inject_agent_token"]; exists {
		t.Error("_inject_agent_token flag not removed")
	}
	if receivedData["name"] != "my-team" {
		t.Errorf("other data not preserved: name=%v", receivedData["name"])
	}
}

func TestIPCMultipleSequential(t *testing.T) {
	sockPath := filepath.Join(os.TempDir(), "agentura-test-multi.sock")
	defer os.Remove(sockPath)

	ln, err := NewListener(sockPath)
	if err != nil {
		t.Fatalf("NewListener: %v", err)
	}
	defer ln.Close()

	callCount := 0
	proxyFn := func(method, path string, data map[string]interface{}) (map[string]interface{}, error) {
		callCount++
		return map[string]interface{}{"call": float64(callCount)}, nil
	}

	done := make(chan struct{})
	go func() {
		for i := 0; i < 3; i++ {
			ln.ProcessPending(proxyFn, 2*time.Second)
		}
		close(done)
	}()
	time.Sleep(50 * time.Millisecond)

	r1, err := Request(sockPath, "GET", "/agents", nil)
	if err != nil {
		t.Fatalf("Request 1: %v", err)
	}
	time.Sleep(50 * time.Millisecond)

	r2, err := Request(sockPath, "GET", "/teams", nil)
	if err != nil {
		t.Fatalf("Request 2: %v", err)
	}
	time.Sleep(50 * time.Millisecond)

	r3, err := Request(sockPath, "POST", "/teams", map[string]interface{}{"name": "x"})
	if err != nil {
		t.Fatalf("Request 3: %v", err)
	}

	<-done

	if r1["call"] != float64(1) {
		t.Errorf("first call: got %v, want 1", r1["call"])
	}
	if r2["call"] != float64(2) {
		t.Errorf("second call: got %v, want 2", r2["call"])
	}
	if r3["call"] != float64(3) {
		t.Errorf("third call: got %v, want 3", r3["call"])
	}
}

func TestIPCSocketCleanup(t *testing.T) {
	sockPath := filepath.Join(os.TempDir(), "agentura-test-cleanup.sock")

	ln, err := NewListener(sockPath)
	if err != nil {
		t.Fatalf("NewListener: %v", err)
	}

	// Socket file should exist
	if _, err := os.Stat(sockPath); os.IsNotExist(err) {
		t.Error("socket file not created")
	}

	ln.Close()

	// Socket file should be removed
	if _, err := os.Stat(sockPath); !os.IsNotExist(err) {
		t.Error("socket file not cleaned up after Close()")
		os.Remove(sockPath) // cleanup
	}
}

func TestIPCProxyError(t *testing.T) {
	sockPath := filepath.Join(os.TempDir(), "agentura-test-error.sock")
	defer os.Remove(sockPath)

	ln, err := NewListener(sockPath)
	if err != nil {
		t.Fatalf("NewListener: %v", err)
	}
	defer ln.Close()

	proxyFn := func(method, path string, data map[string]interface{}) (map[string]interface{}, error) {
		return nil, errors.New("something went wrong")
	}

	done := make(chan struct{})
	go func() {
		ln.ProcessPending(proxyFn, 2*time.Second)
		close(done)
	}()
	time.Sleep(50 * time.Millisecond)

	_, err = Request(sockPath, "GET", "/fail", nil)
	<-done

	if err == nil {
		t.Error("expected error when proxy returns error")
	}
}
