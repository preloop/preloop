package cmd

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

const syntheticAgentKey = "agt_SYNTHETIC0000000000000000000000000000000001"

func TestDiffConfigDocumentsIsMinimal(t *testing.T) {
	original := map[string]interface{}{
		"theme": "dark",
		"env": map[string]interface{}{
			"KEEP":                    "1",
			"CLAUDE_CODE_OAUTH_TOKEN": "old",
		},
		"permissions": map[string]interface{}{"allow": []interface{}{"Bash(ls)"}},
	}
	managed := map[string]interface{}{
		"theme": "dark",
		"env": map[string]interface{}{
			"KEEP":               "1",
			"ANTHROPIC_BASE_URL": "https://preloop.example.com/anthropic",
		},
		"permissions": map[string]interface{}{"allow": []interface{}{"Bash(ls)"}},
		"servers":     map[string]interface{}{"preloop": map[string]interface{}{"url": "u"}},
	}
	merge, remove := diffConfigDocuments(original, managed)
	want := map[string]interface{}{
		"env":     map[string]interface{}{"ANTHROPIC_BASE_URL": "https://preloop.example.com/anthropic"},
		"servers": map[string]interface{}{"preloop": map[string]interface{}{"url": "u"}},
	}
	if !reflect.DeepEqual(merge, want) {
		t.Fatalf("merge not minimal:\n got %v\nwant %v", merge, want)
	}
	if !reflect.DeepEqual(remove, []string{"env.CLAUDE_CODE_OAUTH_TOKEN"}) {
		t.Fatalf("remove: %v", remove)
	}
}

func TestRedactedPlanHoldsNoKey(t *testing.T) {
	plan := manualApplyPlan{
		Files: []manualFileChange{{
			Path:   "/home/agent/.claude/settings.json",
			Format: "json",
			Exists: true,
			Merge: map[string]interface{}{
				"env": map[string]interface{}{"ANTHROPIC_API_KEY": syntheticAgentKey},
				"servers": map[string]interface{}{"preloop": map[string]interface{}{
					"headers": map[string]interface{}{"Authorization": "Bearer " + syntheticAgentKey},
				}},
				"approved": []interface{}{syntheticAgentKey[len(syntheticAgentKey)-20:]},
			},
		}},
		Env: map[string]string{"ANTHROPIC_API_KEY": syntheticAgentKey},
	}
	payload := compactJSON(plan.asServerPayload(syntheticAgentKey))
	if strings.Contains(payload, syntheticAgentKey) || strings.Contains(payload, syntheticAgentKey[len(syntheticAgentKey)-20:]) {
		t.Fatalf("server payload leaks the key: %s", payload)
	}
	if !strings.Contains(payload, manualAgentKeyPlaceholder) {
		t.Fatalf("placeholder missing: %s", payload)
	}
	if plan.Files[0].Merge["env"].(map[string]interface{})["ANTHROPIC_API_KEY"] != syntheticAgentKey {
		t.Fatal("redaction must not mutate the plan the user is shown")
	}
}

func TestDiagnoseJSONSyntaxNamesLineAndSlip(t *testing.T) {
	cases := []struct {
		name, body string
		line       int
		reason     string
	}{
		{"trailing comma", "{\n  \"env\": {\n    \"A\": \"1\",\n  }\n}\n", 4, "trailing comma"},
		{"missing comma", "{\n  \"a\": \"1\"\n  \"b\": \"2\"\n}\n", 3, "missing comma; add one at the end of line 2"},
		{"missing quote", "{\n  \"a\": \"1\",\n  b\": \"2\"\n}\n", 3, "missing quote"},
		{"unterminated", "{\n  \"a\": \"1\n}\n", 2, "missing double quote"},
		{"cut off", "{\n  \"a\": {\n", 2, "ends early"},
		{"empty", "  \n", 1, "empty"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			problem := diagnoseJSONSyntax([]byte(tc.body))
			if problem == nil {
				t.Fatal("expected a problem")
			}
			if problem.Line != tc.line || !strings.Contains(problem.Reason, tc.reason) {
				t.Fatalf("got line %d %q, want line %d containing %q", problem.Line, problem.Reason, tc.line, tc.reason)
			}
		})
	}
	if diagnoseJSONSyntax([]byte(`{"a": 1}`)) != nil {
		t.Fatal("valid JSON reported as broken")
	}
}

