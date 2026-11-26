package cmd

// Extension discovery has a deliberately narrow output type. Parsed files,
// command strings, source locations and project roots never enter the DTO.
// Extension text for review is gathered separately, by the content pass of a
// report, and never through this DTO (agents_extension_content.go).
import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	toml "github.com/pelletier/go-toml/v2"
)

const inventoryExtensionLimit = 200
const inventoryExtensionDirectoryLimit = 256

type inventoryExtension struct {
	Kind           string `json:"kind"`
	ID             string `json:"id"`
	Version        string `json:"version,omitempty"`
	SourceKind     string `json:"source_kind"`
	SourceRefHash  string `json:"source_ref_hash,omitempty"`
	Scope          string `json:"scope"`
	ProjectRefHash string `json:"project_ref_hash,omitempty"`
	Enabled        string `json:"enabled"`
	ContentHash    string `json:"content_hash,omitempty"`
	HashStatus     string `json:"content_hash_status,omitempty"`
	Event          string `json:"event,omitempty"`
	Count          int    `json:"count,omitempty"`
	IDHashed       bool   `json:"id_hashed"`
	// PolicyID is the identifier an inventory report stores. Listings keep
	// the clear name in ID and do not print this field.
	PolicyID string                     `json:"-"`
	Details  *inventoryExtensionDetails `json:"details,omitempty"`
	Local    *extensionLocalDetail      `json:"-"`
	Bundled  []inventoryExtension       `json:"-"`
}

type extensionScan struct {
	home, salt   string
	agent        string
	hookDetails  map[string][]extensionHookMatcher
	hookLocal    map[string][]string
	localDisplay bool
	// capture is set only by the content pass of a report; see
	// agents_extension_content.go. A plain scan reads no extension text.
	capture     *contentCapture
	hookContent map[string][]contentHookSource
	extensions  []inventoryExtension
	errors      map[string]bool
	hookHashes  map[string][]string
}

type extensionRoot struct {
	root *os.Root
	scan *extensionScan
}

func extensionHash(data []byte) string {
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func (s *extensionScan) fail(code string) { s.errors[code] = true }

func (s *extensionScan) open(path string) *extensionRoot {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		s.fail("extension_unreadable")
		return nil
	}
	if info.Mode()&os.ModeSymlink != 0 {
		s.fail("extension_symlink")
		return nil
	}
	if !info.IsDir() {
		s.fail("extension_malformed")
		return nil
	}
	root, err := os.OpenRoot(path)
	if err != nil {
		s.fail("extension_unreadable")
		return nil
	}
	opened, err := root.Stat(".")
	if err != nil || !os.SameFile(info, opened) {
		root.Close()
		s.fail("extension_unreadable")
		return nil
	}
	return &extensionRoot{root: root, scan: s}
}

func (r *extensionRoot) stat(path string) (os.FileInfo, error) {
	if !filepath.IsLocal(path) {
		r.scan.fail("extension_unsafe_path")
		return nil, os.ErrPermission
	}
	part := ""
	var info os.FileInfo
	for _, component := range strings.Split(filepath.Clean(path), string(filepath.Separator)) {
		part = filepath.Join(part, component)
		var err error
		info, err = r.root.Lstat(part)
		if os.IsNotExist(err) {
			return nil, err
		}
		if err != nil {
			r.scan.fail("extension_unreadable")
			return nil, err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			r.scan.fail("extension_symlink")
			return nil, os.ErrPermission
		}
	}
	return info, nil
}

func (r *extensionRoot) read(path string) []byte {
	before, err := r.stat(path)
	if err != nil {
		return nil
	}
	if !before.Mode().IsRegular() {
		r.scan.fail("extension_unreadable")
		return nil
	}
	if before.Size() > inventoryConfigLimit {
		r.scan.fail("extension_file_limit")
		return nil
	}
	file, err := openInventoryConfig(r.root, path)
	if err != nil {
		r.scan.fail("extension_unreadable")
		return nil
	}
	defer file.Close()
	after, err := file.Stat()
	if err != nil || !os.SameFile(before, after) {
		r.scan.fail("extension_unreadable")
		return nil
	}
	data, err := io.ReadAll(io.LimitReader(file, inventoryConfigLimit+1))
	if err != nil {
		r.scan.fail("extension_unreadable")
		return nil
	}
	if len(data) > inventoryConfigLimit {
		r.scan.fail("extension_file_limit")
		return nil
	}
	return data
}

