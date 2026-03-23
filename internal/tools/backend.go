// Package tools implements all MCP tool backends for agentura.
//
// Each tool function communicates with the agentura server via:
//  1. Sidecar IPC (Unix socket) — preferred, if AGENTURA_SIDECAR_SOCK is set
//  2. Direct HTTPS — fallback
package tools

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/dmikushin/agentura/internal/api"
	"github.com/dmikushin/agentura/internal/auth"
	"github.com/dmikushin/agentura/internal/sidecar"
)

// Backend holds shared state for all tool implementations.
type Backend struct {
	monitorURL  string
	sidecarSock string
	agentID     string
	agentToken  string
	authToken   string
	cursors     map[string]int
	hostsPath   string
	agentPresets map[string]bool
}

// NewBackend creates a new tool backend from environment variables.
func NewBackend() (*Backend, error) {
	monitorURL := os.Getenv("AGENTURA_URL")
	if monitorURL == "" {
		return nil, fmt.Errorf("AGENTURA_URL environment variable is required")
	}
	dataDir := os.Getenv("AGENTURA_DATA_DIR")
	if dataDir == "" {
		dataDir = "/data"
	}
	hostsPath := os.Getenv("HOSTS_REGISTRY_PATH")
	if hostsPath == "" {
		hostsPath = filepath.Join(dataDir, "hosts.json")
	}

	return &Backend{
		monitorURL:  monitorURL,
		sidecarSock: os.Getenv("AGENTURA_SIDECAR_SOCK"),
		agentID:     os.Getenv("AGENT_ID"),
		agentToken:  os.Getenv("AGENT_TOKEN"),
		cursors:     make(map[string]int),
		hostsPath:   hostsPath,
		agentPresets: map[string]bool{"claude": true, "gemini": true},
	}, nil
}

// get performs a GET request, trying sidecar IPC first, then direct HTTPS.
func (b *Backend) get(path string) (map[string]interface{}, error) {
	if b.sidecarSock != "" {
		resp, err := sidecar.Request(b.sidecarSock, "GET", path, nil)
		if err == nil {
			return resp, nil
		}
		// Fall through to direct HTTP
	}

	client := b.httpClient()
	for attempt := 0; attempt < 2; attempt++ {
		resp, err := client.Get(path)
		if err != nil {
			if httpErr, ok := err.(*api.HTTPError); ok && httpErr.StatusCode == 401 && attempt == 0 {
				b.refreshToken()
				client = b.httpClient()
				continue
			}
			return nil, err
		}
		return resp, nil
	}
	return nil, fmt.Errorf("GET %s failed after retry", path)
}

// post performs a POST request, trying sidecar IPC first, then direct HTTPS.
func (b *Backend) post(path string, data map[string]interface{}) (map[string]interface{}, error) {
	if b.sidecarSock != "" {
		resp, err := sidecar.Request(b.sidecarSock, "POST", path, data)
		if err == nil {
			return resp, nil
		}
		// Fall through to direct HTTP
	}

	// Direct HTTP fallback: replace _inject_agent_token with real token
	if data != nil {
		if _, ok := data["_inject_agent_token"]; ok {
			delete(data, "_inject_agent_token")
			data["agent_token"] = b.agentToken
		}
	}

	client := b.httpClient()
	for attempt := 0; attempt < 2; attempt++ {
		resp, err := client.Post(path, data)
		if err != nil {
			if httpErr, ok := err.(*api.HTTPError); ok && httpErr.StatusCode == 401 && attempt == 0 {
				b.refreshToken()
				client = b.httpClient()
				continue
			}
			return nil, err
		}
		return resp, nil
	}
	return nil, fmt.Errorf("POST %s failed after retry", path)
}

func (b *Backend) httpClient() *api.Client {
	token := b.authToken
	if token == "" {
		token = b.agentToken
	}
	return api.NewClient(b.monitorURL, token)
}

func (b *Backend) refreshToken() {
	tok, err := auth.Authenticate(b.monitorURL)
	if err == nil && tok != "" {
		b.authToken = tok
	}
	if envTok := os.Getenv("AGENT_TOKEN"); envTok != "" {
		b.agentToken = envTok
	}
	if envID := os.Getenv("AGENT_ID"); envID != "" {
		b.agentID = envID
	}
}

func (b *Backend) resolveAgent(agentID string) (map[string]interface{}, error) {
	data, err := b.get("/agents")
	if err != nil {
		return nil, fmt.Errorf("agentura server is not running")
	}
	agents, _ := data["agents"].([]interface{})
	for _, a := range agents {
		agent, ok := a.(map[string]interface{})
		if !ok {
			continue
		}
		if agent["agent_id"] == agentID {
			return agent, nil
		}
	}
	return nil, fmt.Errorf("agent '%s' not found (use list_agents to see available agents)", agentID)
}

