// agentura-run — agent launcher + sidecar process.
//
// Usage:
//
//	agentura-run --claude           (launch Claude with bypass permissions)
//	agentura-run --gemini           (launch Gemini with auto-accept)
//	agentura-run <command> [args]   (launch arbitrary command)
//
// Flow:
//  1. Parse args, check $TMUX_PANE
//  2. Authenticate: try SSH key first (local), fall back to AGENTURA_TOKEN (remote)
//  3. Register with server
//  4. Deploy skills
//  5. Launch child as subprocess, main goroutine becomes sidecar
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"


	"github.com/dmikushin/agentura/internal/api"
	"github.com/dmikushin/agentura/internal/auth"
	"github.com/dmikushin/agentura/internal/config"
	"github.com/dmikushin/agentura/internal/sidecar"
)

// Agent presets: --flag → (binary, [args...])
var agentPresets = map[string]struct {
	binary string
	args   []string
}{
	"--claude": {
		binary: "claude",
		args:   []string{"--dangerously-skip-permissions", "--permission-mode", "bypassPermissions"},
	},
	"--gemini": {
		binary: "gemini",
		args:   []string{"-y"},
	},
}

func main() {
	log.SetFlags(0)
	log.SetPrefix("")

	if len(os.Args) < 2 || os.Args[1] == "-h" || os.Args[1] == "--help" {
		presets := make([]string, 0, len(agentPresets))
		for k := range agentPresets {
			presets = append(presets, k)
		}
		fmt.Printf("Usage: agentura-run {%s | <command> [args...]}\n", strings.Join(presets, " | "))
		fmt.Println()
		fmt.Println("Agentura agent launcher — registers with the server, deploys")
		fmt.Println("skills, then launches agent as subprocess with sidecar.")
		fmt.Println()
		fmt.Println("Environment:")
		fmt.Println("  AGENTURA_URL    Server URL (required, e.g. https://agents.example.com)")
		fmt.Println("  AGENTURA_TOKEN  Delegation token (set automatically for remote agents)")
		if len(os.Args) >= 2 {
			os.Exit(0)
		}
		os.Exit(1)
	}

	// Log only to file — TUI agents (claude, gemini) need a clean terminal.
	// Logging to stderr corrupts the TUI before the agent can enter raw mode.
	logFile, err := os.OpenFile(fmt.Sprintf("agentura-run-%d.log", os.Getpid()), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
	if err == nil {
		log.SetOutput(logFile)
		defer logFile.Close()
	}
	log.SetFlags(log.Ldate | log.Ltime)

	log.Printf("[agent-run] Starting: %s", strings.Join(os.Args, " "))
	log.Printf("[agent-run] CWD: %s", func() string { d, _ := os.Getwd(); return d }())
	log.Printf("[agent-run] PID: %d", os.Getpid())
	log.Printf("[agent-run] AGENTURA_URL=%s", os.Getenv("AGENTURA_URL"))
	log.Printf("[agent-run] AGENTURA_TOKEN=%v", os.Getenv("AGENTURA_TOKEN") != "")

	// Load .env from cwd
	loadDotenv()

	monitorURL := config.MonitorURL()
	if monitorURL == "" {
		fatal("AGENTURA_URL not set and no default compiled in\n  Set AGENTURA_URL in env, .env file, or build with: make build")
	}

	// --- Resolve command ---
	firstArg := os.Args[1]
	var cmdName string
	var args []string

	if preset, ok := agentPresets[firstArg]; ok {
		cmdName = preset.binary
		args = append([]string{preset.binary}, preset.args...)
		args = append(args, os.Args[2:]...) // extra flags
	} else {
		cmdName = firstArg
		args = os.Args[1:]
	}

	// --- Check TMUX_PANE ---
	paneID := os.Getenv("TMUX_PANE")
	if paneID == "" {
		fatal("not inside a tmux session ($TMUX_PANE not set)")
	}

	// --- Check command exists ---
	cmdPath, err := exec.LookPath(cmdName)
	if err != nil {
		fatal("command '%s' not found in PATH", cmdName)
	}

	// --- Authenticate ---
	var bearerToken string
	delegationToken := os.Getenv("AGENTURA_TOKEN")

	bearerToken, err = auth.Authenticate(monitorURL)
	if err != nil {
		if delegationToken == "" {
			fatal("%v\nConfigure IdentityFile in ~/.ssh/config or start ssh-agent", err)
		}
		log.Printf("[agent-run] Warning: SSH auth failed: %v", err)
	} else if bearerToken != "" {
		log.Printf("[agent-run] Authenticated with SSH key")
	}

	if bearerToken == "" && delegationToken != "" {
		log.Printf("[agent-run] Using delegation token (AGENTURA_TOKEN)")
	} else if bearerToken == "" && delegationToken == "" {
		fatal("no authentication available — configure IdentityFile in ~/.ssh/config or start ssh-agent")
	}

	// --- Register with server ---
	hostname, _ := os.Hostname()
	cwd, _ := os.Getwd()

	bio := os.Getenv("AGENTURA_BIO")
	team := os.Getenv("AGENTURA_TEAM")

	payload := map[string]interface{}{
		"agent_name": filepath.Base(cmdName),
		"pane_id":    paneID,
		"pid":        os.Getpid(),
		"hostname":   hostname,
		"cwd":        cwd,
		"cmd":        args,
		"bio":        bio,
		"team":       team,
	}

	var agentToken, agentID string
	var registerURL string
	var authToken string

	if bearerToken != "" {
		registerURL = monitorURL + "/register"
		authToken = bearerToken
	} else if delegationToken != "" {
		registerURL = monitorURL + "/sidecar/register"
		authToken = delegationToken
	}

	if registerURL != "" {
		client := api.NewClient(monitorURL, authToken)
		// Use the correct endpoint path (strip base URL)
		path := strings.TrimPrefix(registerURL, monitorURL)
		resp, err := client.Post(path, payload)
		if err != nil {
			log.Printf("[agent-run] Warning: registration failed: %v", err)
		} else if status, _ := resp["status"].(string); status == "ok" {
			agentID, _ = resp["agent_id"].(string)
			if agentID == "" {
				agentID = fmt.Sprintf("%s@%s:%d", hostname, cwd, os.Getpid())
			}
			streamFile, _ := resp["stream_file"].(string)
			log.Printf("[agent-run] Registered as '%s' (pane %s), stream: %s",
				filepath.Base(cmdName), paneID, streamFile)

			os.Setenv("AGENT_ID", agentID)
			log.Printf("[agent-run] Agent ID saved to AGENT_ID env: %s", agentID)

			if tok, ok := resp["agent_token"].(string); ok && tok != "" {
				agentToken = tok
				os.Setenv("AGENT_TOKEN", agentToken)
				log.Printf("[agent-run] Agent token saved to AGENT_TOKEN env")
			}
		} else {
			log.Printf("[agent-run] Warning: server responded with %v", resp)
		}
	}

	if agentID == "" {
		agentID = fmt.Sprintf("%s@%s:%d", hostname, cwd, os.Getpid())
	}

	// --- Signal readiness via file (for tests and orchestration) ---
	if readyFile := os.Getenv("AGENTURA_READY_FILE"); readyFile != "" {
		if err := os.WriteFile(readyFile, []byte(agentID), 0644); err != nil {
			log.Printf("[agent-run] Warning: failed to write ready file %s: %v", readyFile, err)
		}
	}

	// --- Deploy skills ---
	deploySkills(cmdName, monitorURL, authToken)

	// --- Clear nesting guards ---
	os.Unsetenv("CLAUDECODE")
	os.Unsetenv("CLAUDE_CODE_ENTRYPOINT")

	// --- Set up IPC socket path ---
	sockPath := fmt.Sprintf("/tmp/agentura-sidecar-%d.sock", os.Getpid())
	os.Setenv("AGENTURA_SIDECAR_SOCK", sockPath)

	// --- Ensure MCP config and context ---
	// All env vars needed by agentura-mcp must be in the MCP config env,
	// NOT relying on env inheritance — Gemini's sanitizeEnvironment filters
	// vars matching /TOKEN/i, and Claude may also not pass all vars.
	// AGENT_TOKEN intentionally excluded — it expires after 1h and
	// cannot be refreshed from a static config. The MCP backend uses
	// sidecar IPC (which manages its own token rotation) or falls back
	// to SSH key auth for HTTPS.
	mcpEnv := map[string]string{
		"AGENTURA_URL":          monitorURL,
		"AGENTURA_SIDECAR_SOCK": sockPath,
		"AGENT_ID":              agentID,
	}
	switch cmdName {
	case "claude":
		ensureClaudeMCP(cwd, mcpEnv)
		ensureClaudeTrust(cwd)
		deployAgentContext(monitorURL, authToken, "CLAUDE.md", filepath.Join(cwd, ".claude", "CLAUDE.md"))
		ensureClaudeClockHook(cwd)
	case "gemini":
		ensureGeminiMCP(cwd, mcpEnv)
		ensureGeminiTrust(cwd)
		deployAgentContext(monitorURL, authToken, "GEMINI.md", filepath.Join(cwd, ".gemini", "GEMINI.md"))
		ensureGeminiClockHook(cwd)
	}

	// --- Launch child subprocess, main goroutine becomes sidecar ---
	// Go can't fork() safely. Instead: start child as subprocess,
	// main process IS the sidecar.
	child := exec.Command(cmdPath, args[1:]...)
	child.Stdin = os.Stdin
	child.Stdout = os.Stdout
	child.Stderr = os.Stderr
	child.Env = os.Environ()

	if err := child.Start(); err != nil {
		fatal("failed to start %s: %v", cmdName, err)
	}

	childPID := child.Process.Pid
	log.Printf("[agent-run] Started child PID %d (agent), parent PID %d (sidecar)",
		childPID, os.Getpid())

	if agentToken != "" {
		sc := sidecar.New(sidecar.Config{
			MonitorURL: monitorURL,
			Token:      agentToken,
			AgentID:    agentID,
			PaneID:     paneID,
			ChildPID:   childPID,
			SocketPath: sockPath,
			CmdPath:    cmdPath,
			CmdArgs:    args[1:],
			CmdName:    cmdName,
		})
		sc.Run()
	} else {
		log.Printf("[agent-run] No agent_token, sidecar disabled. Waiting for child to exit.")
		if err := child.Wait(); err != nil {
			if exitErr, ok := err.(*exec.ExitError); ok {
				log.Printf("[agent-run] Child exited with code %d", exitErr.ExitCode())
			} else {
				log.Printf("[agent-run] Child wait error: %v", err)
			}
		} else {
			log.Printf("[agent-run] Child exited with code 0")
		}
	}
}

func loadDotenv() {
	data, err := os.ReadFile(".env")
	if err != nil {
		return
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if idx := strings.Index(line, "="); idx > 0 {
			key := strings.TrimSpace(line[:idx])
			val := strings.TrimSpace(line[idx+1:])
			// setdefault behavior: don't overwrite existing
			if _, exists := os.LookupEnv(key); !exists {
				os.Setenv(key, val)
			}
		}
	}
}

func deploySkills(cmdName, monitorURL, token string) {
	client := api.NewClient(monitorURL, token)

	resp, err := client.Get("/skills")
	if err != nil {
		log.Printf("[agent-run] Warning: failed to fetch skills: %v", err)
		return
	}
	skillsRaw, ok := resp["skills"]
	if !ok {
		return
	}
	skills, ok := skillsRaw.([]interface{})
	if !ok || len(skills) == 0 {
		return
	}

	for _, s := range skills {
		skillName, ok := s.(string)
		if !ok {
			continue
		}
		skillResp, err := client.Get("/skills/" + skillName)
		if err != nil {
			continue
		}
		content, _ := skillResp["content"].(string)
		if content == "" {
			continue
		}

		switch cmdName {
		case "gemini":
			deployGeminiSkill(skillName, content)
		default:
			deployClaudeSkill(skillName, content)
		}
	}
}

func deployClaudeSkill(skillName, content string) {
	dir := filepath.Join(".", ".claude", "commands")
	os.MkdirAll(dir, 0755)
	dst := filepath.Join(dir, skillName)
	if _, err := os.Stat(dst); err == nil {
		return
	}
	os.WriteFile(dst, []byte(content), 0644)
	log.Printf("[agent-run] Deployed skill: %s", dst)
}

func deployGeminiSkill(skillName, content string) {
	// Strip .md extension for directory name
	name := strings.TrimSuffix(skillName, ".md")
	dir := filepath.Join(".", ".gemini", "skills", name)
	dst := filepath.Join(dir, "SKILL.md")
	if _, err := os.Stat(dst); err == nil {
		return
	}
	os.MkdirAll(dir, 0755)

	// Wrap content in SKILL.md format with YAML frontmatter
	skill := fmt.Sprintf("---\nname: %s\ndescription: Agentura skill — %s\n---\n\n%s", name, name, content)
	os.WriteFile(dst, []byte(skill), 0644)
	log.Printf("[agent-run] Deployed skill: %s", dst)
}

var agenturaServer = map[string]interface{}{
	"command": "agentura-mcp",
}

const agenturaContextMarker = "<!-- agentura-context -->"

func deployAgentContext(monitorURL, token, contextName, contextPath string) {
	// Check if marker already present
	if existing, err := os.ReadFile(contextPath); err == nil {
		if strings.Contains(string(existing), agenturaContextMarker) {
			return
		}
	}

	// Fetch context from server
	client := api.NewClient(monitorURL, token)
	resp, err := client.Get("/context/" + contextName)
	if err != nil {
		log.Printf("[agent-run] Warning: failed to fetch context %s: %v", contextName, err)
		return
	}
	content, _ := resp["content"].(string)
	if content == "" {
		return
	}

	dir := filepath.Dir(contextPath)
	os.MkdirAll(dir, 0755)

	f, err := os.OpenFile(contextPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		log.Printf("[agent-run] Warning: failed to write agent context: %v", err)
		return
	}
	defer f.Close()

	if _, err := f.WriteString(content); err != nil {
		log.Printf("[agent-run] Warning: failed to append agent context: %v", err)
		return
	}
	log.Printf("[agent-run] Agent context deployed to %s", contextPath)
}

func ensureClaudeMCP(cwd string, mcpEnv map[string]string) {
	mcpPath := filepath.Join(cwd, ".mcp.json")
	withFileLock(mcpPath, func() {
		entry := copyMap(agenturaServer)
		entry["env"] = mcpEnv

		mcpConfig := map[string]interface{}{
			"mcpServers": map[string]interface{}{
				"agentura": entry,
			},
		}

		raw, _ := json.MarshalIndent(mcpConfig, "", "  ")
		if err := os.WriteFile(mcpPath, raw, 0644); err != nil {
			log.Printf("[agent-run] Warning: failed to create .mcp.json: %v", err)
			return
		}
		log.Printf("[agent-run] Created MCP config: %s", mcpPath)
	})
}

func ensureGeminiMCP(cwd string, mcpEnv map[string]string) {
	geminiDir := filepath.Join(cwd, ".gemini")
	configPath := filepath.Join(geminiDir, "settings.json")
	os.MkdirAll(geminiDir, 0755)

	withFileLock(configPath, func() {
		var data map[string]interface{}
		if raw, err := os.ReadFile(configPath); err == nil {
			json.Unmarshal(raw, &data)
		}
		if data == nil {
			data = make(map[string]interface{})
		}

		servers, _ := data["mcpServers"].(map[string]interface{})
		if servers == nil {
			servers = make(map[string]interface{})
			data["mcpServers"] = servers
		}

		entry := copyMap(agenturaServer)
		entry["env"] = mcpEnv
		servers["agentura"] = entry

		raw, _ := json.MarshalIndent(data, "", "  ")
		if err := os.WriteFile(configPath, raw, 0644); err != nil {
			log.Printf("[agent-run] Warning: failed to create gemini MCP config: %v", err)
			return
		}
		log.Printf("[agent-run] Created MCP config: %s", configPath)
	})
}

func ensureGeminiTrust(cwd string) {
	geminiDir := filepath.Join(cwd, ".gemini")
	configPath := filepath.Join(geminiDir, "settings.json")

	var data map[string]interface{}
	if raw, err := os.ReadFile(configPath); err == nil {
		json.Unmarshal(raw, &data)
	}
	if data == nil {
		data = make(map[string]interface{})
	}

	// Ensure security.folderTrust.enabled = false so -y (YOLO) works
	security, _ := data["security"].(map[string]interface{})
	if security == nil {
		security = make(map[string]interface{})
		data["security"] = security
	}
	folderTrust, _ := security["folderTrust"].(map[string]interface{})
	if folderTrust == nil {
		folderTrust = make(map[string]interface{})
		security["folderTrust"] = folderTrust
	}

	if enabled, ok := folderTrust["enabled"].(bool); ok && !enabled {
		return // already disabled
	}
	folderTrust["enabled"] = false

	os.MkdirAll(geminiDir, 0755)
	raw, _ := json.MarshalIndent(data, "", "  ")
	os.WriteFile(configPath, raw, 0644)
	log.Printf("[agent-run] Disabled Gemini folderTrust in %s", configPath)
}

func copyMap(m map[string]interface{}) map[string]interface{} {
	cp := make(map[string]interface{}, len(m))
	for k, v := range m {
		cp[k] = v
	}
	return cp
}

// withFileLock acquires an exclusive flock on path+".lock", runs fn, then releases.
// Prevents concurrent agentura-run processes from racing on the same config file.
func withFileLock(path string, fn func()) {
	lockPath := path + ".lock"
	f, err := os.OpenFile(lockPath, os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		fn() // can't lock, run anyway
		return
	}
	defer f.Close()
	defer os.Remove(lockPath)

	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		fn() // can't lock, run anyway
		return
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)

	fn()
}

