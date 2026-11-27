package cmd

// Extension content for review.
//
// A discovery report carries identifiers and hashes. An approver also needs
// to read what a hash stands for, so after a report the collector asks the
// server which reported hashes it has no content for and uploads exactly
// those: text only, redacted on this device, under fixed size caps.
//
// On by default, with three opt-outs: the account setting (the server says
// so and nothing is read), this device (--no-extension-content, the
// PRELOOP_NO_EXTENSION_CONTENT variable or discovery.extension_content in
// the CLI config) and a single extension (a .preloop-no-content file in its
// directory, or a pattern in discovery.extension_content_exclude).
//
// The caps were set against a real developer home (63 skills, 7 plugins):
// the largest SKILL.md there is 85 KiB and the 90th percentile 52 KiB; the
// largest plugin holds 612 KiB of text in 61 skill, command and hook files;
// all SKILL.md files together are 1.5 MiB.

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/redact"
	"github.com/preloop/preloop/cli/internal/version"
)

const (
	// contentFileLimit caps one file or hook command: three times the
	// largest SKILL.md measured.
	contentFileLimit = 256 << 10
	// contentExtensionLimit caps the text of one extension, after redaction.
	contentExtensionLimit = 1 << 20
	// contentExtensionFiles caps how many files of one extension carry text.
	contentExtensionFiles = 200
	// contentHookLimit caps the commands of one hook event.
	contentHookLimit = 200
	// contentReportLimit caps the text one report uploads. What does not fit
	// is sent by a later report, smallest extensions first.
	contentReportLimit = 8 << 20
	// contentBatchLimit and contentBatchItems bound one request.
	contentBatchLimit = 2 << 20
	contentBatchItems = 50
	// contentQueryLimit bounds one "which do you lack" request.
	contentQueryLimit = 1000

	contentMarkerFile  = ".preloop-no-content"
	contentDisableEnv  = "PRELOOP_NO_EXTENSION_CONTENT"
	contentMissingPath = api.RESTPrefix + "/agents/extension-content/missing"
	contentUploadPath  = api.RESTPrefix + "/agents/extension-content"

	contentStored      = "stored"
	contentBinary      = "binary"
	contentTooLarge    = "too_large"
	contentSymlink     = "symlink"
	contentPrivateKey  = "private_key"
	contentOverBudget  = "over_budget"
	contentNotSelected = "not_selected"
	contentUnreadable  = "unreadable"
	contentExcluded    = "excluded"
)

// contentPathLimit is the longest relative path, in bytes, the server lists.
// A variable so a test can reach it on systems whose own limit is lower.
var contentPathLimit = 1024

// discoveryNoExtensionContent is the --no-extension-content flag.
var discoveryNoExtensionContent bool

type contentFile struct {
	Path       string         `json:"path"`
	SHA256     string         `json:"sha256,omitempty"`
	Size       *int64         `json:"size,omitempty"`
	Status     string         `json:"status"`
	Text       *string        `json:"text,omitempty"`
	Redactions map[string]int `json:"redactions,omitempty"`
}

type contentHook struct {
	Matcher     string         `json:"matcher,omitempty"`
	CommandHash string         `json:"command_hash"`
	Status      string         `json:"status"`
	Command     *string        `json:"command,omitempty"`
	Redactions  map[string]int `json:"redactions,omitempty"`
	// MatcherRedactions counts what was replaced in the matcher. It is kept
	// apart from Redactions so that a command sent unredacted can still be
	// verified against its hash.
	MatcherRedactions map[string]int `json:"matcher_redactions,omitempty"`
}

type contentItem struct {
	Kind        string        `json:"extension_kind"`
	ContentHash string        `json:"content_hash"`
	Files       []contentFile `json:"files,omitempty"`
	Hooks       []contentHook `json:"hooks,omitempty"`
	Withheld    string        `json:"withheld,omitempty"`

	bytes      int
	redactions int
	withheld   int
}

type contentPair struct {
	Kind        string `json:"extension_kind"`
	ContentHash string `json:"content_hash"`
}

type contentHookSource struct {
	Matcher string
	Command string
}

