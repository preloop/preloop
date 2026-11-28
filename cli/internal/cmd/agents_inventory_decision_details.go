package cmd

// Decision metadata: the facts an approver needs to judge an extension
// (purpose, publisher, licence, what it bundles, what it is allowed to do).
// Everything here goes through the same bounded text boundary as other
// details: no paths, no URLs (only host names), no command arguments.

import (
	"encoding/json"
	"net"
	"net/url"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode"

	"gopkg.in/yaml.v3"

	"github.com/preloop/preloop/cli/internal/redact"
)

const detailNameLimit = 50

var detailHostPattern = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+$`)
var detailLicensePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$`)
var detailToolPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.:*()| -]{0,63}$`)
var detailBundleNamePattern = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,64}$`)

// genericHomeSegments are system directory names that appear in every home
// path. They identify nobody, so a description that uses the word "users"
// is not withheld for it; the account name segment still is.
var genericHomeSegments = map[string]bool{"users": true, "home": true, "private": true, "tmp": true, "var": true, "root": true, "folders": true}

type extensionBundleNames struct {
	Skills     []string `json:"skills,omitempty"`
	Commands   []string `json:"commands,omitempty"`
	Agents     []string `json:"agents,omitempty"`
	MCPServers []string `json:"mcp_servers,omitempty"`
}

// descriptionText keeps an authored or derived description readable: a
// path-like or URL-like word is replaced by an ellipsis instead of
// withholding the whole sentence. It reports withheld only when nothing
// readable is left.
func (s *extensionScan) descriptionText(value string) (string, bool) {
	identifying := []string{}
	for _, segment := range strings.FieldsFunc(filepath.ToSlash(s.home), func(r rune) bool { return r == '/' || r == '\\' || r == ':' }) {
		// One- and two-letter segments (a drive letter, macOS's "T") name
		// nobody and would hide every word that contains the letter.
		if len(segment) > 2 && !genericHomeSegments[strings.ToLower(segment)] {
			identifying = append(identifying, strings.ToLower(segment))
		}
	}
	clean := strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
			if unicode.IsSpace(r) {
				return ' '
			}
			return -1
		}
		return r
	}, value)
	words := strings.Fields(clean)
	kept := 0
	for i, word := range words {
		lower := strings.ToLower(word)
		hidden := detailLocationPattern.MatchString(lower)
		for _, segment := range identifying {
			hidden = hidden || strings.Contains(lower, segment)
		}
		if hidden {
			words[i] = "…"
		} else {
			kept++
		}
	}
	if kept == 0 {
		return "", value != ""
	}
	clean = strings.Join(words, " ")
	if runes := []rune(clean); len(runes) > 300 {
		clean = strings.TrimSpace(string(runes[:300]))
	}
	// The joined text is checked again, as the server will.
	if detailLocationPattern.MatchString(strings.ToLower(clean)) {
		return "", true
	}
	// Descriptions are metadata, not reviewed content, so a sentence with
	// anything the content redactor would replace is not sent at all.
	if result, err := redact.Text(clean, redact.Options{Home: s.home}); err != nil || result.Total > 0 || result.Withheld != "" {
		return "", true
	}
	return clean, false
}

// urlHost reduces a URL to its host name. Addresses, single-label and
// malformed hosts are dropped; loopback hosts read "localhost".
func urlHost(value string) string {
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil || parsed.Host == "" {
		return ""
	}
	host := strings.ToLower(parsed.Hostname())
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return "localhost"
	}
	if ip := net.ParseIP(host); ip != nil {
		if ip.IsLoopback() {
			return "localhost"
		}
		return ""
	}
	if !detailHostPattern.MatchString(host) || len(host) > 253 {
		return ""
	}
	return host
}

// manifestURLHost reads a string or {"url": ...} manifest field.
func manifestURLHost(raw json.RawMessage) string {
	if value := extensionString(raw); value != "" {
		return urlHost(value)
	}
	var obj struct {
		URL string `json:"url"`
	}
	_ = json.Unmarshal(raw, &obj)
	return urlHost(obj.URL)
}

