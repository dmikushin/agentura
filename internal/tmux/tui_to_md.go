package tmux

import (
	"regexp"
	"strings"
)

// Box-drawing characters (light+heavy+double+rounded)
var boxChars = func() map[rune]bool {
	m := make(map[rune]bool)
	for _, r := range "─━│┃┄┅┆┇┈┉┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣┤┥┦┧┨┩┪┫┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼┽┾┿╀╁╂╃╄╅╆╇╈╉╊╋╌╍╎╏═║╒╓╔╕╖╗╘╙╚╛╜╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬╭╮╯╰╴╵╶╷╸╹╺╻╼╽╾╿" {
		m[r] = true
	}
	return m
}()

// ANSI escape sequence pattern
var ansiRE = regexp.MustCompile(`\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[@-~]`)

// Noise patterns — lines matching these are removed entirely.
// PRINCIPLE: better to let some noise through than to lose real content.
var noisePatterns = []*regexp.Regexp{
	// === Spinners (both CLIs) ===
	regexp.MustCompile(`^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠛⠽⠺⠰⠶⠾⠿⣿⣽⣻⣯⣟⡿⢿⣾⣷⣶⣤⣀⡀⠄⠂⠁⠈⠐⠠⢀⣀⣤⣶⣷⣾⠿⡿⣟⣯⣻⣽⣿]`),
	regexp.MustCompile(`^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠛⠽⠺⠰⠶⠾]\s+\w+.*\(\d+[sm]`),
	regexp.MustCompile(`^\s*[✶✻✽✸✷✹✺]\s+\w+.*\(\d+[sm]`),
	regexp.MustCompile(`^\s*[✶✻✽✸✷✹✺]\s+\w+…`),

	// === Claude Code UI ===
	regexp.MustCompile(`^\s*⏵⏵\s+bypass permissions`),
	regexp.MustCompile(`^\s*… \+\d+ lines \(ctrl\+o`),
	regexp.MustCompile(`^\s*\(ctrl\+b ctrl\+b.*to run in background\)`),
	regexp.MustCompile(`^\s*Tip:`),
	regexp.MustCompile(`^\s*Running…`),
	regexp.MustCompile(`^\s*⎿\s*\{`),
	regexp.MustCompile(`^\s*"(success|message|result)"`),
	regexp.MustCompile(`^\s*\}`),

	// === Gemini CLI UI ===
	regexp.MustCompile(`^\s*YOLO mode \(ctrl`),
	regexp.MustCompile(`^\s*\*\s+Type your message`),
	regexp.MustCompile(`^\s*You are running Gemini CLI`),
	regexp.MustCompile(`^\s*no sandbox \(see /docs\)`),
	regexp.MustCompile(`^\s*Auto \(Gemini \d\) /model`),
	regexp.MustCompile(`^\s*Queued \(press .* to edit\)`),
	regexp.MustCompile(`^\s*esc to (cancel|interrupt)`),
	regexp.MustCompile(`^\s*CRITICAL INSTRUCTION`),
	regexp.MustCompile(`^\s*<ctrl\d+>`),
	regexp.MustCompile(`^\s*✓\s+Shell\s`),
	regexp.MustCompile(`^\s*✓\s+Read(File|Folder)\s`),
	regexp.MustCompile(`^\s*✓\s+Search(Text|Files)\s`),
	regexp.MustCompile(`^\s*Listed \d+ item`),
	regexp.MustCompile(`^\s*\(no new content\)`),
	regexp.MustCompile(`^\s*\{"\s*success`),
	regexp.MustCompile(`^\s*~\s`),
	regexp.MustCompile(`^.*no sandbox \(see /docs\).*Auto \(Gemini`),

	// === Common UI ===
	regexp.MustCompile(`^\s*ctrl\+[a-z].*to (expand|run|toggle|cycle)`),
	regexp.MustCompile(`^\s*Press .* to (edit|cycle|toggle)`),
	regexp.MustCompile(`^─+$`),
	regexp.MustCompile(`^━+$`),

	// === Gemini thought blocks ===
	regexp.MustCompile(`^\s*✦\s*<ctrl\d+>`),
}

