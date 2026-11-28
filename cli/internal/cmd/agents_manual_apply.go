package cmd

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"time"

	"github.com/pelletier/go-toml/v2"
)

// Manual apply: when an agent's configuration cannot be written from where the
// CLI runs (a read-only bind mount, another owner, a read-only root), the
// user makes the change by hand. This file holds what that needs: the minimal
// merge between the file as it is and as onboarding would write it, its
// rendering, the check that the change is in place, and the resume token.

const (
	manualAgentKeyPlaceholder     = "<AGENT_KEY>"
	manualAgentKeyTailPlaceholder = "<AGENT_KEY_LAST_20>"
	enrollmentStatusPendingLocal  = "pending_local_config"
	manualSetupDocsURL            = "https://docs.preloop.ai/guide/clients/sandboxed-agents/"
	manualResumeTokenPrefix       = "plr1."
)

// manualFileChange is the change one file needs.
type manualFileChange struct {
	Path   string `json:"path"`
	Label  string `json:"label,omitempty"`
	Role   string `json:"role,omitempty"`
	Format string `json:"format"`
	Exists bool   `json:"exists"`
	// Merge holds only the keys to add or change. For a file that does not
	// exist it is the whole file.
	Merge map[string]interface{} `json:"merge"`
	// Remove lists dotted key paths that must not remain.
	Remove     []string `json:"remove,omitempty"`
	HostSource string   `json:"host_source,omitempty"`
	Reason     string   `json:"reason,omitempty"`
	// full is the complete resulting document (never sent to the server).
	full map[string]interface{}
}

// manualApplyPlan is everything the user is asked to do for one agent.
type manualApplyPlan struct {
	Files        []manualFileChange `json:"files"`
	Env          map[string]string  `json:"env,omitempty"`
	SettingsFlag string             `json:"settings_flag,omitempty"`
	Limitations  []string           `json:"limitations,omitempty"`
}

// diffConfigDocuments returns the minimal merge that turns original into
// managed, and the dotted paths of keys managed no longer has.
func diffConfigDocuments(original, managed map[string]interface{}) (map[string]interface{}, []string) {
	merge := map[string]interface{}{}
	var remove []string
	diffConfigInto(original, managed, "", merge, &remove)
	sort.Strings(remove)
	return merge, remove
}

func diffConfigInto(original, managed map[string]interface{}, prefix string, merge map[string]interface{}, remove *[]string) {
	for key, want := range managed {
		have, present := original[key]
		wantMap, wantIsMap := asObjectMap(want)
		haveMap, haveIsMap := asObjectMap(have)
		if present && wantIsMap && haveIsMap {
			child := map[string]interface{}{}
			diffConfigInto(haveMap, wantMap, prefix+key+".", child, remove)
			if len(child) > 0 {
				merge[key] = child
			}
			continue
		}
		if present && configValuesEqual(have, want) {
			continue
		}
		merge[key] = want
	}
	for key := range original {
		if _, kept := managed[key]; !kept {
			*remove = append(*remove, prefix+key)
		}
	}
}

// configValuesEqual compares two decoded config values by their JSON form so
// int64 versus float64 and []string versus []interface{} do not read as a
// difference.
func configValuesEqual(a, b interface{}) bool {
	if reflect.DeepEqual(a, b) {
		return true
	}
	left, errLeft := json.Marshal(a)
	right, errRight := json.Marshal(b)
	return errLeft == nil && errRight == nil && bytes.Equal(left, right)
}

// redactAgentKey returns a deep copy of value with the agent key (and its
// 20 character tail, which Claude Code stores as the approval fingerprint)
// replaced by placeholders.
func redactAgentKey(value interface{}, token string) interface{} {
	token = strings.TrimSpace(token)
	switch typed := value.(type) {
	case map[string]interface{}:
		out := make(map[string]interface{}, len(typed))
		for key, item := range typed {
			out[key] = redactAgentKey(item, token)
		}
		return out
	case []interface{}:
		out := make([]interface{}, len(typed))
		for i, item := range typed {
			out[i] = redactAgentKey(item, token)
		}
		return out
	case []string:
		out := make([]interface{}, len(typed))
		for i, item := range typed {
			out[i] = redactAgentKey(item, token)
		}
		return out
	case string:
		if token == "" {
			return typed
		}
		redacted := strings.ReplaceAll(typed, token, manualAgentKeyPlaceholder)
		if len(token) > 20 {
			redacted = strings.ReplaceAll(redacted, token[len(token)-20:], manualAgentKeyTailPlaceholder)
		}
		return redacted
	default:
		return value
	}
}

