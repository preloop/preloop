package cmd

import (
	"encoding/json"
	"fmt"
	"maps"
	"path/filepath"
	"regexp"
	"strings"
	"unicode"

	"gopkg.in/yaml.v3"
)

// inventoryExtensionDetails is the extensible, bounded metadata wire contract.
// Local content is held separately and can never be marshaled into a report.
type inventoryExtensionDetails struct {
	Name                string                 `json:"name,omitempty"`
	Description         string                 `json:"description,omitempty"`
	DescriptionWithheld bool                   `json:"description_withheld,omitempty"`
	Invocation          string                 `json:"invocation,omitempty"`
	ScriptCount         *int                   `json:"script_count,omitempty"`
	Plugin              string                 `json:"plugin,omitempty"`
	Author              string                 `json:"author,omitempty"`
	Bundles             *extensionBundleCounts `json:"bundles,omitempty"`
	HookMatchers        []extensionHookMatcher `json:"hook_matchers,omitempty"`
	// Schema 5 decision metadata. Hosts only, never URLs or paths.
	Marketplace    string                `json:"marketplace,omitempty"`
	HomepageHost   string                `json:"homepage_host,omitempty"`
	RepositoryHost string                `json:"repository_host,omitempty"`
	License        string                `json:"license,omitempty"`
	ModifiedAt     string                `json:"modified_at,omitempty"`
	ToolsDeclared  bool                  `json:"tools_declared,omitempty"`
	AllowedTools   []string              `json:"allowed_tools,omitempty"`
	HookEvents     []string              `json:"hook_events,omitempty"`
	BundleNames    *extensionBundleNames `json:"bundle_names,omitempty"`
	Transport      string                `json:"transport,omitempty"`
	Command        string                `json:"command,omitempty"`
	URLHost        string                `json:"url_host,omitempty"`
}
type extensionBundleCounts struct {
	Skills     int `json:"skills"`
	Commands   int `json:"commands"`
	Hooks      int `json:"hooks"`
	MCPServers int `json:"mcp_servers"`
	Agents     int `json:"agents"`
}
type extensionHookMatcher struct {
	Matcher         string                 `json:"matcher,omitempty"`
	MatcherWithheld bool                   `json:"matcher_withheld,omitempty"`
	Count           int                    `json:"count"`
	Commands        []extensionHookCommand `json:"commands"`
}
type extensionHookCommand struct {
	Executable     string `json:"executable"`
	PreloopManaged bool   `json:"preloop_managed"`
	Script         bool   `json:"script,omitempty"`
	InProject      bool   `json:"in_project,omitempty"`
}
type extensionLocalDetail struct {
	// Version is the authored frontmatter version; add() validates it.
	Version  string
	Body     string
	Files    []string
	Commands []string
}

var detailLocationPattern = regexp.MustCompile(`(?i)([a-z][a-z0-9+.-]*://|www\.|[a-z]:[\\/]|(^|[\s("'])[/\\~]|[\w.-]+/[^\s]+)`)
var detailExecutablePattern = regexp.MustCompile(`^[A-Za-z0-9_.+-]{1,128}$`)
var detailSkillNamePattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

func (s *extensionScan) detailText(value string) (string, bool) {
	// Inspect the entire authored value before truncation, including controls.
	normalized := strings.ToLower(strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
			if unicode.IsSpace(r) {
				return ' '
			}
			return -1
		}
		return r
	}, value))
	for _, segment := range strings.FieldsFunc(filepath.ToSlash(s.home), func(r rune) bool { return r == '/' || r == '\\' || r == ':' }) {
		if segment != "" && strings.Contains(normalized, strings.ToLower(segment)) {
			return "", true
		}
	}
	if detailLocationPattern.MatchString(normalized) {
		return "", true
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
	clean = strings.Join(strings.Fields(clean), " ")
	runes := []rune(clean)
	if len(runes) > 300 {
		clean = string(runes[:300])
	}
	return strings.TrimSpace(clean), false
}

