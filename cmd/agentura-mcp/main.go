// agentura-mcp — stable MCP stdio server for agentura.
//
// Thin shell that delegates every tool call to agentura-mcp-backend by exec'ing
// it as a subprocess. This mirrors the Python design where mcp_server.py
// reloads mcp_backend.py on every call:
//
//   - agentura-mcp  = stable, never needs restart (like mcp_server.py)
//   - agentura-mcp-backend = replaceable on disk (like mcp_backend.py)
//
// Replace the backend binary → next tool call picks up the new version
// automatically, without restarting the MCP connection to the agent.
//
// Backend binary location (in order of priority):
//  1. AGENTURA_BACKEND env var
//  2. agentura-mcp-backend next to this binary
//  3. agentura-mcp-backend in PATH
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/dmikushin/agentura/internal/config"
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

func main() {
	log.SetFlags(0)

	// Ensure AGENTURA_URL is available for backend subprocesses
	if config.MonitorURL() == "" {
		fmt.Fprintln(os.Stderr, "Error: AGENTURA_URL not set and no default compiled in")
		os.Exit(1)
	}
	// Export for backend subprocess (it inherits env)
	os.Setenv("AGENTURA_URL", config.MonitorURL())

	s := server.NewMCPServer("agentura", "1.0.0")

	registerTools(s)

	if err := server.ServeStdio(s); err != nil {
		fmt.Fprintf(os.Stderr, "Server error: %v\n", err)
		os.Exit(1)
	}
}

// findBackend locates the agentura-mcp-backend binary.
func findBackend() string {
	// 1. Explicit env var
	if p := os.Getenv("AGENTURA_MCP_BACKEND"); p != "" {
		return p
	}

	// 2. Next to this binary
	if self, err := os.Executable(); err == nil {
		candidate := filepath.Join(filepath.Dir(self), "agentura-mcp-backend")
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
	}

	// 3. In PATH
	if p, err := exec.LookPath("agentura-mcp-backend"); err == nil {
		return p
	}

	return "agentura-mcp-backend" // fallback, will fail with a clear error
}

// callBackend exec's agentura-mcp-backend with the given tool name and args.
// Returns the tool result string.
func callBackend(toolName string, args map[string]interface{}) string {
	backendPath := findBackend()

	argsJSON, _ := json.Marshal(args)

	cmd := exec.Command(backendPath, toolName)
	cmd.Stdin = bytes.NewReader(argsJSON)
	cmd.Env = os.Environ() // pass through all env vars
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		stderrStr := strings.TrimSpace(stderr.String())
		if stderrStr != "" {
			return fmt.Sprintf("Error: backend failed: %s", stderrStr)
		}
		return fmt.Sprintf("Error: backend failed: %v", err)
	}

	// Parse JSON response
	var resp map[string]string
	if err := json.Unmarshal(stdout.Bytes(), &resp); err != nil {
		// If not valid JSON, return raw output
		return strings.TrimSpace(stdout.String())
	}

	if errMsg, ok := resp["error"]; ok {
		return fmt.Sprintf("Error: %s", errMsg)
	}
	return resp["result"]
}

// makeHandler creates an MCP tool handler that delegates to the backend binary.
func makeHandler(toolName string) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args := make(map[string]interface{})
		if a := req.GetArguments(); a != nil {
			args = a
		}
		result := callBackend(toolName, args)
		return mcp.NewToolResultText(result), nil
	}
}

