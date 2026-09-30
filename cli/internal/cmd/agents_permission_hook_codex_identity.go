package cmd

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// codexPermissionModel reads only identity envelopes from this hook's session.
// Never consult shared config or another process's latest rollout. Bounded
// reads keep a large transcript from becoming an approval-hook memory spike.
func codexPermissionModel(sessionID, transcriptPath, turnID string) string {
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" {
		return ""
	}
	if transcriptPath == "" {
		// A session id is used as a literal filename suffix, never a glob pattern.
		for _, c := range sessionID {
			if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-') {
				return ""
			}
		}
		home := strings.TrimSpace(permissionHookGetenv("CODEX_HOME"))
		if home == "" {
			userHome, err := os.UserHomeDir()
			if err != nil {
				return ""
			}
			home = filepath.Join(userHome, ".codex")
		}
		matches, err := filepath.Glob(filepath.Join(home, "sessions", "*", "*", "*", "rollout-*-"+sessionID+".jsonl"))
		if err != nil || len(matches) != 1 {
			return ""
		}
		transcriptPath = matches[0]
	}
	file, err := os.Open(transcriptPath)
	if err != nil {
		return ""
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return ""
	}
	header, err := bufio.NewReader(io.LimitReader(file, 64*1024)).ReadBytes('\n')
	if err != nil {
		return ""
	}
	var line codexRolloutLine
	var meta codexSessionMeta
	if json.Unmarshal(header, &line) != nil || line.Type != "session_meta" || json.Unmarshal(line.Payload, &meta) != nil {
		return ""
	}
	if firstNonEmptyString(meta.ID, meta.SessionID) != sessionID {
		return ""
	}
	const tailBytes int64 = 1024 * 1024
	offset := max(int64(0), info.Size()-tailBytes)
	if _, err := file.Seek(offset, io.SeekStart); err != nil {
		return ""
	}
	tail, err := io.ReadAll(io.LimitReader(file, tailBytes))
	if err != nil {
		return ""
	}
	lines := bytes.Split(tail, []byte{'\n'})
	if offset > 0 {
		lines = lines[1:]
	}
	model := ""
	for _, raw := range lines {
		if json.Unmarshal(raw, &line) != nil || line.Type != "turn_context" {
			continue
		}
		var context codexTurnContext
		if json.Unmarshal(line.Payload, &context) != nil {
			continue
		}
		if turnID != "" && context.TurnID != turnID {
			continue
		}
		// A newer context without a model must clear an older model.
		model = strings.TrimSpace(context.Model)
	}
	return model
}