func (r *extensionRoot) fileList(base string) []string {
	files := []string{}
	visited := 0
	var walk func(string, int)
	walk = func(path string, depth int) {
		if depth > 16 || len(files) >= 1000 || visited >= 2000 {
			r.scan.fail("extension_limit")
			return
		}
		for _, entry := range r.dirs(path) {
			if visited >= 2000 {
				r.scan.fail("extension_limit")
				return
			}
			visited++
			name := filepath.Join(path, entry.Name())
			info, err := r.stat(name)
			if err != nil {
				continue
			}
			if info.IsDir() {
				walk(name, depth+1)
			} else if info.Mode().IsRegular() {
				if len(files) >= 1000 {
					r.scan.fail("extension_limit")
					return
				}
				files = append(files, name)
			}
		}
	}
	walk(base, 0)
	return files
}

func (r *extensionRoot) markdownDetails(path, id string, skill bool, plugin string) (*inventoryExtensionDetails, *extensionLocalDetail) {
	file := path
	if skill {
		file = filepath.Join(path, "SKILL.md")
	}
	data := r.read(file)
	details := &inventoryExtensionDetails{}
	local := &extensionLocalDetail{}
	if r.scan.localDisplay {
		local.Body = string(data)
		local.Files = []string{file}
	}
	var front struct {
		Name                   string         `yaml:"name"`
		Description            string         `yaml:"description"`
		UserInvocable          *bool          `yaml:"user-invocable"`
		DisableModelInvocation bool           `yaml:"disable-model-invocation"`
		AllowedTools           any            `yaml:"allowed-tools"`
		License                string         `yaml:"license"`
		Hooks                  map[string]any `yaml:"hooks"`
		Version                any            `yaml:"version"`
	}
	text := strings.ReplaceAll(string(data), "\r\n", "\n")
	if strings.HasPrefix(text, "---\n") {
		if end := strings.Index(text[4:], "\n---"); end >= 0 {
			if yaml.Unmarshal([]byte(text[4:4+end]), &front) != nil {
				r.scan.fail("extension_malformed")
			}
		} else {
			r.scan.fail("extension_malformed")
		}
	}
	if front.Version != nil {
		local.Version = fmt.Sprint(front.Version)
	}
	details.Name, _ = r.scan.detailText(front.Name)
	// Bodies never leave the device in a report; only front matter does.
	details.Description, details.DescriptionWithheld = r.scan.descriptionText(front.Description)
	if front.AllowedTools != nil {
		details.ToolsDeclared = true
		details.AllowedTools = r.scan.safeTools(toolList(front.AllowedTools))
	}
	events := map[string]bool{}
	for event := range front.Hooks {
		events[event] = true
	}
	details.HookEvents = eventNames(events)
	name := id
	if skill && front.Name != "" {
		name = front.Name
	}
	safeName, withheld := r.scan.detailText(name)
	if !withheld && detailSkillNamePattern.MatchString(safeName) {
		if plugin != "" {
			safePlugin, hidden := r.scan.detailText(strings.Split(plugin, "@")[0])
			if hidden || !detailSkillNamePattern.MatchString(safePlugin) {
				safeName = ""
			} else {
				safeName = safePlugin + ":" + safeName
			}
		}
		if safeName != "" {
			if r.scan.agent == "codex-cli" {
				details.Invocation = "$" + safeName + " (or /skills)"
			} else if front.UserInvocable == nil || *front.UserInvocable {
				details.Invocation = "/" + safeName
			}
		}
	}
	if skill {
		implicit := !front.DisableModelInvocation
		if r.scan.agent == "codex-cli" {
			implicit = true
			var agent struct {
				Policy struct {
					AllowImplicitInvocation *bool `yaml:"allow_implicit_invocation"`
				} `yaml:"policy"`
			}
			if data := r.read(filepath.Join(path, "agents", "openai.yaml")); data != nil {
				if yaml.Unmarshal(data, &agent) != nil {
					r.scan.fail("extension_malformed")
				} else if agent.Policy.AllowImplicitInvocation != nil {
					implicit = *agent.Policy.AllowImplicitInvocation
				}
			}
		}
		if implicit {
			if details.Invocation != "" {
				details.Invocation += "; "
			}
			details.Invocation += "loaded by the model when relevant"
		}
		files := r.fileList(path)
		count := 0
		// Skills ship executables in scripts/ (the documented place) or bin/.
		for _, dir := range []string{"scripts", "bin"} {
			prefix := filepath.Join(path, dir) + string(filepath.Separator)
			for _, name := range files {
				if strings.HasPrefix(name, prefix) {
					count++
				}
			}
		}
		details.ScriptCount = &count
		details.ModifiedAt = r.modifiedDate(files)
		details.License = licenseName(front.License, r.licenseFile(path))
		if r.scan.localDisplay {
			local.Files = files
		}
	} else {
		details.ModifiedAt = r.modifiedDate([]string{file})
	}
	details.Plugin, _ = r.scan.detailText(plugin)
	if _, market, ok := strings.Cut(plugin, "@"); ok {
		details.Marketplace, _ = r.scan.detailText(market)
	}
	return details, local
}