// redacted returns the plan with every secret replaced by a placeholder. This
// is the only form that is stored on the server or in the resume state.
func (p manualApplyPlan) redacted(token string) manualApplyPlan {
	out := manualApplyPlan{SettingsFlag: p.SettingsFlag, Limitations: append([]string{}, p.Limitations...)}
	for _, file := range p.Files {
		copyFile := file
		copyFile.full = nil
		if merged, ok := redactAgentKey(file.Merge, token).(map[string]interface{}); ok {
			copyFile.Merge = merged
		}
		out.Files = append(out.Files, copyFile)
	}
	if len(p.Env) > 0 {
		out.Env = map[string]string{}
		for key, value := range p.Env {
			out.Env[key] = strings.ReplaceAll(value, token, manualAgentKeyPlaceholder)
		}
	}
	return out
}

func (p manualApplyPlan) asServerPayload(token string) map[string]interface{} {
	data, err := json.Marshal(p.redacted(token))
	if err != nil {
		return map[string]interface{}{}
	}
	var payload map[string]interface{}
	if err := json.Unmarshal(data, &payload); err != nil {
		return map[string]interface{}{}
	}
	return payload
}

func manualApplyPlanFromPayload(value interface{}) (*manualApplyPlan, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var plan manualApplyPlan
	if err := json.Unmarshal(data, &plan); err != nil {
		return nil, err
	}
	return &plan, nil
}

func (p manualApplyPlan) fileForRole(role string) *manualFileChange {
	for i := range p.Files {
		if p.Files[i].Role == role {
			return &p.Files[i]
		}
	}
	return nil
}

// renderConfigBlock prints a document in the file's own format.
func renderConfigBlock(format string, doc map[string]interface{}) string {
	if format == "toml" {
		data, err := toml.Marshal(doc)
		if err == nil {
			return strings.TrimRight(string(data), "\n")
		}
	}
	data, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return "{}"
	}
	return string(data)
}

