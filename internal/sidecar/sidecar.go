package sidecar

import (
	"crypto/md5"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/dmikushin/agentura/internal/api"
	"github.com/dmikushin/agentura/internal/tmux"
)

const (
	// HeartbeatInterval is the time between sidecar loop iterations.
	HeartbeatInterval = 2 * time.Second
	// TokenRefreshInterval is how often to refresh the agent token (45 min).
	TokenRefreshInterval = 45 * time.Minute
)

// Sidecar monitors a local agent and communicates with the central server.
type Sidecar struct {
	client     *api.Client
	agentID    string
	paneID     string
	childPID   int
	socketPath string

	// Child command info (for restarting)
	cmdPath string
	cmdArgs []string
	cmdName string

	restarting       bool
	rateLimited      bool // already notified about rate limit
	prevHashes       map[string]bool
	hashHistory      []string
	lastTokenRefresh time.Time
	listener         *Listener
}

// Config holds the configuration for creating a new Sidecar.
type Config struct {
	MonitorURL string
	Token      string
	AgentID    string
	PaneID     string
	ChildPID   int
	SocketPath string
	CmdPath    string   // full path to child executable
	CmdArgs    []string // child args (without binary name)
	CmdName    string   // "claude" or "gemini"
}

// New creates a new Sidecar instance.
func New(cfg Config) *Sidecar {
	return &Sidecar{
		client:           api.NewClient(cfg.MonitorURL, cfg.Token),
		agentID:          cfg.AgentID,
		paneID:           cfg.PaneID,
		childPID:         cfg.ChildPID,
		socketPath:       cfg.SocketPath,
		cmdPath:          cfg.CmdPath,
		cmdArgs:          cfg.CmdArgs,
		cmdName:          cfg.CmdName,
		prevHashes:       make(map[string]bool),
		lastTokenRefresh: time.Now(),
	}
}

// Run starts the main sidecar loop: capture, push, heartbeat, poll messages.
func (s *Sidecar) Run() {
	// Set up signal handlers
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGHUP)

	go func() {
		sig := <-sigCh
		log.Printf("[sidecar] Signal %v, sending final heartbeat", sig)
		s.heartbeat(false)
		if s.listener != nil {
			s.listener.Close()
		}
		os.Exit(0)
	}()

	// Start IPC listener
	if s.socketPath != "" {
		ln, err := NewListener(s.socketPath)
		if err != nil {
			log.Printf("[sidecar] Warning: IPC listener failed: %v", err)
		} else {
			s.listener = ln
			log.Printf("[sidecar] IPC listening on %s", s.socketPath)
		}
	}

	log.Printf("[sidecar] Started for agent %s (child PID %d)", s.agentID, s.childPID)

	for {
		childAlive := s.pidAlive(s.childPID)

		// Capture and push stream content
		content, err := tmux.CapturePane(s.paneID, 200)
		if err == nil && content != "" {
			// Detect rate limit / quota exhaustion
			s.checkRateLimit(content)

			if newContent := s.dedup(content); newContent != "" {
				cleaned := tmux.TUIToMd(newContent)
				if cleaned != "" {
					s.pushStream(cleaned)
				}
			}
		}

		// Heartbeat — check for restart signal
		resp := s.heartbeat(childAlive)
		if resp != nil {
			if action, _ := resp["action"].(string); action == "restart" {
				resumeID, _ := resp["resume_session_id"].(string)
				log.Printf("[sidecar] Restart requested for %s (resume: %q)", s.agentID, resumeID)
				s.doRestart(resumeID)
				continue
			}
		}

		// Poll and inject messages
		messages := s.pollMessages()
		if len(messages) > 0 {
			log.Printf("[sidecar] Received %d message(s) from server for %s", len(messages), s.agentID)
		}
		for i, msg := range messages {
			if i > 0 {
				time.Sleep(300 * time.Millisecond)
			}
			text, _ := msg["text"].(string)
			sender, _ := msg["sender"].(string)
			if text != "" {
				log.Printf("[sidecar] Injecting message from %s into pane %s (%d chars)", sender, s.paneID, len(text))
				if err := tmux.Inject(s.paneID, text); err != nil {
					log.Printf("[sidecar] ERROR: tmux.Inject failed for pane %s: %v — MESSAGE LOST", s.paneID, err)
				}
			} else {
				log.Printf("[sidecar] WARNING: empty message text from %s, skipping", sender)
			}
		}

		// Token refresh
		s.maybeRefreshToken()

		if !childAlive {
			if s.restarting {
				s.restarting = false
				continue // restart in progress, don't exit
			}
			log.Printf("[sidecar] Child PID %d exited, shutting down", s.childPID)
			break
		}

		// Process IPC requests (also serves as sleep between iterations)
		if s.listener != nil {
			s.listener.ProcessPending(s.proxy, HeartbeatInterval)
		} else {
			time.Sleep(HeartbeatInterval)
		}
	}

	// Final cleanup
	s.heartbeat(false)
	if s.listener != nil {
		s.listener.Close()
	}
}