// contentCapture gathers content for the wanted hashes during one scan.
type contentCapture struct {
	wanted  map[contentPair]bool
	exclude []string
	home    string
	items   map[contentPair]*contentItem
	// err is a redaction failure. It aborts the whole upload: text is never
	// sent without having passed the redactor.
	err error
}

func newContentCapture(home string, wanted []contentPair, exclude []string) *contentCapture {
	capture := &contentCapture{wanted: map[contentPair]bool{}, exclude: exclude, home: home, items: map[contentPair]*contentItem{}}
	for _, pair := range wanted {
		capture.wanted[pair] = true
	}
	return capture
}

// excluded matches "kind:pattern" or a bare pattern against the extension's
// local identifier, with path.Match globbing.
func (c *contentCapture) excluded(kind, id string) bool {
	for _, pattern := range c.exclude {
		pattern = strings.TrimSpace(pattern)
		if prefix, rest, ok := strings.Cut(pattern, ":"); ok && (prefix == "skill" || prefix == "command" || prefix == "hook" || prefix == "plugin") {
			if prefix != kind {
				continue
			}
			pattern = rest
		}
		if matched, err := path.Match(pattern, id); err == nil && matched {
			return true
		}
	}
	return false
}

// claim reports whether content for this pair should be gathered now. An
// extension that is excluded on this device yields a marker with no text.
func (c *contentCapture) claim(kind, id, hash string, marker bool) (contentPair, bool) {
	pair := contentPair{Kind: kind, ContentHash: hash}
	if c == nil || c.err != nil || hash == "" || !c.wanted[pair] {
		return pair, false
	}
	if existing := c.items[pair]; existing != nil && existing.Withheld == "" {
		return pair, false
	}
	if marker || c.excluded(kind, id) {
		c.items[pair] = &contentItem{Kind: kind, ContentHash: hash, Withheld: contentExcluded}
		return pair, false
	}
	return pair, true
}

// redactInto runs the redactor over one text and fills status, text and
// counts. It returns the bytes that count against the extension budget.
func (c *contentCapture) redactInto(data []byte, name string, budget int, status *string, text **string, counts *map[string]int) int {
	if !utf8.Valid(data) || bytes.IndexByte(data, 0) >= 0 {
		*status = contentBinary
		return 0
	}
	result, err := redact.Text(string(data), redact.Options{Path: name, Home: c.home})
	if err != nil {
		c.err = err
		*status = contentUnreadable
		return 0
	}
	if result.Withheld != "" {
		*status = contentPrivateKey
		return 0
	}
	if len(result.Text) > contentFileLimit {
		*status = contentTooLarge
		return 0
	}
	if len(result.Text) > budget {
		*status = contentOverBudget
		return 0
	}
	*status = contentStored
	*text = &result.Text
	if result.Total > 0 {
		*counts = result.Counts
	}
	return len(result.Text)
}

// redactMatcher passes a hook matcher through the same redactor as every
// other text. A matcher is authored text like a command is, and is shown to
// an approver, so a credential placed in one must not be uploaded either.
func (c *contentCapture) redactMatcher(matcher string) (string, map[string]int) {
	if matcher == "" {
		return "", nil
	}
	result, err := redact.Text(matcher, redact.Options{Home: c.home})
	if err != nil {
		c.err = err
		return "", nil
	}
	text := result.Text
	counts := result.Counts
	if result.Withheld != "" {
		text, counts = redact.Placeholder("private-key"), map[string]int{"private-key": 1}
	}
	if runes := []rune(text); len(runes) > 256 {
		text = string(runes[:256])
	}
	if len(counts) == 0 {
		counts = nil
	}
	return text, counts
}

// pluginTextSelected limits plugin text to what an approver reviews: the
// manifest and the skills, commands and hook definitions. Every other file is
// listed by name only.
func pluginTextSelected(name string) bool {
	switch name {
	case "plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json":
		return true
	}
	for _, prefix := range []string{"skills/", "commands/", "hooks/"} {
		if strings.HasPrefix(name, prefix) {
			return true
		}
	}
	return false
}