func sortedEnvKeys(env map[string]string) []string {
	keys := make([]string, 0, len(env))
	for key := range env {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// renderEnvFile renders KEY=VALUE lines usable as a Docker Compose env_file.
func renderEnvFile(env map[string]string) string {
	var out strings.Builder
	for _, key := range sortedEnvKeys(env) {
		fmt.Fprintf(&out, "%s=%s\n", key, env[key])
	}
	return out.String()
}

// renderManualInstructions is the text the user follows. It contains the
// agent key: it is printed once and written to one private file.
func renderManualInstructions(displayName string, plan manualApplyPlan, bundleDir string, resumeCommand string) string {
	var out strings.Builder
	rule := strings.Repeat("-", 72)
	fmt.Fprintf(&out, "Manual setup for %s\n%s\n", displayName, rule)
	fmt.Fprintln(&out, "The agent and its key now exist in Preloop. Nothing works until the")
	fmt.Fprintln(&out, "change below is in place. The key is shown here once: Preloop cannot")
	fmt.Fprintln(&out, "show it again, only issue a new one.")
	for index, file := range plan.Files {
		fmt.Fprintf(&out, "\n%d. %s\n", index+1, file.Path)
		if file.Label != "" {
			fmt.Fprintf(&out, "   %s\n", file.Label)
		}
		if file.Reason != "" {
			fmt.Fprintf(&out, "   Not writable from here: %s\n", file.Reason)
		}
		if file.HostSource != "" {
			fmt.Fprintf(&out, "   Edit it on the host, probably at: %s\n", file.HostSource)
		}
		if file.Exists {
			fmt.Fprintln(&out, "   Merge these keys into the file and keep everything else:")
		} else {
			fmt.Fprintln(&out, "   The file does not exist yet. Create it with exactly this content:")
		}
		fmt.Fprintf(&out, "\n%s\n", renderConfigBlock(file.Format, file.Merge))
		if len(file.Remove) > 0 {
			fmt.Fprintf(&out, "\n   Remove these keys if present: %s\n", strings.Join(file.Remove, ", "))
		}
		if bundleDir != "" && file.Exists && file.full != nil {
			fmt.Fprintf(&out, "\n   Or replace the file with the complete result: %s\n",
				filepath.Join(bundleDir, manualBundleFullName(file)))
		}
	}
	if len(plan.Env) > 0 {
		fmt.Fprintf(&out, "\nAlternative A: environment variables instead of the settings file\n")
		for _, key := range sortedEnvKeys(plan.Env) {
			fmt.Fprintf(&out, "export %s=%s\n", key, shellSingleQuote(plan.Env[key]))
		}
		if bundleDir != "" {
			fmt.Fprintf(&out, "   The same values as a Docker Compose env_file: %s\n",
				filepath.Join(bundleDir, "preloop.env"))
		}
	}
	if plan.SettingsFlag != "" {
		fmt.Fprintf(&out, "\nAlternative B: load the block per session\n%s\n", plan.SettingsFlag)
	}
	if len(plan.Limitations) > 0 {
		fmt.Fprintln(&out, "\nWhat this setup and each form does not enable:")
		for _, limitation := range plan.Limitations {
			fmt.Fprintf(&out, "  - %s\n", limitation)
		}
	}
	fmt.Fprintf(&out, "\nWhen the change is in place, finish with:\n  %s\n", resumeCommand)
	fmt.Fprintf(&out, "Guide: %s\n%s\n", manualSetupDocsURL, rule)
	return out.String()
}

func manualBundleStem(file manualFileChange) string {
	base := filepath.Base(file.Path)
	return strings.TrimPrefix(base, ".")
}

func manualBundleMergeName(file manualFileChange) string {
	stem := manualBundleStem(file)
	ext := filepath.Ext(stem)
	return strings.TrimSuffix(stem, ext) + ".merge" + ext
}

func manualBundleFullName(file manualFileChange) string {
	return manualBundleStem(file)
}

// writeManualBundle writes the instructions and ready-to-use files to dir
// (0700, files 0600). It refuses a directory other users can read.
func writeManualBundle(dir string, instructions string, plan manualApplyPlan, resumeToken string) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	info, err := os.Stat(dir)
	if err != nil {
		return err
	}
	if info.Mode().Perm()&0o077 != 0 {
		if err := os.Chmod(dir, 0o700); err != nil {
			return fmt.Errorf("%s is readable by other users and its mode could not be tightened: %w", dir, err)
		}
	}
	write := func(name string, content string) error {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			return err
		}
		return os.Chmod(path, 0o600)
	}
	if err := write("INSTRUCTIONS.txt", instructions); err != nil {
		return err
	}
	for _, file := range plan.Files {
		if err := write(manualBundleMergeName(file), renderConfigBlock(file.Format, file.Merge)+"\n"); err != nil {
			return err
		}
		if file.Exists && file.full != nil {
			if err := write(manualBundleFullName(file), renderConfigBlock(file.Format, file.full)+"\n"); err != nil {
				return err
			}
		}
	}
	if len(plan.Env) > 0 {
		if err := write("preloop.env", renderEnvFile(plan.Env)); err != nil {
			return err
		}
	}
	if resumeToken != "" {
		if err := write("resume-token.txt", resumeToken+"\n"); err != nil {
			return err
		}
	}
	return nil
}

// jsonSyntaxProblem locates a JSON syntax error for a person editing by hand.
type jsonSyntaxProblem struct {
	Line   int
	Column int
	Reason string
}

func (p jsonSyntaxProblem) String() string {
	return fmt.Sprintf("line %d, column %d: %s", p.Line, p.Column, p.Reason)
}