// licenseName reads an SPDX-like manifest value, or recognizes the first line
// of a licence file. Unrecognized text is not sent.
func licenseName(manifest string, file []byte) string {
	if manifest = strings.TrimSpace(manifest); manifest != "" {
		if detailLicensePattern.MatchString(manifest) {
			return manifest
		}
		return ""
	}
	head := strings.ToLower(string(file))
	if len(head) > 2000 {
		head = head[:2000]
	}
	for _, known := range []struct{ marker, name string }{
		{"apache license", "Apache-2.0"},
		{"mit license", "MIT"},
		{"permission is hereby granted, free of charge", "MIT"},
		{"isc license", "ISC"},
		{"mozilla public license", "MPL-2.0"},
		{"gnu lesser general public license", "LGPL"},
		{"gnu affero general public license", "AGPL"},
		{"gnu general public license", "GPL"},
		{"bsd 3-clause", "BSD-3-Clause"},
		{"bsd 2-clause", "BSD-2-Clause"},
		{"redistribution and use in source and binary forms", "BSD"},
		{"the unlicense", "Unlicense"},
	} {
		if strings.Contains(head, known.marker) {
			return known.name
		}
	}
	return ""
}

func (r *extensionRoot) licenseFile(dir string) []byte {
	for _, name := range []string{"LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE"} {
		path := filepath.Join(dir, name)
		if info, err := r.stat(path); err == nil && info.Mode().IsRegular() {
			return r.read(path)
		}
	}
	return nil
}

// modifiedDate is the newest modification day among files, in UTC.
func (r *extensionRoot) modifiedDate(files []string) string {
	var newest time.Time
	for _, file := range files {
		if info, err := r.stat(file); err == nil && info.ModTime().After(newest) {
			newest = info.ModTime()
		}
	}
	if newest.IsZero() {
		return ""
	}
	return newest.UTC().Format("2006-01-02")
}

// toolList accepts the frontmatter forms Claude Code reads: a YAML list, or a
// comma or space separated string.
func toolList(value any) []string {
	var raw []string
	switch v := value.(type) {
	case string:
		sep := " "
		if strings.Contains(v, ",") {
			sep = ","
		}
		raw = strings.Split(v, sep)
	case []any:
		for _, item := range v {
			if s, ok := item.(string); ok {
				raw = append(raw, s)
			}
		}
	}
	return raw
}

// safeTools bounds declared tool names. A tool rule that names a path is
// withheld, so "Bash(./run.sh)" is reported as "Bash".
func (s *extensionScan) safeTools(raw []string) []string {
	tools := []string{}
	for _, tool := range raw {
		tool = strings.TrimSpace(tool)
		if tool == "" {
			continue
		}
		safe, hidden := s.detailText(tool)
		if hidden || !detailToolPattern.MatchString(safe) {
			safe, _, _ = strings.Cut(tool, "(")
			if !detailToolPattern.MatchString(safe) {
				continue
			}
		}
		tools = append(tools, safe)
		if len(tools) == detailNameLimit {
			break
		}
	}
	return uniqueSorted(tools)
}

// safeNames bounds bundle component names for display.
func safeNames(names []string) []string {
	out := []string{}
	for _, name := range uniqueSorted(names) {
		if detailBundleNamePattern.MatchString(name) && !detailLocationPattern.MatchString(strings.ToLower(name)) {
			out = append(out, name)
		}
		if len(out) == detailNameLimit {
			break
		}
	}
	return out
}

// eventNames returns valid hook event names, sorted.
func eventNames(events map[string]bool) []string {
	names := []string{}
	for event := range events {
		if extensionEventPattern.MatchString(event) {
			names = append(names, event)
		}
	}
	sort.Strings(names)
	if len(names) > detailNameLimit {
		names = names[:detailNameLimit]
	}
	return names
}

