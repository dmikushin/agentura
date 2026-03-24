package tools

import (
	"testing"
)

func TestShellQuote(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"simple", "'simple'"},
		{"with space", "'with space'"},
		{"it's", "'it'\\''s'"},
		{"", "''"},
		{"a'b'c", "'a'\\''b'\\''c'"},
		{"/path/to/dir", "'/path/to/dir'"},
	}
	for _, tt := range tests {
		got := shellQuote(tt.input)
		if got != tt.want {
			t.Errorf("shellQuote(%q): got %q, want %q", tt.input, got, tt.want)
		}
	}
}

func TestFormatAgent(t *testing.T) {
	agent := map[string]interface{}{
		"agent_id":   "host@/cwd:123",
		"name":       "claude",
		"pane_id":    "%5",
		"started_at": "12:00",
		"hostname":   "host",
		"cmd":        []interface{}{"claude", "--dangerously-skip-permissions"},
	}
	result := formatAgent(agent)
	if result == "" {
		t.Error("formatAgent returned empty")
	}
	// Should contain the agent_id
	if got := result; !contains(got, "host@/cwd:123") {
		t.Errorf("missing agent_id in: %s", got)
	}
	if !contains(result, "claude") {
		t.Errorf("missing name in: %s", result)
	}
	if !contains(result, "--dangerously-skip-permissions") {
		t.Errorf("missing cmd in: %s", result)
	}
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && searchString(s, substr)
}

func searchString(s, sub string) bool {
	for i := 0; i <= len(s)-len(sub); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
