// agentura-backend — tool executor for agentura MCP server.
//
// This binary is exec'd by agentura-mcp for each tool call.
// It can be replaced on disk without restarting the MCP server —
// the next tool call will pick up the new version automatically.
//
// Usage:
//
//	echo '{"agent_id":"x@y:1"}' | agentura-backend read_stream
//	agentura-backend list_agents
//
// Protocol:
//   - Tool name from argv[1]
//   - JSON arguments from stdin (empty object {} if no args)
//   - JSON result to stdout: {"result": "..."}
//   - Errors to stderr
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/dmikushin/agentura/internal/tools"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "Usage: agentura-backend <tool_name>")
		fmt.Fprintln(os.Stderr, "  Reads JSON args from stdin, writes JSON result to stdout.")
		fmt.Fprintln(os.Stderr, "  Tools: list_agents, list_hosts, create_agent, read_stream,")
		fmt.Fprintln(os.Stderr, "         send_message, interrupt_agent, list_teams, create_team,")
		fmt.Fprintln(os.Stderr, "         request_join_team, approve_join, deny_join,")
		fmt.Fprintln(os.Stderr, "         list_pending_requests, transfer_ownership, leave_team,")
		fmt.Fprintln(os.Stderr, "         add_admin, remove_admin, force_succession")
		os.Exit(1)
	}

	toolName := os.Args[1]

	// Read JSON args from stdin
	args := make(map[string]interface{})
	stdinData, err := io.ReadAll(os.Stdin)
	if err == nil && len(stdinData) > 0 {
		json.Unmarshal(stdinData, &args)
	}

	backend, err := tools.NewBackend()
	if err != nil {
		writeError(err.Error())
		os.Exit(1)
	}

	result := dispatch(backend, toolName, args)

	// Save cursor state after execution
	backend.SaveCursors()

	writeResult(result)
}

func dispatch(b *tools.Backend, tool string, args map[string]interface{}) string {
	getString := func(key string) string {
		if v, ok := args[key]; ok {
			if s, ok := v.(string); ok {
				return s
			}
		}
		return ""
	}
	getBool := func(key string, def bool) bool {
		if v, ok := args[key]; ok {
			if b, ok := v.(bool); ok {
				return b
			}
		}
		return def
	}

	switch tool {
	// --- Agent tools ---
	case "list_agents":
		return b.ListAgents()
	case "list_hosts":
		return b.ListHosts()
	case "read_stream":
		return b.ReadStream(getString("agent_id"))
	case "create_agent":
		return b.CreateAgent(
			getString("hostname"),
			getString("cwd"),
			getString("agent_type"),
			getBool("blocking", true),
			getString("team"),
		)

	// --- Messaging tools ---
	case "send_message":
		return b.SendMessage(
			getString("target_agent_id"),
			getString("message"),
			getBool("rsvp", false),
		)
	case "interrupt_agent":
		return b.InterruptAgent(getString("target_agent_id"))

	// --- Team tools ---
	case "list_teams":
		return b.ListTeams()
	case "create_team":
		return b.CreateTeam(getString("name"))
	case "request_join_team":
		return b.RequestJoinTeam(getString("team_name"), getString("message"))
	case "approve_join":
		return b.ApproveJoin(getString("team_name"), getString("pending_agent_id"))
	case "deny_join":
		return b.DenyJoin(getString("team_name"), getString("pending_agent_id"))
	case "list_pending_requests":
		return b.ListPendingRequests(getString("team_name"))
	case "transfer_ownership":
		return b.TransferOwnership(getString("team_name"), getString("new_owner"))
	case "leave_team":
		return b.LeaveTeam(getString("team_name"))
	case "add_admin":
		return b.AddAdmin(getString("team_name"), getString("admin_agent_id"))
	case "remove_admin":
		return b.RemoveAdmin(getString("team_name"), getString("admin_agent_id"))
	case "force_succession":
		return b.ForceSuccession(getString("team_name"), getString("reason"))

	default:
		return fmt.Sprintf("Error: unknown tool '%s'", tool)
	}
}

func writeResult(result string) {
	out := map[string]string{"result": result}
	json.NewEncoder(os.Stdout).Encode(out)
}

func writeError(msg string) {
	out := map[string]string{"error": msg}
	json.NewEncoder(os.Stdout).Encode(out)
}
