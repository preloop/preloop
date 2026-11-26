package cmd

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/redact"
	"github.com/preloop/preloop/cli/internal/redact/redacttest"
)

// contentServer stands in for the content routes and records every body.
type contentServer struct {
	mu       sync.Mutex
	server   *httptest.Server
	bodies   [][]byte
	queries  []contentQueryRequest
	uploads  []contentUploadRequest
	collect  bool
	have     map[contentPair]bool
	status   int
	rejected int
}

func newContentServer(t *testing.T) *contentServer {
	t.Helper()
	s := &contentServer{collect: true, have: map[contentPair]bool{}}
	s.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		s.mu.Lock()
		defer s.mu.Unlock()
		s.bodies = append(s.bodies, body)
		if s.status != 0 {
			w.WriteHeader(s.status)
			return
		}
		switch r.URL.Path {
		case contentMissingPath:
			var query contentQueryRequest
			if err := json.Unmarshal(body, &query); err != nil {
				t.Error(err)
			}
			s.queries = append(s.queries, query)
			response := contentQueryResponse{Collect: s.collect, RetentionDays: 90, Missing: []contentPair{}}
			if !s.collect {
				response.Reason = "account_opt_out"
			} else {
				for _, pair := range query.Items {
					if !s.have[pair] {
						response.Missing = append(response.Missing, pair)
					}
				}
			}
			_ = json.NewEncoder(w).Encode(response)
		case contentUploadPath:
			var upload contentUploadRequest
			if err := json.Unmarshal(body, &upload); err != nil {
				t.Error(err)
			}
			s.uploads = append(s.uploads, upload)
			for _, item := range upload.Items {
				s.have[contentPair{Kind: item.Kind, ContentHash: item.ContentHash}] = true
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"stored": len(upload.Items), "kept": 0, "rejected": []any{}})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(s.server.Close)
	return s
}

func (s *contentServer) client() *api.Client {
	return api.NewClientWithToken(s.server.URL, "synthetic-token")
}

func (s *contentServer) items() map[string]*contentItem {
	found := map[string]*contentItem{}
	for _, upload := range s.uploads {
		for _, item := range upload.Items {
			// The fixture has two skills: the standalone one, and the one the
			// plugin bundles. Keep the standalone one, which has more entries
			// or is the one a test excluded.
			if old := found[item.Kind]; old != nil && (old.Withheld != "" || (item.Withheld == "" && len(old.Files) > len(item.Files))) {
				continue
			}
			found[item.Kind] = item
		}
	}
	return found
}

func (s *contentServer) all() []byte { return bytes.Join(s.bodies, []byte("\n")) }

func fileByPath(item *contentItem) map[string]contentFile {
	files := map[string]contentFile{}
	if item != nil {
		for _, file := range item.Files {
			files[file.Path] = file
		}
	}
	return files
}