// firstCommandWord parses only the executable token, without expanding a shell.
func firstCommandWord(command string) (string, string) {
	command = strings.TrimSpace(command)
	if command == "" {
		return "", ""
	}
	if command[0] == '\'' || command[0] == '"' {
		if end := strings.IndexByte(command[1:], command[0]); end >= 0 {
			return command[1 : 1+end], strings.TrimSpace(command[end+2:])
		}
		return "", ""
	}
	end := strings.IndexFunc(command, unicode.IsSpace)
	if end < 0 {
		return command, ""
	}
	return command[:end], strings.TrimSpace(command[end:])
}

// hookInterpreters run the file named by their first argument, or the
// command after -c; that file decides whether the hook runs a script.
var hookInterpreters = map[string]bool{"bash": true, "sh": true, "zsh": true, "node": true, "bun": true, "deno": true, "python": true, "python3": true, "ruby": true, "perl": true}

func (s *extensionScan) hookCommand(command, scope string) extensionHookCommand {
	word, rest := firstCommandWord(command)
	script, inProject := hookScript(word, scope)
	if base := filepath.Base(strings.ReplaceAll(word, "\\", "/")); !script && hookInterpreters[base] {
		next := rest
		for next != "" {
			arg, after := firstCommandWord(next)
			if arg == "-c" {
				arg, _ = firstCommandWord(after)
				inner, innerRest := firstCommandWord(arg)
				if inner == "exec" {
					inner, _ = firstCommandWord(innerRest)
				}
				script, inProject = hookScript(inner, scope)
				break
			}
			if !strings.HasPrefix(arg, "-") {
				script, inProject = hookScript(arg, scope)
				break
			}
			if after == next {
				break
			}
			next = after
		}
	}
	word = filepath.Base(strings.ReplaceAll(word, "\\", "/"))
	safe, hidden := s.detailText(word)
	if hidden || !detailExecutablePattern.MatchString(safe) {
		safe = "unknown"
	}
	managed := false
	args := strings.Fields(rest)
	if (safe == "preloop" || safe == "preloop.exe") && len(args) >= 2 {
		managed = args[0] == "usage" && args[1] == "hook"
		if args[0] == "agents" {
			for _, name := range []string{"permission-hook", "session-hook", "stop-hook", "activity-hook", "session-end-hook"} {
				managed = managed || args[1] == name
			}
		}
	}
	return extensionHookCommand{Executable: safe, PreloopManaged: managed, Script: script, InProject: inProject}
}

// componentRoots augments conventional plugin directories with manifest paths.
// Only paths inside this install are accepted, never sibling or absolute paths.
func (r *extensionRoot) componentRoots(manifest map[string]json.RawMessage, field, fallback string) []string {
	roots := []string{fallback}
	raw := manifest[field]
	var paths []string
	if value := extensionString(raw); value != "" {
		paths = []string{value}
	} else {
		_ = json.Unmarshal(raw, &paths)
	}
	for _, path := range paths {
		if len(roots) >= inventoryExtensionDirectoryLimit {
			r.scan.fail("extension_limit")
			break
		}
		if !filepath.IsLocal(path) || strings.Contains(path, "\\") {
			r.scan.fail("extension_unsafe_path")
			continue
		}
		roots = append(roots, filepath.ToSlash(filepath.Clean(path)))
	}
	return uniqueSorted(roots)
}

