// agentura-clock — post-tool-call hook that prints server time + elapsed time.
//
// Called by Claude Code (postToolExecution) and Gemini CLI (AfterTool) hooks.
// Output goes to stdout and appears in the agent's context after each tool call.
//
// State file: /tmp/agentura-clock-<AGENT_PID>.state (stores last call timestamp)
// Timezone: fetched once from server, cached in state file.
//
// Output format: TIME NOW: 01:15PM (01m34s elapsed since last tool call)
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

type clockState struct {
	Timezone     string `json:"tz"`
	LastCallUnix int64  `json:"last_call"`
}

func main() {
	// Find our state file keyed by parent PID (the sidecar/agentura-run)
	// or by AGENT_ID for uniqueness
	agentID := os.Getenv("AGENT_ID")
	stateKey := agentID
	if stateKey == "" {
		stateKey = fmt.Sprintf("%d", os.Getppid())
	}
	// Sanitize for filename
	stateKey = strings.ReplaceAll(stateKey, "/", "_")
	stateKey = strings.ReplaceAll(stateKey, ":", "_")
	statePath := fmt.Sprintf("/tmp/agentura-clock-%s.state", stateKey)

	now := time.Now()

	// Load or initialize state
	var state clockState
	if data, err := os.ReadFile(statePath); err == nil {
		json.Unmarshal(data, &state)
	}

	// Fetch timezone from server if not cached
	if state.Timezone == "" {
		state.Timezone = fetchTimezone()
	}

	// Calculate elapsed since last tool call
	var elapsed time.Duration
	if state.LastCallUnix > 0 {
		elapsed = now.Sub(time.Unix(state.LastCallUnix, 0))
	}

	// Update last call timestamp
	state.LastCallUnix = now.Unix()

	// Save state
	if data, err := json.Marshal(state); err == nil {
		os.WriteFile(statePath, data, 0644)
	}

	// Format time in server timezone
	loc, err := time.LoadLocation(state.Timezone)
	if err != nil {
		loc = time.UTC
	}
	serverNow := now.In(loc)

	// Format elapsed
	elapsedStr := formatElapsed(elapsed)

	// Output
	fmt.Printf("TIME NOW: %s (%s elapsed since last tool call)\n",
		serverNow.Format("03:04PM"),
		elapsedStr)
}

func formatElapsed(d time.Duration) string {
	if d <= 0 {
		return "first call"
	}
	m := int(d.Minutes())
	s := int(d.Seconds()) % 60
	if m > 0 {
		return fmt.Sprintf("%02dm%02ds", m, s)
	}
	return fmt.Sprintf("%02ds", s)
}

func fetchTimezone() string {
	monitorURL := os.Getenv("AGENTURA_URL")
	if monitorURL == "" {
		return "UTC"
	}

	token := os.Getenv("AGENTURA_TOKEN")
	if token == "" {
		// Try reading from sidecar socket or env
		token = os.Getenv("AGENT_TOKEN")
	}

	url := strings.TrimSuffix(monitorURL, "/") + "/timezone"
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return "UTC"
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}

	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "UTC"
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "UTC"
	}

	var result map[string]string
	if err := json.Unmarshal(body, &result); err != nil {
		return "UTC"
	}

	if tz, ok := result["timezone"]; ok && tz != "" {
		return tz
	}
	return "UTC"
}

func init() {
	// Ensure we don't hang — hard timeout
	go func() {
		time.Sleep(2 * time.Second)
		os.Exit(0)
	}()
}

// getenvInt helper
func getenvInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}