func contentPriority(name string) int {
	switch {
	case name == "SKILL.md" || strings.HasSuffix(name, "plugin.json"):
		return 0
	case strings.HasSuffix(name, "/SKILL.md") || strings.HasPrefix(name, "hooks/"):
		return 1
	case strings.HasSuffix(strings.ToLower(name), ".md"):
		return 2
	}
	return 3
}

// readHashed reads one file and accepts it only if it is still the file the
// tree hash covered.
func readHashed(root *os.Root, full string, record extensionTreeRecord) []byte {
	info, err := root.Lstat(full)
	if err != nil || !info.Mode().IsRegular() || info.Size() != record.Size {
		return nil
	}
	file, err := openInventoryConfig(root, filepath.Clean(full))
	if err != nil {
		return nil
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, record.Size+1))
	if err != nil || int64(len(data)) != record.Size {
		return nil
	}
	sum := sha256.Sum256(data)
	if hex.EncodeToString(sum[:]) != record.Digest {
		return nil
	}
	return data
}

// captureBundledSkill gathers a skill that a plugin bundles. Excluding the
// plugin, by marker or by pattern, excludes the skills inside it too.
func (r *extensionRoot) captureBundledSkill(pluginID, pluginBase, id, base, hash string) {
	capture := r.scan.capture
	if capture == nil {
		return
	}
	_, markerErr := r.root.Lstat(filepath.Join(pluginBase, contentMarkerFile))
	r.captureTreeWith(markerErr == nil || capture.excluded("plugin", pluginID), "skill", id, base, hash)
}

// captureTree gathers a skill or plugin directory: every hashed entry is
// listed, and text is attached to the entries an approver reads.
func (r *extensionRoot) captureTree(kind, id, base, hash string) {
	r.captureTreeWith(false, kind, id, base, hash)
}

func (r *extensionRoot) captureTreeWith(excluded bool, kind, id, base, hash string) {
	capture := r.scan.capture
	if capture == nil {
		return
	}
	_, markerErr := r.root.Lstat(filepath.Join(base, contentMarkerFile))
	pair, ok := capture.claim(kind, id, hash, excluded || markerErr == nil)
	if !ok {
		return
	}
	again, status, records := treeHashRecords(r.root, base, true)
	if status != contentHashOK || again != hash {
		// The directory changed since it was hashed; the next report retries.
		return
	}
	for _, record := range records {
		// The server lists a path of at most 1024 bytes of valid UTF-8. A
		// tree that holds anything else cannot be described entry by entry,
		// so it is reported as withheld instead of failing the whole upload.
		if len(record.Path) > contentPathLimit || !utf8.ValidString(record.Path) {
			capture.items[pair] = &contentItem{Kind: kind, ContentHash: hash, Withheld: contentTooLarge}
			return
		}
	}
	item := &contentItem{Kind: kind, ContentHash: hash, Files: make([]contentFile, len(records))}
	order := make([]int, len(records))
	for i := range records {
		order[i] = i
	}
	sort.SliceStable(order, func(a, b int) bool {
		return contentPriority(records[order[a]].Path) < contentPriority(records[order[b]].Path)
	})
	stored := 0
	for _, index := range order {
		record := records[index]
		entry := contentFile{Path: record.Path}
		switch {
		case record.Symlink:
			entry.Status = contentSymlink
		default:
			size := record.Size
			entry.SHA256, entry.Size = record.Digest, &size
			switch {
			case kind == "plugin" && !pluginTextSelected(record.Path):
				entry.Status = contentNotSelected
			case record.Size > contentFileLimit:
				entry.Status = contentTooLarge
			case stored >= contentExtensionFiles:
				entry.Status = contentOverBudget
			default:
				data := readHashed(r.root, filepath.Join(base, filepath.FromSlash(record.Path)), record)
				if data == nil {
					entry.Status = contentUnreadable
					break
				}
				used := capture.redactInto(data, record.Path, contentExtensionLimit-item.bytes, &entry.Status, &entry.Text, &entry.Redactions)
				item.bytes += used
			}
		}
		if entry.Status == contentStored {
			stored++
			for _, count := range entry.Redactions {
				item.redactions += count
			}
		} else if entry.Status != contentNotSelected {
			item.withheld++
		}
		item.Files[index] = entry
	}
	if capture.err == nil {
		capture.items[pair] = item
	}
}