// diagnoseJSONSyntax returns nil for valid JSON. Otherwise it names the line
// and the likely slip: a trailing comma, a missing comma, a missing quote.
func diagnoseJSONSyntax(data []byte) *jsonSyntaxProblem {
	var probe interface{}
	err := json.Unmarshal(data, &probe)
	if err == nil {
		return nil
	}
	offset := int64(len(data))
	var syntaxErr *json.SyntaxError
	if errors.As(err, &syntaxErr) {
		offset = syntaxErr.Offset
	}
	if offset > int64(len(data)) {
		offset = int64(len(data))
	}
	// Offset counts bytes read, so the offending byte is the one before it.
	position := int(offset) - 1
	if position < 0 {
		position = 0
	}
	line, column := 1, 1
	for i := 0; i < position && i < len(data); i++ {
		if data[i] == '\n' {
			line++
			column = 1
		} else {
			column++
		}
	}
	problem := &jsonSyntaxProblem{Line: line, Column: column, Reason: err.Error()}
	var bad byte
	if position < len(data) {
		bad = data[position]
	}
	previous := byte(0)
	previousLine := line
	for i := position - 1; i >= 0; i-- {
		if data[i] == '\n' {
			previousLine--
		}
		if !strings.ContainsRune(" \t\r\n", rune(data[i])) {
			previous = data[i]
			break
		}
	}
	message := err.Error()
	switch {
	case len(bytes.TrimSpace(data)) == 0:
		problem.Reason = "the file is empty; it must contain a JSON object"
	case (bad == '}' || bad == ']') && previous == ',':
		problem.Reason = fmt.Sprintf("trailing comma before %q; remove the comma at the end of line %d", string(bad), previousLine)
	case strings.Contains(message, "after object key:value pair") || strings.Contains(message, "after array element"):
		problem.Reason = fmt.Sprintf("missing comma; add one at the end of line %d", previousLine)
	case strings.Contains(message, "looking for beginning of object key string"):
		problem.Reason = "expected a key in double quotes here; check for a missing quote"
	case strings.Contains(message, "after object key"):
		problem.Reason = "missing colon or closing quote after this key"
	case strings.Contains(message, "after top-level value"):
		problem.Reason = "extra content after the end of the JSON object; the file must hold exactly one object"
	case strings.Contains(message, "unexpected end of JSON input"):
		problem.Reason = "the file ends early; a closing quote, } or ] is missing"
	case strings.Contains(message, "in string literal"):
		problem.Reason = "a string is not closed; check for a missing double quote on this line"
	case strings.Contains(message, "looking for beginning of value"):
		problem.Reason = "expected a value here; check for a missing quote or a stray character"
	}
	return problem
}

// expectedValueMatches compares one expected leaf with what the file holds.
// A placeholder stands for the agent key, checked by hash so the key itself
// is never stored.
func expectedValueMatches(expected, actual interface{}, tokenSHA256, keyTail string) bool {
	if items, ok := expected.([]interface{}); ok {
		values, ok := actual.([]interface{})
		if !ok || len(items) != len(values) {
			return false
		}
		for i, item := range items {
			if !expectedValueMatches(item, values[i], tokenSHA256, keyTail) {
				return false
			}
		}
		return true
	}
	expectedText, isText := expected.(string)
	if !isText {
		return configValuesEqual(expected, actual)
	}
	if strings.Contains(expectedText, manualAgentKeyTailPlaceholder) {
		value, ok := actual.(string)
		return ok && keyTail != "" && value == strings.ReplaceAll(expectedText, manualAgentKeyTailPlaceholder, keyTail)
	}
	if !strings.Contains(expectedText, manualAgentKeyPlaceholder) {
		return configValuesEqual(expected, actual)
	}
	actualText, ok := actual.(string)
	if !ok {
		return false
	}
	prefix, suffix, _ := strings.Cut(expectedText, manualAgentKeyPlaceholder)
	if !strings.HasPrefix(actualText, prefix) || !strings.HasSuffix(actualText, suffix) ||
		len(actualText) < len(prefix)+len(suffix) {
		return false
	}
	key := actualText[len(prefix) : len(actualText)-len(suffix)]
	if tokenSHA256 == "" {
		return strings.TrimSpace(key) != "" && key != manualAgentKeyPlaceholder
	}
	return sha256Hex(key) == tokenSHA256
}

