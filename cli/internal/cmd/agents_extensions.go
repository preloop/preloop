package cmd

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"

	"bytes"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/spf13/cobra"
)

const (
	extensionListSchema   = "preloop.extensions.v1"
	extensionPolicySchema = "preloop.extension-policy.v1"
	extensionCheckSchema  = "preloop.extension-check.v1"
	extensionUsageExit    = 64
)

var (
	extensionAgentKindPattern   = regexp.MustCompile(`^[a-z0-9][a-z0-9_\-]{0,63}$`)
	extensionConstraintPattern  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.+_\-]*(?:\.\*)?$`)
	extensionContentHashPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
)

// agentsExtensionsCmd prints installed extensions without leaving the machine.
var agentsExtensionsCmd = &cobra.Command{
	Use:   "extensions",
	Short: "List installed agent extensions on this machine",
	Long: `List plugins, skills, hooks and commands found in known local locations.

The command is read-only: it does not authenticate, use the network, or write
a file. Identifiers stay in clear, including names an inventory report would
hash. A partial scan prints "Scan incomplete: <reason>" and exits 2.

Claude project skills, commands and hooks are scanned only for roots in
~/.claude.json or explicitly included with --project. Use --project . to include
the current directory, and the same flag on agents discover --report to send it.

Examples:
  preloop agents extensions
  preloop agents extensions --agent claude-code --kind plugin
  preloop agents extensions --json
  preloop agents extensions --all-users`,
	RunE: runAgentsExtensions,
}

var agentsExtensionsCheckCmd = &cobra.Command{
	Use:   "check",
	Short: "Compare local extensions with an approval policy",
	Long: `Compare installed extensions with an account policy file.

Pass exactly one of --policy-file (offline) or --online. --online fetches
GET /api/v1/agents/extension-approvals/export and does not upload inventory.
Matching is local, on agent kind, extension kind and extension id.

Exit codes:
  0   nothing at or above --fail-on (default unapproved)
  3   at least one unapproved extension
  4   at least one unknown extension and --fail-on unknown (3 wins)
  2   scan incomplete, or the policy is unreadable or invalid
  1   any other error
  64  both --policy-file and --online, or neither

This proves files are present or absent in known locations. It does not prove
what an agent loaded or ran.

