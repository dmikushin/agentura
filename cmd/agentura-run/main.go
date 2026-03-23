// agentura-run — agent launcher + sidecar process.
//
// Phase 1 stub: validates structure compiles.
// Full implementation in Phase 2.
package main

import (
	"fmt"
	"os"
)

func main() {
	if len(os.Args) < 2 || os.Args[1] == "-h" || os.Args[1] == "--help" {
		fmt.Println("Usage: agentura-run {--claude | --gemini | <command> [args...]}")
		fmt.Println()
		fmt.Println("Agentura agent launcher — registers with the server, deploys")
		fmt.Println("skills, then launches agent as subprocess with sidecar.")
		fmt.Println()
		fmt.Println("Environment:")
		fmt.Println("  AGENTURA_URL    Server URL (required)")
		fmt.Println("  AGENTURA_TOKEN  Delegation token (set automatically for remote agents)")
		os.Exit(0)
	}

	fmt.Fprintln(os.Stderr, "[agentura-run] Phase 1 stub — full implementation in Phase 2")
	os.Exit(1)
}