// captureCommand gathers one command file.
func (r *extensionRoot) captureCommand(id, name, hash string) {
	capture := r.scan.capture
	pair, ok := capture.claim("command", id, hash, false)
	if !ok {
		return
	}
	info, err := r.root.Lstat(name)
	if err != nil || !info.Mode().IsRegular() {
		return
	}
	size := info.Size()
	record := extensionTreeRecord{Path: filepath.Base(name), Digest: strings.TrimPrefix(hash, "sha256:"), Size: size}
	entry := contentFile{Path: record.Path, SHA256: record.Digest, Size: &size}
	item := &contentItem{Kind: "command", ContentHash: hash}
	if size > contentFileLimit {
		entry.Status = contentTooLarge
	} else if data := readHashed(r.root, name, record); data == nil {
		// Changed since it was hashed; the next report retries.
		return
	} else {
		item.bytes = capture.redactInto(data, record.Path, contentExtensionLimit, &entry.Status, &entry.Text, &entry.Redactions)
	}
	for _, count := range entry.Redactions {
		item.redactions += count
	}
	if entry.Status != contentStored {
		item.withheld = 1
	}
	item.Files = []contentFile{entry}
	if capture.err == nil {
		capture.items[pair] = item
	}
}

// captureHooks gathers the commands of every hook event with the matcher each
// runs under. The hook hash covers the commands, not the matchers.
func (s *extensionScan) captureHooks() {
	capture := s.capture
	if capture == nil {
		return
	}
	keys := make([]string, 0, len(s.hookContent))
	for key := range s.hookContent {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		sources := s.hookContent[key]
		hashes := make([]string, len(sources))
		for i, source := range sources {
			hashes[i] = extensionHash([]byte(source.Command))
		}
		sorted := append([]string{}, hashes...)
		sort.Strings(sorted)
		hash := extensionHash([]byte(strings.Join(sorted, "\n")))
		event := key[strings.LastIndexByte(key, 0)+1:]
		pair, ok := capture.claim("hook", event, hash, false)
		if !ok {
			continue
		}
		item := &contentItem{Kind: "hook", ContentHash: hash}
		if len(sources) > contentHookLimit {
			capture.items[pair] = &contentItem{Kind: "hook", ContentHash: hash, Withheld: contentTooLarge}
			continue
		}
		for i, source := range sources {
			entry := contentHook{CommandHash: hashes[i]}
			if redact.HasPrivateKey(source.Matcher) {
				entry.Status = contentPrivateKey
			} else {
				entry.Matcher, entry.MatcherRedactions = capture.redactMatcher(source.Matcher)
			}
			item.bytes += len(entry.Matcher)
			item.Hooks = append(item.Hooks, entry)
		}
		// Reserve every matcher first, including those beside withheld commands.
		// The same UTF-8 total then governs extension, report and batch limits.
		for i, source := range sources {
			entry := &item.Hooks[i]
			if entry.Status == contentPrivateKey {
				item.withheld++
				continue
			}
			if len(source.Command) > contentFileLimit {
				entry.Status = contentTooLarge
			} else {
				item.bytes += capture.redactInto([]byte(source.Command), "", contentExtensionLimit-item.bytes, &entry.Status, &entry.Command, &entry.Redactions)
				if entry.Status == contentBinary || entry.Status == contentUnreadable {
					entry.Status = contentTooLarge
				}
			}
			for _, count := range entry.Redactions {
				item.redactions += count
			}
			for _, count := range entry.MatcherRedactions {
				item.redactions += count
			}
			if entry.Status != contentStored {
				item.withheld++
			}
		}
		if capture.err == nil {
			capture.items[pair] = item
		}
	}
}

type contentQueryRequest struct {
	// WorkstationFingerprint lets the server hold a device-bound credential
	// to its own device for this question too.
	WorkstationFingerprint string        `json:"workstation_fingerprint,omitempty"`
	Items                  []contentPair `json:"items"`
}

