package tools

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// ListAgents returns a formatted list of all registered agents.
func (b *Backend) ListAgents() string {
	data, err := b.get("/agents")
	if err != nil {
		return fmt.Sprintf("Error: agentura server is not running (cannot connect to %s)", b.monitorURL)
	}

	agents, _ := data["agents"].([]interface{})
	if len(agents) == 0 {
		return "No agents currently registered."
	}

	var lines []string
	for _, a := range agents {
		if agent, ok := a.(map[string]interface{}); ok {
			lines = append(lines, formatAgent(agent))
		}
	}
	return fmt.Sprintf("%d agent(s) connected:\n\n%s", len(agents), strings.Join(lines, "\n"))
}

// ListHosts returns available hosts including remote hosts from hosts.json.
func (b *Backend) ListHosts() string {
	hostname, _ := os.Hostname()
	lines := []string{fmt.Sprintf("- **%s** (local)", hostname)}

	hosts := b.loadHostRegistry()
	for name, infoRaw := range hosts {
		if name == hostname {
			continue
		}
		info, ok := infoRaw.(map[string]interface{})
		if !ok {
			continue
		}
		parts := []string{fmt.Sprintf("**%s**", name)}

		if tagsRaw, ok := info["tags"].([]interface{}); ok && len(tagsRaw) > 0 {
			var tags []string
			for _, t := range tagsRaw {
				if s, ok := t.(string); ok {
					tags = append(tags, s)
				}
			}
			if len(tags) > 0 {
				parts = append(parts, "tags: "+strings.Join(tags, ", "))
			}
		}
		if gpu, ok := info["gpu"].(string); ok && gpu != "" {
			parts = append(parts, "GPU: "+gpu)
		}
		if cpu, ok := info["cpu_count"].(float64); ok && cpu > 0 {
			parts = append(parts, fmt.Sprintf("CPUs: %d", int(cpu)))
		}
		if notes, ok := info["notes"].(string); ok && notes != "" {
			parts = append(parts, notes)
		}
		lines = append(lines, "- "+strings.Join(parts, " | "))
	}

	return fmt.Sprintf("%d host(s) available:\n\n%s", len(lines), strings.Join(lines, "\n"))
}

// ReadStream reads new output from an agent's stream since the last read.
func (b *Backend) ReadStream(agentID string) string {
	agent, err := b.resolveAgent(agentID)
	if err != nil {
		return fmt.Sprintf("Error: %v", err)
	}
	paneID, _ := agent["pane_id"].(string)

	offset := b.cursors[agentID]
	encoded := urlEncode(paneID)

	resp, err := b.get(fmt.Sprintf("/stream/%s?offset=%d", encoded, offset))
	if err != nil {
		return "Error: agentura server is not running"
	}

	content, _ := resp["content"].(string)
	nextOffset := offset
	if no, ok := resp["next_offset"].(float64); ok {
		nextOffset = int(no)
	}
	b.cursors[agentID] = nextOffset

	if strings.TrimSpace(content) == "" {
		return "(no new content)"
	}
	return content
}

// CreateAgent creates a new AI agent in a tmux window (local or remote).
func (b *Backend) CreateAgent(hostname, cwd, agentType string, blocking bool, team string) string {
	senderAgentID := b.agentID

	if !b.agentPresets[agentType] {
		var types []string
		for k := range b.agentPresets {
			types = append(types, k)
		}
		return fmt.Sprintf("Error: unknown agent type '%s', expected one of: %s", agentType, strings.Join(types, ", "))
	}

	localHostname, _ := os.Hostname()
	if hostname == localHostname {
		return b.createLocalAgent(hostname, cwd, agentType, blocking, team, senderAgentID)
	}

	hostRegistry := b.loadHostRegistry()
	if hostRegistry == nil {
		return fmt.Sprintf("Error: host '%s' not found in host registry. Available: %s", hostname, localHostname)
	}
	if _, ok := hostRegistry[hostname]; !ok {
		var available []string
		available = append(available, localHostname)
		for k := range hostRegistry {
			available = append(available, k)
		}
		return fmt.Sprintf("Error: host '%s' not found in host registry. Available: %s",
			hostname, strings.Join(available, ", "))
	}

	return b.createRemoteAgent(hostname, cwd, agentType, blocking, team, senderAgentID)
}