func ensureClaudeClockHook(cwd string) {
	// Claude Code uses .claude/settings.json for hooks
	dir := filepath.Join(cwd, ".claude")
	os.MkdirAll(dir, 0755)
	configPath := filepath.Join(dir, "settings.json")

	var data map[string]interface{}
	if raw, err := os.ReadFile(configPath); err == nil {
		json.Unmarshal(raw, &data)
	}
	if data == nil {
		data = make(map[string]interface{})
	}

	// Check if hook already exists
	if hooks, ok := data["hooks"].(map[string]interface{}); ok {
		if _, ok := hooks["postToolExecution"]; ok {
			return // already configured
		}
	}

	hooks, _ := data["hooks"].(map[string]interface{})
	if hooks == nil {
		hooks = make(map[string]interface{})
		data["hooks"] = hooks
	}

	hooks["postToolExecution"] = []interface{}{
		map[string]interface{}{
			"command": "agentura-clock",
			"timeout": 2000,
		},
	}

	raw, _ := json.MarshalIndent(data, "", "  ")
	if err := os.WriteFile(configPath, raw, 0644); err != nil {
		log.Printf("[agent-run] Warning: failed to write Claude clock hook: %v", err)
		return
	}
	log.Printf("[agent-run] Claude clock hook configured in %s", configPath)
}