// missingMergeKeys lists what of expected is absent from or different in
// actual, as dotted paths with a reason.
func missingMergeKeys(expected, actual map[string]interface{}, prefix, tokenSHA256, keyTail string) []string {
	var problems []string
	keys := make([]string, 0, len(expected))
	for key := range expected {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		want := expected[key]
		have, present := actual[key]
		path := prefix + key
		if !present {
			problems = append(problems, path+" is missing")
			continue
		}
		if wantMap, ok := asObjectMap(want); ok {
			haveMap, haveOK := asObjectMap(have)
			if !haveOK {
				problems = append(problems, path+" should be an object")
				continue
			}
			problems = append(problems, missingMergeKeys(wantMap, haveMap, path+".", tokenSHA256, keyTail)...)
			continue
		}
		if expectedValueMatches(want, have, tokenSHA256, keyTail) {
			continue
		}
		if text, ok := want.(string); ok && strings.Contains(text, manualAgentKeyPlaceholder) {
			if actualText, _ := have.(string); strings.Contains(actualText, manualAgentKeyPlaceholder) {
				problems = append(problems, path+" still holds the placeholder "+manualAgentKeyPlaceholder+"; put the agent key there")
			} else {
				problems = append(problems, path+" does not hold the key issued for this setup")
			}
			continue
		}
		problems = append(problems, fmt.Sprintf("%s should be %s", path, truncateForLine(compactJSON(want))))
	}
	return problems
}

func truncateForLine(text string) string {
	if len(text) > 120 {
		return text[:117] + "..."
	}
	return text
}

func lookupDottedPath(doc map[string]interface{}, path string) bool {
	current := doc
	parts := strings.Split(path, ".")
	for index, part := range parts {
		value, ok := current[part]
		if !ok {
			return false
		}
		if index == len(parts)-1 {
			return true
		}
		next, isMap := asObjectMap(value)
		if !isMap {
			return false
		}
		current = next
	}
	return false
}

// manualFileCheck is the result of reading one file back.
type manualFileCheck struct {
	Document map[string]interface{}
	File     manualFileChange
	Path     string
	Problems []string
}

func (c manualFileCheck) ok() bool { return len(c.Problems) == 0 }

// sideFileBearer reads only credential fields populated by this handoff.
func (file manualFileChange) sideFileBearer(doc map[string]interface{}) string {
	switch file.Role {
	case writeRoleUserConfig:
		if manualMCPBearer(file.Merge) == manualAgentKeyPlaceholder {
			return manualMCPBearer(doc)
		}
	case writeRoleControl:
		if lookupString(file.Merge, "control", "bearer_token") == manualAgentKeyPlaceholder {
			return lookupString(doc, "control", "bearer_token")
		}
	}
	return ""
}

// verifyManualFile reads path and reports exactly what of file's change is
// not in place. path may differ from file.Path when the user points resume
// at a `claude --settings` file.
func verifyManualFile(file manualFileChange, path string, tokenSHA256 string) manualFileCheck {
	check := manualFileCheck{File: file, Path: path}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			check.Problems = append(check.Problems, "the file does not exist")
		} else {
			check.Problems = append(check.Problems, "cannot read the file: "+describeWriteRefusal(err, path))
		}
		return check
	}
	var doc map[string]interface{}
	if file.Format == "toml" {
		parsed, parseErr := parseDocumentFromTOML(data)
		if parseErr != nil {
			check.Problems = append(check.Problems, "invalid TOML: "+parseErr.Error())
			return check
		}
		doc = parsed
	} else {
		if problem := diagnoseJSONSyntax(data); problem != nil {
			check.Problems = append(check.Problems, "invalid JSON at "+problem.String())
			return check
		}
		if err := json.Unmarshal(data, &doc); err != nil || doc == nil {
			check.Problems = append(check.Problems, "the file must contain a JSON object at the top level")
			return check
		}
	}
	check.Document = doc
	keyTail := claudeAPIKeyFingerprint(file.sideFileBearer(doc))
	check.Problems = append(check.Problems, missingMergeKeys(file.Merge, doc, "", tokenSHA256, keyTail)...)
	for _, removed := range file.Remove {
		if lookupDottedPath(doc, removed) {
			check.Problems = append(check.Problems, removed+" is still present; remove it")
		}
	}
	return check
}

// verifyManualEnv checks the environment-variable form against getenv.
func verifyManualEnv(env map[string]string, getenv func(string) string, tokenSHA256 string) []string {
	var problems []string
	for _, key := range sortedEnvKeys(env) {
		actual := getenv(key)
		if actual == "" {
			problems = append(problems, key+" is not set in this environment")
			continue
		}
		if !expectedValueMatches(env[key], actual, tokenSHA256, "") {
			if strings.Contains(env[key], manualAgentKeyPlaceholder) {
				problems = append(problems, key+" does not hold the key issued for this setup")
			} else {
				problems = append(problems, fmt.Sprintf("%s should be %s", key, env[key]))
			}
		}
	}
	return problems
}

