.PHONY: build clean test linux-amd64 linux-arm64 darwin-amd64

# Read default AGENTURA_URL from .env (compiled into binaries)
AGENTURA_URL := $(shell grep '^AGENTURA_URL=' .env 2>/dev/null | cut -d= -f2)
LDFLAGS := -X github.com/dmikushin/agentura/internal/config.DefaultMonitorURL=$(AGENTURA_URL)

build:
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-run ./cmd/agentura-run
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-mcp ./cmd/agentura-mcp
	CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-mcp-backend ./cmd/agentura-mcp-backend

linux-amd64:
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-run-linux-amd64 ./cmd/agentura-run
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-mcp-linux-amd64 ./cmd/agentura-mcp
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-mcp-backend-linux-amd64 ./cmd/agentura-mcp-backend

linux-arm64:
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-run-linux-arm64 ./cmd/agentura-run
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-mcp-linux-arm64 ./cmd/agentura-mcp
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-mcp-backend-linux-arm64 ./cmd/agentura-mcp-backend

darwin-amd64:
	GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-run-darwin-amd64 ./cmd/agentura-run
	GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-mcp-darwin-amd64 ./cmd/agentura-mcp
	GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 go build -ldflags "$(LDFLAGS)" -o bin/agentura-mcp-backend-darwin-amd64 ./cmd/agentura-mcp-backend

test:
	go test ./...

clean:
	rm -rf bin/
