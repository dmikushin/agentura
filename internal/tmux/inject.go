package tmux

import (
	"os/exec"
	"strings"
	"time"
)

// Inject sends text into a tmux pane and ensures it gets submitted.
// Uses load-buffer + paste-buffer for reliable text input, then
// hammers Enter until the pane content changes (confirming submission).
func Inject(paneID, text string) error {
	if text == "\x1b" {
		return sendKeys(paneID, "Escape")
	}

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
	// Try single Enter, then double, with increasing delays.
	// Total budget: ~5 seconds.
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

		// Alternate single and double Enter
		if i%2 == 0 {
			sendKeys(paneID, "Enter")
		} else {
			sendKeys(paneID, "Enter")
			time.Sleep(30 * time.Millisecond)
			sendKeys(paneID, "Enter")
		}

		// Check if pane changed (agent started processing)
		time.Sleep(100 * time.Millisecond)
		after, _ := CapturePane(paneID, 50)
		if paneChanged(before, after, text) {
			return nil
		}
	}

	return nil // best effort
}

// paneChanged checks if the pane content changed meaningfully after injection.
// Looks for signs that the agent consumed the input: new output appeared,
// or the injected text is no longer visible (agent cleared input field).
func paneChanged(before, after, injectedText string) bool {
	if after == before {
		return false
	}
	// If pane has more lines, agent is responding
	beforeLines := strings.Count(before, "\n")
	afterLines := strings.Count(after, "\n")
	if afterLines > beforeLines+1 {
		return true
	}
	// If the injected text appeared and then new content appeared after it
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
