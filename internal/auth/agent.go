// SSH agent protocol client — communicates with ssh-agent via SSH_AUTH_SOCK.
package auth

import (
	"encoding/binary"
	"fmt"
	"net"
	"os"
)

// SSHAgentClient communicates with ssh-agent via SSH_AUTH_SOCK (RFC 4253 agent protocol).
type SSHAgentClient struct {
	sockPath string
}

// Key represents an SSH key from the agent.
type Key struct {
	Blob    []byte
	Comment string
}

// Signature holds the result of signing data with an SSH key.
type Signature struct {
	Type string
	Data []byte
}

const (
	requestIdentities = 11
	identitiesAnswer  = 12
	signRequest       = 13
	signResponse      = 14
	rsaSHA2256Flag    = 0x02
)

// NewSSHAgentClient creates a client connected to the ssh-agent.
func NewSSHAgentClient() (*SSHAgentClient, error) {
	sockPath := os.Getenv("SSH_AUTH_SOCK")
	if sockPath == "" {
		return nil, fmt.Errorf("ssh-agent is not running (SSH_AUTH_SOCK not set)")
	}
	return &SSHAgentClient{sockPath: sockPath}, nil
}

func (c *SSHAgentClient) communicate(msg []byte) ([]byte, error) {
	conn, err := net.Dial("unix", c.sockPath)
	if err != nil {
		return nil, fmt.Errorf("connect to ssh-agent: %w", err)
	}
	defer conn.Close()

	// Send length-prefixed message
	lenBuf := make([]byte, 4)
	binary.BigEndian.PutUint32(lenBuf, uint32(len(msg)))
	if _, err := conn.Write(lenBuf); err != nil {
		return nil, err
	}
	if _, err := conn.Write(msg); err != nil {
		return nil, err
	}

	// Read response length
	if _, err := readFull(conn, lenBuf); err != nil {
		return nil, fmt.Errorf("read response length: %w", err)
	}
	respLen := binary.BigEndian.Uint32(lenBuf)

	// Read response body
	resp := make([]byte, respLen)
	if _, err := readFull(conn, resp); err != nil {
		return nil, fmt.Errorf("read response body: %w", err)
	}
	return resp, nil
}

func readFull(conn net.Conn, buf []byte) (int, error) {
	total := 0
	for total < len(buf) {
		n, err := conn.Read(buf[total:])
		if err != nil {
			return total, err
		}
		total += n
	}
	return total, nil
}

func readString(data []byte, offset int) ([]byte, int, error) {
	if offset+4 > len(data) {
		return nil, offset, fmt.Errorf("short read at offset %d", offset)
	}
	slen := int(binary.BigEndian.Uint32(data[offset:]))
	offset += 4
	if offset+slen > len(data) {
		return nil, offset, fmt.Errorf("string overflows buffer at offset %d", offset)
	}
	return data[offset : offset+slen], offset + slen, nil
}

// ListKeys returns all keys available in the SSH agent.
func (c *SSHAgentClient) ListKeys() ([]Key, error) {
	resp, err := c.communicate([]byte{requestIdentities})
	if err != nil {
		return nil, err
	}
	if len(resp) < 5 || resp[0] != identitiesAnswer {
		return nil, fmt.Errorf("unexpected agent response: %d", resp[0])
	}

	nkeys := int(binary.BigEndian.Uint32(resp[1:5]))
	keys := make([]Key, 0, nkeys)
	offset := 5
	for i := 0; i < nkeys; i++ {
		blob, off, err := readString(resp, offset)
		if err != nil {
			return nil, fmt.Errorf("read key %d blob: %w", i, err)
		}
		comment, off, err := readString(resp, off)
		if err != nil {
			return nil, fmt.Errorf("read key %d comment: %w", i, err)
		}
		keys = append(keys, Key{
			Blob:    append([]byte(nil), blob...),
			Comment: string(comment),
		})
		offset = off
	}
	return keys, nil
}

// Sign signs data with the specified key via the SSH agent.
func (c *SSHAgentClient) Sign(keyBlob, data []byte) (*Signature, error) {
	// Determine key type for flags
	keyType, _, err := readString(keyBlob, 0)
	if err != nil {
		return nil, fmt.Errorf("read key type: %w", err)
	}
	var flags uint32
	if string(keyType) == "ssh-rsa" {
		flags = rsaSHA2256Flag
	}

	// Build sign request message
	msg := []byte{signRequest}
	msg = appendString(msg, keyBlob)
	msg = appendString(msg, data)
	flagBuf := make([]byte, 4)
	binary.BigEndian.PutUint32(flagBuf, flags)
	msg = append(msg, flagBuf...)

	resp, err := c.communicate(msg)
	if err != nil {
		return nil, err
	}
	if len(resp) < 1 || resp[0] != signResponse {
		return nil, fmt.Errorf("agent refused to sign: response type %d", resp[0])
	}

	sigBlob, _, err := readString(resp, 1)
	if err != nil {
		return nil, fmt.Errorf("read signature blob: %w", err)
	}
	sigType, off, err := readString(sigBlob, 0)
	if err != nil {
		return nil, fmt.Errorf("read signature type: %w", err)
	}
	sigData, _, err := readString(sigBlob, off)
	if err != nil {
		return nil, fmt.Errorf("read signature data: %w", err)
	}
	return &Signature{
		Type: string(sigType),
		Data: append([]byte(nil), sigData...),
	}, nil
}

func appendString(buf, s []byte) []byte {
	lenBuf := make([]byte, 4)
	binary.BigEndian.PutUint32(lenBuf, uint32(len(s)))
	buf = append(buf, lenBuf...)
	buf = append(buf, s...)
	return buf
}