func (s *Sidecar) pidAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	proc, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	// Signal 0 checks if process exists
	err = proc.Signal(syscall.Signal(0))
	if err == nil {
		return true
	}
	// EPERM means process exists but we can't signal it
	if err == syscall.EPERM {
		return true
	}
	return false
}

func (s *Sidecar) dedup(content string) string {
	lines := splitLines(content)
	var hashes []string
	var newLines []string

	for i, line := range lines {
		context := ""
		if i > 0 {
			context = lines[i-1]
		}
		h := fmt.Sprintf("%x", md5.Sum([]byte(context+"|"+line)))[:12]
		hashes = append(hashes, h)
		if !s.prevHashes[h] {
			newLines = append(newLines, line)
		}
	}

	// Update hash set (keep only last 500)
	s.prevHashes = make(map[string]bool)
	start := 0
	if len(hashes) > 500 {
		start = len(hashes) - 500
	}
	s.hashHistory = hashes[start:]
	for _, h := range s.hashHistory {
		s.prevHashes[h] = true
	}

	if len(newLines) == 0 {
		return ""
	}
	return joinLines(newLines)
}

// doRestart gracefully stops the child process and restarts it with --resume.
func (s *Sidecar) doRestart(resumeID string) {
	s.restarting = true

	// Send SIGINT twice (graceful shutdown, like double Ctrl-C)
	if proc, err := os.FindProcess(s.childPID); err == nil {
		log.Printf("[sidecar] Sending SIGINT to child PID %d", s.childPID)
		proc.Signal(syscall.SIGINT)
		time.Sleep(500 * time.Millisecond)
		proc.Signal(syscall.SIGINT)
	}

	// Wait for child to exit (up to 15 seconds)
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if !s.pidAlive(s.childPID) {
			break
		}
		time.Sleep(500 * time.Millisecond)
	}

	// SIGKILL fallback
	if s.pidAlive(s.childPID) {
		log.Printf("[sidecar] Child did not exit after SIGINT, sending SIGKILL")
		if proc, err := os.FindProcess(s.childPID); err == nil {
			proc.Signal(syscall.SIGKILL)
		}
		time.Sleep(1 * time.Second)
	}

	// Capture resume UUID from pane if not provided
	if resumeID == "" {
		resumeID = s.captureResumeID()
	}

	// Build new args with --resume
	newArgs := make([]string, len(s.cmdArgs))
	copy(newArgs, s.cmdArgs)
	if resumeID != "" {
		newArgs = append(newArgs, "--resume", resumeID)
	}

	// Start new child process
	log.Printf("[sidecar] Restarting: %s %v", s.cmdPath, newArgs)
	child := exec.Command(s.cmdPath, newArgs...)
	child.Stdin = os.Stdin
	child.Stdout = os.Stdout
	child.Stderr = os.Stderr
	child.Env = os.Environ()

	if err := child.Start(); err != nil {
		log.Printf("[sidecar] ERROR: failed to restart child: %v", err)
		s.restarting = false
		return
	}

	s.childPID = child.Process.Pid
	log.Printf("[sidecar] Restarted child PID %d", s.childPID)

	// Notify server of new PID
	s.client.Post("/sidecar/update-pid", map[string]interface{}{
		"agent_id": s.agentID,
		"new_pid":  s.childPID,
	})

	// Reset dedup state (new session = fresh output)
	s.prevHashes = make(map[string]bool)
	s.hashHistory = nil
	s.restarting = false
}

var uuidRe = regexp.MustCompile(`[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`)

// captureResumeID reads the pane and extracts the last UUID (session ID).
func (s *Sidecar) captureResumeID() string {
	content, err := tmux.CapturePane(s.paneID, 30)
	if err != nil {
		return ""
	}
	matches := uuidRe.FindAllString(content, -1)
	if len(matches) > 0 {
		return matches[len(matches)-1]
	}
	return ""
}

