// SSH config parser and key file signing.
package auth

import (
	"bufio"
	"crypto/rand"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/crypto/ssh"
)

// LookupIdentityFile parses ~/.ssh/config and returns the IdentityFile
// path for the given hostname, or "" if not found.
func LookupIdentityFile(hostname string) string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return lookupIdentityFileIn(filepath.Join(home, ".ssh", "config"), hostname)
}

func lookupIdentityFileIn(configPath, hostname string) string {
	f, err := os.Open(configPath)
	if err != nil {
		return ""
	}
	defer f.Close()

	inMatchingHost := false
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}

		// Check for Host directive
		if strings.HasPrefix(strings.ToLower(line), "host ") {
			inMatchingHost = false
			patterns := strings.Fields(line)[1:]
			for _, p := range patterns {
				if p == hostname {
					inMatchingHost = true
					break
				}
			}
			continue
		}

		if !inMatchingHost {
			continue
		}

		// Check for IdentityFile inside matching Host block
		if strings.HasPrefix(strings.ToLower(line), "identityfile ") {
			path := strings.TrimSpace(line[len("identityfile "):])
			return expandTilde(path)
		}
	}
	return ""
}

func expandTilde(path string) string {
	if !strings.HasPrefix(path, "~/") {
		return path
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return path
	}
	return filepath.Join(home, path[2:])
}

// SignWithKeyFile reads a private key from disk and signs the nonce.
// Returns the wire-format public key blob and signature.
func SignWithKeyFile(keyPath string, nonce []byte) (keyBlob []byte, sig *Signature, err error) {
	pemBytes, err := os.ReadFile(keyPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil, fmt.Errorf("ssh config: IdentityFile %s does not exist", keyPath)
		}
		return nil, nil, fmt.Errorf("ssh config: read %s: %w", keyPath, err)
	}

	signer, err := ssh.ParsePrivateKey(pemBytes)
	if err != nil {
		if _, ok := err.(*ssh.PassphraseMissingError); ok {
			return nil, nil, fmt.Errorf("ssh config: IdentityFile %s is passphrase-protected, skipping", keyPath)
		}
		return nil, nil, fmt.Errorf("ssh config: parse %s: %w", keyPath, err)
	}

	keyBlob = signer.PublicKey().Marshal()

	// For RSA keys, request SHA-256 signature instead of default SHA-1
	var sshSig *ssh.Signature
	if algSigner, ok := signer.(ssh.AlgorithmSigner); ok && signer.PublicKey().Type() == "ssh-rsa" {
		sshSig, err = algSigner.SignWithAlgorithm(rand.Reader, nonce, ssh.KeyAlgoRSASHA256)
	} else {
		sshSig, err = signer.Sign(rand.Reader, nonce)
	}
	if err != nil {
		return nil, nil, fmt.Errorf("ssh config: sign with %s: %w", keyPath, err)
	}

	return keyBlob, &Signature{Type: sshSig.Format, Data: sshSig.Blob}, nil
}
