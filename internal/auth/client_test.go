package auth

import (
	"encoding/binary"
	"testing"
)

func TestReadString(t *testing.T) {
	// Build a wire-format string: length (4 bytes big-endian) + content
	content := []byte("ssh-ed25519")
	data := make([]byte, 4+len(content))
	binary.BigEndian.PutUint32(data[0:4], uint32(len(content)))
	copy(data[4:], content)

	got, off, err := readString(data, 0)
	if err != nil {
		t.Fatalf("readString: %v", err)
	}
	if string(got) != "ssh-ed25519" {
		t.Errorf("readString content: got %q, want %q", got, "ssh-ed25519")
	}
	if off != 4+len(content) {
		t.Errorf("readString offset: got %d, want %d", off, 4+len(content))
	}
}

func TestReadStringShortBuffer(t *testing.T) {
	// Only 2 bytes — not enough for length prefix
	_, _, err := readString([]byte{0x00, 0x01}, 0)
	if err == nil {
		t.Error("readString should error on short buffer")
	}
}

func TestReadStringOverflow(t *testing.T) {
	// Length says 100 bytes but only 5 available
	data := make([]byte, 9)
	binary.BigEndian.PutUint32(data[0:4], 100)
	_, _, err := readString(data, 0)
	if err == nil {
		t.Error("readString should error on overflow")
	}
}

func TestAppendString(t *testing.T) {
	content := []byte("hello")
	result := appendString(nil, content)
	if len(result) != 4+5 {
		t.Fatalf("appendString length: got %d, want %d", len(result), 9)
	}
	slen := binary.BigEndian.Uint32(result[0:4])
	if slen != 5 {
		t.Errorf("appendString encoded length: got %d, want 5", slen)
	}
	if string(result[4:]) != "hello" {
		t.Errorf("appendString content: got %q, want %q", result[4:], "hello")
	}
}

func TestNewSSHAgentClientNoSocket(t *testing.T) {
	// Temporarily unset SSH_AUTH_SOCK
	t.Setenv("SSH_AUTH_SOCK", "")
	_, err := NewSSHAgentClient()
	if err == nil {
		t.Error("expected error when SSH_AUTH_SOCK is not set")
	}
}

func TestNewSSHAgentClientWithSocket(t *testing.T) {
	t.Setenv("SSH_AUTH_SOCK", "/tmp/test-agent.sock")
	client, err := NewSSHAgentClient()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if client.sockPath != "/tmp/test-agent.sock" {
		t.Errorf("sockPath: got %q, want %q", client.sockPath, "/tmp/test-agent.sock")
	}
}
