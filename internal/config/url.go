// Package config provides build-time configuration for agentura binaries.
package config

import "os"

// DefaultMonitorURL is set at build time via -ldflags:
//
//	-X github.com/dmikushin/agentura/internal/config.DefaultMonitorURL=https://...
//
// If AGENTURA_URL env is set, it takes precedence.
var DefaultMonitorURL string

// MonitorURL returns the agentura server URL.
// Priority: AGENTURA_URL env > compiled default.
func MonitorURL() string {
	if u := os.Getenv("AGENTURA_URL"); u != "" {
		return u
	}
	return DefaultMonitorURL
}