Examples:
  preloop agents extensions check --policy-file policy.json
  preloop agents extensions check --policy-file policy.json --fail-on unknown
  preloop agents extensions check --online --json`,
	RunE: runAgentsExtensionsCheck,
}

func init() {
	agentsExtensionsCmd.PersistentFlags().StringArray("agent", nil, "app id to scan (repeatable; default claude-code and codex-cli)")
	agentsExtensionsCmd.PersistentFlags().Bool("json", false, "print JSON instead of text")
	agentsExtensionsCmd.PersistentFlags().StringArray("project", nil, "include a Claude project root (repeatable; use . for the current directory)")
	agentsExtensionsCmd.PersistentFlags().Bool("all-users", false, "scan bounded local profiles; requires elevated administrator rights")
	agentsExtensionsCmd.Flags().StringArray("kind", nil, "extension kind to show: plugin, skill, hook, or command (repeatable)")
	agentsExtensionsCheckCmd.Flags().String("policy-file", "", "path to an offline extension policy file")
	agentsExtensionsCheckCmd.Flags().Bool("online", false, "fetch the account policy; sends no local inventory")
	agentsExtensionsCheckCmd.Flags().String("fail-on", "unapproved", "failure threshold: unapproved or unknown")
	agentsExtensionsCmd.AddCommand(agentsExtensionsCheckCmd)
}

type extensionApp struct {
	id, name, kind string
}

type extensionGroup struct {
	app          extensionApp
	profileRef   string
	profileLabel string
	extensions   []inventoryExtension
}

type extensionRule struct {
	state      string
	constraint *string
	hashes     []string
}

type extensionsDocument struct {
	Schema           string                  `json:"schema"`
	Completeness     string                  `json:"completeness"`
	IncompleteReason string                  `json:"incomplete_reason,omitempty"`
	Apps             []extensionsAppDocument `json:"apps"`
}

type extensionsAppDocument struct {
	AppID      string               `json:"app_id"`
	ProfileRef string               `json:"profile_ref"`
	Extensions []inventoryExtension `json:"extensions"`
}

type extensionCheckCounts struct {
	Approved   int `json:"approved"`
	Unapproved int `json:"unapproved"`
	Unknown    int `json:"unknown"`
}

type extensionFinding struct {
	Agent      string `json:"agent"`
	Kind       string `json:"kind"`
	ID         string `json:"id"`
	State      string `json:"state"`
	Reason     string `json:"reason,omitempty"`
	ProfileRef string `json:"profile_ref,omitempty"`
}

type extensionCheckDocument struct {
	Schema           string               `json:"schema"`
	Result           string               `json:"result"`
	IncompleteReason string               `json:"incomplete_reason,omitempty"`
	Counts           extensionCheckCounts `json:"counts"`
	Findings         []extensionFinding   `json:"findings"`
}

type extensionCheckError struct {
	Schema string `json:"schema"`
	Result string `json:"result"`
	Error  string `json:"error"`
}

type policyEntryJSON struct {
	AgentKind         *string  `json:"agent_kind"`
	ExtensionKind     *string  `json:"extension_kind"`
	ExtensionID       *string  `json:"extension_id"`
	ApprovalState     *string  `json:"approval_state"`
	VersionConstraint *string  `json:"version_constraint"`
	ContentHashes     []string `json:"content_hashes"`
}

func agentsExtensionsCommand(cmd *cobra.Command) (*cobra.Command, bool) {
	if cmd == nil || cmd.Parent() == nil {
		return nil, false
	}
	if cmd.Name() == "extensions" && cmd.Parent().Name() == "agents" {
		return cmd, true
	}
	parent := cmd.Parent()
	if cmd.Name() == "check" && parent.Name() == "extensions" && parent.Parent() != nil && parent.Parent().Name() == "agents" {
		return cmd, true
	}
	return nil, false
}

func isOfflineExtensionsCommand(cmd *cobra.Command) bool {
	command, ok := agentsExtensionsCommand(cmd)
	if !ok {
		return false
	}
	if command.Name() != "check" {
		return true
	}
	online, _ := command.Flags().GetBool("online")
	policy, _ := command.Flags().GetString("policy-file")
	return !online || strings.TrimSpace(policy) != ""
}

func isExtensionsAllUsersCommand(cmd *cobra.Command) bool {
	command, ok := agentsExtensionsCommand(cmd)
	if !ok {
		return false
	}
	allUsers, _ := command.Flags().GetBool("all-users")
	return allUsers
}

func runAgentsExtensions(cmd *cobra.Command, _ []string) error {
	apps, err := selectedExtensionApps(cmd)
	if err != nil {
		return extensionUsage(cmd, err.Error())
	}
	kinds, err := selectedExtensionKinds(cmd)
	if err != nil {
		return extensionUsage(cmd, err.Error())
	}
	groups, reasons, err := scanExtensionGroups(cmd, apps, kinds)
	if err != nil {
		return err
	}
	asJSON, _ := cmd.Flags().GetBool("json")
	if asJSON {
		err = writeExtensionJSON(cmd, extensionsDocumentFrom(groups, reasons))
	} else {
		err = writeExtensionTables(cmd.OutOrStdout(), groups, reasons)
	}
	if err != nil {
		return err
	}
	if len(reasons) > 0 {
		cmd.SilenceErrors = true
		return &exitCodeError{code: 2}
	}
	return nil
}

func runAgentsExtensionsCheck(cmd *cobra.Command, _ []string) error {
	apps, err := selectedExtensionApps(cmd)
	if err != nil {
		return extensionUsage(cmd, err.Error())
	}
	failOn, _ := cmd.Flags().GetString("fail-on")
	if failOn != "unapproved" && failOn != "unknown" {
		return extensionUsage(cmd, "invalid --fail-on")
	}
	online, _ := cmd.Flags().GetBool("online")
	policyPath, _ := cmd.Flags().GetString("policy-file")
	policyPath = strings.TrimSpace(policyPath)
	if online == (policyPath != "") {
		return extensionUsage(cmd, "specify exactly one of --policy-file or --online")
	}
	allUsers, _ := cmd.Flags().GetBool("all-users")
	if allUsers && !inventoryProfiles.Elevated() {
		return errors.New("--all-users requires elevated administrator rights")
	}
	raw, problem, err := loadExtensionPolicy(online, policyPath)
	if err != nil {
		return err
	}
	asJSON, _ := cmd.Flags().GetBool("json")
	if problem != "" {
		return extensionPolicyProblem(cmd, asJSON, problem)
	}
	rules, problem := parseExtensionPolicy(raw)
	if problem != "" {
		return extensionPolicyProblem(cmd, asJSON, problem)
	}
	groups, reasons, err := scanExtensionGroups(cmd, apps, nil)
	if err != nil {
		return err
	}
	findings, counts := evaluateExtensions(groups, rules, allUsers)
	if asJSON {
		err = writeExtensionJSON(cmd, extensionCheckDocumentFrom(findings, counts, reasons, failOn))
	} else {
		err = writeExtensionCheck(cmd.OutOrStdout(), findings, counts, reasons, allUsers)
	}
	if err != nil {
		return err
	}
	code := extensionCheckExit(counts, failOn, len(reasons) > 0)
	if code == 0 {
		return nil
	}
	cmd.SilenceErrors = true
	return &exitCodeError{code: code}
}

func extensionUsage(cmd *cobra.Command, message string) error {
	fmt.Fprintln(cmd.ErrOrStderr(), message)
	_ = cmd.Usage()
	cmd.SilenceErrors = true
	return &exitCodeError{code: extensionUsageExit, message: message}
}

func extensionPolicyProblem(cmd *cobra.Command, asJSON bool, problem string) error {
	if asJSON {
		cmd.SilenceErrors = true
		if err := writeExtensionJSON(cmd, extensionCheckError{Schema: extensionCheckSchema, Result: "error", Error: problem}); err != nil {
			return err
		}
		return &exitCodeError{code: 2, message: problem}
	}
	return &exitCodeError{code: 2, message: problem}
}

func selectedExtensionApps(cmd *cobra.Command) ([]extensionApp, error) {
	ids, err := cmd.Flags().GetStringArray("agent")
	if err != nil {
		return nil, err
	}
	if len(ids) == 0 {
		return defaultExtensionApps(), nil
	}
	seen := map[string]bool{}
	apps := make([]extensionApp, 0, len(ids))
	for _, id := range ids {
		id = strings.TrimSpace(id)
		if id == "" {
			return nil, errors.New(`unknown agent ""`)
		}
		if seen[id] {
			continue
		}
		seen[id] = true
		app, ok := extensionAppByID(id)
		if !ok {
			return nil, fmt.Errorf("unknown agent %q", id)
		}
		apps = append(apps, app)
	}
	return apps, nil
}

func defaultExtensionApps() []extensionApp {
	apps := make([]extensionApp, 0, 2)
	for _, name := range []string{"Claude Code", "Codex CLI"} {
		apps = append(apps, extensionApp{id: inventoryAppIDs[name], name: name, kind: managedAgentKindForAgent(name)})
	}
	return apps
}

func extensionAppByID(id string) (extensionApp, bool) {
	for _, app := range defaultExtensionApps() {
		if app.id == id {
			return app, true
		}
	}
	return extensionApp{}, false
}

func selectedExtensionKinds(cmd *cobra.Command) (map[string]bool, error) {
	values, err := cmd.Flags().GetStringArray("kind")
	if err != nil {
		return nil, err
	}
	if len(values) == 0 {
		return nil, nil
	}
	allowed := map[string]bool{"plugin": true, "skill": true, "hook": true, "command": true}
	kinds := map[string]bool{}
	for _, value := range values {
		if !allowed[value] {
			return nil, fmt.Errorf("unknown extension kind %q", value)
		}
		kinds[value] = true
	}
	return kinds, nil
}

// extensionProjects resolves and validates explicit project scan scope.
func extensionProjects(cmd *cobra.Command) ([]string, error) {
	projects, _ := cmd.Flags().GetStringArray("project")
	if len(projects) == 0 && cmd.Flags().Changed("project") {
		return nil, errors.New("--project requires a directory")
	}
	allUsers, _ := cmd.Flags().GetBool("all-users")
	if allUsers && len(projects) > 0 {
		return nil, errors.New("--project cannot be combined with --all-users")
	}
	paths := make([]string, 0, len(projects))
	for _, project := range projects {
		if strings.TrimSpace(project) == "" {
			return nil, errors.New("--project requires a directory")
		}
		path, err := filepath.Abs(project)
		if err != nil {
			return nil, errors.New("could not resolve --project directory")
		}
		paths = append(paths, path)
	}
	return uniqueSorted(paths), nil
}

func scanExtensionGroups(cmd *cobra.Command, apps []extensionApp, kinds map[string]bool) ([]extensionGroup, []string, error) {
	projects, err := extensionProjects(cmd)
	if err != nil {
		return nil, nil, err
	}
	allUsers, _ := cmd.Flags().GetBool("all-users")
	if !allUsers {
		return scanCurrentUserExtensions(apps, kinds, projects...)
	}
	if !inventoryProfiles.Elevated() {
		return nil, nil, errors.New("--all-users requires elevated administrator rights")
	}
	return scanAllUserExtensions(apps, kinds)
}

func scanCurrentUserExtensions(apps []extensionApp, kinds map[string]bool, projects ...string) ([]extensionGroup, []string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, nil, errors.New("home directory is unavailable")
	}
	groups := make([]extensionGroup, 0, len(apps))
	reasons := []string{}
	for _, app := range apps {
		found, errs := collectDisplayExtensions(home, app.id, "", false, projects...)
		reasons = append(reasons, errs...)
		groups = append(groups, extensionGroup{app: app, profileRef: "current", profileLabel: "current user", extensions: filterExtensionKinds(found, kinds)})
	}
	return groups, uniqueSorted(reasons), nil
}

func scanAllUserExtensions(apps []extensionApp, kinds map[string]bool) ([]extensionGroup, []string, error) {
	salt, err := newExtensionProfileSalt()
	if err != nil {
		return nil, nil, err
	}
	profiles, truncated, err := inventoryProfiles.Profiles()
	reasons := []string{}
	if err != nil {
		reasons = append(reasons, "profiles_unavailable")
		if len(profiles) == 0 {
			return nil, uniqueSorted(reasons), nil
		}
	}
	if len(profiles) > inventoryProfileLimit {
		profiles = profiles[:inventoryProfileLimit]
		truncated = true
	}
	if truncated {
		reasons = append(reasons, "profile_limit")
	}
	groups := []extensionGroup{}
	for _, profile := range profiles {
		if profile.ErrorCode != "" {
			reasons = append(reasons, profile.ErrorCode)
			continue
		}
		info, statErr := os.Lstat(profile.Home)
		if statErr != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			reasons = append(reasons, "profile_unavailable")
			continue
		}
		identity := profile.Home
		if profile.LocalRef != "" {
			identity = profile.LocalRef
		}
		ref := inventoryProfileRef(salt, "offline", identity)
		for _, app := range apps {
			found, errs := collectDisplayExtensions(profile.Home, app.id, "", true)
			reasons = append(reasons, errs...)
			groups = append(groups, extensionGroup{app: app, profileRef: ref, profileLabel: "profile " + ref, extensions: filterExtensionKinds(found, kinds)})
		}
	}
	return groups, uniqueSorted(reasons), nil
}

func newExtensionProfileSalt() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", errors.New("could not generate profile reference")
	}
	return hex.EncodeToString(buf), nil
}

func filterExtensionKinds(found []inventoryExtension, kinds map[string]bool) []inventoryExtension {
	if found == nil {
		found = []inventoryExtension{}
	}
	if len(kinds) == 0 {
		return found
	}
	filtered := []inventoryExtension{}
	for _, ext := range found {
		if kinds[ext.Kind] {
			filtered = append(filtered, ext)
		}
	}
	return filtered
}

func uniqueSorted(values []string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, value := range values {
		if value == "" || seen[value] {
			continue
		}
		seen[value] = true
		out = append(out, value)
	}
	sort.Strings(out)
	return out
}

func extensionsDocumentFrom(groups []extensionGroup, reasons []string) extensionsDocument {
	doc := extensionsDocument{Schema: extensionListSchema, Completeness: "complete", Apps: []extensionsAppDocument{}}
	if len(reasons) > 0 {
		doc.Completeness = "partial"
		doc.IncompleteReason = strings.Join(reasons, ", ")
	}
	for _, group := range groups {
		extensions := group.extensions
		if extensions == nil {
			extensions = []inventoryExtension{}
		}
		doc.Apps = append(doc.Apps, extensionsAppDocument{AppID: group.app.id, ProfileRef: group.profileRef, Extensions: extensions})
	}
	return doc
}

func writeExtensionTables(w io.Writer, groups []extensionGroup, reasons []string) error {
	count := 0
	for _, group := range groups {
		if _, err := fmt.Fprintf(w, "%s (%s)\n", group.app.name, group.profileLabel); err != nil {
			return err
		}
		rows := make([][]string, len(group.extensions))
		for i, ext := range group.extensions {
			rows[i] = []string{ext.Kind, extensionLabel(ext), dash(ext.Version), ext.SourceKind, ext.Scope, enabledLabel(ext)}
		}
		if err := writeAlignedRows(w, []string{"KIND", "EXTENSION", "VERSION", "SOURCE", "SCOPE", "ENABLED"}, rows); err != nil {
			return err
		}
		count += len(group.extensions)
	}
	for _, group := range groups {
		if group.app.id == "claude-code" && group.profileRef == "current" {
			if _, err := io.WriteString(w, "Claude project coverage: roots in ~/.claude.json and --project only. Use --project . to include the current directory; also pass it to agents discover --report.\n"); err != nil {
				return err
			}
			break
		}
	}
	_, err := io.WriteString(w, extensionSummary(count, reasons))
	return err
}

func extensionSummary(count int, reasons []string) string {
	noun := "extensions"
	if count == 1 {
		noun = "extension"
	}
	if len(reasons) == 0 {
		return fmt.Sprintf("%d %s. Scan complete.\n", count, noun)
	}
	return fmt.Sprintf("%d %s.\nScan incomplete: %s\n", count, noun, strings.Join(reasons, ", "))
}

func writeAlignedRows(w io.Writer, header []string, rows [][]string) error {
	widths := make([]int, len(header))
	for i, cell := range header {
		widths[i] = utf8.RuneCountInString(cell)
	}
	for _, row := range rows {
		for i, cell := range row {
			if n := utf8.RuneCountInString(cell); n > widths[i] {
				widths[i] = n
			}
		}
	}
	write := func(row []string) error {
		for i, cell := range row {
			if i > 0 {
				if _, err := io.WriteString(w, "  "); err != nil {
					return err
				}
			}
			text := cell
			if i < len(row)-1 {
				text = cell + strings.Repeat(" ", widths[i]-utf8.RuneCountInString(cell))
			}
			if _, err := io.WriteString(w, text); err != nil {
				return err
			}
		}
		_, err := io.WriteString(w, "\n")
		return err
	}
	if err := write(header); err != nil {
		return err
	}
	for _, row := range rows {
		if err := write(row); err != nil {
			return err
		}
	}
	return nil
}

func extensionLabel(ext inventoryExtension) string {
	if ext.Kind != "hook" || ext.Count == 0 {
		return ext.ID
	}
	noun := "commands"
	if ext.Count == 1 {
		noun = "command"
	}
	return fmt.Sprintf("%s (%d %s)", ext.ID, ext.Count, noun)
}

func enabledLabel(ext inventoryExtension) string {
	if ext.Kind == "hook" {
		return "-"
	}
	if ext.Enabled == "" {
		return "unknown"
	}
	return ext.Enabled
}

func dash(value string) string {
	if value == "" {
		return "-"
	}
	return value
}

func loadExtensionPolicy(online bool, path string) ([]byte, string, error) {
	if !online {
		data, problem := readPolicyFile(path)
		return data, problem, nil
	}
	config.SetConfigFile(cfgFile)
	applySelection()
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return nil, "", err
	}
	var payload json.RawMessage
	if err := client.Get(api.RESTPrefix+"/agents/extension-approvals/export", &payload); err != nil {
		return nil, "", err
	}
	if len(payload) > inventoryConfigLimit {
		return nil, "policy file exceeds 4 MiB", nil
	}
	return payload, "", nil
}

func readPolicyFile(path string) ([]byte, string) {
	file, err := os.Open(path)
	if err != nil {
		return nil, "policy file is unreadable"
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return nil, "policy file is unreadable"
	}
	data, err := io.ReadAll(io.LimitReader(file, inventoryConfigLimit+1))
	if err != nil {
		return nil, "policy file is unreadable"
	}
	if len(data) > inventoryConfigLimit {
		return nil, "policy file exceeds 4 MiB"
	}
	return data, ""
}

func parseExtensionPolicy(data []byte) (map[string]extensionRule, string) {
	if len(data) == 0 || !json.Valid(data) {
		return nil, "invalid JSON"
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var envelope struct {
		Schema      string          `json:"schema"`
		AccountRef  json.RawMessage `json:"account_ref"`
		GeneratedAt json.RawMessage `json:"generated_at"`
		Entries     json.RawMessage `json:"entries"`
	}
	if err := dec.Decode(&envelope); err != nil {
		if field, ok := unknownJSONField(err); ok {
			return nil, "unknown field " + field
		}
		return nil, "invalid JSON"
	}
	if dec.More() {
		return nil, "invalid JSON"
	}
	if envelope.Schema != extensionPolicySchema {
		return nil, "wrong schema"
	}
	if len(envelope.Entries) == 0 || string(envelope.Entries) == "null" {
		return nil, "missing entries"
	}
	if problem := requireJSONString(envelope.AccountRef, "account_ref"); problem != "" {
		return nil, problem
	}
	if problem := requireJSONString(envelope.GeneratedAt, "generated_at"); problem != "" {
		return nil, problem
	}
	entryDec := json.NewDecoder(bytes.NewReader(envelope.Entries))
	entryDec.DisallowUnknownFields()
	var entries []policyEntryJSON
	if err := entryDec.Decode(&entries); err != nil {
		if field, ok := unknownJSONField(err); ok {
			return nil, "unknown field " + field
		}
		return nil, "invalid JSON"
	}
	rules := map[string]extensionRule{}
	seen := map[string]bool{}
	for _, entry := range entries {
		agent, kind, id, state, problem := policyIdentity(entry)
		if problem != "" {
			return nil, problem
		}
		if entry.VersionConstraint != nil {
			constraint := *entry.VersionConstraint
			if constraint == "" || len(constraint) > 64 || !extensionConstraintPattern.MatchString(constraint) {
				return nil, "invalid version_constraint"
			}
		}
		for _, hash := range entry.ContentHashes {
			if !extensionContentHashPattern.MatchString(hash) {
				return nil, "invalid content hash"
			}
		}
		key := agent + "\x00" + kind + "\x00" + id
		if seen[key] {
			return nil, "duplicate policy entry " + agent + "/" + kind + "/" + id
		}
		seen[key] = true
		if state == "unknown" {
			continue
		}
		hashes := entry.ContentHashes
		if hashes == nil {
			hashes = []string{}
		}
		rules[key] = extensionRule{state: state, constraint: entry.VersionConstraint, hashes: hashes}
	}
	return rules, ""
}

func unknownJSONField(err error) (string, bool) {
	const prefix = "json: unknown field "
	if err == nil || !strings.HasPrefix(err.Error(), prefix) {
		return "", false
	}
	return strings.TrimPrefix(err.Error(), prefix), true
}

func requireJSONString(raw json.RawMessage, name string) string {
	if len(raw) == 0 {
		return ""
	}
	if string(raw) == "null" {
		return "invalid " + name
	}
	var value string
	if json.Unmarshal(raw, &value) != nil {
		return "invalid " + name
	}
	return ""
}

func policyIdentity(entry policyEntryJSON) (string, string, string, string, string) {
	if entry.AgentKind == nil || *entry.AgentKind == "" {
		return "", "", "", "", "missing agent_kind"
	}
	if !extensionAgentKindPattern.MatchString(*entry.AgentKind) {
		return "", "", "", "", "invalid agent_kind"
	}
	if entry.ExtensionKind == nil || *entry.ExtensionKind == "" {
		return "", "", "", "", "missing extension_kind"
	}
	switch *entry.ExtensionKind {
	case "plugin", "skill", "hook", "command":
	default:
		return "", "", "", "", "invalid extension_kind"
	}
	if entry.ExtensionID == nil || *entry.ExtensionID == "" {
		return "", "", "", "", "missing extension_id"
	}
	if !validExtensionID(*entry.ExtensionID) {
		return "", "", "", "", "invalid extension_id"
	}
	if entry.ApprovalState == nil || *entry.ApprovalState == "" {
		return "", "", "", "", "missing approval_state"
	}
	switch *entry.ApprovalState {
	case "approved", "unapproved", "unknown":
	default:
		return "", "", "", "", "invalid approval_state"
	}
	return *entry.AgentKind, *entry.ExtensionKind, *entry.ExtensionID, *entry.ApprovalState, ""
}

func validExtensionID(id string) bool {
	if id == "" || len(id) > 128 || utf8.RuneCountInString(id) > 128 {
		return false
	}
	if strings.ContainsAny(id, `/\`) {
		return false
	}
	for _, r := range id {
		if r < 0x20 || r == 0x7f {
			return false
		}
	}
	return true
}

func evaluateExtensions(groups []extensionGroup, rules map[string]extensionRule, allUsers bool) ([]extensionFinding, extensionCheckCounts) {
	findings := []extensionFinding{}
	var counts extensionCheckCounts
	for _, group := range groups {
		for _, ext := range group.extensions {
			matchID := ext.PolicyID
			if matchID == "" {
				matchID = ext.ID
			}
			rule, ok := rules[group.app.kind+"\x00"+ext.Kind+"\x00"+matchID]
			state, reason := decideExtension(ext, rule, ok)
			switch state {
			case "approved":
				counts.Approved++
			case "unapproved":
				counts.Unapproved++
			default:
				counts.Unknown++
			}
			finding := extensionFinding{Agent: group.app.kind, Kind: ext.Kind, ID: ext.ID, State: state, Reason: reason}
			if allUsers {
				finding.ProfileRef = group.profileRef
			}
			findings = append(findings, finding)
		}
	}
	return findings, counts
}

func decideExtension(ext inventoryExtension, rule extensionRule, ok bool) (string, string) {
	if !ok {
		if ext.SourceKind == "builtin" {
			return "approved", "builtin"
		}
		return "unknown", ""
	}
	if rule.state == "unapproved" {
		return "unapproved", "policy"
	}
	if rule.constraint != nil && !extensionVersionAllowed(ext.Version, *rule.constraint) {
		if ext.Version == "" || !discoveryVersionPattern.MatchString(ext.Version) {
			return "unapproved", "version_unknown"
		}
		return "unapproved", "version"
	}
	if len(rule.hashes) > 0 {
		if ext.ContentHash == "" {
			return "unknown", "content_unverified"
		}
		for _, hash := range rule.hashes {
			if hash == ext.ContentHash {
				return "approved", ""
			}
		}
		return "unapproved", "content_changed"
	}
	return "approved", ""
}

func extensionVersionAllowed(version, constraint string) bool {
	if version == "" || !discoveryVersionPattern.MatchString(version) {
		return false
	}
	if strings.HasSuffix(constraint, ".*") {
		return strings.HasPrefix(version, strings.TrimSuffix(constraint, "*"))
	}
	return version == constraint
}

func extensionCheckDocumentFrom(findings []extensionFinding, counts extensionCheckCounts, reasons []string, failOn string) extensionCheckDocument {
	if findings == nil {
		findings = []extensionFinding{}
	}
	doc := extensionCheckDocument{
		Schema: extensionCheckSchema, Result: extensionCheckDocumentResult(counts, reasons, failOn),
		Counts: counts, Findings: findings,
	}
	if len(reasons) > 0 {
		doc.IncompleteReason = strings.Join(reasons, ", ")
	}
	return doc
}

func extensionCheckExit(counts extensionCheckCounts, failOn string, incomplete bool) int {
	if incomplete {
		return 2
	}
	if counts.Unapproved > 0 {
		return 3
	}
	if failOn == "unknown" && counts.Unknown > 0 {
		return 4
	}
	return 0
}

func writeExtensionCheck(w io.Writer, findings []extensionFinding, counts extensionCheckCounts, reasons []string, allUsers bool) error {
	profile := ""
	for _, finding := range findings {
		if finding.State == "approved" {
			continue
		}
		if allUsers && finding.ProfileRef != profile {
			profile = finding.ProfileRef
			if _, err := fmt.Fprintln(w, profile); err != nil {
				return err
			}
		}
		line := fmt.Sprintf("%s  %s  %s  %s", finding.Agent, finding.Kind, finding.ID, finding.State)
		if finding.Reason != "" {
			line += "  " + finding.Reason
		}
		if _, err := fmt.Fprintln(w, line); err != nil {
			return err
		}
	}
	if _, err := fmt.Fprintf(w, "%d unapproved, %d unknown.\n", counts.Unapproved, counts.Unknown); err != nil {
		return err
	}
	if len(reasons) > 0 {
		_, err := fmt.Fprintf(w, "Scan incomplete: %s\n", strings.Join(reasons, ", "))
		return err
	}
	return nil
}

func extensionCheckDocumentResult(counts extensionCheckCounts, reasons []string, failOn string) string {
	if len(reasons) > 0 {
		return "incomplete"
	}
	if extensionCheckExit(counts, failOn, false) != 0 {
		return "fail"
	}
	return "pass"
}

func writeExtensionJSON(cmd *cobra.Command, value any) error {
	encoder := json.NewEncoder(cmd.OutOrStdout())
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}