func registerTools(s *server.MCPServer) {
	// --- Agent tools ---

	s.AddTool(
		mcp.NewTool("list_agents",
			mcp.WithDescription("List all AI agents currently registered with agentura.\n\nEach agent is identified by hostname@cwd:PID where:\n- hostname: machine where the agent runs\n- cwd: the starting working directory of the agent\n- PID: process ID of the agent\n\nReturns a formatted list of connected agents with their details."),
		),
		makeHandler("list_agents"),
	)

	s.AddTool(
		mcp.NewTool("list_hosts",
			mcp.WithDescription("List available hosts — local machine and remote SSH-accessible hosts.\n\nRemote hosts are configured in hosts.json with SSH address, tags,\nGPU/CPU info, and notes. Returns all hosts where agents can be deployed."),
		),
		makeHandler("list_hosts"),
	)

	s.AddTool(
		mcp.NewTool("create_agent",
			mcp.WithDescription("Create a new AI agent on a local or remote host.\n\n"+
				"For remote hosts, the agent is launched via SSH. A sidecar process\n"+
				"handles monitoring and message delivery.\n\n"+
				"**Resume a saved session:** provide resume_session_id to continue\n"+
				"a previous conversation. The agent starts with full context from\n"+
				"the saved session instead of a blank slate. Useful for:\n"+
				"- Recovering a crashed agent's work\n"+
				"- Continuing a long task after rate limit reset\n"+
				"- Spawning a new agent that picks up where another left off"),
			mcp.WithString("hostname",
				mcp.Required(),
				mcp.Description("target host (local hostname or a remote host from list_hosts)"),
			),
			mcp.WithString("cwd",
				mcp.Required(),
				mcp.Description("starting working directory for the agent"),
			),
			mcp.WithString("agent_type",
				mcp.Required(),
				mcp.Description(`"claude" or "gemini"`),
				mcp.Enum("claude", "gemini"),
			),
			mcp.WithBoolean("blocking",
				mcp.Description("if true, wait for the agent to register and return its info"),
				mcp.DefaultBool(true),
			),
			mcp.WithString("team",
				mcp.Description("team name to assign the new agent to (optional)"),
			),
			mcp.WithString("resume_session_id",
				mcp.Description("session UUID to resume (agent starts with saved conversation context instead of blank)"),
			),
		),
		makeHandler("create_agent"),
	)

	s.AddTool(
		mcp.NewTool("read_stream",
			mcp.WithDescription("Read new output from another agent's stream since the last read.\n\nUses a cursor internally — first call returns all accumulated output,\nsubsequent calls return only what appeared since the previous read."),
			mcp.WithString("agent_id",
				mcp.Required(),
				mcp.Description("target agent identifier (hostname@cwd:PID from list_agents)"),
			),
		),
		makeHandler("read_stream"),
	)

	// --- Messaging tools ---

	s.AddTool(
		mcp.NewTool("send_message",
			mcp.WithDescription("Send a message to another agent.\n\nYour identity is taken automatically from AGENT_ID (set by agent-run)."),
			mcp.WithString("target_agent_id",
				mcp.Required(),
				mcp.Description("recipient agent (hostname@cwd:PID from list_agents)"),
			),
			mcp.WithString("message",
				mcp.Required(),
				mcp.Description("text to send"),
			),
			mcp.WithBoolean("rsvp",
				mcp.Description("if true, the target agent will reply to you directly via send_message when done. This lets you work on other tasks in parallel instead of polling read_stream in a loop. Strongly recommended for any request that expects a response."),
				mcp.DefaultBool(false),
			),
		),
		makeHandler("send_message"),
	)

	s.AddTool(
		mcp.NewTool("interrupt_agent",
			mcp.WithDescription("Interrupt an agent by sending Escape.\n\nUse this to cancel a hanging operation (e.g. a stuck tool call).\nThe agent's current action is aborted and it returns to the prompt."),
			mcp.WithString("target_agent_id",
				mcp.Required(),
				mcp.Description("agent to interrupt (hostname@cwd:PID from list_agents)"),
			),
		),
		makeHandler("interrupt_agent"),
	)

	s.AddTool(
		mcp.NewTool("restart_agent",
			mcp.WithDescription(
				"Restart an agent's AI process while preserving its identity and team membership.\n\n"+
					"Gracefully stops the agent (double SIGINT, like pressing Ctrl-C twice), "+
					"captures the session UUID from the terminal, and restarts with --resume "+
					"to continue the conversation exactly where it left off.\n\n"+
					"The agent keeps its agent_id, team membership, message queue, stream history, "+
					"and sidecar connection. Only the child AI process restarts.\n\n"+
					"**Use cases:**\n"+
					"- Agent is stuck or unresponsive → restart to recover\n"+
					"- MCP configuration changed → restart to reload tools\n"+
					"- Self-restart → pass your own agent_id to refresh your context\n\n"+
					"**Session switching (power feature):** Provide resume_session_id to restart "+
					"into a DIFFERENT session entirely. This lets you:\n"+
					"- Switch an agent between conversation threads\n"+
					"- Roll back to an earlier session state\n"+
					"- Recover a crashed agent using its last session UUID\n"+
					"- Hand off context: give another agent YOUR session UUID via send_message, "+
					"and they can restart into your conversation to pick up where you left off.",
			),
			mcp.WithString("target_agent_id",
				mcp.Required(),
				mcp.Description("agent to restart (hostname@cwd:PID from list_agents). Pass your own agent_id for self-restart."),
			),
			mcp.WithString("resume_session_id",
				mcp.Description("specific session UUID to resume into. If omitted, auto-detects from terminal output. Provide this to switch to a completely different conversation."),
			),
			mcp.WithString("reason",
				mcp.Description("why this restart is needed — logged to the team board so teammates know what happened"),
			),
		),
		makeHandler("restart_agent"),
	)

	s.AddTool(
		mcp.NewTool("post_to_board",
			mcp.WithDescription("Post a note to the team's shared board.\n\nThe board is a persistent, append-only log visible to all team members.\nUse it for decisions, findings, status updates, and shared context."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team"),
			),
			mcp.WithString("text",
				mcp.Required(),
				mcp.Description("note text to post"),
			),
		),
		makeHandler("post_to_board"),
	)

	s.AddTool(
		mcp.NewTool("read_board",
			mcp.WithDescription("Read the team's shared board.\n\nReturns recent entries with timestamps, authors, and importance scores.\nThe board is backed by semantic memory — entries are searchable and auto-linked.\nUse this to catch up on team context and decisions."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team"),
			),
			mcp.WithNumber("limit",
				mcp.Description("max entries to return (default 50)"),
			),
		),
		makeHandler("read_board"),
	)

	s.AddTool(
		mcp.NewTool("search_board",
			mcp.WithDescription("Search the team board using semantic + full-text hybrid search.\n\n"+
				"Finds entries by meaning, not just keywords. Powered by pgvector embeddings "+
				"and PostgreSQL full-text search with Reciprocal Rank Fusion scoring.\n\n"+
				"Use this when you need to find specific information on a large board — "+
				"much faster and more relevant than reading all entries."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team"),
			),
			mcp.WithString("query",
				mcp.Required(),
				mcp.Description("what to search for — can be a question, keywords, or a description of what you're looking for"),
			),
			mcp.WithNumber("limit",
				mcp.Description("max results to return (default 20)"),
			),
		),
		makeHandler("search_board"),
	)

	s.AddTool(
		mcp.NewTool("broadcast_message",
			mcp.WithDescription("Send a message to all members of a team.\n\nThe message is delivered to every team member except yourself.\nYou must be a member of the team to broadcast."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team to broadcast to"),
			),
			mcp.WithString("message",
				mcp.Required(),
				mcp.Description("text to send to all team members"),
			),
		),
		makeHandler("broadcast_message"),
	)

	// --- Team tools ---

	s.AddTool(
		mcp.NewTool("list_teams",
			mcp.WithDescription("List all agent teams with their owners and members."),
		),
		makeHandler("list_teams"),
	)

	s.AddTool(
		mcp.NewTool("create_team",
			mcp.WithDescription("Create a new team. You become the owner.\n\nUses your AGENT_TOKEN (set automatically at registration) to prove identity."),
			mcp.WithString("name",
				mcp.Required(),
				mcp.Description("team name (must be unique)"),
			),
		),
		makeHandler("create_team"),
	)

	s.AddTool(
		mcp.NewTool("request_join_team",
			mcp.WithDescription("Request to join an existing team. The team owner must approve.\n\nUses your AGENT_TOKEN to prove identity."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team to join"),
			),
			mcp.WithString("message",
				mcp.Description("optional message to the team owner explaining why you want to join"),
			),
		),
		makeHandler("request_join_team"),
	)

	s.AddTool(
		mcp.NewTool("approve_join",
			mcp.WithDescription("Approve a pending join request (team owner only)."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("team name"),
			),
			mcp.WithString("pending_agent_id",
				mcp.Required(),
				mcp.Description("agent_id of the requester to approve"),
			),
		),
		makeHandler("approve_join"),
	)

	s.AddTool(
		mcp.NewTool("deny_join",
			mcp.WithDescription("Deny a pending join request (team owner only)."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("team name"),
			),
			mcp.WithString("pending_agent_id",
				mcp.Required(),
				mcp.Description("agent_id of the requester to deny"),
			),
		),
		makeHandler("deny_join"),
	)

	s.AddTool(
		mcp.NewTool("list_pending_requests",
			mcp.WithDescription("List pending join requests for a team."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team"),
			),
		),
		makeHandler("list_pending_requests"),
	)

	s.AddTool(
		mcp.NewTool("transfer_ownership",
			mcp.WithDescription("Transfer team ownership to another member (owner only)."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team"),
			),
			mcp.WithString("new_owner",
				mcp.Required(),
				mcp.Description("agent_id of the member to become the new owner"),
			),
		),
		makeHandler("transfer_ownership"),
	)

	s.AddTool(
		mcp.NewTool("leave_team",
			mcp.WithDescription("Leave a team voluntarily. If you are the owner, succession is triggered."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team to leave"),
			),
		),
		makeHandler("leave_team"),
	)

	s.AddTool(
		mcp.NewTool("add_admin",
			mcp.WithDescription("Add an admin to the team (owner only). Admins can approve/deny join requests."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team"),
			),
			mcp.WithString("admin_agent_id",
				mcp.Required(),
				mcp.Description("agent_id of the member to promote to admin"),
			),
		),
		makeHandler("add_admin"),
	)

	s.AddTool(
		mcp.NewTool("remove_admin",
			mcp.WithDescription("Remove an admin from the team (owner only)."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team"),
			),
			mcp.WithString("admin_agent_id",
				mcp.Required(),
				mcp.Description("agent_id of the admin to demote"),
			),
		),
		makeHandler("remove_admin"),
	)

	s.AddTool(
		mcp.NewTool("force_succession",
			mcp.WithDescription("Request forced succession of team ownership (admin only).\n\nStarts a 60-second grace period. If the current owner doesn't respond\nwith any team action within that time, ownership passes to the next member."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team"),
			),
			mcp.WithString("reason",
				mcp.Description("optional reason for the force-succession request"),
			),
		),
		makeHandler("force_succession"),
	)

	// --- Clock / Sprint tools ---
	s.AddTool(
		mcp.NewTool("timenow",
			mcp.WithDescription("Check the current server time and sprint status.\n\nReturns: TIME NOW: HH:MMAM/PM (Xm since sprint start, Ym left till sprint end)\n\nCall this anytime you want to check how much time has passed or remains in the sprint. The same information is shown automatically after every tool call via the clock hook."),
		),
		makeHandler("timenow"),
	)

	s.AddTool(
		mcp.NewTool("start_sprint",
			mcp.WithDescription("Start a sprint timer for a team. Scrum Master should call this at the beginning of each sprint.\n\nAll team members will see sprint elapsed/remaining time after every tool call."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team"),
			),
			mcp.WithNumber("duration_minutes",
				mcp.Description("sprint duration in minutes (default: 30)"),
			),
		),
		makeHandler("start_sprint"),
	)
}