func componentContains(file string, roots []string, skills bool) bool {
	for _, root := range roots {
		if file == root {
			return true
		}
		prefix := strings.TrimSuffix(root, "/") + "/"
		if !strings.HasPrefix(file, prefix) {
			continue
		}
		rest := strings.TrimPrefix(file, prefix)
		if !skills || rest == "SKILL.md" || (strings.Count(rest, "/") == 1 && strings.HasSuffix(rest, "/SKILL.md")) {
			return true
		}
	}
	return false
}

func (r *extensionRoot) pluginDetails(path string, manifest map[string]json.RawMessage, ext *inventoryExtension) {
	details := &inventoryExtensionDetails{Bundles: &extensionBundleCounts{}, BundleNames: &extensionBundleNames{}}
	entry := r.scan.marketEntries[ext.ID]
	// Purpose: the manifest, else the marketplace listing. Both are published
	// metadata; README and component bodies are content and stay on the device.
	for _, candidate := range []string{extensionString(manifest["description"]), extensionString(entry["description"])} {
		if strings.TrimSpace(candidate) != "" {
			details.Description, details.DescriptionWithheld = r.scan.descriptionText(candidate)
			break
		}
	}
	if _, market, ok := strings.Cut(ext.ID, "@"); ok {
		details.Marketplace, _ = r.scan.detailText(market)
	}
	details.HomepageHost = manifestURLHost(manifest["homepage"])
	if details.HomepageHost == "" {
		details.HomepageHost = manifestURLHost(entry["homepage"])
	}
	details.RepositoryHost = manifestURLHost(manifest["repository"])
	if details.RepositoryHost == "" {
		details.RepositoryHost = manifestURLHost(entry["repository"])
	}
	details.License = licenseName(extensionString(manifest["license"]), r.licenseFile(path))
	if details.License == "" {
		details.License = licenseName(extensionString(entry["license"]), nil)
	}
	if manifest["author"] == nil && entry["author"] != nil {
		manifest = maps.Clone(manifest)
		manifest["author"] = entry["author"]
	}
	var author struct {
		Name string `json:"name"`
	}
	_ = json.Unmarshal(manifest["author"], &author)
	if author.Name == "" {
		author.Name = extensionString(manifest["author"])
	}
	details.Author, _ = r.scan.detailText(author.Name)
	files := r.fileList(path)
	details.ModifiedAt = r.modifiedDate(files)
	// The plugin's tools are the union of what its components declare; one
	// undeclared component leaves the plugin's tool use undeclared.
	tools, declared, components := []string{}, true, 0
	addTools := func(found []string, ok bool) {
		components++
		tools = append(tools, found...)
		declared = declared && ok
	}
	skills := r.componentRoots(manifest, "skills", "skills")
	commands := r.componentRoots(manifest, "commands", "commands")
	agents := r.componentRoots(manifest, "agents", "agents")
	for _, file := range files {
		rel, _ := filepath.Rel(path, file)
		rel = filepath.ToSlash(rel)
		if filepath.Base(file) == "SKILL.md" && componentContains(rel, skills, true) {
			details.Bundles.Skills++
			skillPath := filepath.Dir(file)
			id := filepath.Base(skillPath)
			details.BundleNames.Skills = append(details.BundleNames.Skills, id)
			d, local := r.markdownDetails(skillPath, id, true, ext.ID)
			addTools(d.AllowedTools, d.ToolsDeclared)
			hash, status := treeHash(r.root, skillPath)
			r.captureBundledSkill(ext.ID, path, strings.Split(ext.ID, "@")[0]+":"+id, skillPath, hash)
			ext.Bundled = append(ext.Bundled, inventoryExtension{Kind: "skill", ID: strings.Split(ext.ID, "@")[0] + ":" + id, Details: d, Local: local, ContentHash: hash, HashStatus: status})
		}
		if strings.HasSuffix(rel, ".md") && componentContains(rel, commands, false) {
			details.Bundles.Commands++
			details.BundleNames.Commands = append(details.BundleNames.Commands, strings.TrimSuffix(filepath.Base(rel), ".md"))
			addTools(r.componentTools(file))
		}
		if strings.HasSuffix(rel, ".md") && componentContains(rel, agents, false) {
			details.Bundles.Agents++
			details.BundleNames.Agents = append(details.BundleNames.Agents, strings.TrimSuffix(filepath.Base(rel), ".md"))
			addTools(r.componentTools(file))
		}
	}
	hookDocs := []map[string]json.RawMessage{}
	for _, file := range r.componentRoots(manifest, "hooks", "hooks/hooks.json") {
		if doc := r.document(filepath.Join(path, file)); doc != nil {
			hookDocs = append(hookDocs, doc)
		}
	}
	var inline map[string]json.RawMessage
	if json.Unmarshal(manifest["hooks"], &inline) == nil && inline != nil {
		hookDocs = append(hookDocs, inline)
	}
	localCommands := []string{}
	hookEvents := map[string]bool{}
	scripts := 0
	for _, doc := range hookDocs {
		var events map[string][]json.RawMessage
		if json.Unmarshal(doc["hooks"], &events) != nil {
			r.scan.fail("extension_malformed")
			continue
		}
		for event, groups := range events {
			hookEvents[event] = true
			for _, group := range groups {
				var obj struct {
					Matcher string `json:"matcher"`
					Command string `json:"command"`
					Hooks   []struct {
						Command string `json:"command"`
					} `json:"hooks"`
				}
				if json.Unmarshal(group, &obj) != nil {
					r.scan.fail("extension_malformed")
					continue
				}
				details.Bundles.Hooks += len(obj.Hooks)
				for _, command := range append([]string{obj.Command}, func() []string {
					out := []string{}
					for _, hook := range obj.Hooks {
						out = append(out, hook.Command)
					}
					return out
				}()...) {
					if command != "" && r.scan.hookCommand(command, "user").Script {
						scripts++
					}
				}
				if obj.Command != "" {
					details.Bundles.Hooks++
					localCommands = append(localCommands, fmt.Sprintf("Matcher: %s\n%s", obj.Matcher, obj.Command))
				}
				for _, hook := range obj.Hooks {
					if hook.Command != "" {
						localCommands = append(localCommands, fmt.Sprintf("Matcher: %s\n%s", obj.Matcher, hook.Command))
					}
				}
			}
		}
	}
	serverNames := map[string]bool{}
	for _, file := range r.componentRoots(manifest, "mcpServers", ".mcp.json") {
		doc := r.document(filepath.Join(path, file))
		if doc == nil {
			continue
		}
		var servers map[string]json.RawMessage
		if json.Unmarshal(doc["mcpServers"], &servers) != nil {
			r.scan.fail("extension_malformed")
			continue
		}
		for name := range servers {
			serverNames[name] = true
		}
	}
	var servers map[string]json.RawMessage
	if json.Unmarshal(manifest["mcpServers"], &servers) == nil {
		for name := range servers {
			serverNames[name] = true
		}
	}
	details.Bundles.MCPServers = len(serverNames)
	details.HookEvents = eventNames(hookEvents)
	if components > 0 {
		details.ToolsDeclared = declared
		details.AllowedTools = uniqueSorted(tools)
		if len(details.AllowedTools) > detailNameLimit {
			details.AllowedTools = details.AllowedTools[:detailNameLimit]
		}
	}
	if details.Bundles.Hooks > 0 {
		details.ScriptCount = &scripts
	}
	names := []string{}
	for name := range serverNames {
		names = append(names, name)
	}
	details.BundleNames.Skills = safeNames(details.BundleNames.Skills)
	details.BundleNames.Commands = safeNames(details.BundleNames.Commands)
	details.BundleNames.Agents = safeNames(details.BundleNames.Agents)
	details.BundleNames.MCPServers = safeNames(names)
	if details.Bundles.Hooks > 10000 || details.Bundles.MCPServers > 10000 {
		r.scan.fail("extension_limit")
		details.Bundles = nil
		details.BundleNames = nil
	}
	ext.Details = details
	if r.scan.localDisplay {
		body, _ := json.MarshalIndent(manifest, "", "  ")
		ext.Local = &extensionLocalDetail{Files: files, Body: string(body), Commands: localCommands}
	}
}
