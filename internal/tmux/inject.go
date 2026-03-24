package tmux

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log"
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

	// Use a unique named buffer to avoid races with concurrent pastes.
	// tmux's default buffer is global — two simultaneous load-buffer calls
	// overwrite each other, delivering the wrong text to the wrong pane.
	rb := make([]byte, 4)
	rand.Read(rb)
	bufName := fmt.Sprintf("ag_%s", hex.EncodeToString(rb))

	// Try paste up to 3 times — verify text actually appeared in pane.
	pasteOK := false
	for pasteAttempt := 0; pasteAttempt < 3; pasteAttempt++ {
		// Load text into a named tmux buffer
		ctx1, cancel1 := timeoutContext(5 * time.Second)
		cmd := exec.CommandContext(ctx1, "tmux", "load-buffer", "-b", bufName, "-")
		cmd.Stdin = stringReader(text)
		if err := cmd.Run(); err != nil {
			cancel1()
			return err
		}
		cancel1()

		// Paste from the named buffer into pane (-p: bracketed paste mode)
		ctx2, cancel2 := timeoutContext(5 * time.Second)
		if err := exec.CommandContext(ctx2, "tmux", "paste-buffer", "-b", bufName, "-p", "-t", paneID).Run(); err != nil {
			cancel2()
			return err
		}
		cancel2()

		// Delete the named buffer (cleanup)
		exec.Command("tmux", "delete-buffer", "-b", bufName).Run()

		// Verify the paste actually appeared in the pane
		time.Sleep(200 * time.Millisecond)
		after, _ := CapturePane(paneID, 50)
		if after != before {
			pasteOK = true
			break
		}
		log.Printf("[tmux] Paste attempt %d failed for pane %s (pane unchanged), retrying", pasteAttempt+1, paneID)
		time.Sleep(300 * time.Millisecond)
	}
	if !pasteOK {
		log.Printf("[tmux] WARNING: paste failed after 3 attempts for pane %s (%d chars)", paneID, len(text))
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