// contentHome builds a Claude Code home with one skill, one command, one hook
// event and one plugin. Every secret class is planted in each of them.
func contentHome(t *testing.T) (string, []string) {
	t.Helper()
	home := filepath.Join(t.TempDir(), "fixture-person")
	var lines, secrets []string
	for _, planted := range redacttest.PlantedSecrets() {
		lines = append(lines, planted.Line)
		secrets = append(secrets, planted.Secret)
	}
	body := "---\nname: deploy\ndescription: Deploy the service.\n---\n# Deploy\n\nRead before running.\n\n" + strings.Join(lines, "\n") + "\n"
	writeInventoryFixture(t, home, ".claude/skills/deploy/SKILL.md", body)
	writeInventoryFixture(t, home, ".claude/skills/deploy/scripts/run.sh", "#!/bin/sh\ncd "+home+"/work && ./run\n")
	writeInventoryFixture(t, home, ".claude/skills/deploy/reference.md", "# Reference\n\nNothing secret here.\n")
	writeInventoryFixture(t, home, ".claude/skills/deploy/id_key", "notes\n"+redacttest.PlantedPrivateKey())
	writeInventoryFixture(t, home, ".claude/skills/deploy/logo.png", "\x89PNG\x00\x01\x02binary-marker")
	writeInventoryFixture(t, home, ".claude/skills/deploy/big.txt", strings.Repeat("oversize-marker ", contentFileLimit/16+1))
	if runtime.GOOS != "windows" {
		if err := os.Symlink(filepath.Join(home, ".claude", "skills", "deploy", "reference.md"), filepath.Join(home, ".claude", "skills", "deploy", "link.md")); err != nil {
			t.Fatal(err)
		}
	}
	writeInventoryFixture(t, home, ".claude/commands/release.md", "---\ndescription: Cut a release.\n---\n"+strings.Join(lines, "\n")+"\n")
	hooks := []map[string]any{}
	for _, planted := range redacttest.PlantedSecrets() {
		hooks = append(hooks, map[string]any{"type": "command", "command": planted.Line})
	}
	settings, _ := json.Marshal(map[string]any{
		"enabledPlugins": map[string]bool{"reviewer@claude-plugins-official": true},
		"hooks": map[string]any{"PreToolUse": []map[string]any{
			{"matcher": "Bash", "hooks": hooks},
			{"matcher": "Write|Edit", "hooks": []map[string]any{{"type": "command", "command": "preloop agents permission-hook"}}},
		}},
	})
	writeInventoryFixture(t, home, ".claude/settings.json", string(settings))
	plugin := ".claude/plugins/marketplaces/claude-plugins-official/plugins/reviewer"
	writeClaudePluginRecord(t, home, "reviewer", `{"name":"reviewer","marketplace":"claude-plugins-official","version":"1.0.0","installPath":"`+plugin+`"}`)
	writeInventoryFixture(t, home, plugin+"/.claude-plugin/plugin.json", `{"name":"reviewer","version":"1.4.0","description":"Review changes"}`)
	writeInventoryFixture(t, home, plugin+"/skills/bundled/SKILL.md", "# Bundled\n\n"+lines[0]+"\n")
	writeInventoryFixture(t, home, plugin+"/commands/check.md", "Run the check.\n")
	writeInventoryFixture(t, home, plugin+"/hooks/hooks.json", `{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo done"}]}]}}`)
	writeInventoryFixture(t, home, plugin+"/src/index.js", "module.exports = 'not-selected-marker'\n")
	return home, secrets
}

func contentReport(t *testing.T, home string) discoveryReportRequest {
	t.Helper()
	deps := inventoryTestDependencies(t, home)
	report := buildDiscoveryReport("synthetic-salt", "synthetic-device", home, []AgentConfig{{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}})
	if err := addDiscoveryExtensions(&report, "synthetic-salt", home, deps); err != nil {
		t.Fatal(err)
	}
	if len(reportedContentPairs(report)) < 4 {
		t.Fatalf("fixture reported too few hashes: %+v", reportedContentPairs(report))
	}
	return report
}

func runContent(t *testing.T, server *contentServer, home string, report discoveryReportRequest) (contentOutcome, string) {
	t.Helper()
	var out bytes.Buffer
	outcome := sendExtensionContent(server.client(), &out, report, "synthetic-salt", home, nil)
	return outcome, out.String()
}

