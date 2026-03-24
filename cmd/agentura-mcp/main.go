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

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

func main() {
	log.SetFlags(0)

	if os.Getenv("AGENTURA_URL") == "" {
		fmt.Fprintln(os.Stderr, "Error: AGENTURA_URL environment variable is required")
		os.Exit(1)
	}

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
			mcp.WithDescription("Create a new AI agent on a local or remote host.\n\nFor remote hosts, the agent is launched via SSH. A sidecar process\nhandles monitoring and message delivery.\nYour identity is taken automatically from AGENT_ID (set by agent-run)."),
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
}
