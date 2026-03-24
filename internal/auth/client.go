// Package auth implements SSH challenge-response authentication.
//
// Two authentication strategies are tried in order:
//  1. IdentityFile from ~/.ssh/config (reads key from disk, no ssh-agent needed)
//  2. ssh-agent (fallback: tries all keys available in the agent)
package auth

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Authenticate performs SSH challenge-response auth against the agentura server.
// Returns a bearer token on success. Returns ("", nil) if the server is unreachable.
func Authenticate(monitorURL string) (string, error) {
	client := &http.Client{Timeout: 5 * time.Second}

	nonceBytes, nonceB64, err := fetchNonce(client, monitorURL)
	if err != nil {
		return "", err
	}
	if nonceBytes == nil {
		return "", nil // server unreachable
	}

	hostname := extractHostname(monitorURL)

	// Stage 1: try IdentityFile from ~/.ssh/config
	token, stage1Err := tryIdentityFile(client, monitorURL, hostname, nonceBytes, nonceB64)
	if token != "" {
		return token, nil
	}

	// Stage 2: fall back to ssh-agent
	token, stage2Err := trySSHAgent(client, monitorURL, nonceBytes, nonceB64)
	if token != "" {
		return token, nil
	}

	return "", buildAuthError(hostname, stage1Err, stage2Err)
}

func fetchNonce(client *http.Client, monitorURL string) (nonceBytes []byte, nonceB64 string, err error) {
	resp, err := client.Get(monitorURL + "/api/auth/challenge")
	if err != nil {
		return nil, "", nil // server not running
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return nil, "", fmt.Errorf("challenge request failed: server returned HTTP %d", resp.StatusCode)
	}

	var challengeResp struct {
		Nonce string `json:"nonce"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&challengeResp); err != nil {
		return nil, "", fmt.Errorf("challenge response is not JSON (server may be behind a misconfigured proxy): %w", err)
	}

	decoded, err := base64.StdEncoding.DecodeString(challengeResp.Nonce)
	if err != nil {
		return nil, "", fmt.Errorf("decode nonce: %w", err)
	}
	return decoded, challengeResp.Nonce, nil
}

func extractHostname(monitorURL string) string {
	u, err := url.Parse(monitorURL)
	if err != nil {
		return monitorURL
	}
	return u.Hostname()
}

func tryIdentityFile(client *http.Client, monitorURL, hostname string, nonce []byte, nonceB64 string) (string, error) {
	keyPath := LookupIdentityFile(hostname)
	if keyPath == "" {
		return "", nil // no IdentityFile configured for this host
	}

	keyBlob, sig, err := SignWithKeyFile(keyPath, nonce)
	if err != nil {
		return "", err
	}

	token, err := verifyWithServer(client, monitorURL, nonceB64, keyBlob, sig)
	if err != nil {
		return "", fmt.Errorf("ssh config: server rejected key from %s", keyPath)
	}
	if token != "" {
		return token, nil
	}
	return "", fmt.Errorf("ssh config: server rejected key from %s", keyPath)
}

func trySSHAgent(client *http.Client, monitorURL string, nonce []byte, nonceB64 string) (string, error) {
	agent, err := NewSSHAgentClient()
	if err != nil {
		return "", err
	}

	keys, err := agent.ListKeys()
	if err != nil {
		return "", fmt.Errorf("ssh-agent: %w", err)
	}
	if len(keys) == 0 {
		return "", fmt.Errorf("ssh-agent has no keys — run 'ssh-add' to load your keys")
	}

	var tried []string
	for _, key := range keys {
		sig, err := agent.Sign(key.Blob, nonce)
		if err != nil {
			continue
		}

		token, err := verifyWithServer(client, monitorURL, nonceB64, key.Blob, sig)
		if err != nil {
			tried = append(tried, key.Comment)
			continue
		}
		if token != "" {
			return token, nil
		}
		tried = append(tried, key.Comment)
	}

	return "", fmt.Errorf("ssh-agent: server rejected all %d keys (tried: %s)", len(keys), strings.Join(tried, ", "))
}

func buildAuthError(hostname string, stage1Err, stage2Err error) error {
	var parts []string
	if stage1Err != nil {
		parts = append(parts, stage1Err.Error())
	} else {
		parts = append(parts, fmt.Sprintf("no IdentityFile for host %q in ~/.ssh/config", hostname))
	}
	if stage2Err != nil {
		parts = append(parts, stage2Err.Error())
	}
	return fmt.Errorf("SSH authentication failed:\n  config: %s\n  agent:  %s", parts[0], strings.Join(parts[1:], "; "))
}

func verifyWithServer(client *http.Client, monitorURL, nonce string, keyBlob []byte, sig *Signature) (string, error) {
	body := map[string]string{
		"nonce":     nonce,
		"key_blob":  base64.StdEncoding.EncodeToString(keyBlob),
		"signature": base64.StdEncoding.EncodeToString(sig.Data),
		"sig_type":  sig.Type,
	}
	bodyJSON, _ := json.Marshal(body)

	req, err := http.NewRequest("POST", monitorURL+"/api/auth/verify", jsonReader(bodyJSON))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return "", fmt.Errorf("key rejected")
	}
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("verify returned %d", resp.StatusCode)
	}

	var verifyResp struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&verifyResp); err != nil {
		return "", err
	}
	return verifyResp.Token, nil
}