// protects=the redaction promise end to end: no planted secret of any class,
// no private key, no binary, no oversize file, no symlink target and no home
// path reaches a request body; fails_when=any of them appears in what the
// collector sends; no_claim=synthetic secrets and a synthetic home.
func TestExtensionContentUploadCarriesNoPlantedSecret(t *testing.T) {
	home, secrets := contentHome(t)
	report := contentReport(t, home)
	server := newContentServer(t)
	outcome, output := runContent(t, server, home, report)
	if outcome.Uploaded < 4 {
		t.Fatalf("uploaded %d extensions, output:\n%s", outcome.Uploaded, output)
	}
	sent := server.all()
	for i, secret := range secrets {
		if bytes.Contains(sent, []byte(secret)) {
			t.Errorf("planted secret %d (%s) reached a request body", i, redacttest.PlantedSecrets()[i].Class)
		}
	}
	for _, needle := range []string{"MIIEvQIBADANBg", "PRIVATE KEY", "binary-marker", "oversize-marker", "not-selected-marker", home, "fixture-person"} {
		if bytes.Contains(sent, []byte(needle)) {
			t.Errorf("%q reached a request body", needle)
		}
	}
	if !bytes.Contains(sent, []byte("[REDACTED:")) || !bytes.Contains(sent, []byte("Read before running.")) {
		t.Fatal("expected redacted, readable text in the upload")
	}
	if strings.Contains(output, "ghp_") || !strings.Contains(output, "value(s) redacted") {
		t.Fatalf("unexpected summary: %s", output)
	}

	items := server.items()
	skill := fileByPath(items["skill"])
	wantStatus := map[string]string{"SKILL.md": contentStored, "scripts/run.sh": contentStored, "reference.md": contentStored, "id_key": contentPrivateKey, "logo.png": contentBinary, "big.txt": contentTooLarge}
	if runtime.GOOS != "windows" {
		wantStatus["link.md"] = contentSymlink
	}
	for name, status := range wantStatus {
		if skill[name].Status != status {
			t.Errorf("skill file %s status = %q, want %q", name, skill[name].Status, status)
		}
		if (skill[name].Text != nil) != (status == contentStored) {
			t.Errorf("skill file %s text presence does not match status %s", name, status)
		}
	}
	if len(skill["SKILL.md"].Redactions) == 0 || skill["scripts/run.sh"].Redactions[redact.TypeHomePath] != 1 {
		t.Errorf("redaction counts missing: %+v / %+v", skill["SKILL.md"].Redactions, skill["scripts/run.sh"].Redactions)
	}
	if len(skill["reference.md"].Redactions) != 0 || *skill["reference.md"].Text != "# Reference\n\nNothing secret here.\n" {
		t.Errorf("clean file was altered: %+v", skill["reference.md"])
	}

	hook := items["hook"]
	if hook == nil || len(hook.Hooks) != len(secrets)+1 {
		t.Fatalf("hook item = %+v", hook)
	}
	last := hook.Hooks[len(hook.Hooks)-1]
	if last.Matcher != "Write|Edit" || last.Command == nil || *last.Command != "preloop agents permission-hook" || hook.Hooks[0].Matcher != "Bash" {
		t.Errorf("hook matcher or command lost: %+v", last)
	}

	plugin := fileByPath(items["plugin"])
	if plugin["src/index.js"].Status != contentNotSelected || plugin["src/index.js"].Text != nil {
		t.Errorf("plugin source file should be listed only: %+v", plugin["src/index.js"])
	}
	for _, name := range []string{".claude-plugin/plugin.json", "skills/bundled/SKILL.md", "commands/check.md", "hooks/hooks.json"} {
		if plugin[name].Status != contentStored {
			t.Errorf("plugin file %s status = %q", name, plugin[name].Status)
		}
	}
	command := fileByPath(items["command"])
	if command["release.md"].Status != contentStored || len(command["release.md"].Redactions) == 0 {
		t.Errorf("command = %+v", command)
	}
}