type contentQueryResponse struct {
	Collect       bool          `json:"collect"`
	Reason        string        `json:"reason"`
	Missing       []contentPair `json:"missing"`
	RetentionDays int           `json:"retention_days"`
}

type contentUploadRequest struct {
	WorkstationFingerprint string         `json:"workstation_fingerprint"`
	CollectorVersion       string         `json:"collector_version,omitempty"`
	RedactionRules         string         `json:"redaction_rules"`
	Items                  []*contentItem `json:"items"`
}

type contentUploadResponse struct {
	Stored   int `json:"stored"`
	Kept     int `json:"kept"`
	Rejected []struct {
		Reason string `json:"reason"`
	} `json:"rejected"`
}

// extensionContentDisabled reports the device-level opt-out and its source.
func extensionContentDisabled() (bool, string) {
	if discoveryNoExtensionContent {
		return true, "--no-extension-content"
	}
	switch strings.ToLower(strings.TrimSpace(os.Getenv(contentDisableEnv))) {
	case "1", "true", "yes", "on":
		return true, contentDisableEnv
	}
	if !config.ExtensionContentEnabled() {
		return true, "discovery.extension_content in the CLI config"
	}
	return false, ""
}

func extensionContentNotice(w io.Writer, count, retentionDays int) {
	fmt.Fprintf(w, `Extension content for review (shown once on this device)
  This report also uploads the text of %d extension(s) so that an approver can
  read what they approve: SKILL.md and the text files beside it, command files,
  hook commands with their matcher, and for a plugin its manifest, its file list
  and the text of its skills, commands and hook definitions.
  Before upload, on this device: credentials are replaced with [REDACTED:type]
  and home directory paths are removed. Redaction lowers the chance that a
  secret is stored; it is not a guarantee. A file with a private key, a binary,
  a symlink or a file over 256 KiB is never uploaded.
  Stored encrypted for your account only, readable by people who can approve
  extensions, deleted %d days after the last report.
  Turn it off: --no-extension-content, %s=1, or
  "discovery.extension_content: false" in the CLI config. Leave one extension
  out with a %s file in its directory or a pattern in
  "discovery.extension_content_exclude". An administrator can turn it off for
  the whole account: Settings, "Collect extension content for review".
`, count, retentionDays, contentDisableEnv, contentMarkerFile)
}

// reportedContentPairs lists the distinct hashes a report carries.
func reportedContentPairs(report discoveryReportRequest) []contentPair {
	seen := map[contentPair]bool{}
	pairs := []contentPair{}
	for _, candidate := range report.Candidates {
		if candidate.Extensions == nil {
			continue
		}
		for _, ext := range *candidate.Extensions {
			// MCP servers have inventory hashes but no uploadable content.
			switch ext.Kind {
			case "plugin", "skill", "hook", "command":
			default:
				continue
			}
			pair := contentPair{Kind: ext.Kind, ContentHash: ext.ContentHash}
			if ext.ContentHash == "" || seen[pair] {
				continue
			}
			seen[pair] = true
			pairs = append(pairs, pair)
		}
	}
	return pairs
}

// queryMissingContent asks which reported hashes the server lacks, in
// requests of at most contentQueryLimit pairs, so a report with more hashes
// than one request carries still negotiates every one of them. It stops at
// the first answer that says the account does not collect content.
func queryMissingContent(client inventoryReportClient, fingerprint string, pairs []contentPair) (contentQueryResponse, error) {
	var combined contentQueryResponse
	for start := 0; start < len(pairs); start += contentQueryLimit {
		end := min(start+contentQueryLimit, len(pairs))
		var query contentQueryResponse
		if err := client.Post(contentMissingPath, contentQueryRequest{WorkstationFingerprint: fingerprint, Items: pairs[start:end]}, &query); err != nil {
			return contentQueryResponse{}, err
		}
		if !query.Collect {
			return query, nil
		}
		missing := combined.Missing
		combined = query
		combined.Missing = append(missing, query.Missing...)
	}
	return combined, nil
}

type contentOutcome struct {
	// Uploaded is how many extensions had content stored by this report.
	Uploaded int
	Failed   bool
}