func (r *extensionRoot) dirs(path string) []os.DirEntry {
	before, err := r.stat(path)
	if err != nil {
		return nil
	}
	if !before.IsDir() {
		r.scan.fail("extension_malformed")
		return nil
	}
	file, err := r.root.Open(path)
	if err != nil {
		r.scan.fail("extension_unreadable")
		return nil
	}
	defer file.Close()
	after, err := file.Stat()
	if err != nil || !os.SameFile(before, after) {
		r.scan.fail("extension_unreadable")
		return nil
	}
	entries, err := file.ReadDir(inventoryExtensionDirectoryLimit + 1)
	if err != nil && err != io.EOF {
		r.scan.fail("extension_unreadable")
	}
	if len(entries) > inventoryExtensionDirectoryLimit {
		r.scan.fail("extension_limit")
		entries = entries[:inventoryExtensionDirectoryLimit]
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
	return entries
}

func (r *extensionRoot) document(path string) map[string]json.RawMessage {
	data := r.read(path)
	if data == nil {
		return nil
	}
	var doc map[string]json.RawMessage
	if json.Unmarshal(data, &doc) != nil || doc == nil {
		r.scan.fail("extension_malformed")
		return nil
	}
	return doc
}

func extensionString(raw json.RawMessage) string {
	var value string
	_ = json.Unmarshal(raw, &value)
	return value
}

func extensionIdentifier(value string) string {
	value = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return -1
		}
		return r
	}, value)
	if len(value) > 128 {
		value = value[:128]
		for !utf8.ValidString(value) {
			value = value[:len(value)-1]
		}
	}
	return value
}