// protects=content is addressed by the inventory hash: the manifest the
// collector sends reproduces the reported hash and unredacted text matches
// its own digest; fails_when=the server could not verify what it stores.
func TestExtensionContentManifestReproducesTheReportedHash(t *testing.T) {
	home, _ := contentHome(t)
	report := contentReport(t, home)
	server := newContentServer(t)
	runContent(t, server, home, report)
	reported := map[contentPair]bool{}
	for _, pair := range reportedContentPairs(report) {
		reported[pair] = true
	}
	checked := 0
	for _, upload := range server.uploads {
		if upload.RedactionRules != redact.RulesVersion || upload.WorkstationFingerprint != report.WorkstationFingerprint {
			t.Fatalf("upload envelope = %+v", upload)
		}
		for _, item := range upload.Items {
			if !reported[contentPair{Kind: item.Kind, ContentHash: item.ContentHash}] {
				t.Errorf("uploaded a hash the report does not carry: %s %s", item.Kind, item.ContentHash)
			}
			var recomputed string
			switch item.Kind {
			case "hook":
				hashes := []string{}
				for _, hook := range item.Hooks {
					hashes = append(hashes, hook.CommandHash)
				}
				sort.Strings(hashes)
				recomputed = extensionHash([]byte(strings.Join(hashes, "\n")))
			case "command":
				recomputed = "sha256:" + item.Files[0].SHA256
			default:
				var tree strings.Builder
				for _, file := range item.Files {
					digest := file.SHA256
					if file.Status == contentSymlink {
						digest = "symlink"
					}
					tree.WriteString(file.Path + "\x00" + digest + "\n")
				}
				recomputed = extensionHash([]byte(tree.String()))
			}
			if recomputed != item.ContentHash {
				t.Errorf("%s manifest hashes to %s, reported %s", item.Kind, recomputed, item.ContentHash)
			}
			for _, file := range item.Files {
				if file.Text != nil && len(file.Redactions) == 0 {
					sum := sha256.Sum256([]byte(*file.Text))
					if hex.EncodeToString(sum[:]) != file.SHA256 {
						t.Errorf("%s: unredacted text does not match its digest", file.Path)
					}
					checked++
				}
			}
		}
	}
	if checked == 0 {
		t.Fatal("no unredacted file was checked")
	}
}

// protects=content the server already holds is not sent again; fails_when=a
// second report uploads anything or reads files it does not need to.
func TestExtensionContentIsNotResent(t *testing.T) {
	home, _ := contentHome(t)
	report := contentReport(t, home)
	server := newContentServer(t)
	runContent(t, server, home, report)
	first := len(server.uploads)
	outcome, output := runContent(t, server, home, report)
	if len(server.uploads) != first || outcome.Uploaded != 0 || len(server.queries) != 2 {
		t.Fatalf("second run uploaded again: uploads=%d outcome=%+v", len(server.uploads), outcome)
	}
	if strings.Contains(output, "shown once") {
		t.Fatal("the notice was printed a second time")
	}
	// One changed skill is the only thing a third report sends.
	writeInventoryFixture(t, home, ".claude/skills/deploy/reference.md", "# Reference\n\nChanged.\n")
	changed := contentReport(t, home)
	outcome, _ = runContent(t, server, home, changed)
	if outcome.Uploaded != 1 || len(server.uploads[len(server.uploads)-1].Items) != 1 || server.uploads[len(server.uploads)-1].Items[0].Kind != "skill" {
		t.Fatalf("expected exactly the changed skill, got %+v", outcome)
	}
}

// protects=the one-time device notice is printed before anything is sent and
// names what is sent and each way to turn it off; fails_when=the upload
// precedes the notice or the notice repeats.
func TestExtensionContentNoticeComesBeforeTheFirstUpload(t *testing.T) {
	home, _ := contentHome(t)
	report := contentReport(t, home)
	server := newContentServer(t)
	var out bytes.Buffer
	uploadsAtNotice := -1
	writer := writerFunc(func(p []byte) (int, error) {
		if uploadsAtNotice < 0 && bytes.Contains(p, []byte("shown once")) {
			server.mu.Lock()
			uploadsAtNotice = len(server.uploads)
			server.mu.Unlock()
		}
		return out.Write(p)
	})
	sendExtensionContent(server.client(), writer, report, "synthetic-salt", home, nil)
	if uploadsAtNotice != 0 || len(server.uploads) == 0 {
		t.Fatalf("notice printed after %d upload(s); uploads=%d", uploadsAtNotice, len(server.uploads))
	}
	for _, needle := range []string{"SKILL.md", "hook commands with their matcher", "--no-extension-content", contentDisableEnv, "discovery.extension_content: false", contentMarkerFile, "discovery.extension_content_exclude", "Collect extension content for review", "not a guarantee", "90 days"} {
		if !strings.Contains(out.String(), needle) {
			t.Errorf("notice does not mention %q", needle)
		}
	}
}

type writerFunc func([]byte) (int, error)

func (f writerFunc) Write(p []byte) (int, error) { return f(p) }