// manualResumeState is what resume needs. It holds no secret: the key is
// represented by its SHA-256 only. It is stored under ~/.preloop when that
// is writable and is always printable as a token.
type manualResumeState struct {
	Version            int       `json:"v"`
	AgentName          string    `json:"agent"`
	DisplayName        string    `json:"name,omitempty"`
	ConfigPath         string    `json:"config"`
	RuntimePrincipalID string    `json:"principal"`
	AgentID            string    `json:"agent_id"`
	EnrollmentID       string    `json:"enrollment_id"`
	TokenSHA256        string    `json:"key_sha256"`
	BaseURL            string    `json:"url,omitempty"`
	BundleDir          string    `json:"bundle,omitempty"`
	CreatedAt          time.Time `json:"created"`
	Operation          string    `json:"op,omitempty"`
}

func encodeManualResumeToken(state manualResumeState) string {
	state.BundleDir = ""
	data, err := json.Marshal(state)
	if err != nil {
		return ""
	}
	return manualResumeTokenPrefix + base64.RawURLEncoding.EncodeToString(data)
}

func decodeManualResumeToken(token string) (*manualResumeState, error) {
	token = strings.TrimSpace(token)
	if !strings.HasPrefix(token, manualResumeTokenPrefix) {
		return nil, fmt.Errorf("not a Preloop resume token (it starts with %q)", manualResumeTokenPrefix)
	}
	data, err := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(token, manualResumeTokenPrefix))
	if err != nil {
		return nil, errors.New("the resume token is damaged; copy it again in full")
	}
	var state manualResumeState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, errors.New("the resume token is damaged; copy it again in full")
	}
	if state.AgentID == "" || state.EnrollmentID == "" || state.AgentName == "" {
		return nil, errors.New("the resume token is incomplete; copy it again in full")
	}
	return &state, nil
}

func manualResumeStatePath(agentName, configPath string) (string, error) {
	statePath, err := localEnrollmentStatePath(agentName, configPath)
	if err != nil {
		return "", err
	}
	return filepath.Join(filepath.Dir(filepath.Dir(statePath)), "pending", filepath.Base(statePath)), nil
}

func saveManualResumeState(state manualResumeState) error {
	path, err := manualResumeStatePath(state.AgentName, state.ConfigPath)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	return writeFileAtomically(path, append(data, '\n'), 0o600, ".pending-*.tmp")
}

func loadManualResumeState(agent AgentConfig) (*manualResumeState, error) {
	path, err := manualResumeStatePath(agent.Name, agent.ConfigPath)
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var state manualResumeState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, err
	}
	return &state, nil
}

func removeManualResumeState(agentName, configPath string) {
	if path, err := manualResumeStatePath(agentName, configPath); err == nil {
		_ = os.Remove(path)
	}
}

// listManualResumeStates returns every pending setup recorded locally.
func listManualResumeStates() []manualResumeState {
	probe, err := manualResumeStatePath("probe", "probe")
	if err != nil {
		return nil
	}
	entries, err := os.ReadDir(filepath.Dir(probe))
	if err != nil {
		return nil
	}
	var states []manualResumeState
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(filepath.Dir(probe), entry.Name()))
		if err != nil {
			continue
		}
		var state manualResumeState
		if json.Unmarshal(data, &state) == nil && state.EnrollmentID != "" {
			states = append(states, state)
		}
	}
	return states
}

// manualApplyPendingError reports that onboarding stopped at the manual step.
// It is not a failure: the agent exists and waits for the local change.
type manualApplyPendingError struct {
	AgentName     string
	ResumeCommand string
}

func (e *manualApplyPendingError) Error() string {
	return fmt.Sprintf("%s is waiting for local configuration; finish with: %s", e.AgentName, e.ResumeCommand)
}

// manualApplyExitCode is the process status for "waiting for a manual step".
const manualApplyExitCode = 3

func printManualLine(w io.Writer, format string, args ...interface{}) {
	fmt.Fprintf(w, format+"\n", args...) //nolint:errcheck
}