func manualTestFile(path string) manualFileChange {
	return manualFileChange{
		Path:   path,
		Role:   writeRoleConfig,
		Format: "json",
		Exists: true,
		Merge: map[string]interface{}{"env": map[string]interface{}{
			"ANTHROPIC_BASE_URL": "https://preloop.example.com/anthropic",
			"ANTHROPIC_API_KEY":  manualAgentKeyPlaceholder,
		}},
		Remove: []string{"env.CLAUDE_CODE_OAUTH_TOKEN"},
	}
}

func TestVerifyManualFileSaysExactlyWhatIsMissing(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "settings.json")
	file := manualTestFile(path)
	keyHash := sha256Hex(syntheticAgentKey)

	check := verifyManualFile(file, path, keyHash)
	if check.ok() || !strings.Contains(check.Problems[0], "does not exist") {
		t.Fatalf("missing file: %v", check.Problems)
	}

	write := func(body string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("{\n  \"env\": {\n    \"ANTHROPIC_BASE_URL\": \"https://preloop.example.com/anthropic\",\n  }\n}\n")
	check = verifyManualFile(file, path, keyHash)
	if check.ok() || !strings.Contains(check.Problems[0], "line 4") || !strings.Contains(check.Problems[0], "trailing comma") {
		t.Fatalf("trailing comma: %v", check.Problems)
	}

	write(`{"env": {"ANTHROPIC_BASE_URL": "https://other.example.com", "ANTHROPIC_API_KEY": "<AGENT_KEY>", "CLAUDE_CODE_OAUTH_TOKEN": "x"}}`)
	check = verifyManualFile(file, path, keyHash)
	joined := strings.Join(check.Problems, "\n")
	for _, want := range []string{
		"env.ANTHROPIC_API_KEY still holds the placeholder",
		`env.ANTHROPIC_BASE_URL should be "https://preloop.example.com/anthropic"`,
		"env.CLAUDE_CODE_OAUTH_TOKEN is still present",
	} {
		if !strings.Contains(joined, want) {
			t.Fatalf("missing %q in:\n%s", want, joined)
		}
	}

	write(`{"env": {"ANTHROPIC_BASE_URL": "https://preloop.example.com/anthropic", "ANTHROPIC_API_KEY": "agt_someone_elses_key"}}`)
	check = verifyManualFile(file, path, keyHash)
	if check.ok() || !strings.Contains(check.Problems[0], "does not hold the key issued for this setup") {
		t.Fatalf("wrong key: %v", check.Problems)
	}
	if strings.Contains(strings.Join(check.Problems, " "), "agt_someone") {
		t.Fatal("a key value must never be echoed")
	}

	write(`{"theme": "dark", "env": {"ANTHROPIC_BASE_URL": "https://preloop.example.com/anthropic", "ANTHROPIC_API_KEY": "` + syntheticAgentKey + `"}}`)
	if check = verifyManualFile(file, path, keyHash); !check.ok() {
		t.Fatalf("applied change reported as missing: %v", check.Problems)
	}
}

func TestVerifyManualEnv(t *testing.T) {
	env := map[string]string{
		"ANTHROPIC_BASE_URL": "https://preloop.example.com/anthropic",
		"ANTHROPIC_API_KEY":  manualAgentKeyPlaceholder,
	}
	keyHash := sha256Hex(syntheticAgentKey)
	problems := verifyManualEnv(env, func(string) string { return "" }, keyHash)
	if len(problems) != 2 || !strings.Contains(problems[0], "ANTHROPIC_API_KEY is not set") {
		t.Fatalf("unset env: %v", problems)
	}
	actual := map[string]string{
		"ANTHROPIC_BASE_URL": "https://preloop.example.com/anthropic",
		"ANTHROPIC_API_KEY":  syntheticAgentKey,
	}
	if problems := verifyManualEnv(env, func(k string) string { return actual[k] }, keyHash); len(problems) != 0 {
		t.Fatalf("matching env: %v", problems)
	}
}