// protects=the device opt-out by flag, environment and config key;
// fails_when=any request is made while the device is opted out.
func TestExtensionContentDeviceOptOut(t *testing.T) {
	cases := map[string]func(t *testing.T, home string){
		"flag": func(t *testing.T, _ string) {
			discoveryNoExtensionContent = true
			t.Cleanup(func() { discoveryNoExtensionContent = false })
		},
		"environment": func(t *testing.T, _ string) { t.Setenv(contentDisableEnv, "1") },
		"config": func(t *testing.T, home string) {
			writeInventoryFixture(t, home, ".preloop/config.yaml", "discovery:\n  extension_content: false\n")
		},
	}
	for name, optOut := range cases {
		t.Run(name, func(t *testing.T) {
			home, _ := contentHome(t)
			report := contentReport(t, home)
			optOut(t, home)
			server := newContentServer(t)
			outcome, output := runContent(t, server, home, report)
			if len(server.bodies) != 0 || outcome.Uploaded != 0 {
				t.Fatalf("made %d request(s) while opted out", len(server.bodies))
			}
			if !strings.Contains(output, "turned off on this device") {
				t.Fatalf("output does not explain the opt-out: %q", output)
			}
		})
	}
}

// protects=the account opt-out: the server says no and the collector reads
// and sends nothing; fails_when=an upload follows collect=false, or a 409
// from a server that changed its mind mid-run is treated as success.
func TestExtensionContentAccountOptOut(t *testing.T) {
	home, _ := contentHome(t)
	report := contentReport(t, home)
	server := newContentServer(t)
	server.collect = false
	outcome, output := runContent(t, server, home, report)
	if len(server.uploads) != 0 || outcome.Uploaded != 0 || len(server.queries) != 1 {
		t.Fatalf("uploaded despite the account opt-out: %+v", outcome)
	}
	if !strings.Contains(output, "this account has turned off") || strings.Contains(output, "shown once") {
		t.Fatalf("unexpected output: %q", output)
	}
}

// protects=per extension exclusion by marker file and by config pattern;
// fails_when=text of an excluded extension is sent.
func TestExtensionContentPerExtensionExclusion(t *testing.T) {
	home, _ := contentHome(t)
	// The marker is part of the directory, so it is placed before hashing.
	writeInventoryFixture(t, home, ".claude/skills/deploy/"+contentMarkerFile, "")
	writeInventoryFixture(t, home, ".preloop/config.yaml", "discovery:\n  extension_content_exclude:\n    - \"hook:Pre*\"\n    - \"command:release\"\n")
	report := contentReport(t, home)
	server := newContentServer(t)
	_, output := runContent(t, server, home, report)
	items := server.items()
	for _, kind := range []string{"skill", "hook", "command"} {
		item := items[kind]
		if item == nil || item.Withheld != contentExcluded || len(item.Files) != 0 || len(item.Hooks) != 0 {
			t.Errorf("%s was not excluded: %+v", kind, item)
		}
	}
	if items["plugin"] == nil || items["plugin"].Withheld != "" {
		t.Errorf("the plugin should still be sent: %+v", items["plugin"])
	}
	sent := server.all()
	for _, needle := range []string{"Read before running.", "Cut a release.", "permission-hook"} {
		if bytes.Contains(sent, []byte(needle)) {
			t.Errorf("excluded text %q was sent", needle)
		}
	}
	if !strings.Contains(output, "excluded on this device") {
		t.Fatalf("summary does not mention exclusions: %q", output)
	}
}