// sendExtensionContent runs after a discovery report was accepted. It never
// fails the report: every problem becomes one line on w.
func sendExtensionContent(client inventoryReportClient, w io.Writer, report discoveryReportRequest, salt, home string, projects []string, allProfiles ...bool) contentOutcome {
	outcome := contentOutcome{}
	pairs := reportedContentPairs(report)
	if len(pairs) == 0 {
		return outcome
	}
	if disabled, source := extensionContentDisabled(); disabled {
		fmt.Fprintf(w, "Extension content was not sent: turned off on this device (%s).\n", source) //nolint:errcheck
		return outcome
	}
	query, err := queryMissingContent(client, report.WorkstationFingerprint, pairs)
	if err != nil {
		var apiErr *api.APIError
		if errors.As(err, &apiErr) && (apiErr.StatusCode == 404 || apiErr.StatusCode == 405) {
			// A server from before this feature: there is nowhere to send
			// content, and nothing about it to say.
			return outcome
		}
		outcome.Failed = true
		fmt.Fprintln(w, "Extension content was not sent: the server did not answer the content query.") //nolint:errcheck
		return outcome
	}
	if !query.Collect {
		fmt.Fprintln(w, "Extension content was not sent: this account has turned off \"Collect extension content for review\".") //nolint:errcheck
		return outcome
	}
	if len(query.Missing) == 0 {
		return outcome
	}
	capture := captureReportedExtensionContent(report, salt, home, projects, query.Missing, len(allProfiles) > 0 && allProfiles[0])
	if capture.err != nil {
		outcome.Failed = true
		fmt.Fprintln(w, "Extension content was not sent: the redaction rules could not be loaded.") //nolint:errcheck
		return outcome
	}
	items := make([]*contentItem, 0, len(capture.items))
	for _, pair := range query.Missing {
		if item := capture.items[pair]; item != nil {
			items = append(items, item)
		}
	}
	if len(items) == 0 {
		return outcome
	}
	sort.SliceStable(items, func(i, j int) bool { return items[i].bytes < items[j].bytes })
	total, deferred := 0, 0
	sendable := items[:0]
	for _, item := range items {
		if total+item.bytes > contentReportLimit {
			deferred++
			continue
		}
		total += item.bytes
		sendable = append(sendable, item)
	}
	retention := query.RetentionDays
	if retention <= 0 {
		retention = 90
	}
	if !config.ExtensionContentNoticeShown() {
		extensionContentNotice(w, len(sendable), retention)
		// Best effort: a device that cannot record the notice sees it again.
		_ = config.SetExtensionContentNoticeShown()
	}
	redactions, withheld, excluded, rejected, markersStored := 0, 0, 0, 0, 0
	for start := 0; start < len(sendable); {
		// Excluded markers travel in their own batches: the server answers
		// with one stored count per batch, and only this way can the summary
		// tell stored text from stored markers.
		marker := sendable[start].Withheld == contentExcluded
		end, size := start, 0
		for end < len(sendable) && end-start < contentBatchItems && (end == start || size+sendable[end].bytes <= contentBatchLimit) && (sendable[end].Withheld == contentExcluded) == marker {
			size += sendable[end].bytes
			end++
		}
		request := contentUploadRequest{WorkstationFingerprint: report.WorkstationFingerprint, RedactionRules: redact.RulesVersion, Items: sendable[start:end]}
		if discoveryVersionPattern.MatchString(version.Version) {
			request.CollectorVersion = version.Version
		}
		var response contentUploadResponse
		err := client.Post(contentUploadPath, request, &response)
		var invalid *api.APIError
		if errors.As(err, &invalid) && invalid.StatusCode == 422 && end-start > 1 {
			// One item the server cannot parse must not hold back the rest:
			// send the batch again one item at a time and count the refusals.
			err = nil
			for _, item := range sendable[start:end] {
				single := request
				single.Items = []*contentItem{item}
				var one contentUploadResponse
				if postErr := client.Post(contentUploadPath, single, &one); postErr != nil {
					var apiErr *api.APIError
					if errors.As(postErr, &apiErr) && apiErr.StatusCode == 422 {
						rejected++
						continue
					}
					err = postErr
					break
				}
				response.Stored += one.Stored
				response.Rejected = append(response.Rejected, one.Rejected...)
			}
		} else if errors.As(err, &invalid) && invalid.StatusCode == 422 {
			rejected++
			err = nil
		}
		if err != nil {
			var apiErr *api.APIError
			if errors.As(err, &apiErr) && apiErr.StatusCode == 409 {
				fmt.Fprintln(w, "Extension content was not sent: this account has turned off \"Collect extension content for review\".") //nolint:errcheck
			} else {
				outcome.Failed = true
				fmt.Fprintln(w, "Extension content upload did not complete; the next report sends what is still missing.") //nolint:errcheck
			}
			break
		}
		outcome.Uploaded += response.Stored
		if marker {
			markersStored += response.Stored
		}
		rejected += len(response.Rejected)
		for _, item := range sendable[start:end] {
			redactions += item.redactions
			withheld += item.withheld
			if item.Withheld == contentExcluded {
				excluded++
			}
		}
		start = end
	}
	if outcome.Uploaded > 0 || excluded > 0 {
		fmt.Fprintf(w, "Extension content: uploaded for %d extension(s) after redaction (%d value(s) redacted, %d file(s) or command(s) withheld, %d extension(s) excluded on this device).\n", outcome.Uploaded-markersStored, redactions, withheld, excluded) //nolint:errcheck
	}
	if rejected > 0 {
		fmt.Fprintf(w, "Extension content: the server did not store %d extension(s).\n", rejected) //nolint:errcheck
	}
	if deferred > 0 {
		fmt.Fprintf(w, "Extension content: %d extension(s) left for the next report (per report cap of %d MiB).\n", deferred, contentReportLimit>>20) //nolint:errcheck
	}
	return outcome
}

