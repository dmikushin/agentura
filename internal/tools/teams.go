package tools

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/dmikushin/agentura/internal/api"
)

// ListTeams returns a formatted list of all teams.
func (b *Backend) ListTeams() string {
	data, err := b.get("/teams")
	if err != nil {
		return "Error: agentura server is not running"
	}

	teams, _ := data["teams"].([]interface{})
	if len(teams) == 0 {
		return "No teams exist."
	}

	var lines []string
	for _, t := range teams {
		team, ok := t.(map[string]interface{})
		if !ok {
			continue
		}
		name, _ := team["name"].(string)
		owner, _ := team["owner"].(string)
		membersRaw, _ := team["members"].([]interface{})
		var members []string
		for _, m := range membersRaw {
			if s, ok := m.(string); ok {
				members = append(members, s)
			}
		}
		adminsRaw, _ := team["admins"].([]interface{})
		var admins []string
		for _, a := range adminsRaw {
			if s, ok := a.(string); ok {
				admins = append(admins, s)
			}
		}
		adminsStr := "none"
		if len(admins) > 0 {
			adminsStr = strings.Join(admins, ", ")
		}
		pendingRaw, _ := team["pending"].(map[string]interface{})
		pendingStr := ""
		if len(pendingRaw) > 0 {
			pendingStr = fmt.Sprintf(", %d pending", len(pendingRaw))
		}
		lastActivity, _ := team["last_owner_activity"].(string)
		if lastActivity == "" {
			lastActivity = "?"
		}
		fspStr := ""
		if fsp, ok := team["force_succession_pending"].(map[string]interface{}); ok && fsp != nil {
			requestedBy, _ := fsp["requested_by"].(string)
			fspStr = fmt.Sprintf("\n  ⚠ force-succession pending (by %s)", requestedBy)
		}

		lines = append(lines, fmt.Sprintf(
			"- **%s** (owner: %s, %d members%s)\n  members: %s\n  admins: %s\n  last owner activity: %s%s",
			name, owner, len(members), pendingStr,
			strings.Join(members, ", "), adminsStr, lastActivity, fspStr))
	}

	return fmt.Sprintf("%d team(s):\n\n%s", len(teams), strings.Join(lines, "\n"))
}

// CreateTeam creates a new team. The caller becomes the owner.
func (b *Backend) CreateTeam(name string) string {
	if b.agentToken == "" {
		return "Error: no AGENT_TOKEN available (agent not registered?)"
	}
	resp, err := b.post("/teams", map[string]interface{}{
		"name":                name,
		"_inject_agent_token": true,
	})
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok {
			switch httpErr.StatusCode {
			case 409:
				return fmt.Sprintf("Error: team '%s' already exists", name)
			case 401:
				return "Error: agent_token expired or invalid"
			}
		}
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status == "ok" {
		return fmt.Sprintf("Team '%s' created", name)
	}
	errMsg, _ := resp["error"].(string)
	return fmt.Sprintf("Error: %s", errMsg)
}

// RequestJoinTeam requests to join a team. The team owner must approve.
func (b *Backend) RequestJoinTeam(teamName, message string) string {
	if b.agentToken == "" {
		return "Error: no AGENT_TOKEN available (agent not registered?)"
	}
	resp, err := b.post("/teams/request-join", map[string]interface{}{
		"team":                teamName,
		"_inject_agent_token": true,
		"message":             message,
	})
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok {
			switch httpErr.StatusCode {
			case 404:
				return fmt.Sprintf("Error: team '%s' not found", teamName)
			case 409:
				return fmt.Sprintf("Error: already a member of '%s'", teamName)
			case 401:
				return "Error: agent_token expired or invalid"
			}
		}
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status == "ok" {
		return fmt.Sprintf("Join request sent to team '%s'. Waiting for owner approval.", teamName)
	}
	errMsg, _ := resp["error"].(string)
	return fmt.Sprintf("Error: %s", errMsg)
}

// ApproveJoin approves a pending join request (owner/admin only).
func (b *Backend) ApproveJoin(teamName, pendingAgentID string) string {
	if b.agentToken == "" {
		return "Error: no AGENT_TOKEN available (agent not registered?)"
	}
	resp, err := b.post("/teams/approve", map[string]interface{}{
		"team":                teamName,
		"pending_agent_id":    pendingAgentID,
		"_inject_agent_token": true,
	})
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok {
			switch httpErr.StatusCode {
			case 403:
				return "Error: only the team owner can approve requests"
			case 404:
				return fmt.Sprintf("Error: no pending request from '%s'", pendingAgentID)
			case 401:
				return "Error: agent_token expired or invalid"
			}
		}
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status == "ok" {
		return fmt.Sprintf("Approved: %s is now a member of '%s'", pendingAgentID, teamName)
	}
	errMsg, _ := resp["error"].(string)
	return fmt.Sprintf("Error: %s", errMsg)
}