func (b *Backend) createLocalAgent(hostname, cwd, agentType string, blocking bool, team, senderAgentID string) string {
	shellCmd := fmt.Sprintf("cd %s && AGENTURA_URL=%s exec agent-run --%s",
		shellQuote(cwd), shellQuote(b.monitorURL), agentType)

	result, err := exec.Command("tmux", "new-window", "-P", "-F", "#{pane_id}", "-n", agentType, shellCmd).Output()
	if err != nil {
		return fmt.Sprintf("Error: failed to create tmux window: %v", err)
	}

	paneID := strings.TrimSpace(string(result))
	newAgentID := b.waitForRegistration(paneID, blocking)
	teamMsg := b.handleTeamAssignment(newAgentID, team, senderAgentID)

	if newAgentID != "" {
		data, err := b.get("/agents")
		if err == nil {
			agents, _ := data["agents"].([]interface{})
			for _, a := range agents {
				agent, ok := a.(map[string]interface{})
				if !ok {
					continue
				}
				if agent["agent_id"] == newAgentID {
					return fmt.Sprintf("Agent created%s:\n\n%s", teamMsg, formatAgent(agent))
				}
			}
		}
	}

	if !blocking {
		return fmt.Sprintf("Agent '%s' launched in pane %s (non-blocking, use list_agents to check)", agentType, paneID)
	}
	return fmt.Sprintf("Warning: agent launched in pane %s but not registered after 30s", paneID)
}

func (b *Backend) createRemoteAgent(hostname, cwd, agentType string, blocking bool, team, senderAgentID string) string {
	// Step 0: Deploy Go binaries to remote host
	if err := b.ensureRemoteAgentura(hostname); err != nil {
		return fmt.Sprintf("Error: remote setup failed (%s): %v", hostname, err)
	}

	// Step 0.5: Ensure .mcp.json in cwd
	if err := b.ensureRemoteMCPConfig(hostname, cwd); err != nil {
		return fmt.Sprintf("Error: remote MCP config failed (%s): %v", hostname, err)
	}

	// Step 1: Create delegation token
	if b.agentToken == "" {
		return "Error: no AGENT_TOKEN available (agent not registered?)"
	}

	resp, err := b.post("/api/auth/delegate", map[string]interface{}{
		"target_host":         hostname,
		"_inject_agent_token": true,
	})
	if err != nil {
		return fmt.Sprintf("Error: delegation token creation failed: %v", err)
	}
	if status, _ := resp["status"].(string); status != "ok" {
		errMsg, _ := resp["error"].(string)
		return fmt.Sprintf("Error: failed to create delegation token: %s", errMsg)
	}
	delegationToken, _ := resp["delegation_token"].(string)

	// Step 2: SSH to remote host and launch agent
	windowCmd := fmt.Sprintf("AGENTURA_URL=%s AGENTURA_TOKEN=%s exec agent-run --%s",
		shellQuote(b.monitorURL), shellQuote(delegationToken), agentType)
	remoteCmd := fmt.Sprintf("tmux new-window -c %s -P -F '#{pane_id}' -n %s %s",
		shellQuote(cwd), shellQuote(agentType), shellQuote(windowCmd))

	_, stderr, err := sshRun(hostname, remoteCmd, 15*time.Second)
	if err != nil {
		return fmt.Sprintf("Error: SSH command failed: %s", strings.TrimSpace(stderr))
	}

	// Step 3: Wait for remote agent to register
	var newAgentID string
	if blocking {
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
				if agent["hostname"] == hostname {
					newAgentID, _ = agent["agent_id"].(string)
					break
				}
			}
			if newAgentID != "" {
				break
			}
		}
	}

	teamMsg := b.handleTeamAssignment(newAgentID, team, senderAgentID)

	if newAgentID != "" {
		data, err := b.get("/agents")
		if err == nil {
			agents, _ := data["agents"].([]interface{})
			for _, a := range agents {
				agent, ok := a.(map[string]interface{})
				if !ok {
					continue
				}
				if agent["agent_id"] == newAgentID {
					return fmt.Sprintf("Remote agent created on %s%s:\n\n%s", hostname, teamMsg, formatAgent(agent))
				}
			}
		}
	}

	if !blocking {
		return fmt.Sprintf("Remote agent '%s' launched on %s (non-blocking, use list_agents to check)", agentType, hostname)
	}
	return fmt.Sprintf("Warning: remote agent launched on %s but not registered after 30s", hostname)
}

