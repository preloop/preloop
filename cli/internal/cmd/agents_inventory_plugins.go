package cmd

import (
	"encoding/json"
	"path/filepath"
	"sort"
	"strings"

	toml "github.com/pelletier/go-toml/v2"
	"golang.org/x/mod/semver"
)

// claudePlugins reads the versioned install registry, not marketplace catalogs
// or orphaned cache directories. Only version 2 has been verified on disk.
func (r *extensionRoot) claudePlugins(enabled map[string]bool, profileOnly bool) []inventoryExtension {
	before := len(r.scan.extensions)
	marketplaces := r.document(filepath.Join(".claude", "plugins", "known_marketplaces.json"))
	doc := r.document(filepath.Join(".claude", "plugins", "installed_plugins.json"))
	if doc == nil {
		return nil
	}
	var version int
	if json.Unmarshal(doc["version"], &version) != nil || version == 0 {
		r.scan.fail("extension_malformed")
		return nil
	}
	if version != 2 {
		r.scan.fail("extension_unsupported_version")
		return nil
	}
	var records map[string][]map[string]json.RawMessage
	if json.Unmarshal(doc["plugins"], &records) != nil || records == nil {
		r.scan.fail("extension_malformed")
		return nil
	}
	ids := make([]string, 0, len(records))
	for id := range records {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	visited := 0
	for _, id := range ids {
		if len(records[id]) == 0 {
			r.scan.fail("extension_malformed")
		}
		for _, record := range records[id] {
			visited++
			if visited > inventoryExtensionLimit {
				r.scan.fail("extension_limit")
				return append([]inventoryExtension{}, r.scan.extensions[before:]...)
			}
			if record == nil || extensionString(record["installPath"]) == "" {
				r.scan.fail("extension_malformed")
				continue
			}
			scope := extensionString(record["scope"])
			projectPath := extensionString(record["projectPath"])
			switch scope {
			case "user":
			case "project", "local":
				if !filepath.IsAbs(projectPath) {
					r.scan.fail("extension_malformed")
					continue
				}
				if profileOnly {
					continue
				}
			default:
				r.scan.fail("extension_unsupported_scope")
				continue
			}
			market := ""
			if parts := strings.SplitN(id, "@", 2); len(parts) == 2 {
				market = parts[1]
			}
			if raw, ok := marketplaces[market]; ok {
				var known map[string]json.RawMessage
				if json.Unmarshal(raw, &known) != nil || known == nil {
					r.scan.fail("extension_malformed")
				} else {
					record["source"] = known["source"]
				}
			}
			ext := r.plugin(id, market, record, enabled, false)
			if scope != "user" {
				ext.Scope = "project"
				ext.ProjectRefHash = r.scan.projectHash(projectPath)
				r.scan.projectPaths[ext.ProjectRefHash] = projectPath
				project := r.scan.open(projectPath)
				if project != nil {
					for _, file := range []string{"settings.json", "settings.local.json"} {
						settings := project.document(filepath.Join(".claude", file))
						if value, ok := project.extensionEnabled(settings)[id]; ok {
							ext.Enabled = "false"
							if value {
								ext.Enabled = "true"
							}
						}
					}
					project.root.Close()
				} else {
					r.scan.fail("extension_unreadable")
					ext.Enabled = "unknown"
				}
			}
			r.scan.add(ext)
		}
	}
	return append([]inventoryExtension{}, r.scan.extensions[before:]...)
}

// codexPlugins follows Codex's versioned cache. Configuration controls enabled
// state; the presence of a marketplace alone is not an installed plugin.
func (r *extensionRoot) codexPlugins() {
	var config struct {
		Plugins map[string]struct {
			Enabled *bool `toml:"enabled"`
		} `toml:"plugins"`
		Marketplaces map[string]struct {
			Source string `toml:"source"`
		} `toml:"marketplaces"`
	}
	if data := r.read(filepath.Join(".codex", "config.toml")); data != nil {
		if toml.Unmarshal(data, &config) != nil {
			r.scan.fail("extension_malformed")
		}
	}
	enabled := map[string]bool{}
	for id, plugin := range config.Plugins {
		if plugin.Enabled != nil {
			enabled[id] = *plugin.Enabled
		}
	}
	base := filepath.Join(".codex", "plugins", "cache")
	visited := 0
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
			visited++
			if visited > inventoryExtensionLimit {
				r.scan.fail("extension_limit")
				return
			}
			version := r.codexPluginVersion(path)
			if version == "" {
				continue
			}
			record := map[string]json.RawMessage{}
			record["installPath"], _ = json.Marshal(filepath.Join(path, version))
			record["version"], _ = json.Marshal(version)
			if source := config.Marketplaces[market.Name()].Source; source != "" {
				record["source"], _ = json.Marshal(source)
			}
			r.scan.add(r.plugin(plugin.Name(), market.Name(), record, enabled, true))
		}
	}
}

// Codex selects "local" first, then the highest version. Pairs of full SemVer
// versions compare semantically; other pairs compare lexically, as upstream.
func (r *extensionRoot) codexPluginVersion(path string) string {
	versions := []string{}
	for _, entry := range r.dirs(path) {
		if info, err := r.stat(filepath.Join(path, entry.Name())); err != nil || !info.IsDir() {
			continue
		}
		if !discoveryVersionPattern.MatchString(entry.Name()) {
			r.scan.fail("extension_malformed")
			continue
		}
		versions = append(versions, entry.Name())
	}
	for _, version := range versions {
		if version == "local" {
			return version
		}
	}
	sort.SliceStable(versions, func(i, j int) bool {
		left, right := "v"+versions[i], "v"+versions[j]
		if semver.IsValid(left) && semver.IsValid(right) && fullPluginSemver(left) && fullPluginSemver(right) {
			return semver.Compare(left, right) < 0
		}
		return versions[i] < versions[j]
	})
	if len(versions) == 0 {
		return ""
	}
	return versions[len(versions)-1]
}

// x/mod also accepts abbreviated versions; Codex's semver parser requires all
// three numeric components before the prerelease or build suffix.
func fullPluginSemver(value string) bool {
	core, _, _ := strings.Cut(value, "-")
	core, _, _ = strings.Cut(core, "+")
	return strings.Count(core, ".") == 2
}