// DenyJoin denies a pending join request (owner/admin only).
func (b *Backend) DenyJoin(teamName, pendingAgentID string) string {
	if b.agentToken == "" {
		return "Error: no AGENT_TOKEN available (agent not registered?)"
	}
	resp, err := b.post("/teams/deny", map[string]interface{}{
		"team":                teamName,
		"pending_agent_id":    pendingAgentID,
		"_inject_agent_token": true,
	})
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok {
			switch httpErr.StatusCode {
			case 403:
				return "Error: only the team owner can deny requests"
			case 404:
				return fmt.Sprintf("Error: no pending request from '%s'", pendingAgentID)
			case 401:
				return "Error: agent_token expired or invalid"
			}
		}
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status == "ok" {
		return fmt.Sprintf("Denied: %s request for '%s' rejected", pendingAgentID, teamName)
	}
	errMsg, _ := resp["error"].(string)
	return fmt.Sprintf("Error: %s", errMsg)
}

// ListPendingRequests lists pending join requests for a team.
func (b *Backend) ListPendingRequests(teamName string) string {
	resp, err := b.get(fmt.Sprintf("/teams/%s/pending", urlEncode(teamName)))
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok && httpErr.StatusCode == 404 {
			return fmt.Sprintf("Error: team '%s' not found", teamName)
		}
		return fmt.Sprintf("Error: %v", err)
	}

	pending, _ := resp["pending"].(map[string]interface{})
	if len(pending) == 0 {
		return fmt.Sprintf("No pending requests for team '%s'.", teamName)
	}

	var lines []string
	for agentID, infoRaw := range pending {
		info, ok := infoRaw.(map[string]interface{})
		if !ok {
			continue
		}
		msg, _ := info["message"].(string)
		at, _ := info["requested_at"].(string)
		if at == "" {
			at = "?"
		}
		msgPart := ""
		if msg != "" {
			msgPart = fmt.Sprintf(` — "%s"`, msg)
		}
		lines = append(lines, fmt.Sprintf("- **%s** (requested %s)%s", agentID, at, msgPart))
	}

	return fmt.Sprintf("%d pending request(s) for '%s':\n\n%s", len(pending), teamName, strings.Join(lines, "\n"))
}

// TransferOwnership transfers team ownership to another member (owner only).
func (b *Backend) TransferOwnership(teamName, newOwner string) string {
	if b.agentToken == "" {
		return "Error: no AGENT_TOKEN available (agent not registered?)"
	}
	resp, err := b.post("/teams/transfer", map[string]interface{}{
		"team":                teamName,
		"new_owner":           newOwner,
		"_inject_agent_token": true,
	})
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok {
			switch httpErr.StatusCode {
			case 403:
				return "Error: only the team owner can transfer ownership"
			case 404:
				return fmt.Sprintf("Error: team '%s' not found", teamName)
			case 400:
				var body map[string]interface{}
				json.Unmarshal([]byte(httpErr.Body), &body)
				errMsg, _ := body["error"].(string)
				if errMsg == "" {
					errMsg = "bad request"
				}
				return fmt.Sprintf("Error: %s", errMsg)
			case 401:
				return "Error: agent_token expired or invalid"
			}
		}
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status == "ok" {
		return fmt.Sprintf("Ownership of '%s' transferred to %s", teamName, newOwner)
	}
	errMsg, _ := resp["error"].(string)
	return fmt.Sprintf("Error: %s", errMsg)
}

// LeaveTeam leaves a team. If the caller is the owner, succession is triggered.
func (b *Backend) LeaveTeam(teamName string) string {
	if b.agentToken == "" {
		return "Error: no AGENT_TOKEN available (agent not registered?)"
	}
	resp, err := b.post("/teams/leave", map[string]interface{}{
		"team":                teamName,
		"_inject_agent_token": true,
	})
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok {
			switch httpErr.StatusCode {
			case 404:
				return fmt.Sprintf("Error: team '%s' not found", teamName)
			case 400:
				var body map[string]interface{}
				json.Unmarshal([]byte(httpErr.Body), &body)
				errMsg, _ := body["error"].(string)
				if errMsg == "" {
					errMsg = "bad request"
				}
				return fmt.Sprintf("Error: %s", errMsg)
			case 401:
				return "Error: agent_token expired or invalid"
			}
		}
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status == "ok" {
		msg := fmt.Sprintf("Left team '%s'", teamName)
		if succession, _ := resp["succession"].(bool); succession {
			msg += " (ownership was transferred to next member)"
		}
		return msg
	}
	errMsg, _ := resp["error"].(string)
	return fmt.Sprintf("Error: %s", errMsg)
}

// AddAdmin adds an admin to the team (owner only).
func (b *Backend) AddAdmin(teamName, adminAgentID string) string {
	if b.agentToken == "" {
		return "Error: no AGENT_TOKEN available (agent not registered?)"
	}
	resp, err := b.post("/teams/add-admin", map[string]interface{}{
		"team":                teamName,
		"admin_agent_id":      adminAgentID,
		"_inject_agent_token": true,
	})
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok {
			switch httpErr.StatusCode {
			case 403:
				return "Error: only the team owner can manage admins"
			case 404:
				return fmt.Sprintf("Error: team '%s' not found", teamName)
			case 400:
				var body map[string]interface{}
				json.Unmarshal([]byte(httpErr.Body), &body)
				errMsg, _ := body["error"].(string)
				if errMsg == "" {
					errMsg = "bad request"
				}
				return fmt.Sprintf("Error: %s", errMsg)
			case 409:
				return fmt.Sprintf("Error: '%s' is already an admin", adminAgentID)
			case 401:
				return "Error: agent_token expired or invalid"
			}
		}
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status == "ok" {
		return fmt.Sprintf("%s is now an admin of '%s'", adminAgentID, teamName)
	}
	errMsg, _ := resp["error"].(string)
	return fmt.Sprintf("Error: %s", errMsg)
}

