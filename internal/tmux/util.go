package tmux

import (
	"context"
	"strings"
	"time"
)

func timeoutContext(d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), d)
}

func stringReader(s string) *strings.Reader {
	return strings.NewReader(s)
}