// protects=the size caps: per file, per extension and per report;
// fails_when=more text than a cap allows is sent, or a capped file is not
// marked.
func TestExtensionContentSizeCaps(t *testing.T) {
	home := filepath.Join(t.TempDir(), "fixture-person")
	writeInventoryFixture(t, home, ".claude/settings.json", `{}`)
	chunk := strings.Repeat("plain words here\n", contentFileLimit/17-1)
	// Six files just under the file cap exceed the extension cap of 1 MiB.
	for i := 0; i < 6; i++ {
		writeInventoryFixture(t, home, filepath.Join(".claude", "skills", "heavy", "part"+string(rune('a'+i))+".md"), chunk)
	}
	// Ten more skills of about 900 KiB each exceed the report cap of 8 MiB.
	for i := 0; i < 10; i++ {
		for j := 0; j < 4; j++ {
			writeInventoryFixture(t, home, filepath.Join(".claude", "skills", "bulk"+string(rune('a'+i)), "part"+string(rune('a'+j))+".md"), chunk[:len(chunk)-i-64])
		}
	}
	report := contentReport(t, home)
	server := newContentServer(t)
	_, output := runContent(t, server, home, report)
	total, heavySeen := 0, false
	for _, upload := range server.uploads {
		batch := 0
		for _, item := range upload.Items {
			size, over := 0, 0
			for _, file := range item.Files {
				if file.Text != nil {
					if len(*file.Text) > contentFileLimit {
						t.Errorf("file over the file cap was sent: %d bytes", len(*file.Text))
					}
					size += len(*file.Text)
				}
				if file.Status == contentOverBudget {
					over++
				}
			}
			if size > contentExtensionLimit {
				t.Errorf("extension over the extension cap was sent: %d bytes", size)
			}
			if len(item.Files) == 6 {
				heavySeen = true
				if over == 0 {
					t.Error("the heavy skill has no over_budget file")
				}
			}
			batch += size
		}
		if batch > contentBatchLimit && len(upload.Items) > 1 {
			t.Errorf("batch of %d bytes exceeds the batch bound", batch)
		}
		total += batch
	}
	if total > contentReportLimit || total < contentReportLimit/2 {
		t.Fatalf("report sent %d bytes, cap is %d", total, contentReportLimit)
	}
	if !strings.Contains(output, "left for the next report") {
		t.Fatalf("deferred extensions were not announced: %q", output)
	}
	if !heavySeen {
		// The heavy skill is the largest, so it may be among the deferred.
		t.Log("heavy skill deferred to the next report")
	}
	// The next report sends what was deferred and nothing twice.
	before := len(server.uploads)
	runContent(t, server, home, report)
	if len(server.uploads) == before {
		t.Fatal("deferred extensions were not sent by the next report")
	}
}

// protects=a current CLI against a server from before this feature: the
// content routes are missing and the report still succeeds quietly;
// fails_when=the collector reports an error or prints the notice.
func TestExtensionContentAgainstOlderServer(t *testing.T) {
	home, _ := contentHome(t)
	report := contentReport(t, home)
	server := newContentServer(t)
	server.status = http.StatusNotFound
	outcome, output := runContent(t, server, home, report)
	if outcome.Uploaded != 0 || output != "" || len(server.bodies) != 1 {
		t.Fatalf("older server: outcome=%+v output=%q requests=%d", outcome, output, len(server.bodies))
	}
	server.status = http.StatusInternalServerError
	_, output = runContent(t, server, home, report)
	if !strings.Contains(output, "was not sent") || strings.Contains(output, "shown once") {
		t.Fatalf("server error was not reported plainly: %q", output)
	}
}

// protects=the full discover --report path: the report goes first, content
// follows, and the closing line no longer claims that no file content was
// sent; fails_when=content is sent before the report or the line is stale.
func TestDiscoveryReportSendsContentAfterTheReport(t *testing.T) {
	home, secrets := contentHome(t)
	inventoryTestDependencies(t, home)
	oldProbes := inventoryProbes
	inventoryProbes = inventoryTestDependencies(t, home)
	oldReader := readMachineID
	readMachineID = func() (string, error) { return "synthetic-device", nil }
	t.Cleanup(func() { inventoryProbes, readMachineID = oldProbes, oldReader })
	content := newContentServer(t)
	var order []string
	var sent [][]byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		order = append(order, r.URL.Path)
		sent = append(sent, body)
		switch r.URL.Path {
		case discoverySaltPath:
			_ = json.NewEncoder(w).Encode(map[string]any{"salt": strings.Repeat("s", 40), "algorithm": "hmac-sha256", "retention_days": 90})
		case discoveryReportPath:
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]int{"received": 1, "created": 1})
		default:
			r.Body = io.NopCloser(bytes.NewReader(body))
			content.server.Config.Handler.ServeHTTP(w, r)
		}
	}))
	defer server.Close()
	var out bytes.Buffer
	agents := []AgentConfig{{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}}
	if err := sendDiscoveryReport(api.NewClientWithToken(server.URL, "synthetic-token"), agents, &out, ""); err != nil {
		t.Fatal(err)
	}
	if len(order) < 4 || order[1] != discoveryReportPath || order[2] != contentMissingPath || order[3] != contentUploadPath {
		t.Fatalf("request order = %v", order)
	}
	if strings.Contains(out.String(), "no file contents") || !strings.Contains(out.String(), "redacted text of") {
		t.Fatalf("closing line is stale: %s", out.String())
	}
	all := bytes.Join(sent, []byte("\n"))
	for i, secret := range secrets {
		if bytes.Contains(all, []byte(secret)) {
			t.Errorf("planted secret %d reached the wire", i)
		}
	}
	// The report itself still carries no content.
	if bytes.Contains(sent[1], []byte("Read before running.")) || bytes.Contains(sent[1], []byte("[REDACTED:")) {
		t.Fatal("extension text leaked into the discovery report")
	}
}