// RemoveAdmin removes an admin from the team (owner only).
func (b *Backend) RemoveAdmin(teamName, adminAgentID string) string {
	if b.agentToken == "" {
		return "Error: no AGENT_TOKEN available (agent not registered?)"
	}
	resp, err := b.post("/teams/remove-admin", map[string]interface{}{
		"team":                teamName,
		"admin_agent_id":      adminAgentID,
		"_inject_agent_token": true,
	})
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok {
			switch httpErr.StatusCode {
			case 403:
				return "Error: only the team owner can manage admins"
			case 404:
				return fmt.Sprintf("Error: team '%s' not found", teamName)
			case 400:
				var body map[string]interface{}
				json.Unmarshal([]byte(httpErr.Body), &body)
				errMsg, _ := body["error"].(string)
				if errMsg == "" {
					errMsg = "bad request"
				}
				return fmt.Sprintf("Error: %s", errMsg)
			case 401:
				return "Error: agent_token expired or invalid"
			}
		}
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status == "ok" {
		return fmt.Sprintf("%s is no longer an admin of '%s'", adminAgentID, teamName)
	}
	errMsg, _ := resp["error"].(string)
	return fmt.Sprintf("Error: %s", errMsg)
}

// ForceSuccession requests forced succession of team ownership (admin only).
func (b *Backend) ForceSuccession(teamName, reason string) string {
	if b.agentToken == "" {
		return "Error: no AGENT_TOKEN available (agent not registered?)"
	}
	resp, err := b.post("/teams/force-succession", map[string]interface{}{
		"team":                teamName,
		"_inject_agent_token": true,
		"reason":              reason,
	})
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok {
			switch httpErr.StatusCode {
			case 403:
				return "Error: only admins can request force-succession"
			case 404:
				return fmt.Sprintf("Error: team '%s' not found", teamName)
			case 400:
				var body map[string]interface{}
				json.Unmarshal([]byte(httpErr.Body), &body)
				errMsg, _ := body["error"].(string)
				if errMsg == "" {
					errMsg = "bad request"
				}
				return fmt.Sprintf("Error: %s", errMsg)
			case 401:
				return "Error: agent_token expired or invalid"
			}
		}
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status == "ok" {
		return fmt.Sprintf("Force-succession requested for '%s'. Owner has 60 seconds to respond with any team action to cancel.", teamName)
	}
	errMsg, _ := resp["error"].(string)
	return fmt.Sprintf("Error: %s", errMsg)
}

// PostToBoard appends a note to the team's shared board.
func (b *Backend) PostToBoard(teamName, text string) string {
	if b.agentID == "" {
		return "Error: AGENT_ID env not set (not running under agent-run?)"
	}

	resp, err := b.post("/teams/board", map[string]interface{}{
		"team_name": teamName,
		"text":      text,
		"sender":    b.agentID,
	})
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok {
			switch httpErr.StatusCode {
			case 404:
				return fmt.Sprintf("Error: team '%s' not found", teamName)
			case 403:
				return fmt.Sprintf("Error: you are not a member of team '%s'", teamName)
			}
		}
		return fmt.Sprintf("Error: %v", err)
	}
	if status, _ := resp["status"].(string); status == "ok" {
		return fmt.Sprintf("Posted to '%s' board", teamName)
	}
	errMsg, _ := resp["error"].(string)
	return fmt.Sprintf("Error: %s", errMsg)
}

// ReadBoard reads entries from the team's shared board.
func (b *Backend) ReadBoard(teamName string) string {
	resp, err := b.get(fmt.Sprintf("/teams/board?team_name=%s", urlEncode(teamName)))
	if err != nil {
		if httpErr, ok := err.(*api.HTTPError); ok && httpErr.StatusCode == 404 {
			return fmt.Sprintf("Error: team '%s' not found", teamName)
		}
		return fmt.Sprintf("Error: %v", err)
	}

	entries, _ := resp["entries"].([]interface{})
	if len(entries) == 0 {
		return fmt.Sprintf("Team '%s' board is empty.", teamName)
	}

	var lines []string
	for _, e := range entries {
		entry, ok := e.(map[string]interface{})
		if !ok {
			continue
		}
		author, _ := entry["author"].(string)
		text, _ := entry["text"].(string)
		ts, _ := entry["timestamp"].(string)
		lines = append(lines, fmt.Sprintf("[%s] **%s**: %s", ts, author, text))
	}

	return fmt.Sprintf("Team '%s' board (%d entries):\n\n%s", teamName, len(entries), strings.Join(lines, "\n"))
}
