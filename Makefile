.PHONY: build clean test linux-amd64 linux-arm64 darwin-amd64

build:
	CGO_ENABLED=0 go build -o bin/agentura-run ./cmd/agentura-run
	CGO_ENABLED=0 go build -o bin/agentura-mcp ./cmd/agentura-mcp

linux-amd64:
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o bin/agentura-run-linux-amd64 ./cmd/agentura-run
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o bin/agentura-mcp-linux-amd64 ./cmd/agentura-mcp

linux-arm64:
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -o bin/agentura-run-linux-arm64 ./cmd/agentura-run
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -o bin/agentura-mcp-linux-arm64 ./cmd/agentura-mcp

darwin-amd64:
	GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 go build -o bin/agentura-run-darwin-amd64 ./cmd/agentura-run
	GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 go build -o bin/agentura-mcp-darwin-amd64 ./cmd/agentura-mcp

test:
	go test ./...

clean:
	rm -rf bin/
