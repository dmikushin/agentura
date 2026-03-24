package sidecar

import (
	"fmt"
	"strings"
	"testing"
)

// newTestSidecar creates a minimal Sidecar for testing dedup logic.
func newTestSidecar() *Sidecar {
	return &Sidecar{
		prevHashes: make(map[string]bool),
	}
}

func TestDedupFirstCallReturnsAll(t *testing.T) {
	s := newTestSidecar()
	content := "line1\nline2\nline3"
	result := s.dedup(content)
	if result == "" {
		t.Error("first call should return all lines")
	}
	lines := splitLines(result)
	if len(lines) != 3 {
		t.Errorf("expected 3 lines, got %d: %q", len(lines), result)
	}
}

func TestDedupSecondCallSameContentEmpty(t *testing.T) {
	s := newTestSidecar()
	content := "line1\nline2\nline3"

	s.dedup(content)           // first call populates hashes
	result := s.dedup(content) // same content → no new lines

	if result != "" {
		t.Errorf("second call with same content should be empty, got: %q", result)
	}
}

func TestDedupPartialOverlap(t *testing.T) {
	s := newTestSidecar()

	s.dedup("line1\nline2\nline3")
	result := s.dedup("line2\nline3\nline4\nline5")

	if result == "" {
		t.Error("partial overlap should return new lines")
	}
	if !strings.Contains(result, "line4") {
		t.Errorf("result should contain line4, got: %q", result)
	}
	if !strings.Contains(result, "line5") {
		t.Errorf("result should contain line5, got: %q", result)
	}
}

func TestDedupContextHashing(t *testing.T) {
	s := newTestSidecar()

	// Same line "data" but different preceding context
	s.dedup("context-A\ndata")
	result := s.dedup("context-B\ndata")

	// "data" preceded by "context-B" has a different hash than "data" preceded by "context-A"
	// so it should appear as new
	if result == "" {
		t.Error("same line with different context should be treated as new")
	}
	if !strings.Contains(result, "data") {
		t.Errorf("result should contain 'data', got: %q", result)
	}
}

func TestDedupHashWindowLimit(t *testing.T) {
	s := newTestSidecar()

	// Generate 600 unique lines
	var lines []string
	for i := 0; i < 600; i++ {
		lines = append(lines, fmt.Sprintf("unique-line-%d", i))
	}
	s.dedup(strings.Join(lines, "\n"))

	// Hash set should be limited to 500
	if len(s.prevHashes) > 500 {
		t.Errorf("hash window should be limited to 500, got %d", len(s.prevHashes))
	}

	// Early lines (0-99) should have been evicted, so they appear as "new" again
	earlyLines := strings.Join(lines[:10], "\n")
	result := s.dedup(earlyLines)
	if result == "" {
		t.Error("evicted lines should appear as new after window limit")
	}
}

func TestDedupEmptyContent(t *testing.T) {
	s := newTestSidecar()
	result := s.dedup("")
	if result != "" {
		t.Errorf("empty content should return empty, got: %q", result)
	}
}

func TestSplitLines(t *testing.T) {
	tests := []struct {
		input string
		want  int
	}{
		{"a\nb\nc", 3},
		{"single", 1},
		{"", 0},
		{"a\n", 1},
		{"\n", 1}, // one empty string before the newline
		{"a\nb\n", 2},
	}
	for _, tt := range tests {
		got := splitLines(tt.input)
		if len(got) != tt.want {
			t.Errorf("splitLines(%q): got %d lines %v, want %d", tt.input, len(got), got, tt.want)
		}
	}
}

func TestJoinLines(t *testing.T) {
	if joinLines(nil) != "" {
		t.Error("joinLines(nil) should be empty")
	}
	if joinLines([]string{"a"}) != "a" {
		t.Error("joinLines single")
	}
	if joinLines([]string{"a", "b", "c"}) != "a\nb\nc" {
		t.Error("joinLines multiple")
	}
}