// protects=excluding a plugin also excludes the skills it bundles, which the
// inventory lists as extensions of their own; fails_when=a bundled SKILL.md
// is uploaded for a plugin that opted out by marker or by pattern.
func TestExtensionContentPluginExclusionCoversBundledSkills(t *testing.T) {
	plugin := ".claude/plugins/marketplaces/claude-plugins-official/plugins/reviewer"
	cases := map[string]func(t *testing.T, home string){
		"marker": func(t *testing.T, home string) {
			writeInventoryFixture(t, home, plugin+"/"+contentMarkerFile, "")
		},
		"pattern": func(t *testing.T, home string) {
			writeInventoryFixture(t, home, ".preloop/config.yaml", "discovery:\n  extension_content_exclude:\n    - \"plugin:reviewer@*\"\n")
		},
	}
	for name, exclude := range cases {
		t.Run(name, func(t *testing.T) {
			home, _ := contentHome(t)
			exclude(t, home)
			report := contentReport(t, home)
			server := newContentServer(t)
			runContent(t, server, home, report)
			if bytes.Contains(server.all(), []byte("# Bundled")) || bytes.Contains(server.all(), []byte("Run the check.")) {
				t.Fatal("text of an excluded plugin was uploaded")
			}
			excluded := 0
			for _, upload := range server.uploads {
				for _, item := range upload.Items {
					if item.Withheld == contentExcluded {
						excluded++
					}
				}
			}
			// The plugin and its one bundled skill.
			if excluded != 2 {
				t.Fatalf("excluded markers = %d, want 2", excluded)
			}
			if !bytes.Contains(server.all(), []byte("Read before running.")) {
				t.Fatal("the standalone skill should still be uploaded")
			}
		})
	}
}

