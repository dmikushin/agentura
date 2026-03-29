.PHONY: build clean test launchers impl impl-linux-amd64 impl-linux-arm64

# Read default AGENTURA_URL from .env (compiled into binaries)
AGENTURA_URL := $(shell grep '^AGENTURA_URL=' .env 2>/dev/null | cut -d= -f2)
LDFLAGS := -X github.com/dmikushin/agentura/internal/config.DefaultMonitorURL=$(AGENTURA_URL)
LAUNCHER_LDFLAGS := -X main.DefaultMonitorURL=$(AGENTURA_URL)

IMPL_CMDS := ./cmd/agentura-run ./cmd/agentura-mcp ./cmd/agentura-mcp-backend ./cmd/agentura-clock
IMPL_NAMES := agentura-run agentura-mcp agentura-mcp-backend agentura-clock

# Build both launchers and implementations (local platform)
build: launchers impl

# Thin launchers — single source, symlinked by argv[0]
launchers:
	CGO_ENABLED=0 go build -ldflags "$(LAUNCHER_LDFLAGS)" -o bin/agentura-run ./cmd/launcher
	cp bin/agentura-run bin/agentura-mcp
	cp bin/agentura-run bin/agentura-mcp-backend
	cp bin/agentura-run bin/agentura-clock

# Implementation binaries (local platform)
impl:
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-run-impl ./cmd/agentura-run
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-mcp-impl ./cmd/agentura-mcp
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-mcp-backend-impl ./cmd/agentura-mcp-backend
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-clock-impl ./cmd/agentura-clock

# Cross-compiled implementations for Docker container
impl-linux-amd64:
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/impl-linux-amd64/agentura-run ./cmd/agentura-run
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/impl-linux-amd64/agentura-mcp ./cmd/agentura-mcp
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/impl-linux-amd64/agentura-mcp-backend ./cmd/agentura-mcp-backend
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/impl-linux-amd64/agentura-clock ./cmd/agentura-clock

impl-linux-arm64:
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/impl-linux-arm64/agentura-run ./cmd/agentura-run
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/impl-linux-arm64/agentura-mcp ./cmd/agentura-mcp
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/impl-linux-arm64/agentura-mcp-backend ./cmd/agentura-mcp-backend
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/impl-linux-arm64/agentura-clock ./cmd/agentura-clock

# Cross-compiled thin launchers for remote deployment
launchers-linux-amd64:
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LAUNCHER_LDFLAGS)" -o bin/agentura-run-linux-amd64 ./cmd/launcher
	cp bin/agentura-run-linux-amd64 bin/agentura-mcp-linux-amd64
	cp bin/agentura-run-linux-amd64 bin/agentura-mcp-backend-linux-amd64
	cp bin/agentura-run-linux-amd64 bin/agentura-clock-linux-amd64

launchers-linux-arm64:
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "$(LAUNCHER_LDFLAGS)" -o bin/agentura-run-linux-arm64 ./cmd/launcher
	cp bin/agentura-run-linux-arm64 bin/agentura-mcp-linux-arm64
	cp bin/agentura-run-linux-arm64 bin/agentura-mcp-backend-linux-arm64
	cp bin/agentura-run-linux-arm64 bin/agentura-clock-linux-arm64

# Legacy aliases
linux-amd64: launchers-linux-amd64 impl-linux-amd64
linux-arm64: launchers-linux-arm64 impl-linux-arm64

test:
	go test ./...

clean:
	rm -rf bin/
