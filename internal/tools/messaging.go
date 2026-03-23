package tools

import (
	"fmt"
	"strings"
)

// SendMessage sends a message to another agent via the server's message queue.
func (b *Backend) SendMessage(targetAgentID, message string, rsvp bool) string {
	if b.agentID == "" {
		return "Error: AGENT_ID env not set (not running under agent-run?)"
	}

	agent, err := b.resolveAgent(targetAgentID)
	if err != nil {
		return fmt.Sprintf("Error: %v", err)
	}

	fullMessage := fmt.Sprintf("Agent %s says to you: %s", b.agentID, message)

	// Gemini doesn't handle exclamation marks well
	agentName, _ := agent["name"].(string)
	if agentName == "gemini" {
		fullMessage = strings.ReplaceAll(fullMessage, "!", ".")
	}

	if rsvp {
		fullMessage += fmt.Sprintf("\n/rsvp %s", b.agentID)
	}

	resp, err := b.post("/sidecar/queue-message", map[string]interface{}{
		"agent_id": targetAgentID,
		"text":     fullMessage,
		"sender":   b.agentID,
	})
	if err != nil {
		return fmt.Sprintf("Error sending message: %v", err)
	}
	if status, _ := resp["status"].(string); status != "ok" {
		errMsg, _ := resp["error"].(string)
		return fmt.Sprintf("Error queuing message: %s", errMsg)
	}

	status := "sent"
	if rsvp {
		status += " (RSVP requested)"
	}
	return fmt.Sprintf("Message %s to %s", status, targetAgentID)
}

// InterruptAgent sends Escape to an agent's tmux pane to cancel its current operation.
func (b *Backend) InterruptAgent(targetAgentID string) string {
	_, err := b.resolveAgent(targetAgentID)
	if err != nil {
		return fmt.Sprintf("Error: %v", err)
	}

	resp, err := b.post("/sidecar/queue-message", map[string]interface{}{
		"agent_id": targetAgentID,
		"text":     "\x1b", // Escape character
		"sender":   "interrupt",
	})
	if err != nil {
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status != "ok" {
		errMsg, _ := resp["error"].(string)
		return fmt.Sprintf("Error: %s", errMsg)
	}

	return fmt.Sprintf("Escape sent to %s", targetAgentID)
}