func formatAgent(a map[string]interface{}) string {
	agentID, _ := a["agent_id"].(string)
	name, _ := a["name"].(string)
	paneID, _ := a["pane_id"].(string)
	startedAt, _ := a["started_at"].(string)
	host, _ := a["hostname"].(string)
	cmdSlice, _ := a["cmd"].([]interface{})
	var cmdParts []string
	for _, c := range cmdSlice {
		if s, ok := c.(string); ok {
			cmdParts = append(cmdParts, s)
		}
	}
	cmd := strings.Join(cmdParts, " ")
	return fmt.Sprintf("- **%s** — %s (pane %s, host %s, since %s)\n  cmd: `%s`",
		agentID, name, paneID, host, startedAt, cmd)
}

func (b *Backend) loadHostRegistry() map[string]interface{} {
	data, err := os.ReadFile(b.hostsPath)
	if err != nil {
		return nil
	}
	var hosts map[string]interface{}
	if err := json.Unmarshal(data, &hosts); err != nil {
		return nil
	}
	return hosts
}

func (b *Backend) getAgentTeams(agentID string) []string {
	data, err := b.get("/agents")
	if err != nil {
		return nil
	}
	agents, _ := data["agents"].([]interface{})
	for _, a := range agents {
		agent, ok := a.(map[string]interface{})
		if !ok {
			continue
		}
		if agent["agent_id"] == agentID {
			teamsRaw, _ := agent["teams"].([]interface{})
			var teams []string
			for _, t := range teamsRaw {
				if s, ok := t.(string); ok {
					teams = append(teams, s)
				}
			}
			return teams
		}
	}
	return nil
}

func (b *Backend) waitForRegistration(paneID string, blocking bool) string {
	if !blocking {
		return ""
	}
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		time.Sleep(1 * time.Second)
		data, err := b.get("/agents")
		if err != nil {
			continue
		}
		agents, _ := data["agents"].([]interface{})
		for _, a := range agents {
			agent, ok := a.(map[string]interface{})
			if !ok {
				continue
			}
			if agent["pane_id"] == paneID {
				id, _ := agent["agent_id"].(string)
				return id
			}
		}
	}
	return ""
}

func (b *Backend) handleTeamAssignment(newAgentID, team, senderAgentID string) string {
	if newAgentID == "" || b.agentToken == "" {
		return ""
	}

	teamMsg := ""
	teamName := team

	if teamName == "" && senderAgentID != "" {
		senderTeams := b.getAgentTeams(senderAgentID)
		if len(senderTeams) > 0 {
			teamName = senderTeams[0]
		} else {
			teamName = fmt.Sprintf("team-%d", time.Now().Unix())
			b.post("/teams", map[string]interface{}{
				"name":                teamName,
				"_inject_agent_token": true,
			})
			teamMsg = fmt.Sprintf(", new team '%s' created", teamName)
		}
	}

	if teamName != "" {
		resp, err := b.post("/api/auth/agent-token", map[string]interface{}{
			"agent_id": newAgentID,
		})
		if err == nil {
			newToken, _ := resp["agent_token"].(string)
			if newToken != "" {
				b.post("/teams/request-join", map[string]interface{}{
					"team":        teamName,
					"agent_token": newToken,
					"message":     fmt.Sprintf("Auto-created by %s", senderAgentID),
				})
				b.post("/teams/approve", map[string]interface{}{
					"team":                teamName,
					"pending_agent_id":    newAgentID,
					"_inject_agent_token": true,
				})
				if teamMsg == "" {
					teamMsg = fmt.Sprintf(", joined team '%s'", teamName)
				}
			}
		}
		if teamMsg == "" {
			teamMsg = fmt.Sprintf(", team join pending for '%s'", teamName)
		}
	}

	return teamMsg
}

func sshRun(host, cmd string, timeout time.Duration) (string, string, error) {
	ctx, cancel := timeoutCtx(timeout)
	defer cancel()
	c := exec.CommandContext(ctx, "ssh", "-o", "StrictHostKeyChecking=accept-new", host, cmd)
	var stdout, stderr strings.Builder
	c.Stdout = &stdout
	c.Stderr = &stderr
	err := c.Run()
	return stdout.String(), stderr.String(), err
}

func urlEncode(s string) string {
	return url.QueryEscape(s)
}
