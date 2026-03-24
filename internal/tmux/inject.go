package tmux

import (
	"crypto/rand"
	"encoding/hex"
	"os/exec"
	"strings"
	"time"
)

// Inject sends text into a tmux pane and ensures it gets submitted.
// First probes pane readiness by typing a unique test sequence and
// waiting for it to echo back. Then pastes the actual text and
// hammers Enter until the pane content changes.
func Inject(paneID, text string) error {
	if text == "\x1b" {
		return sendKeys(paneID, "Escape")
	}

	// Wait for TUI to be ready by sending a probe string and watching for echo
	waitForReady(paneID)

	// Snapshot pane content before injection
	before, _ := CapturePane(paneID, 50)

	// Paste text via tmux buffer
	ctx1, cancel1 := timeoutContext(5 * time.Second)
	defer cancel1()
	cmd := exec.CommandContext(ctx1, "tmux", "load-buffer", "-")
	cmd.Stdin = stringReader(text)
	if err := cmd.Run(); err != nil {
		return err
	}

	ctx2, cancel2 := timeoutContext(5 * time.Second)
	defer cancel2()
	// -p: bracketed paste mode so multiline \n doesn't trigger submit
	if err := exec.CommandContext(ctx2, "tmux", "paste-buffer", "-p", "-t", paneID).Run(); err != nil {
		return err
	}

	// Hammer Enter until pane content changes (agent consumed the input)
	delays := []time.Duration{
		100 * time.Millisecond,
		150 * time.Millisecond,
		200 * time.Millisecond,
		300 * time.Millisecond,
		400 * time.Millisecond,
		500 * time.Millisecond,
		500 * time.Millisecond,
		500 * time.Millisecond,
		500 * time.Millisecond,
		500 * time.Millisecond,
	}

	for i, delay := range delays {
		time.Sleep(delay)

		if i%2 == 0 {
			sendKeys(paneID, "Enter")
		} else {
			sendKeys(paneID, "Enter")
			time.Sleep(30 * time.Millisecond)
			sendKeys(paneID, "Enter")
		}

		time.Sleep(100 * time.Millisecond)
		after, _ := CapturePane(paneID, 50)
		if paneChanged(before, after, text) {
			return nil
		}
	}

	return nil // best effort
}

// waitForReady probes the pane by typing a unique random string and
// checking if it echoes back in the capture. This confirms the TUI
// has initialized and the input field is accepting keystrokes.
// Retries up to 15 seconds, then gives up (best effort).
func waitForReady(paneID string) {
	// Generate unique probe: "agentura_probe_<random>"
	b := make([]byte, 4)
	rand.Read(b)
	probe := "ag_" + hex.EncodeToString(b)

	for attempt := 0; attempt < 30; attempt++ {
		// Type the probe characters via send-keys (not paste)
		sendKeys(paneID, probe)

		time.Sleep(500 * time.Millisecond)

		content, err := CapturePane(paneID, 20)
		if err == nil && strings.Contains(content, probe) {
			// TUI is ready — clear the probe with backspaces
			for range probe {
				sendKeys(paneID, "BSpace")
			}
			time.Sleep(100 * time.Millisecond)
			return
		}

		// Probe not echoed — TUI not ready yet. Clean up and retry.
		// Send Ctrl-U to clear any partial input
		sendKeys(paneID, "C-u")
		time.Sleep(100 * time.Millisecond)
	}
	// Gave up — proceed anyway (best effort)
}

func paneChanged(before, after, injectedText string) bool {
	if after == before {
		return false
	}
	beforeLines := strings.Count(before, "\n")
	afterLines := strings.Count(after, "\n")
	if afterLines > beforeLines+1 {
		return true
	}
	if strings.Contains(after, injectedText) && after != before {
		return true
	}
	return false
}

func sendKeys(paneID string, keys ...string) error {
	ctx, cancel := timeoutContext(5 * time.Second)
	defer cancel()
	args := append([]string{"send-keys", "-t", paneID}, keys...)
	return exec.CommandContext(ctx, "tmux", args...).Run()
}
