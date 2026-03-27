package main

import (
	"encoding/json"
	"testing"
	"time"
)

func TestFmtDur(t *testing.T) {
	tests := []struct {
		input time.Duration
		want  string
	}{
		{0, "00s"},
		{-1 * time.Second, "00s"},
		{5 * time.Second, "05s"},
		{30 * time.Second, "30s"},
		{60 * time.Second, "01m00s"},
		{94 * time.Second, "01m34s"},
		{5*time.Minute + 39*time.Second, "05m39s"},
		{24*time.Minute + 21*time.Second, "24m21s"},
	}
	for _, tt := range tests {
		got := fmtDur(tt.input)
		if got != tt.want {
			t.Errorf("fmtDur(%v): got %q, want %q", tt.input, got, tt.want)
		}
	}
}

func TestFmtDurFirstCall(t *testing.T) {
	got := fmtDur(0)
	if got != "00s" {
		t.Errorf("fmtDur(0): got %q, want %q", got, "00s")
	}
}

func TestClockStateSerialization(t *testing.T) {
	state := clockState{
		Timezone:     "Europe/Zurich",
		LastCallUnix: 1711500000,
		Team:         "test-team",
	}
	data, err := json.Marshal(state)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var decoded clockState
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	if decoded.Timezone != state.Timezone {
		t.Errorf("timezone: got %q, want %q", decoded.Timezone, state.Timezone)
	}
	if decoded.LastCallUnix != state.LastCallUnix {
		t.Errorf("last_call: got %d, want %d", decoded.LastCallUnix, state.LastCallUnix)
	}
	if decoded.Team != state.Team {
		t.Errorf("team: got %q, want %q", decoded.Team, state.Team)
	}
}

func TestFetchTimezoneDefault(t *testing.T) {
	// Without AGENTURA_URL, should return UTC
	tz := fetchTimezone()
	if tz != "UTC" {
		t.Errorf("fetchTimezone() without URL: got %q, want %q", tz, "UTC")
	}
}