func (s *extensionScan) add(ext inventoryExtension) {
	if len(s.extensions) == inventoryExtensionLimit {
		s.fail("extension_limit")
		return
	}
	rawID := ext.ID
	ext.ID = extensionIdentifier(ext.ID)
	if ext.ID == "" || strings.ContainsAny(ext.ID, `/\`) {
		s.fail("extension_malformed")
		return
	}
	// Inventory reports hash identifiers that contain a home-path segment.
	// A local listing keeps those identifiers in clear: nothing is sent.
	// PolicyID stays the report identifier so a check can match the export.
	ext.PolicyID = ext.ID
	if (ext.SourceKind == "local" || ext.SourceKind == "git" || ext.Scope == "project") && !(ext.Kind == "plugin" && strings.HasSuffix(ext.SourceKind, "marketplace")) {
		for _, segment := range strings.FieldsFunc(filepath.ToSlash(s.home), func(r rune) bool { return r == '/' || r == '\\' || r == ':' }) {
			if segment != "" && strings.Contains(strings.ToLower(rawID), strings.ToLower(segment)) {
				ext.PolicyID = "hash:" + strings.TrimPrefix(extensionHash([]byte(rawID)), "sha256:")[:16]
				break
			}
		}
	}
	if !s.localDisplay {
		ext.ID = ext.PolicyID
		ext.IDHashed = ext.PolicyID != extensionIdentifier(rawID)
	}
	if ext.Enabled == "" {
		ext.Enabled = "unknown"
	}
	if !discoveryVersionPattern.MatchString(ext.Version) {
		ext.Version = ""
	}
	for i, old := range s.extensions {
		if old.Kind == ext.Kind && old.ID == ext.ID && old.Scope == ext.Scope && old.ProjectRefHash == ext.ProjectRefHash {
			s.extensions[i] = ext
			return
		}
	}
	s.extensions = append(s.extensions, ext)
}

func (s *extensionScan) projectHash(project string) string {
	if s.salt != "" {
		return "sha256:" + configPathHash(s.salt, s.home, project)
	}
	return extensionHash([]byte(homeRelativeConfigPath(s.home, project)))
}

func (r *extensionRoot) localFiles(base, scope, project string, skills bool) {
	for _, entry := range r.dirs(base) {
		path := filepath.Join(base, entry.Name())
		info, err := r.stat(path)
		if err != nil {
			continue
		}
		kind, id := "command", strings.TrimSuffix(entry.Name(), filepath.Ext(entry.Name()))
		var hash, status string
		if skills {
			if !info.IsDir() {
				continue
			}
			kind, id = "skill", entry.Name()
			// The whole skill directory: scripts and references beside SKILL.md
			// change behaviour as much as SKILL.md itself does.
			hash, status = treeHash(r.root, path)
			r.captureTree(kind, id, path, hash)
		} else {
			if !info.Mode().IsRegular() || !strings.EqualFold(filepath.Ext(path), ".md") {
				continue
			}
			hash, status = singleFileHash(r.root, path)
			r.captureCommand(id, path, hash)
		}
		details, local := r.markdownDetails(path, id, skills, "")
		r.scan.add(inventoryExtension{Details: details, Local: local, Kind: kind, ID: id, SourceKind: "local", Scope: scope, ProjectRefHash: project, ContentHash: hash, HashStatus: status})
	}
}

var extensionEventPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_-]{0,31}$`)

func (r *extensionRoot) hooks(raw json.RawMessage, scope, project string, files ...string) {
	if raw == nil {
		return
	}
	var events map[string]json.RawMessage
	if json.Unmarshal(raw, &events) != nil || events == nil {
		r.scan.fail("extension_malformed")
		return
	}
	names := make([]string, 0, len(events))
	for event := range events {
		names = append(names, event)
	}
	sort.Strings(names)
	for _, event := range names {
		if !extensionEventPattern.MatchString(event) {
			r.scan.fail("extension_malformed")
			continue
		}
		var groups []json.RawMessage
		if json.Unmarshal(events[event], &groups) != nil {
			r.scan.fail("extension_malformed")
			continue
		}
		hashes := []string{}
		matchers := []extensionHookMatcher{}
		commands := []string{}
		captured := []contentHookSource{}
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
			matcher, hidden := r.scan.detailText(obj.Matcher)
			item := extensionHookMatcher{Matcher: matcher, MatcherWithheld: hidden, Commands: []extensionHookCommand{}}
			for _, hook := range append([]struct {
				Command string `json:"command"`
			}{{Command: obj.Command}}, obj.Hooks...) {
				if hook.Command != "" && r.scan.capture != nil {
					captured = append(captured, contentHookSource{Matcher: obj.Matcher, Command: hook.Command})
				}
			}
			if obj.Command != "" {
				item.Commands = append(item.Commands, r.scan.hookCommand(obj.Command))
				commands = append(commands, obj.Command)
				hashes = append(hashes, extensionHash([]byte(obj.Command)))
			}
			for _, hook := range obj.Hooks {
				if hook.Command != "" {
					item.Commands = append(item.Commands, r.scan.hookCommand(hook.Command))
					commands = append(commands, hook.Command)
					hashes = append(hashes, extensionHash([]byte(hook.Command)))
				}
			}
			item.Count = len(item.Commands)
			if item.Count > 0 {
				matchers = append(matchers, item)
			}
		}
		if len(hashes) == 0 {
			continue
		}
		if len(hashes) > 10000 {
			r.scan.fail("extension_limit")
			continue
		}
		key := scope + "\x00" + project + "\x00" + event
		if r.scan.hookHashes == nil {
			r.scan.hookHashes = map[string][]string{}
			r.scan.hookDetails = map[string][]extensionHookMatcher{}
			r.scan.hookLocal = map[string][]string{}
		}
		hashes = append(r.scan.hookHashes[key], hashes...)
		if len(hashes) > 10000 {
			r.scan.fail("extension_limit")
			continue
		}
		r.scan.hookHashes[key] = hashes
		if r.scan.capture != nil {
			if r.scan.hookContent == nil {
				r.scan.hookContent = map[string][]contentHookSource{}
			}
			r.scan.hookContent[key] = append(r.scan.hookContent[key], captured...)
		}
		r.scan.hookDetails[key] = append(r.scan.hookDetails[key], matchers...)
		var local *extensionLocalDetail
		if r.scan.localDisplay {
			r.scan.hookLocal[key] = append(r.scan.hookLocal[key], commands...)
			local = &extensionLocalDetail{Commands: r.scan.hookLocal[key], Files: files}
			for _, old := range r.scan.extensions {
				if old.Kind == "hook" && old.Event == event && old.Scope == scope && old.ProjectRefHash == project && old.Local != nil {
					local.Files = uniqueSorted(append(local.Files, old.Local.Files...))
				}
			}
		}
		sort.Strings(hashes)
		r.scan.add(inventoryExtension{Details: &inventoryExtensionDetails{Invocation: "Runs on " + event, HookMatchers: r.scan.hookDetails[key]}, Local: local, Kind: "hook", ID: event, SourceKind: "local", Scope: scope, ProjectRefHash: project, Event: event, Count: len(hashes), ContentHash: extensionHash([]byte(strings.Join(hashes, "\n"))), HashStatus: contentHashOK})
	}
}