// protects=one extension the server cannot describe does not block the rest;
// fails_when=an over-long path, or a batch the server refuses to parse, stops
// every other extension of the device from being uploaded.
func TestExtensionContentOneBadItemDoesNotBlockTheRest(t *testing.T) {
	home, _ := contentHome(t)
	old := contentPathLimit
	contentPathLimit = 64
	t.Cleanup(func() { contentPathLimit = old })
	deep := filepath.Join(".claude", "skills", "deep", strings.Repeat("d", 40), strings.Repeat("e", 40))
	writeInventoryFixture(t, home, filepath.Join(deep, "note.md"), "deep\n")
	writeInventoryFixture(t, home, ".claude/skills/deep/SKILL.md", "# Deep\n")
	report := contentReport(t, home)
	server := newContentServer(t)
	outcome, _ := runContent(t, server, home, report)
	withheld := 0
	for _, upload := range server.uploads {
		for _, item := range upload.Items {
			for _, file := range item.Files {
				if len(file.Path) > contentPathLimit {
					t.Fatalf("a path of %d bytes was sent", len(file.Path))
				}
			}
			if item.Withheld == contentTooLarge {
				withheld++
			}
		}
	}
	if withheld != 1 || outcome.Uploaded < 5 {
		t.Fatalf("withheld=%d uploaded=%d", withheld, outcome.Uploaded)
	}

	// A server that refuses a whole batch is asked again item by item.
	home, _ = contentHome(t)
	report = contentReport(t, home)
	stored, calls := 0, 0
	picky := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.URL.Path == contentMissingPath {
			var query contentQueryRequest
			_ = json.Unmarshal(body, &query)
			_ = json.NewEncoder(w).Encode(contentQueryResponse{Collect: true, RetentionDays: 90, Missing: query.Items})
			return
		}
		calls++
		var upload contentUploadRequest
		_ = json.Unmarshal(body, &upload)
		if len(upload.Items) > 1 || upload.Items[0].Kind == "command" {
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"detail":{"code":"invalid_request"}}`))
			return
		}
		stored++
		_ = json.NewEncoder(w).Encode(map[string]any{"stored": 1, "kept": 0, "rejected": []any{}})
	}))
	defer picky.Close()
	var out bytes.Buffer
	result := sendExtensionContent(api.NewClientWithToken(picky.URL, "synthetic-token"), &out, report, "synthetic-salt", home, nil)
	if stored < 4 || result.Uploaded != stored || calls != stored+2 {
		t.Fatalf("stored=%d uploaded=%d calls=%d output=%s", stored, result.Uploaded, calls, out.String())
	}
	if !strings.Contains(out.String(), "did not store 1 extension(s)") {
		t.Fatalf("the refused item was not reported: %s", out.String())
	}
}

// protects=a hook matcher passes through the redactor like every other text;
// fails_when=a credential written into a matcher reaches a request body, or
// the redaction is not recorded on the hook.
func TestExtensionContentRedactsHookMatchers(t *testing.T) {
	home := filepath.Join(t.TempDir(), "fixture-person")
	var matchers []map[string]any
	var secrets []string
	for _, planted := range redacttest.PlantedSecrets() {
		matchers = append(matchers, map[string]any{"matcher": planted.Line, "hooks": []map[string]any{{"type": "command", "command": "echo ok"}}})
		secrets = append(secrets, planted.Secret)
	}
	matchers = append(matchers, map[string]any{"matcher": "Bash", "hooks": []map[string]any{{"type": "command", "command": "echo plain"}}})
	settings, _ := json.Marshal(map[string]any{"hooks": map[string]any{"PreToolUse": matchers}})
	writeInventoryFixture(t, home, ".claude/settings.json", string(settings))
	deps := inventoryTestDependencies(t, home)
	report := buildDiscoveryReport("synthetic-salt", "synthetic-device", home, []AgentConfig{{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}})
	if err := addDiscoveryExtensions(&report, "synthetic-salt", home, deps); err != nil {
		t.Fatal(err)
	}
	server := newContentServer(t)
	runContent(t, server, home, report)
	sent := server.all()
	for i, secret := range secrets {
		if bytes.Contains(sent, []byte(secret)) {
			t.Errorf("planted secret %d (%s) in a matcher reached a request body", i, redacttest.PlantedSecrets()[i].Class)
		}
	}
	hook := server.items()["hook"]
	if hook == nil || len(hook.Hooks) != len(secrets)+1 {
		t.Fatalf("hook item = %+v", hook)
	}
	redacted, plain := 0, 0
	for _, entry := range hook.Hooks {
		if len(entry.MatcherRedactions) > 0 {
			redacted++
			if !strings.Contains(entry.Matcher, "[REDACTED:") {
				t.Errorf("matcher counted as redacted but not replaced: %q", entry.Matcher)
			}
		}
		if entry.Matcher == "Bash" && len(entry.MatcherRedactions) == 0 && len(entry.Redactions) == 0 {
			plain++
		}
	}
	if redacted != len(secrets) || plain != 1 {
		t.Fatalf("redacted matchers = %d of %d, plain = %d", redacted, len(secrets), plain)
	}
	// The query names the device, so a device-bound credential can be held to it.
	if len(server.queries) != 1 || server.queries[0].WorkstationFingerprint != report.WorkstationFingerprint {
		t.Fatalf("query did not carry the fingerprint: %+v", server.queries)
	}
}