// captureReportedExtensionContent reuses the content collector for the same
// profiles as the accepted inventory. Profile paths stay local, and elevated
// scans do not infer projects or read another profile through a symlink.
func captureReportedExtensionContent(report discoveryReportRequest, salt, home string, projects []string, wanted []contentPair, allUsers bool) *contentCapture {
	capture := newContentCapture(home, wanted, config.ExtensionContentExclude())
	homes := []string{home}
	if allUsers {
		homes = nil
		profiles, _, _ := inventoryProfiles.Profiles()
		accepted := map[string]bool{}
		if report.Evidence != nil {
			for _, profile := range report.Evidence.Profiles {
				if profile.Completeness != "failed" {
					accepted[profile.ProfileRef] = true
				}
			}
		}
		for _, profile := range profiles[:min(len(profiles), inventoryProfileLimit)] {
			ref := inventoryProfileRef(salt, report.WorkstationFingerprint, profile.Home)
			if profile.ErrorCode == "" && accepted[ref] {
				homes = append(homes, profile.Home)
			}
		}
	}
	for _, profileHome := range homes {
		info, err := os.Lstat(profileHome)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			continue
		}
		capture.home = profileHome
		for _, agent := range []string{"claude-code", "codex-cli"} {
			collectExtensionsWith(capture, profileHome, agent, salt, allUsers, false, projects...)
		}
		if capture.err != nil {
			break
		}
	}
	return capture
}

// sendInventoryReportWithContent is the reporting entry point for both manual
// discovery and the resident runner. Every content opt-out and the one-time
// notice are enforced by the shared content pass after inventory is accepted.
func sendInventoryReportWithContent(client inventoryReportClient, w io.Writer, report discoveryReportRequest, salt string, allUsers bool, projects []string) (discoveryReportResponse, contentOutcome, error) {
	var response discoveryReportResponse
	if err := postInventoryReport(client, report, &response); err != nil {
		return response, contentOutcome{}, err
	}
	if response.LegacyEvidence {
		return response, contentOutcome{}, nil
	}
	home, err := inventoryProbes.Home()
	if err != nil && !allUsers {
		return response, contentOutcome{Failed: true}, nil
	}
	outcome := sendExtensionContent(client, w, report, salt, home, projects, allUsers)
	return response, outcome, nil
}