var geminiThoughtStartRE = regexp.MustCompile(`^\s*✦\s*<ctrl\d+>`)
var geminiThoughtContentRE = regexp.MustCompile(`^\s{2,}(CRITICAL INSTRUCTION|I will|I need|Plan:|Generating|Wait,|Let me|Looking at|I must|The task|Claude)`)

// Gemini thought block prefixes (for lines after thought start)
var geminiThoughtPrefixes = []string{
	"CRITICAL", "I will", "I need", "Plan:", "Generating",
	"Wait,", "Let me", "Looking at", "I must", "The task",
	"Claude has", "The previous", "I have", "Now I",
	"I'll", "I'm", "I should", "I can", "The user",
	"This is", "So I", "My plan", "OK", "Hmm",
}

// TUIToMd cleans TUI terminal output into readable markdown.
func TUIToMd(text string) string {
	lines := strings.Split(text, "\n")
	var result []string
	inGeminiThought := false

	for _, line := range lines {
		// Strip ANSI escape sequences
		clean := ansiRE.ReplaceAllString(line, "")

		stripped := strings.TrimSpace(clean)
		if stripped == "" {
			if !inGeminiThought {
				result = append(result, "")
			}
			continue
		}

		// --- Gemini thought block filtering ---
		if geminiThoughtStartRE.MatchString(stripped) {
			inGeminiThought = true
			continue
		}
		if inGeminiThought {
			if geminiThoughtContentRE.MatchString(clean) {
				continue
			}
			if hasAnyPrefix(stripped, geminiThoughtPrefixes) {
				continue
			}
			if strings.HasPrefix(clean, "  ") {
				continue
			}
			inGeminiThought = false
		}

		// Skip lines that are purely box-drawing decorations
		if isAllBoxChars(stripped) {
			continue
		}

		// Remove box borders FIRST, then check noise patterns
		clean = stripBoxBorders(clean)
		stripped = strings.TrimSpace(clean)

		if stripped == "" {
			continue
		}

		// Skip noise patterns (checked AFTER border removal)
		if matchesAnyNoise(stripped) {
			continue
		}

		result = append(result, clean)
	}

	// Collapse multiple blank lines into max 2
	var output []string
	blankCount := 0
	for _, line := range result {
		if strings.TrimSpace(line) == "" {
			blankCount++
			if blankCount <= 2 {
				output = append(output, "")
			}
		} else {
			blankCount = 0
			output = append(output, line)
		}
	}

	return strings.TrimSpace(strings.Join(output, "\n"))
}

func isAllBoxChars(s string) bool {
	for _, r := range s {
		if !boxChars[r] && r != ' ' && r != '\t' {
			return false
		}
	}
	return true
}

func hasAnyPrefix(s string, prefixes []string) bool {
	for _, p := range prefixes {
		if strings.HasPrefix(s, p) {
			return true
		}
	}
	return false
}

func matchesAnyNoise(s string) bool {
	for _, p := range noisePatterns {
		if p.MatchString(s) {
			return true
		}
	}
	return false
}

func stripBoxBorders(line string) string {
	runes := []rune(line)
	n := len(runes)

	// Remove leading box chars + spaces
	i := 0
	for i < n {
		if boxChars[runes[i]] {
			i++
			// Also skip one space after a box char
			if i < n && runes[i] == ' ' {
				i++
			}
		} else if runes[i] == ' ' && i < 3 {
			// Only skip leading spaces if within first 3 chars
			break
		} else {
			break
		}
	}

	// Remove trailing box chars + spaces
	j := n
	for j > i {
		if boxChars[runes[j-1]] {
			j--
		} else if runes[j-1] == ' ' && j-2 >= i && boxChars[runes[j-2]] {
			j--
		} else {
			break
		}
	}

	return string(runes[i:j])
}
