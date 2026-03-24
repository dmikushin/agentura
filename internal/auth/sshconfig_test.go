package auth

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/crypto/ssh"
)

func TestLookupIdentityFileExactMatch(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config")
	os.WriteFile(configPath, []byte(`
Host agents.mikush.in
    HostName agents.mikush.in
    IdentityFile ~/.ssh/keys/id_ed25519.agents
    User deploy

Host oracle
    HostName gateway.mikush.in
    IdentityFile ~/.ssh/keys/id_rsa.oracle
`), 0644)

	got := lookupIdentityFileIn(configPath, "agents.mikush.in")
	home, _ := os.UserHomeDir()
	want := filepath.Join(home, ".ssh/keys/id_ed25519.agents")
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestLookupIdentityFileNoMatch(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config")
	os.WriteFile(configPath, []byte(`
Host oracle
    HostName gateway.mikush.in
    IdentityFile ~/.ssh/keys/id_rsa.oracle
`), 0644)

	got := lookupIdentityFileIn(configPath, "agents.mikush.in")
	if got != "" {
		t.Errorf("expected empty, got %q", got)
	}
}

func TestLookupIdentityFileMultiHost(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config")
	os.WriteFile(configPath, []byte(`
Host agents.mikush.in agents.example.com
    IdentityFile ~/.ssh/keys/id_ed25519.agents
`), 0644)

	for _, host := range []string{"agents.mikush.in", "agents.example.com"} {
		got := lookupIdentityFileIn(configPath, host)
		if got == "" {
			t.Errorf("expected match for %q", host)
		}
	}
}

func TestLookupIdentityFileSkipsWildcard(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config")
	os.WriteFile(configPath, []byte(`
Host *
    IdentityFile ~/.ssh/id_rsa
`), 0644)

	got := lookupIdentityFileIn(configPath, "anything.example.com")
	if got != "" {
		t.Errorf("wildcard should not match, got %q", got)
	}
}

func TestLookupIdentityFileMissingFile(t *testing.T) {
	got := lookupIdentityFileIn("/nonexistent/path/config", "example.com")
	if got != "" {
		t.Errorf("expected empty for missing config, got %q", got)
	}
}

func TestLookupIdentityFileCaseInsensitive(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config")
	os.WriteFile(configPath, []byte(`
HOST agents.mikush.in
    IDENTITYFILE ~/.ssh/keys/id_ed25519.agents
`), 0644)

	got := lookupIdentityFileIn(configPath, "agents.mikush.in")
	if got == "" {
		t.Error("expected case-insensitive match")
	}
}

func TestExtractHostname(t *testing.T) {
	tests := []struct {
		url  string
		want string
	}{
		{"https://agents.mikush.in", "agents.mikush.in"},
		{"https://agents.mikush.in:8080", "agents.mikush.in"},
		{"https://agents.mikush.in/api/v1", "agents.mikush.in"},
		{"http://localhost:7850", "localhost"},
	}
	for _, tt := range tests {
		got := extractHostname(tt.url)
		if got != tt.want {
			t.Errorf("extractHostname(%q) = %q, want %q", tt.url, got, tt.want)
		}
	}
}

func TestSignWithKeyFile(t *testing.T) {
	// Generate a temporary ed25519 key
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}

	signer, err := ssh.NewSignerFromKey(priv)
	if err != nil {
		t.Fatalf("new signer: %v", err)
	}

	// Write private key to temp file
	dir := t.TempDir()
	keyPath := filepath.Join(dir, "id_ed25519")
	pemBytes := marshalPrivateKey(t, priv)
	os.WriteFile(keyPath, pemBytes, 0600)

	nonce := []byte("test-nonce-data-1234567890123456")
	keyBlob, sig, err := SignWithKeyFile(keyPath, nonce)
	if err != nil {
		t.Fatalf("SignWithKeyFile: %v", err)
	}

	// Verify keyBlob matches the public key
	sshPub, err := ssh.NewPublicKey(pub)
	if err != nil {
		t.Fatalf("new public key: %v", err)
	}
	if string(keyBlob) != string(sshPub.Marshal()) {
		t.Error("keyBlob doesn't match expected public key")
	}

	// Verify signature is valid
	sshSig := &ssh.Signature{Format: sig.Type, Blob: sig.Data}
	if err := signer.PublicKey().Verify(nonce, sshSig); err != nil {
		t.Errorf("signature verification failed: %v", err)
	}
}

func TestSignWithKeyFileNotFound(t *testing.T) {
	_, _, err := SignWithKeyFile("/nonexistent/key", []byte("nonce"))
	if err == nil {
		t.Error("expected error for missing key file")
	}
}

func marshalPrivateKey(t *testing.T, key ed25519.PrivateKey) []byte {
	t.Helper()
	block, err := ssh.MarshalPrivateKey(key, "")
	if err != nil {
		t.Fatalf("marshal private key: %v", err)
	}
	return pem.EncodeToMemory(block)
}
