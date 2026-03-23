package tmux

import (
	"os/exec"
	"time"
)

// Inject sends text into a tmux pane.
// Uses load-buffer + paste-buffer for reliable text input.
// For escape sequences, uses send-keys directly.
func Inject(paneID, text string) error {
	if text == "\x1b" {
		return sendKeys(paneID, "Escape")
	}

	// Paste text via tmux buffer (reliable for arbitrary content)
	ctx1, cancel1 := timeoutContext(5 * time.Second)
	defer cancel1()
	cmd := exec.CommandContext(ctx1, "tmux", "load-buffer", "-")
	cmd.Stdin = stringReader(text)
	if err := cmd.Run(); err != nil {
		return err
	}

	ctx2, cancel2 := timeoutContext(5 * time.Second)
	defer cancel2()
	if err := exec.CommandContext(ctx2, "tmux", "paste-buffer", "-t", paneID).Run(); err != nil {
		return err
	}

	// Press Enter separately
	time.Sleep(50 * time.Millisecond)
	return sendKeys(paneID, "Enter")
}

func sendKeys(paneID string, keys ...string) error {
	ctx, cancel := timeoutContext(5 * time.Second)
	defer cancel()
	args := append([]string{"send-keys", "-t", paneID}, keys...)
	return exec.CommandContext(ctx, "tmux", args...).Run()
}
