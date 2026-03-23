// agentura-mcp — MCP stdio server for agentura.
//
// Reads AGENTURA_URL, AGENTURA_SIDECAR_SOCK, AGENT_ID, AGENT_TOKEN from env.
// Registers all 17 tools and serves them over stdio.
package main

import (
	"context"
	"fmt"
	"log"
	"os"

	"github.com/dmikushin/agentura/internal/tools"
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

func main() {
	log.SetFlags(0)

	backend, err := tools.NewBackend()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}

	s := server.NewMCPServer("agentura", "1.0.0")

	registerTools(s, backend)

	if err := server.ServeStdio(s); err != nil {
		fmt.Fprintf(os.Stderr, "Server error: %v\n", err)
		os.Exit(1)
	}
}

func registerTools(s *server.MCPServer, b *tools.Backend) {
	// --- Agent tools ---

	s.AddTool(
		mcp.NewTool("list_agents",
			mcp.WithDescription("List all AI agents currently registered with agentura.\n\nEach agent is identified by hostname@cwd:PID where:\n- hostname: machine where the agent runs\n- cwd: the starting working directory of the agent\n- PID: process ID of the agent\n\nReturns a formatted list of connected agents with their details."),
		),
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			return mcp.NewToolResultText(b.ListAgents()), nil
		},
	)

	s.AddTool(
		mcp.NewTool("list_hosts",
			mcp.WithDescription("List available hosts — local machine and remote SSH-accessible hosts.\n\nRemote hosts are configured in hosts.json with SSH address, tags,\nGPU/CPU info, and notes. Returns all hosts where agents can be deployed."),
		),
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			return mcp.NewToolResultText(b.ListHosts()), nil
		},
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
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			hostname := req.GetString("hostname", "")
			cwd := req.GetString("cwd", "")
			agentType := req.GetString("agent_type", "")
			blocking := req.GetBool("blocking", true)
			team := req.GetString("team", "")
			return mcp.NewToolResultText(b.CreateAgent(hostname, cwd, agentType, blocking, team)), nil
		},
	)

	s.AddTool(
		mcp.NewTool("read_stream",
			mcp.WithDescription("Read new output from another agent's stream since the last read.\n\nUses a cursor internally — first call returns all accumulated output,\nsubsequent calls return only what appeared since the previous read."),
			mcp.WithString("agent_id",
				mcp.Required(),
				mcp.Description("target agent identifier (hostname@cwd:PID from list_agents)"),
			),
		),
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			agentID := req.GetString("agent_id", "")
			return mcp.NewToolResultText(b.ReadStream(agentID)), nil
		},
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
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			targetID := req.GetString("target_agent_id", "")
			message := req.GetString("message", "")
			rsvp := req.GetBool("rsvp", false)
			return mcp.NewToolResultText(b.SendMessage(targetID, message, rsvp)), nil
		},
	)

	s.AddTool(
		mcp.NewTool("interrupt_agent",
			mcp.WithDescription("Interrupt an agent by sending Escape.\n\nUse this to cancel a hanging operation (e.g. a stuck tool call).\nThe agent's current action is aborted and it returns to the prompt."),
			mcp.WithString("target_agent_id",
				mcp.Required(),
				mcp.Description("agent to interrupt (hostname@cwd:PID from list_agents)"),
			),
		),
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			targetID := req.GetString("target_agent_id", "")
			return mcp.NewToolResultText(b.InterruptAgent(targetID)), nil
		},
	)

	// --- Team tools ---

	s.AddTool(
		mcp.NewTool("list_teams",
			mcp.WithDescription("List all agent teams with their owners and members."),
		),
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			return mcp.NewToolResultText(b.ListTeams()), nil
		},
	)

	s.AddTool(
		mcp.NewTool("create_team",
			mcp.WithDescription("Create a new team. You become the owner.\n\nUses your AGENT_TOKEN (set automatically at registration) to prove identity."),
			mcp.WithString("name",
				mcp.Required(),
				mcp.Description("team name (must be unique)"),
			),
		),
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			name := req.GetString("name", "")
			return mcp.NewToolResultText(b.CreateTeam(name)), nil
		},
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
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			teamName := req.GetString("team_name", "")
			message := req.GetString("message", "")
			return mcp.NewToolResultText(b.RequestJoinTeam(teamName, message)), nil
		},
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
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			teamName := req.GetString("team_name", "")
			pendingID := req.GetString("pending_agent_id", "")
			return mcp.NewToolResultText(b.ApproveJoin(teamName, pendingID)), nil
		},
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
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			teamName := req.GetString("team_name", "")
			pendingID := req.GetString("pending_agent_id", "")
			return mcp.NewToolResultText(b.DenyJoin(teamName, pendingID)), nil
		},
	)

	s.AddTool(
		mcp.NewTool("list_pending_requests",
			mcp.WithDescription("List pending join requests for a team."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team"),
			),
		),
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			teamName := req.GetString("team_name", "")
			return mcp.NewToolResultText(b.ListPendingRequests(teamName)), nil
		},
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
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			teamName := req.GetString("team_name", "")
			newOwner := req.GetString("new_owner", "")
			return mcp.NewToolResultText(b.TransferOwnership(teamName, newOwner)), nil
		},
	)

	s.AddTool(
		mcp.NewTool("leave_team",
			mcp.WithDescription("Leave a team voluntarily. If you are the owner, succession is triggered."),
			mcp.WithString("team_name",
				mcp.Required(),
				mcp.Description("name of the team to leave"),
			),
		),
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			teamName := req.GetString("team_name", "")
			return mcp.NewToolResultText(b.LeaveTeam(teamName)), nil
		},
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
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			teamName := req.GetString("team_name", "")
			adminID := req.GetString("admin_agent_id", "")
			return mcp.NewToolResultText(b.AddAdmin(teamName, adminID)), nil
		},
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
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			teamName := req.GetString("team_name", "")
			adminID := req.GetString("admin_agent_id", "")
			return mcp.NewToolResultText(b.RemoveAdmin(teamName, adminID)), nil
		},
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
		func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			teamName := req.GetString("team_name", "")
			reason := req.GetString("reason", "")
			return mcp.NewToolResultText(b.ForceSuccession(teamName, reason)), nil
		},
	)
}