// mcpDetails states how a configured server runs: its transport, the
// executable it starts or the host it calls. Arguments, environment,
// headers and URL paths are never read into the DTO.
func (s *extensionScan) mcpDetails(server map[string]any) *inventoryExtensionDetails {
	command, _ := server["command"].(string)
	address, _ := server["url"].(string)
	if address == "" {
		address, _ = server["serverUrl"].(string)
	}
	transport, _ := server["type"].(string)
	if transport == "" {
		transport, _ = server["transport"].(string)
	}
	transport = strings.ToLower(transport)
	switch {
	case transport == "streamable-http" || transport == "streamable_http":
		transport = "http"
	case transport == "stdio" || transport == "http" || transport == "sse":
	case command != "":
		transport = "stdio"
	case address != "":
		transport = "http"
	default:
		transport = ""
	}
	details := &inventoryExtensionDetails{Transport: transport}
	if command != "" {
		details.Command = s.hookCommand(command, "user").Executable
	}
	if address != "" {
		details.URLHost = urlHost(address)
	}
	return details
}

// hookScript reports whether a hook's executable is a file run by path, and
// whether that file lives in the project that configured the hook.
func hookScript(word, scope string) (script, inProject bool) {
	word = strings.TrimSpace(word)
	if word == "" {
		return false, false
	}
	script = strings.ContainsAny(word, `/\`) || strings.HasPrefix(word, "~") || strings.HasPrefix(word, "$")
	if !script {
		return false, false
	}
	upper := strings.ToUpper(word)
	inProject = strings.Contains(upper, "CLAUDE_PROJECT_DIR") ||
		(scope == "project" && !strings.HasPrefix(word, "/") && !strings.HasPrefix(word, "~") && !strings.HasPrefix(word, "$"))
	return script, inProject
}

// schema4 is the details a version 4 server accepts: no decision metadata,
// no details for MCP servers, and no script flags on hook commands.
func (d *inventoryExtensionDetails) schema4() *inventoryExtensionDetails {
	if d == nil {
		return nil
	}
	out := &inventoryExtensionDetails{Name: d.Name, Description: d.Description, DescriptionWithheld: d.DescriptionWithheld, Invocation: d.Invocation, ScriptCount: d.ScriptCount, Plugin: d.Plugin, Author: d.Author, Bundles: d.Bundles}
	if d.Bundles != nil && d.ScriptCount != nil {
		out.ScriptCount = nil // plugin script counts are new in version 5
	}
	for _, matcher := range d.HookMatchers {
		commands := make([]extensionHookCommand, len(matcher.Commands))
		for i, command := range matcher.Commands {
			commands[i] = extensionHookCommand{Executable: command.Executable, PreloopManaged: command.PreloopManaged}
		}
		matcher.Commands = commands
		out.HookMatchers = append(out.HookMatchers, matcher)
	}
	if out.Name == "" && out.Description == "" && !out.DescriptionWithheld && out.Invocation == "" && out.ScriptCount == nil && out.Plugin == "" && out.Author == "" && out.Bundles == nil && len(out.HookMatchers) == 0 {
		return nil
	}
	return out
}

// componentTools reads the tools a plugin command or agent declares in its
// frontmatter: "allowed-tools" for commands, "tools" for agents.
func (r *extensionRoot) componentTools(path string) ([]string, bool) {
	text := strings.ReplaceAll(string(r.read(path)), "\r\n", "\n")
	if !strings.HasPrefix(text, "---\n") {
		return nil, false
	}
	end := strings.Index(text[4:], "\n---")
	if end < 0 {
		return nil, false
	}
	var front struct {
		AllowedTools any `yaml:"allowed-tools"`
		Tools        any `yaml:"tools"`
	}
	if yaml.Unmarshal([]byte(text[4:4+end]), &front) != nil {
		return nil, false
	}
	if front.AllowedTools != nil {
		return r.scan.safeTools(toolList(front.AllowedTools)), true
	}
	if front.Tools != nil {
		return r.scan.safeTools(toolList(front.Tools)), true
	}
	return nil, false
}
