// Package tmux provides abstractions over tmux for AI-to-AI communication.
package tmux

import (
	"fmt"
	"os/exec"
	"time"
)

// CapturePane captures raw content from a tmux pane.
// Returns the captured text or an error.
func CapturePane(paneID string, lines int) (string, error) {
	ctx, cancel := timeoutContext(5 * time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, "tmux", "capture-pane", "-pt", paneID, "-S", fmt.Sprintf("-%d", lines))
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("capture-pane %s: %w", paneID, err)
	}
	return string(out), nil
}
