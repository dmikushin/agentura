package tmux

import (
	"strings"
	"testing"
)

func TestTUIToMd_StripANSI(t *testing.T) {
	input := "\x1b[32mHello\x1b[0m world"
	got := TUIToMd(input)
	if got != "Hello world" {
		t.Errorf("ANSI strip: got %q, want %q", got, "Hello world")
	}
}

func TestTUIToMd_StripBoxDrawing(t *testing.T) {
	input := "╭─────────────────╮\n│ actual content   │\n╰─────────────────╯"
	got := TUIToMd(input)
	if !strings.Contains(got, "actual content") {
		t.Errorf("box drawing: content lost, got %q", got)
	}
	if strings.ContainsAny(got, "╭╮╰╯│─") {
		t.Errorf("box drawing: borders not stripped, got %q", got)
	}
}

func TestTUIToMd_PureBoxLine(t *testing.T) {
	input := "real content\n───────────────\nmore content"
	got := TUIToMd(input)
	if strings.Contains(got, "────") {
		t.Errorf("pure box line not removed, got %q", got)
	}
	if !strings.Contains(got, "real content") || !strings.Contains(got, "more content") {
		t.Errorf("real content lost, got %q", got)
	}
}

func TestTUIToMd_Spinners(t *testing.T) {
	tests := []string{
		"⠹ Thinking... (5s)",
		"  ⠙ Processing...(10s)",
		"✶ Observing... (10s)",
		"✻ Churned…",
	}
	for _, input := range tests {
		got := TUIToMd(input)
		if strings.TrimSpace(got) != "" {
			t.Errorf("spinner not removed: input=%q got=%q", input, got)
		}
	}
}

func TestTUIToMd_ClaudeCodeUI(t *testing.T) {
	tests := []string{
		"  ⏵⏵ bypass permissions",
		"  … +15 lines (ctrl+o to expand)",
		"  (ctrl+b ctrl+b to run in background)",
		"  Tip: use /help for commands",
		"  Running…",
	}
	for _, input := range tests {
		got := TUIToMd(input)
		if strings.TrimSpace(got) != "" {
			t.Errorf("Claude UI noise not removed: input=%q got=%q", input, got)
		}
	}
}

func TestTUIToMd_GeminiUI(t *testing.T) {
	tests := []string{
		"  YOLO mode (ctrl+y to toggle)",
		"  * Type your message",
		"  ✓ Shell  echo hello",
		"  ✓ ReadFile  /tmp/test",
		"  ✓ SearchText  pattern",
		"  esc to cancel",
		"  esc to interrupt",
	}
	for _, input := range tests {
		got := TUIToMd(input)
		if strings.TrimSpace(got) != "" {
			t.Errorf("Gemini UI noise not removed: input=%q got=%q", input, got)
		}
	}
}

func TestTUIToMd_GeminiThoughtBlock(t *testing.T) {
	input := "✦ <ctrl46>thought\n  I will analyze this code\n  Looking at the structure\n  Plan: first do X\nReal output here"
	got := TUIToMd(input)
	if strings.Contains(got, "I will") || strings.Contains(got, "Looking at") || strings.Contains(got, "Plan:") {
		t.Errorf("Gemini thought content not filtered, got %q", got)
	}
	if !strings.Contains(got, "Real output here") {
		t.Errorf("real content lost after thought block, got %q", got)
	}
}

func TestTUIToMd_PreserveRealContent(t *testing.T) {
	input := "func main() {\n    fmt.Println(\"hello\")\n}\n\nError: file not found"
	got := TUIToMd(input)
	if !strings.Contains(got, "func main()") {
		t.Errorf("code content lost, got %q", got)
	}
	if !strings.Contains(got, "Error: file not found") {
		t.Errorf("error message lost, got %q", got)
	}
}

func TestTUIToMd_CollapseBlankLines(t *testing.T) {
	input := "line1\n\n\n\n\nline2"
	got := TUIToMd(input)
	// Should have at most 2 blank lines between content
	parts := strings.Split(got, "\n")
	blankCount := 0
	maxBlank := 0
	for _, p := range parts {
		if strings.TrimSpace(p) == "" {
			blankCount++
			if blankCount > maxBlank {
				maxBlank = blankCount
			}
		} else {
			blankCount = 0
		}
	}
	if maxBlank > 2 {
		t.Errorf("blank lines not collapsed: max consecutive = %d, want <= 2", maxBlank)
	}
}

func TestTUIToMd_EmptyInput(t *testing.T) {
	got := TUIToMd("")
	if got != "" {
		t.Errorf("empty input: got %q, want empty", got)
	}
}

func TestTUIToMd_MixedContent(t *testing.T) {
	input := "\x1b[1m╭──────╮\x1b[0m\n│ Hello │\n╰──────╯\n⠹ Thinking (2s)\nActual result: 42"
	got := TUIToMd(input)
	if !strings.Contains(got, "Hello") {
		t.Errorf("boxed content lost, got %q", got)
	}
	if !strings.Contains(got, "Actual result: 42") {
		t.Errorf("result lost, got %q", got)
	}
	if strings.Contains(got, "Thinking") {
		t.Errorf("spinner not removed, got %q", got)
	}
}

func TestTUIToMd_CommonUIHints(t *testing.T) {
	tests := []string{
		"ctrl+r to cycle between modes",
		"Press Enter to edit",
	}
	for _, input := range tests {
		got := TUIToMd(input)
		if strings.TrimSpace(got) != "" {
			t.Errorf("UI hint not removed: input=%q got=%q", input, got)
		}
	}
}