func (r *extensionRoot) hookDirectory(base, scope, project string) {
	// Standalone hook directories have no standard event binding. Read only
	// JSON event maps; unbound scripts make coverage partial, never invent events.
	for _, entry := range r.dirs(base) {
		path := filepath.Join(base, entry.Name())
		if _, err := r.stat(path); err != nil {
			continue
		}
		if !entry.IsDir() && filepath.Ext(path) == ".json" {
			doc := r.document(path)
			if hooks, ok := doc["hooks"]; ok {
				r.hooks(hooks, scope, project, path)
			} else if doc != nil {
				data, _ := json.Marshal(doc)
				r.hooks(data, scope, project, path)
			}
		} else {
			r.scan.fail("extension_hook_unbound")
		}
	}
}

func (r *extensionRoot) extensionEnabled(doc map[string]json.RawMessage) map[string]bool {
	raw, ok := doc["enabledPlugins"]
	if !ok {
		return nil
	}
	var values map[string]*bool
	if json.Unmarshal(raw, &values) != nil || values == nil {
		r.scan.fail("extension_malformed")
		return nil
	}
	enabled := map[string]bool{}
	for id, value := range values {
		if value == nil {
			r.scan.fail("extension_malformed")
			continue
		}
		enabled[id] = *value
	}
	return enabled
}

func (r *extensionRoot) plugin(id, marketplace string, record map[string]json.RawMessage, enabled map[string]bool, codex bool) inventoryExtension {
	name := extensionString(record["name"])
	if name == "" {
		name = strings.Split(id, "@")[0]
	}
	if marketplace == "" {
		marketplace = extensionString(record["marketplace"])
	}
	if strings.Contains(id, "@") {
		parts := strings.SplitN(id, "@", 2)
		name, marketplace = parts[0], parts[1]
	}
	kind := "unknown"
	if marketplace != "" {
		kind = "third_party_marketplace"
	}
	if (!codex && marketplace == "claude-plugins-official") || (codex && marketplace == "openai-curated-remote") {
		kind = "official_marketplace"
	}
	source := extensionString(record["source"])
	var sourceObj struct {
		Source string `json:"source"`
		URL    string `json:"url"`
		Repo   string `json:"repo"`
		Path   string `json:"path"`
	}
	_ = json.Unmarshal(record["source"], &sourceObj)
	if marketplace == "" {
		if sourceObj.Source == "git" || sourceObj.Source == "github" || strings.HasPrefix(source, "https://") || strings.HasPrefix(source, "git@") {
			kind = "git"
		}
		if sourceObj.Source == "directory" || sourceObj.Source == "file" {
			kind = "local"
		}
	}
	var builtin bool
	_ = json.Unmarshal(record["builtin"], &builtin)
	if builtin {
		kind = "builtin"
	}
	if marketplace != "" {
		id = name + "@" + marketplace
	} else {
		id = name
	}
	ext := inventoryExtension{Kind: "plugin", ID: id, SourceKind: kind, Scope: "user", Version: extensionString(record["version"])}
	if raw := record["source"]; len(raw) > 0 {
		ext.SourceRefHash = extensionHash(raw)
	}
	path := extensionString(record["installPath"])
	if path == "" {
		path = extensionString(record["installLocation"])
	}
	if path != "" {
		if filepath.IsAbs(path) {
			path, _ = filepath.Rel(r.scan.home, path)
		}
		if !filepath.IsLocal(path) {
			r.scan.fail("extension_unsafe_path")
		} else if _, err := r.stat(path); err == nil {
			var manifest map[string]json.RawMessage
			if codex {
				manifest = r.document(filepath.Join(path, "plugin.json"))
				if manifest == nil {
					manifest = r.document(filepath.Join(path, ".codex-plugin", "plugin.json"))
				}
			}
			if manifest == nil {
				manifest = r.document(filepath.Join(path, ".claude-plugin", "plugin.json"))
			}
			if version := extensionString(manifest["version"]); version != "" {
				ext.Version = version
			}
			ext.ContentHash, ext.HashStatus = treeHash(r.root, path)
			r.captureTree("plugin", id, path, ext.ContentHash)
			r.pluginDetails(path, manifest, &ext)
		}
	}
	if ext.HashStatus == "" {
		ext.HashStatus = contentHashUnreadable
	}
	if value, ok := enabled[id]; ok {
		if value {
			ext.Enabled = "true"
		} else {
			ext.Enabled = "false"
		}
	}
	return ext
}

