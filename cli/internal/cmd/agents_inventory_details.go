package cmd

import (
	"encoding/json"
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
}
type extensionLocalDetail struct {
	Body     string
	Files    []string
	Commands []string
}

var detailLocationPattern = regexp.MustCompile(`(?i)([a-z][a-z0-9+.-]*://|www\.|[a-z]:[\\/]|(^|[\s("'])[/\\~]|[\w.-]+/[^\s]+)`)
var detailExecutablePattern = regexp.MustCompile(`^[A-Za-z0-9_.+-]{1,128}$`)
var detailSkillNamePattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

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
	return clean, false
}

func (r *extensionRoot) fileList(base string) []string {
	files := []string{}
	var walk func(string, int)
	walk = func(path string, depth int) {
		if depth > 16 || len(files) >= 1000 {
			r.scan.fail("extension_limit")
			return
		}
		for _, entry := range r.dirs(path) {
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
		Name                   string `yaml:"name"`
		Description            string `yaml:"description"`
		UserInvocable          *bool  `yaml:"user-invocable"`
		DisableModelInvocation bool   `yaml:"disable-model-invocation"`
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
	details.Name, _ = r.scan.detailText(front.Name)
	details.Description, details.DescriptionWithheld = r.scan.detailText(front.Description)
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
		prefix := filepath.Join(path, "scripts") + string(filepath.Separator)
		for _, name := range files {
			if strings.HasPrefix(name, prefix) {
				count++
			}
		}
		details.ScriptCount = &count
		if r.scan.localDisplay {
			local.Files = files
		}
	}
	details.Plugin, _ = r.scan.detailText(plugin)
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

func (s *extensionScan) hookCommand(command string) extensionHookCommand {
	word, rest := firstCommandWord(command)
	word = filepath.Base(strings.ReplaceAll(word, "\\", "/"))
	safe, hidden := s.detailText(word)
	if hidden || !detailExecutablePattern.MatchString(safe) {
		safe = "unknown"
	}
	managed := (safe == "preloop" || safe == "preloop.exe") && (strings.HasPrefix(rest, "agents permission-hook") || strings.HasPrefix(rest, "agents session-hook") || strings.HasPrefix(rest, "agents stop-hook") || strings.HasPrefix(rest, "agents activity-hook") || strings.HasPrefix(rest, "agents session-end-hook") || strings.HasPrefix(rest, "usage hook"))
	return extensionHookCommand{Executable: safe, PreloopManaged: managed}
}

func (r *extensionRoot) pluginDetails(path string, manifest map[string]json.RawMessage, ext *inventoryExtension) {
	details := &inventoryExtensionDetails{Bundles: &extensionBundleCounts{}}
	details.Description, details.DescriptionWithheld = r.scan.detailText(extensionString(manifest["description"]))
	var author struct {
		Name string `json:"name"`
	}
	_ = json.Unmarshal(manifest["author"], &author)
	if author.Name == "" {
		author.Name = extensionString(manifest["author"])
	}
	details.Author, _ = r.scan.detailText(author.Name)
	files := r.fileList(path)
	for _, file := range files {
		rel, _ := filepath.Rel(path, file)
		rel = filepath.ToSlash(rel)
		switch {
		case strings.HasPrefix(rel, "skills/") && strings.HasSuffix(rel, "/SKILL.md"):
			details.Bundles.Skills++
			skillPath := filepath.Dir(file)
			id := filepath.Base(skillPath)
			d, local := r.markdownDetails(skillPath, id, true, ext.ID)
			hash, status := treeHash(r.root, skillPath)
			ext.Bundled = append(ext.Bundled, inventoryExtension{Kind: "skill", ID: strings.Split(ext.ID, "@")[0] + ":" + id, Details: d, Local: local, ContentHash: hash, HashStatus: status})
		case strings.HasPrefix(rel, "commands/") && strings.HasSuffix(rel, ".md"):
			details.Bundles.Commands++
		case strings.HasPrefix(rel, "agents/") && strings.HasSuffix(rel, ".md"):
			details.Bundles.Agents++
		}
	}
	hookDoc := r.document(filepath.Join(path, "hooks", "hooks.json"))
	if hookDoc == nil {
		_ = json.Unmarshal(manifest["hooks"], &hookDoc)
	}
	var events map[string][]json.RawMessage
	if json.Unmarshal(hookDoc["hooks"], &events) == nil {
		for _, groups := range events {
			for _, group := range groups {
				var obj struct {
					Hooks []json.RawMessage `json:"hooks"`
				}
				_ = json.Unmarshal(group, &obj)
				details.Bundles.Hooks += len(obj.Hooks)
			}
		}
	}
	mcp := r.document(filepath.Join(path, ".mcp.json"))
	var servers map[string]json.RawMessage
	if json.Unmarshal(mcp["mcpServers"], &servers) == nil {
		details.Bundles.MCPServers = len(servers)
	} else if json.Unmarshal(manifest["mcpServers"], &servers) == nil {
		details.Bundles.MCPServers = len(servers)
	}
	ext.Details = details
	if r.scan.localDisplay {
		ext.Local = &extensionLocalDetail{Files: files}
	}
}