func ensureGeminiClockHook(cwd string) {
	// Gemini CLI uses .gemini/settings.json with AfterTool hook
	dir := filepath.Join(cwd, ".gemini")
	configPath := filepath.Join(dir, "settings.json")

	var data map[string]interface{}
	if raw, err := os.ReadFile(configPath); err == nil {
		json.Unmarshal(raw, &data)
	}
	if data == nil {
		data = make(map[string]interface{})
	}

	// Check if hook already exists
	if hooks, ok := data["hooks"].(map[string]interface{}); ok {
		if _, ok := hooks["AfterTool"]; ok {
			return // already configured
		}
	}

	hooks, _ := data["hooks"].(map[string]interface{})
	if hooks == nil {
		hooks = make(map[string]interface{})
		data["hooks"] = hooks
	}

	hooks["AfterTool"] = []interface{}{
		map[string]interface{}{
			"matcher": "*",
			"hooks": []interface{}{
				map[string]interface{}{
					"name":    "agentura-clock",
					"type":    "command",
					"command": "agentura-clock",
					"timeout": 2000,
				},
			},
		},
	}

	os.MkdirAll(dir, 0755)
	raw, _ := json.MarshalIndent(data, "", "  ")
	if err := os.WriteFile(configPath, raw, 0644); err != nil {
		log.Printf("[agent-run] Warning: failed to write Gemini clock hook: %v", err)
		return
	}
	log.Printf("[agent-run] Gemini clock hook configured in %s", configPath)
}

