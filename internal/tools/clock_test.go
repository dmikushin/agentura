package tools

import (
	"testing"
	"time"
)

func TestFmtDuration(t *testing.T) {
	tests := []struct {
		input time.Duration
		want  string
	}{
		{0, "00s"},
		{-1 * time.Second, "00s"},
		{5 * time.Second, "05s"},
		{30 * time.Second, "30s"},
		{59 * time.Second, "59s"},
		{60 * time.Second, "01m00s"},
		{61 * time.Second, "01m01s"},
		{90 * time.Second, "01m30s"},
		{5*time.Minute + 39*time.Second, "05m39s"},
		{24*time.Minute + 21*time.Second, "24m21s"},
		{59*time.Minute + 59*time.Second, "59m59s"},
		{60 * time.Minute, "60m00s"},
		{90 * time.Minute, "90m00s"},
	}
	for _, tt := range tests {
		got := fmtDuration(tt.input)
		if got != tt.want {
			t.Errorf("fmtDuration(%v): got %q, want %q", tt.input, got, tt.want)
		}
	}
}

func TestFmtDurationSubSecond(t *testing.T) {
	// Sub-second durations should show 00s
	got := fmtDuration(500 * time.Millisecond)
	if got != "00s" {
		t.Errorf("fmtDuration(500ms): got %q, want %q", got, "00s")
	}
}