// remoteArchSuffix detects the remote host architecture via `uname -m`
// and returns the Go binary suffix ("linux-amd64" or "linux-arm64").
func remoteArchSuffix(hostname string) (string, error) {
	stdout, _, err := sshRun(hostname, "uname -m", 10*time.Second)
	if err != nil {
		return "", fmt.Errorf("cannot detect remote arch: %v", err)
	}
	arch := strings.TrimSpace(stdout)
	switch arch {
	case "x86_64":
		return "linux-amd64", nil
	case "aarch64":
		return "linux-arm64", nil
	default:
		return "", fmt.Errorf("unsupported remote architecture: %s", arch)
	}
}

// findBinDir locates the directory containing cross-compiled Go binaries.
// Checks: AGENTURA_BIN_DIR env → ./bin/ next to the running binary → ./bin/ in cwd.
func findBinDir() string {
	if d := os.Getenv("AGENTURA_BIN_DIR"); d != "" {
		return d
	}
	if self, err := os.Executable(); err == nil {
		candidate := filepath.Join(filepath.Dir(self), "..", "bin")
		if info, err := os.Stat(candidate); err == nil && info.IsDir() {
			return candidate
		}
		// Also check sibling bin/
		candidate = filepath.Join(filepath.Dir(self))
		if info, err := os.Stat(candidate); err == nil && info.IsDir() {
			return candidate
		}
	}
	return "bin"
}

// binaries that need to be deployed to remote hosts.
var remoteBinaries = []string{"agentura-run", "agentura-mcp", "agentura-mcp-backend"}

func (b *Backend) ensureRemoteAgentura(hostname string) error {
	// Check if already installed (any of the 3 binaries)
	stdout, _, err := sshRun(hostname, "which agentura-run 2>/dev/null && agentura-run --help >/dev/null 2>&1 && echo ok", 10*time.Second)
	if err == nil && strings.Contains(stdout, "ok") {
		return nil // already installed and working
	}

	// Detect remote architecture
	archSuffix, err := remoteArchSuffix(hostname)
	if err != nil {
		return err
	}

	binDir := findBinDir()

	// Ensure ~/.local/bin exists on remote
	sshRun(hostname, "mkdir -p ~/.local/bin", 10*time.Second)

	// Deploy each binary via scp
	for _, name := range remoteBinaries {
		localPath := filepath.Join(binDir, name+"-"+archSuffix)
		if _, err := os.Stat(localPath); err != nil {
			return fmt.Errorf("binary not found: %s (run 'make %s' first)", localPath, archSuffix)
		}

		remotePath := fmt.Sprintf("%s:~/.local/bin/%s", hostname, name)
		cmd := exec.Command("scp", "-o", "StrictHostKeyChecking=accept-new", localPath, remotePath)
		if out, err := cmd.CombinedOutput(); err != nil {
			return fmt.Errorf("scp %s failed: %s", name, strings.TrimSpace(string(out)))
		}

		// Make executable
		sshRun(hostname, fmt.Sprintf("chmod +x ~/.local/bin/%s", name), 10*time.Second)
	}

	// Verify it works
	stdout, _, err = sshRun(hostname, "~/.local/bin/agentura-run --help >/dev/null 2>&1 && echo ok", 10*time.Second)
	if err != nil || !strings.Contains(stdout, "ok") {
		return fmt.Errorf("deployed binaries don't work on remote host")
	}

	return nil
}

func (b *Backend) ensureRemoteMCPConfig(hostname, cwd string) error {
	mcpConfig, _ := json.Marshal(map[string]interface{}{
		"mcpServers": map[string]interface{}{
			"agentura": map[string]interface{}{
				"command": "agentura-mcp",
				"env": map[string]string{
					"AGENTURA_URL": b.monitorURL,
				},
			},
		},
	})

	cmd := fmt.Sprintf("test -f %s/.mcp.json || echo %s > %s/.mcp.json",
		shellQuote(cwd), shellQuote(string(mcpConfig)), shellQuote(cwd))
	_, stderr, err := sshRun(hostname, cmd, 10*time.Second)
	if err != nil {
		return fmt.Errorf("failed to create .mcp.json: %s", strings.TrimSpace(stderr))
	}
	return nil
}

func shellQuote(s string) string {
	// Simple single-quote escaping
	return "'" + strings.ReplaceAll(s, "'", "'\\''") + "'"
}