func ensureClaudeTrust(cwd string) {
	configPath := filepath.Join(homeDir(), ".claude.json")

	var data map[string]interface{}
	if raw, err := os.ReadFile(configPath); err == nil {
		json.Unmarshal(raw, &data)
	}
	if data == nil {
		data = make(map[string]interface{})
	}

	projects, _ := data["projects"].(map[string]interface{})
	if projects == nil {
		projects = make(map[string]interface{})
		data["projects"] = projects
	}

	project, _ := projects[cwd].(map[string]interface{})
	if project == nil {
		project = make(map[string]interface{})
		projects[cwd] = project
	}

	if accepted, _ := project["hasTrustDialogAccepted"].(bool); accepted {
		return
	}

	project["hasTrustDialogAccepted"] = true

	raw, _ := json.MarshalIndent(data, "", "  ")
	tmpPath := configPath + ".tmp"
	if err := os.WriteFile(tmpPath, raw, 0644); err != nil {
		log.Printf("[agent-run] Warning: failed to set trust for %s: %v", cwd, err)
		return
	}
	if err := os.Rename(tmpPath, configPath); err != nil {
		log.Printf("[agent-run] Warning: failed to set trust for %s: %v", cwd, err)
		return
	}
	log.Printf("[agent-run] Trusted directory: %s", cwd)
}

func homeDir() string {
	if h := os.Getenv("HOME"); h != "" {
		return h
	}
	if runtime.GOOS == "windows" {
		return os.Getenv("USERPROFILE")
	}
	return "/"
}

func fatal(format string, args ...interface{}) {
	msg := fmt.Sprintf("Error: "+format, args...)
	log.Println(msg)
	fmt.Fprintln(os.Stderr, msg)
	os.Exit(1)
}
