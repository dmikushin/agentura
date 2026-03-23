// Package sidecar implements the agent sidecar process and its IPC mechanism.
//
// The sidecar is the sole network gateway for remote agents. The MCP backend
// connects to the sidecar over a local Unix socket instead of making HTTPS
// calls directly.
//
// Protocol: JSON-lines over Unix domain socket (one request + one response per connection).
//
//	Request:  {"method": "GET"|"POST", "path": "/agents", "data": {...}}\n
//	Response: {"status": 200, "body": {...}}\n
package sidecar

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"sync"
	"time"
)

// ErrSidecarUnavailable is returned when the sidecar IPC socket is not reachable.
var ErrSidecarUnavailable = errors.New("sidecar IPC unavailable")

// IPCRequest represents a request from the MCP backend.
type IPCRequest struct {
	Method string                 `json:"method"`
	Path   string                 `json:"path"`
	Data   map[string]interface{} `json:"data,omitempty"`
}

// IPCResponse represents a response to the MCP backend.
type IPCResponse struct {
	Status int                    `json:"status"`
	Body   map[string]interface{} `json:"body"`
}

// ProxyFunc is called for each IPC request. It should proxy the request
// to the agentura server and return the response body.
type ProxyFunc func(method, path string, data map[string]interface{}) (map[string]interface{}, error)

// Listener accepts IPC connections from the MCP backend over a Unix socket.
type Listener struct {
	socketPath string
	listener   net.Listener
	mu         sync.Mutex
	closed     bool
}

// NewListener creates and starts a Unix socket listener at the given path.
func NewListener(socketPath string) (*Listener, error) {
	// Remove stale socket file
	os.Remove(socketPath)

	ln, err := net.Listen("unix", socketPath)
	if err != nil {
		return nil, fmt.Errorf("listen on %s: %w", socketPath, err)
	}

	return &Listener{
		socketPath: socketPath,
		listener:   ln,
	}, nil
}

// ProcessPending accepts and handles IPC connections for up to timeout duration.
// This replaces time.Sleep in the sidecar loop: it waits for IPC requests
// OR times out after the heartbeat interval.
func (l *Listener) ProcessPending(proxyFn ProxyFunc, timeout time.Duration) {
	l.listener.(*net.UnixListener).SetDeadline(time.Now().Add(timeout))

	for {
		conn, err := l.listener.Accept()
		if err != nil {
			// Timeout or closed — both are normal
			return
		}
		// Handle each connection synchronously (simple, matches Python behavior)
		l.handleConnection(conn, proxyFn)
		conn.Close()
	}
}

func (l *Listener) handleConnection(conn net.Conn, proxyFn ProxyFunc) {
	conn.SetDeadline(time.Now().Add(5 * time.Second))

	// Read until newline
	buf := make([]byte, 0, 4096)
	tmp := make([]byte, 4096)
	for {
		n, err := conn.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
		}
		if err != nil {
			if len(buf) == 0 {
				return
			}
			break
		}
		// Check for newline
		for _, b := range buf {
			if b == '\n' {
				goto done
			}
		}
	}
done:

	var req IPCRequest
	if err := json.Unmarshal(buf, &req); err != nil {
		resp := IPCResponse{Status: 400, Body: map[string]interface{}{"error": "invalid JSON"}}
		data, _ := json.Marshal(resp)
		conn.Write(append(data, '\n'))
		return
	}

	result, err := proxyFn(req.Method, req.Path, req.Data)
	var resp IPCResponse
	if err != nil {
		resp = IPCResponse{Status: 500, Body: map[string]interface{}{"error": err.Error()}}
	} else {
		resp = IPCResponse{Status: 200, Body: result}
	}

	data, _ := json.Marshal(resp)
	conn.Write(append(data, '\n'))
}

// Close shuts down the listener and removes the socket file.
func (l *Listener) Close() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closed {
		return
	}
	l.closed = true
	l.listener.Close()
	os.Remove(l.socketPath)
}

// SocketPath returns the path to the Unix socket.
func (l *Listener) SocketPath() string {
	return l.socketPath
}

// Request sends a request to the sidecar via Unix socket IPC.
// Returns the response body or ErrSidecarUnavailable on connection failure.
func Request(sockPath, method, path string, data map[string]interface{}) (map[string]interface{}, error) {
	conn, err := net.DialTimeout("unix", sockPath, 5*time.Second)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrSidecarUnavailable, err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(30 * time.Second))

	req := IPCRequest{Method: method, Path: path, Data: data}
	reqData, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	if _, err := conn.Write(append(reqData, '\n')); err != nil {
		return nil, fmt.Errorf("%w: write: %v", ErrSidecarUnavailable, err)
	}

	// Read response until newline
	buf := make([]byte, 0, 65536)
	tmp := make([]byte, 65536)
	for {
		n, err := conn.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
		}
		if err != nil {
			break
		}
		for _, b := range buf {
			if b == '\n' {
				goto respDone
			}
		}
	}
respDone:

	if len(buf) == 0 {
		return nil, fmt.Errorf("%w: empty response", ErrSidecarUnavailable)
	}

	var resp IPCResponse
	if err := json.Unmarshal(buf, &resp); err != nil {
		return nil, fmt.Errorf("%w: decode: %v", ErrSidecarUnavailable, err)
	}

	if resp.Status >= 400 {
		errMsg := "server error"
		if e, ok := resp.Body["error"]; ok {
			errMsg = fmt.Sprint(e)
		}
		return nil, fmt.Errorf("sidecar: HTTP %d: %s", resp.Status, errMsg)
	}

	return resp.Body, nil
}
