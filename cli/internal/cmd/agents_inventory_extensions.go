package cmd

// Extension discovery has a deliberately narrow output type. Parsed files,
// command strings, source locations and project roots never enter the DTO.
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
	Event          string `json:"event,omitempty"`
	Count          int    `json:"count,omitempty"`
	IDHashed       bool   `json:"id_hashed"`
}

type extensionScan struct {
	home, salt   string
	localDisplay bool
	extensions   []inventoryExtension
	errors       map[string]bool
	hookHashes   map[string][]string
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
	if !s.localDisplay && (ext.SourceKind == "local" || ext.SourceKind == "git" || ext.Scope == "project") && !(ext.Kind == "plugin" && strings.HasSuffix(ext.SourceKind, "marketplace")) {
		for _, segment := range strings.FieldsFunc(filepath.ToSlash(s.home), func(r rune) bool { return r == '/' || r == '\\' || r == ':' }) {
			if segment != "" && strings.Contains(strings.ToLower(rawID), strings.ToLower(segment)) {
				ext.ID = "hash:" + strings.TrimPrefix(extensionHash([]byte(rawID)), "sha256:")[:16]
				ext.IDHashed = true
				break
			}
		}
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
		kind, id, hash := "command", strings.TrimSuffix(entry.Name(), filepath.Ext(entry.Name())), ""
		if skills {
			if !info.IsDir() {
				continue
			}
			kind, id = "skill", entry.Name()
			data := r.read(filepath.Join(path, "SKILL.md"))
			if data != nil {
				hash = extensionHash(data)
			}
		} else {
			if !info.Mode().IsRegular() || !strings.EqualFold(filepath.Ext(path), ".md") {
				continue
			}
			if data := r.read(path); data != nil {
				hash = extensionHash(data)
			}
		}
		r.scan.add(inventoryExtension{Kind: kind, ID: id, SourceKind: "local", Scope: scope, ProjectRefHash: project, ContentHash: hash})
	}
}

var extensionEventPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_-]{0,31}$`)

func (r *extensionRoot) hooks(raw json.RawMessage, scope, project string) {
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
		for _, group := range groups {
			var obj struct {
				Command string `json:"command"`
				Hooks   []struct {
					Command string `json:"command"`
				} `json:"hooks"`
			}
			if json.Unmarshal(group, &obj) != nil {
				r.scan.fail("extension_malformed")
				continue
			}
			if obj.Command != "" {
				hashes = append(hashes, extensionHash([]byte(obj.Command)))
			}
			for _, hook := range obj.Hooks {
				if hook.Command != "" {
					hashes = append(hashes, extensionHash([]byte(hook.Command)))
				}
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
		}
		hashes = append(r.scan.hookHashes[key], hashes...)
		if len(hashes) > 10000 {
			r.scan.fail("extension_limit")
			continue
		}
		r.scan.hookHashes[key] = hashes
		sort.Strings(hashes)
		r.scan.add(inventoryExtension{Kind: "hook", ID: event, SourceKind: "local", Scope: scope, ProjectRefHash: project, Event: event, Count: len(hashes), ContentHash: extensionHash([]byte(strings.Join(hashes, "\n")))})
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
				r.hooks(hooks, scope, project)
			} else if doc != nil {
				data, _ := json.Marshal(doc)
				r.hooks(data, scope, project)
			}
		} else {
			r.scan.fail("extension_hook_unbound")
		}
	}
}

func extensionEnabled(doc map[string]json.RawMessage) map[string]bool {
	var enabled map[string]bool
	_ = json.Unmarshal(doc["enabledPlugins"], &enabled)
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
		} else {
			manifest := r.document(filepath.Join(path, ".claude-plugin", "plugin.json"))
			if codex && manifest == nil {
				manifest = r.document(filepath.Join(path, "plugin.json"))
			}
			if version := extensionString(manifest["version"]); version != "" {
				ext.Version = version
			}
			// v1 hashes only manifest bytes; full plugin tree hashes are a follow-up.
			if manifest != nil {
				data, _ := json.Marshal(manifest)
				ext.ContentHash = extensionHash(data)
			}
		}
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

func (r *extensionRoot) claudePlugins(enabled map[string]bool) []inventoryExtension {
	before := len(r.scan.extensions)
	marketplaces := r.document(filepath.Join(".claude", "plugins", "known_marketplaces.json"))
	for _, entry := range r.dirs(filepath.Join(".claude", "plugins", "store")) {
		path := filepath.Join(".claude", "plugins", "store", entry.Name())
		if _, err := r.stat(path); err != nil {
			continue
		}
		if entry.IsDir() || filepath.Ext(path) != ".json" {
			continue
		}
		doc := r.document(path)
		if doc == nil {
			continue
		}
		var records map[string][]map[string]json.RawMessage
		if raw, ok := doc["plugins"]; ok {
			if json.Unmarshal(raw, &records) != nil {
				r.scan.fail("extension_malformed")
				continue
			}
		} else {
			records = map[string][]map[string]json.RawMessage{strings.TrimSuffix(entry.Name(), ".json"): {doc}}
		}
		ids := make([]string, 0, len(records))
		for id := range records {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		for _, id := range ids {
			for _, record := range records[id] {
				market := extensionString(record["marketplace"])
				if parts := strings.SplitN(id, "@", 2); len(parts) == 2 {
					market = parts[1]
				}
				var known map[string]json.RawMessage
				_ = json.Unmarshal(marketplaces[market], &known)
				if record["source"] == nil {
					record["source"] = known["source"]
				}
				if record["installPath"] == nil {
					install := extensionString(known["installLocation"])
					if install == "" {
						install = filepath.Join(".claude", "plugins", "marketplaces", market)
					}
					name := extensionString(record["name"])
					if name == "" {
						name = strings.Split(id, "@")[0]
					}
					record["installPath"], _ = json.Marshal(filepath.Join(install, "plugins", name))
				}
				r.scan.add(r.plugin(id, market, record, enabled, false))
			}
		}
	}
	return append([]inventoryExtension{}, r.scan.extensions[before:]...)
}

func (r *extensionRoot) codexPlugins() {
	base := filepath.Join(".codex", "plugins")
	for _, market := range r.dirs(base) {
		marketPath := filepath.Join(base, market.Name())
		if info, err := r.stat(marketPath); err != nil || !info.IsDir() {
			continue
		}
		for _, plugin := range r.dirs(marketPath) {
			path := filepath.Join(marketPath, plugin.Name())
			if info, err := r.stat(path); err != nil || !info.IsDir() {
				continue
			}
			record := r.document(filepath.Join(path, "plugin.json"))
			if record == nil {
				record = map[string]json.RawMessage{}
			}
			record["installPath"], _ = json.Marshal(path)
			r.scan.add(r.plugin(plugin.Name(), market.Name(), record, nil, true))
		}
	}
}

func collectInventoryExtensions(home, agent, salt string, profileOnly bool) ([]inventoryExtension, []string) {
	return collectExtensions(home, agent, salt, profileOnly, false)
}

// collectDisplayExtensions is the inventory collector with identifiers left in
// clear for a local listing. Report hashing is unchanged.
func collectDisplayExtensions(home, agent, salt string, profileOnly bool) ([]inventoryExtension, []string) {
	return collectExtensions(home, agent, salt, profileOnly, true)
}

func collectExtensions(home, agent, salt string, profileOnly, localDisplay bool) ([]inventoryExtension, []string) {
	scan := &extensionScan{home: home, salt: salt, localDisplay: localDisplay, extensions: []inventoryExtension{}, errors: map[string]bool{}}
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
		enabled := extensionEnabled(settings)
		plugins := root.claudePlugins(enabled)
		root.localFiles(filepath.Join(".claude", "skills"), "user", "", true)
		root.localFiles(filepath.Join(".claude", "commands"), "user", "", false)
		root.hooks(settings["hooks"], "user", "")
		root.hookDirectory(filepath.Join(".claude", "hooks"), "user", "")
		if !profileOnly {
			doc := root.document(".claude.json")
			var projects map[string]json.RawMessage
			if raw := doc["projects"]; raw != nil && json.Unmarshal(raw, &projects) != nil {
				scan.fail("extension_malformed")
			}
			paths := make([]string, 0, len(projects))
			for path := range projects {
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
					continue
				}
				hash := scan.projectHash(path)
				settings := project.document(filepath.Join(".claude", "settings.json"))
				overrides := extensionEnabled(settings)
				for _, plugin := range plugins {
					if value, ok := overrides[plugin.ID]; ok {
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
				project.hooks(settings["hooks"], "project", hash)
				project.hookDirectory(filepath.Join(".claude", "hooks"), "project", hash)
				project.root.Close()
			}
		}
	} else {
		root.codexPlugins()
		root.localFiles(filepath.Join(".codex", "skills"), "user", "", true)
		doc := root.document(filepath.Join(".codex", "hooks.json"))
		if hooks, ok := doc["hooks"]; ok {
			root.hooks(hooks, "user", "")
		} else if doc != nil {
			data, _ := json.Marshal(doc)
			root.hooks(data, "user", "")
		}
		if data := root.read(filepath.Join(".codex", "config.toml")); data != nil {
			var doc map[string]any
			if toml.Unmarshal(data, &doc) != nil {
				scan.fail("extension_malformed")
			} else if hooks, ok := doc["hooks"]; ok {
				raw, _ := json.Marshal(hooks)
				root.hooks(raw, "user", "")
			}
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
