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
	// -p: use bracketed paste mode so CLIs treat content as paste (not keystrokes)
	// This prevents \n inside multiline text from triggering submit
	if err := exec.CommandContext(ctx2, "tmux", "paste-buffer", "-p", "-t", paneID).Run(); err != nil {
		return err
	}

	// Wait for bracketed paste to complete and Gemini's bufferFastReturn (30ms) to expire
	time.Sleep(100 * time.Millisecond)
	// send-keys Enter = \r (0x0D) = ink 'return' = SUBMIT in both Claude and Gemini
	return sendKeys(paneID, "Enter")
}

func sendKeys(paneID string, keys ...string) error {
	ctx, cancel := timeoutContext(5 * time.Second)
	defer cancel()
	args := append([]string{"send-keys", "-t", paneID}, keys...)
	return exec.CommandContext(ctx, "tmux", args...).Run()
}
