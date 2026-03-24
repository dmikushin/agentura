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
		log.Printf("[agent-run] Warning: SSH auth failed: %v", err)
	} else if bearerToken != "" {
		log.Printf("[agent-run] Authenticated with SSH key")
	}

	if bearerToken == "" && delegationToken != "" {
		log.Printf("[agent-run] Using delegation token (AGENTURA_TOKEN)")
	} else if bearerToken == "" && delegationToken == "" {
		log.Printf("[agent-run] Warning: no auth available, proceeding without monitoring")
	}

	// --- Register with server ---
	hostname, _ := os.Hostname()
	cwd, _ := os.Getwd()

	payload := map[string]interface{}{
		"agent_name": filepath.Base(cmdName),
		"pane_id":    paneID,
		"pid":        os.Getpid(),
		"hostname":   hostname,
		"cwd":        cwd,
		"cmd":        args,
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
	deploySkills(monitorURL, authToken)

	// --- Clear nesting guards ---
	os.Unsetenv("CLAUDECODE")
	os.Unsetenv("CLAUDE_CODE_ENTRYPOINT")

	// --- Pre-trust cwd for Claude ---
	if cmdName == "claude" {
		ensureClaudeTrust(cwd)
	}

	// --- Set up IPC socket path ---
	sockPath := fmt.Sprintf("/tmp/agentura-sidecar-%d.sock", os.Getpid())
	os.Setenv("AGENTURA_SIDECAR_SOCK", sockPath)

	// --- Launch child subprocess, main goroutine becomes sidecar ---
	// Go can't fork() safely. Instead: start child as subprocess,
	// main process IS the sidecar.
	child := exec.Command(cmdPath, args[1:]...)
	child.Stdin = os.Stdin
	child.Stdout = os.Stdout
	child.Stderr = os.Stderr
	child.Env = os.Environ()
	// Set process group so child can be signaled independently
	child.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

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

func deploySkills(monitorURL, token string) {
	client := api.NewClient(monitorURL, token)

	resp, err := client.Get("/skills")
	if err != nil {
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

	cwdSkills := filepath.Join(".", ".claude", "commands")
	os.MkdirAll(cwdSkills, 0755)

	for _, s := range skills {
		skillName, ok := s.(string)
		if !ok {
			continue
		}
		dst := filepath.Join(cwdSkills, skillName)
		if _, err := os.Stat(dst); err == nil {
			continue // already exists
		}
		skillResp, err := client.Get("/skills/" + skillName)
		if err != nil {
			continue
		}
		content, _ := skillResp["content"].(string)
		if content != "" {
			os.WriteFile(dst, []byte(content), 0644)
			log.Printf("[agent-run] Deployed skill: %s", skillName)
		}
	}
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
	fmt.Fprintf(os.Stderr, "Error: "+format+"\n", args...)
	os.Exit(1)
}
