// agentura-clock — post-tool-call hook that prints server time + sprint status.
//
// Called by Claude Code (postToolExecution) and Gemini CLI (AfterTool) hooks.
// Also available as MCP tool /timenow for agents to call manually.
//
// Output format:
//   TIME NOW: 01:15PM (01m34s spent on this tool call, 24m21s since sprint start, 5m39s left till sprint end)
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

type clockState struct {
	Timezone     string `json:"tz"`
	LastCallUnix int64  `json:"last_call"`
	Team         string `json:"team"`
}

type sprintInfo struct {
	Start       float64 `json:"start"`
	DurationSec int     `json:"duration_sec"`
}

func main() {
	agentID := os.Getenv("AGENT_ID")
	stateKey := agentID
	if stateKey == "" {
		stateKey = fmt.Sprintf("%d", os.Getppid())
	}
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

	// Detect team from env if not cached
	if state.Team == "" {
		state.Team = os.Getenv("AGENTURA_TEAM")
	}

	// Time spent on this tool call (since last AfterTool fired)
	var toolDuration time.Duration
	if state.LastCallUnix > 0 {
		toolDuration = now.Sub(time.Unix(state.LastCallUnix, 0))
	}

	// Update last call timestamp
	state.LastCallUnix = now.Unix()

	// Save state
	if data, err := json.Marshal(state); err == nil {
		os.WriteFile(statePath, data, 0644)
	}

	// Format server time
	loc, err := time.LoadLocation(state.Timezone)
	if err != nil {
		loc = time.UTC
	}
	serverNow := now.In(loc)

	// Fetch sprint info
	sprintStr := fetchSprintStr(state.Team, now)

	// Tool call duration string
	toolStr := "first call"
	if toolDuration > 0 {
		toolStr = fmt.Sprintf("%s spent on this tool call", fmtDur(toolDuration))
	}

	// Output
	if sprintStr != "" {
		fmt.Printf("TIME NOW: %s (%s, %s)\n", serverNow.Format("03:04PM"), toolStr, sprintStr)
	} else {
		fmt.Printf("TIME NOW: %s (%s)\n", serverNow.Format("03:04PM"), toolStr)
	}
}

func fmtDur(d time.Duration) string {
	if d <= 0 {
		return "00s"
	}
	total := int(d.Seconds())
	m := total / 60
	s := total % 60
	if m > 0 {
		return fmt.Sprintf("%02dm%02ds", m, s)
	}
	return fmt.Sprintf("%02ds", s)
}

func fetchSprintStr(team string, now time.Time) string {
	if team == "" {
		return ""
	}
	monitorURL := os.Getenv("AGENTURA_URL")
	if monitorURL == "" {
		return ""
	}

	url := strings.TrimSuffix(monitorURL, "/") + "/sprint?team_name=" + team
	resp, err := httpGet(url)
	if err != nil {
		return ""
	}

	var result struct {
		Sprint *sprintInfo `json:"sprint"`
	}
	if err := json.Unmarshal(resp, &result); err != nil || result.Sprint == nil {
		return ""
	}

	sprintStart := time.Unix(int64(result.Sprint.Start), 0)
	sprintEnd := sprintStart.Add(time.Duration(result.Sprint.DurationSec) * time.Second)
	elapsed := now.Sub(sprintStart)
	remaining := sprintEnd.Sub(now)

	if remaining < 0 {
		return fmt.Sprintf("%s since sprint start, SPRINT OVERTIME by %s", fmtDur(elapsed), fmtDur(-remaining))
	}
	return fmt.Sprintf("%s since sprint start, %s left till sprint end", fmtDur(elapsed), fmtDur(remaining))
}

func fetchTimezone() string {
	monitorURL := os.Getenv("AGENTURA_URL")
	if monitorURL == "" {
		return "UTC"
	}
	url := strings.TrimSuffix(monitorURL, "/") + "/timezone"
	body, err := httpGet(url)
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

func httpGet(url string) ([]byte, error) {
	token := os.Getenv("AGENTURA_TOKEN")
	if token == "" {
		token = os.Getenv("AGENT_TOKEN")
	}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, err
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

func init() {
	// Hard timeout — never block the agent
	go func() {
		time.Sleep(2 * time.Second)
		os.Exit(0)
	}()
}