func TestResumeTokenRoundTripCarriesNoSecret(t *testing.T) {
	state := manualResumeState{
		Version:            1,
		AgentName:          "Claude Code",
		ConfigPath:         "/home/agent/.claude/settings.json",
		RuntimePrincipalID: "claude-code-abc",
		AgentID:            "agent-1",
		EnrollmentID:       "enrollment-1",
		TokenSHA256:        sha256Hex(syntheticAgentKey),
		BaseURL:            "https://preloop.example.com",
		CreatedAt:          time.Unix(1790000000, 0).UTC(),
	}
	token := encodeManualResumeToken(state)
	if !strings.HasPrefix(token, manualResumeTokenPrefix) || strings.ContainsAny(token, " \n") {
		t.Fatalf("token should be one copyable word: %q", token)
	}
	decoded, err := decodeManualResumeToken("  " + token + "\n")
	if err != nil {
		t.Fatal(err)
	}
	if decoded.EnrollmentID != "enrollment-1" || decoded.TokenSHA256 != state.TokenSHA256 {
		t.Fatalf("round trip: %+v", decoded)
	}
	if _, err := decodeManualResumeToken(token[:len(token)-9]); err == nil {
		t.Fatal("a truncated token must be refused")
	}
	if _, err := decodeManualResumeToken("agt_notatoken"); err == nil {
		t.Fatal("a non-token must be refused")
	}
}

func TestWriteManualBundleIsPrivate(t *testing.T) {
	skipIfRootOrWindows(t)
	dir := filepath.Join(t.TempDir(), "bundle")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	file := manualTestFile("/home/agent/.claude/settings.json")
	file.full = map[string]interface{}{"theme": "dark"}
	plan := manualApplyPlan{Files: []manualFileChange{file}, Env: map[string]string{"A": "b"}}
	if err := writeManualBundle(dir, "instructions", plan, "plr1.x"); err != nil {
		t.Fatal(err)
	}
	info, _ := os.Stat(dir)
	if info.Mode().Perm() != 0o700 {
		t.Fatalf("bundle dir mode %o", info.Mode().Perm())
	}
	for _, name := range []string{"INSTRUCTIONS.txt", "settings.merge.json", "settings.json", "preloop.env", "resume-token.txt"} {
		fileInfo, err := os.Stat(filepath.Join(dir, name))
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if fileInfo.Mode().Perm() != 0o600 {
			t.Fatalf("%s mode %o", name, fileInfo.Mode().Perm())
		}
	}
}

func TestRenderManualInstructionsIsCopyable(t *testing.T) {
	file := manualTestFile("/home/agent/.claude/settings.json")
	file.Merge["env"].(map[string]interface{})["ANTHROPIC_API_KEY"] = syntheticAgentKey
	file.HostSource = "/home/jane/.claude/settings.json"
	file.Reason = "read-only file system"
	plan := manualApplyPlan{
		Files:        []manualFileChange{file},
		Env:          map[string]string{"ANTHROPIC_API_KEY": syntheticAgentKey},
		SettingsFlag: "claude --settings /tmp/x/settings.merge.json",
		Limitations:  []string{"Environment variables: no approval hooks."},
	}
	text := renderManualInstructions("Claude Code", plan, "", `preloop agents onboard --resume "Claude Code"`)
	block := "\n{\n  \"env\": {\n    \"ANTHROPIC_API_KEY\": \"" + syntheticAgentKey + "\",\n"
	if !strings.Contains(text, block) {
		t.Fatalf("the JSON block must start at column 0 so it pastes cleanly:\n%s", text)
	}
	for _, want := range []string{
		"probably at: /home/jane/.claude/settings.json",
		"shown here once",
		"Remove these keys if present: env.CLAUDE_CODE_OAUTH_TOKEN",
		"export ANTHROPIC_API_KEY='" + syntheticAgentKey + "'",
		"claude --settings",
		"What each form does not enable",
		`preloop agents onboard --resume "Claude Code"`,
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("missing %q in:\n%s", want, text)
		}
	}
	if strings.Contains(text, "—") {
		t.Fatal("no em dashes in user-facing text")
	}
}