func collectInventoryExtensions(home, agent, salt string, profileOnly bool, projects ...string) ([]inventoryExtension, []string) {
	return collectExtensions(home, agent, salt, profileOnly, false, projects...)
}

// collectDisplayExtensions is the inventory collector with identifiers left in
// clear for a local listing. Report hashing is unchanged.
func collectDisplayExtensions(home, agent, salt string, profileOnly bool, projects ...string) ([]inventoryExtension, []string) {
	return collectExtensions(home, agent, salt, profileOnly, true, projects...)
}

func collectExtensions(home, agent, salt string, profileOnly, localDisplay bool, explicitProjects ...string) ([]inventoryExtension, []string) {
	return collectExtensionsWith(nil, home, agent, salt, profileOnly, localDisplay, explicitProjects...)
}

// collectExtensionsWith runs the scan and, when capture is set, also gathers
// redacted content for the hashes capture wants.
func collectExtensionsWith(capture *contentCapture, home, agent, salt string, profileOnly, localDisplay bool, explicitProjects ...string) ([]inventoryExtension, []string) {
	scan := &extensionScan{home: home, salt: salt, agent: agent, localDisplay: localDisplay, capture: capture, extensions: []inventoryExtension{}, errors: map[string]bool{}}
	if agent != "claude-code" && agent != "codex-cli" {
		return scan.extensions, nil
	}
	root := scan.open(home)
	if root == nil {
		return scan.extensions, []string{"extension_unreadable"}
	}
	defer root.root.Close()
	if agent == "claude-code" {
		settings := root.document(filepath.Join(".claude", "settings.json"))
		enabled := root.extensionEnabled(settings)
		plugins := root.claudePlugins(enabled, profileOnly)
		installedProjects := map[string]bool{}
		for _, plugin := range plugins {
			if plugin.Scope == "project" {
				installedProjects[plugin.ProjectRefHash+"\x00"+plugin.ID] = true
			}
		}
		root.localFiles(filepath.Join(".claude", "skills"), "user", "", true)
		root.localFiles(filepath.Join(".claude", "commands"), "user", "", false)
		root.hooks(settings["hooks"], "user", "", ".claude/settings.json")
		root.hookDirectory(filepath.Join(".claude", "hooks"), "user", "")
		if !profileOnly {
			doc := root.document(".claude.json")
			var projects map[string]json.RawMessage
			if raw := doc["projects"]; raw != nil && json.Unmarshal(raw, &projects) != nil {
				scan.fail("extension_malformed")
			}
			// Explicit roots opt into the same bounded walk without modifying
			// Claude's configuration or broadening other users' profile scans.
			if projects == nil {
				projects = map[string]json.RawMessage{}
			}
			for _, path := range explicitProjects {
				projects[path] = nil
			}
			// The user root has already been scanned. Recognize filesystem
			// aliases too, without following them for a second content walk.
			homeInfo, _ := root.root.Stat(".")
			isHome := func(path string) bool {
				info, err := os.Stat(path)
				return err == nil && homeInfo != nil && os.SameFile(homeInfo, info)
			}
			projectHashes := map[string]bool{scan.projectHash(home): true}
			for path := range projects {
				projectHashes[scan.projectHash(path)] = true
			}
			for _, plugin := range plugins {
				if plugin.Scope == "project" && !projectHashes[plugin.ProjectRefHash] {
					scan.fail("extension_project_unscanned")
				}
			}
			// Detect a local project omitted from the registry without walking
			// its contents. The caller can explicitly include it with --project.
			if cwd, err := os.Getwd(); err != nil {
				scan.fail("extension_unreadable")
			} else if !isHome(cwd) && !projectHashes[scan.projectHash(cwd)] {
				if _, err := os.Lstat(filepath.Join(cwd, ".claude")); err == nil {
					scan.fail("extension_project_unscanned")
				} else if !os.IsNotExist(err) {
					scan.fail("extension_unreadable")
				}
			}
			paths := make([]string, 0, len(projects))
			for path := range projects {
				if filepath.IsAbs(path) && isHome(path) {
					continue
				}
				paths = append(paths, path)
			}
			sort.Strings(paths)
			if len(paths) > 100 {
				paths = paths[:100]
				scan.fail("extension_limit")
			}
			for _, path := range paths {
				if !filepath.IsAbs(path) {
					scan.fail("extension_unsafe_path")
					continue
				}
				project := scan.open(path)
				if project == nil {
					scan.fail("extension_unreadable")
					continue
				}
				hash := scan.projectHash(path)
				settings := project.document(filepath.Join(".claude", "settings.json"))
				overrides := project.extensionEnabled(settings)
				localSettings := project.document(filepath.Join(".claude", "settings.local.json"))
				for id, value := range project.extensionEnabled(localSettings) {
					if overrides == nil {
						overrides = map[string]bool{}
					}
					overrides[id] = value
				}
				for _, plugin := range plugins {
					if value, ok := overrides[plugin.ID]; ok && plugin.Scope == "user" && !installedProjects[hash+"\x00"+plugin.ID] {
						plugin.Scope = "project"
						plugin.ProjectRefHash = hash
						plugin.Enabled = "false"
						if value {
							plugin.Enabled = "true"
						}
						scan.add(plugin)
					}
				}
				project.localFiles(filepath.Join(".claude", "skills"), "project", hash, true)
				project.localFiles(filepath.Join(".claude", "commands"), "project", hash, false)
				project.hooks(settings["hooks"], "project", hash, ".claude/settings.json")
				project.hooks(localSettings["hooks"], "project", hash, ".claude/settings.local.json")
				project.hookDirectory(filepath.Join(".claude", "hooks"), "project", hash)
				project.root.Close()
			}
		}
	} else {
		root.codexPlugins()
		root.localFiles(filepath.Join(".codex", "skills"), "user", "", true)
		root.localFiles(filepath.Join(".agents", "skills"), "user", "", true)
		if !profileOnly {
			for _, path := range explicitProjects {
				project := scan.open(path)
				if project == nil {
					scan.fail("extension_unreadable")
					continue
				}
				hash := scan.projectHash(path)
				project.localFiles(filepath.Join(".codex", "skills"), "project", hash, true)
				project.localFiles(filepath.Join(".agents", "skills"), "project", hash, true)
				project.root.Close()
			}
		}
		doc := root.document(filepath.Join(".codex", "hooks.json"))
		if hooks, ok := doc["hooks"]; ok {
			root.hooks(hooks, "user", "", ".codex/hooks.json")
		} else if doc != nil {
			data, _ := json.Marshal(doc)
			root.hooks(data, "user", "", ".codex/hooks.json")
		}
		if data := root.read(filepath.Join(".codex", "config.toml")); data != nil {
			var doc map[string]any
			if toml.Unmarshal(data, &doc) != nil {
				scan.fail("extension_malformed")
			} else if hooks, ok := doc["hooks"]; ok {
				raw, _ := json.Marshal(hooks)
				root.hooks(raw, "user", "", ".codex/config.toml")
			}
		}
	}
	scan.captureHooks()
	for _, plugin := range append([]inventoryExtension{}, scan.extensions...) {
		for _, bundled := range plugin.Bundled {
			bundled.Scope = plugin.Scope
			bundled.ProjectRefHash = plugin.ProjectRefHash
			bundled.Enabled = plugin.Enabled
			bundled.SourceKind = plugin.SourceKind
			scan.add(bundled)
		}
	}
	sort.Slice(scan.extensions, func(i, j int) bool {
		a, b := scan.extensions[i], scan.extensions[j]
		return a.Kind+"\x00"+a.ID+"\x00"+a.Scope+a.ProjectRefHash < b.Kind+"\x00"+b.ID+"\x00"+b.Scope+b.ProjectRefHash
	})
	errors := make([]string, 0, len(scan.errors))
	for code := range scan.errors {
		errors = append(errors, code)
	}
	sort.Strings(errors)
	return scan.extensions, errors
}