// checkRateLimit detects API rate limit messages in the pane and notifies the team.
// Patterns: "hit your limit", "rate limit", "resets at", "quota exceeded"
func (s *Sidecar) checkRateLimit(content string) {
	if s.rateLimited {
		return // already notified
	}

	lower := strings.ToLower(content)
	if !strings.Contains(lower, "hit your limit") &&
		!strings.Contains(lower, "rate limit") &&
		!strings.Contains(lower, "quota exceeded") {
		return
	}

	s.rateLimited = true
	log.Printf("[sidecar] Rate limit detected for %s", s.agentID)

	// Extract reset time from anywhere near the limit message.
	// Claude format: "resets 10am (America/New_York)"
	// Search the entire pane content, not just around the trigger line.
	resetInfo := ""
	if idx := strings.Index(lower, "resets "); idx >= 0 {
		end := idx + 80
		if end > len(content) {
			end = len(content)
		}
		snippet := content[idx:end]
		// Take until newline or closing paren or pipe
		for i, ch := range snippet {
			if ch == '\n' || ch == '│' || ch == '|' {
				snippet = snippet[:i]
				break
			}
		}
		snippet = strings.TrimSpace(snippet)
		if snippet != "" {
			resetInfo = " — " + snippet
		}
	}

	msg := fmt.Sprintf("hit API rate limit%s", resetInfo)
	if resetInfo == "" {
		msg += " — offline until reset"
	}

	// Notify all team members via server
	s.client.Post("/sidecar/rate-limited", map[string]interface{}{
		"agent_id": s.agentID,
		"message":  msg,
	})

	// Push to stream so it's visible in read_stream
	s.pushStream(fmt.Sprintf("\n---\n*Agent %s (%s) %s*\n", s.agentID, s.cmdName, msg))
}

func (s *Sidecar) pushStream(content string) {
	s.client.Post("/sidecar/stream-push", map[string]interface{}{
		"agent_id": s.agentID,
		"content":  content,
	})
}

func (s *Sidecar) heartbeat(childAlive bool) map[string]interface{} {
	resp, err := s.client.Post("/sidecar/heartbeat", map[string]interface{}{
		"agent_id":    s.agentID,
		"child_alive": childAlive,
	})
	if err != nil {
		log.Printf("[sidecar] ERROR: heartbeat failed for %s: %v", s.agentID, err)
		return nil
	}
	return resp
}

func (s *Sidecar) pollMessages() []map[string]interface{} {
	resp, err := s.client.Get(fmt.Sprintf("/sidecar/messages?agent_id=%s", s.agentID))
	if err != nil {
		log.Printf("[sidecar] ERROR: pollMessages failed for %s: %v", s.agentID, err)
		return nil
	}
	msgsRaw, ok := resp["messages"]
	if !ok {
		return nil
	}
	msgsSlice, ok := msgsRaw.([]interface{})
	if !ok {
		return nil
	}
	var result []map[string]interface{}
	for _, m := range msgsSlice {
		if msg, ok := m.(map[string]interface{}); ok {
			result = append(result, msg)
		}
	}
	return result
}

func (s *Sidecar) proxy(method, path string, data map[string]interface{}) (map[string]interface{}, error) {
	// Inject agent_token if requested
	if data != nil {
		if _, ok := data["_inject_agent_token"]; ok {
			delete(data, "_inject_agent_token")
			data["agent_token"] = s.client.Token
		}
	}

	if method == "POST" && data != nil {
		return s.client.Post(path, data)
	}
	return s.client.Get(path)
}

func (s *Sidecar) maybeRefreshToken() {
	if time.Since(s.lastTokenRefresh) < TokenRefreshInterval {
		return
	}

	// Try agent-token refresh
	resp, err := s.client.Post("/api/auth/agent-token", map[string]interface{}{
		"agent_id": s.agentID,
	})
	if err == nil {
		if status, _ := resp["status"].(string); status == "ok" {
			if token, ok := resp["agent_token"].(string); ok {
				s.client.Token = token
				s.lastTokenRefresh = time.Now()
				log.Printf("[sidecar] Agent token refreshed")
				return
			}
		}
	}

	// Fallback: delegation token refresh
	resp, err = s.client.PostRaw("/api/auth/delegate-refresh", map[string]interface{}{
		"delegation_token": s.client.Token,
	})
	if err == nil {
		if status, _ := resp["status"].(string); status == "ok" {
			if token, ok := resp["delegation_token"].(string); ok {
				s.client.Token = token
				s.lastTokenRefresh = time.Now()
				log.Printf("[sidecar] Delegation token refreshed")
				return
			}
		}
	}

	log.Printf("[sidecar] Token refresh failed")
}

// UpdateToken updates the auth token used by the sidecar's API client.
func (s *Sidecar) UpdateToken(token string) {
	s.client.Token = token
}

// splitLines splits text into lines without using strings to keep the import minimal.
func splitLines(s string) []string {
	var lines []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			lines = append(lines, s[start:i])
			start = i + 1
		}
	}
	if start < len(s) {
		lines = append(lines, s[start:])
	}
	return lines
}

func joinLines(lines []string) string {
	if len(lines) == 0 {
		return ""
	}
	result := lines[0]
	for _, l := range lines[1:] {
		result += "\n" + l
	}
	return result
}

// marshalJSON is a helper for JSON encoding (unused import avoidance).
var _ = json.Marshal
