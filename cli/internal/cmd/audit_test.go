package cmd

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/preloop/preloop/cli/internal/verify"
	"github.com/preloop/preloop/cli/internal/version"
)

// fakeChain is a server side chain the CLI can be pointed at, including the
// ability to serve rows that no longer match their sealed hashes.
type fakeChain struct {
	entries       []verify.SegmentEntry
	checkpoints   []chainCheckpoint
	keys          verify.KeyList
	private       ed25519.PrivateKey
	verdict       string
	serverVersion string
	versionHits   int
}

func newFakeChain(t *testing.T, rows int) *fakeChain {
	t.Helper()
	public, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	chain := &fakeChain{
		private: private,
		verdict: "ok",
		keys: verify.KeyList{
			ActiveKeyID:     "key-1",
			SignatureSchema: "preloop.signature/v1",
			Keys: []verify.PublicKey{{
				KeyID:     "key-1",
				Algorithm: verify.AlgorithmEd25519,
				PublicKey: base64.StdEncoding.EncodeToString(public),
				Active:    true,
			}},
		},
	}
	prev := verify.GenesisHash
	for index := 1; index <= rows; index++ {
		payload := map[string]interface{}{
			"seq":       index,
			"prev_hash": prev,
			"action":    fmt.Sprintf("action_%d", index),
			"status":    "success",
		}
		hash, err := verify.RowHash(verify.RowDomainV1, payload)
		if err != nil {
			t.Fatal(err)
		}
		chain.entries = append(chain.entries, verify.SegmentEntry{
			Seq:      int64(index),
			RowID:    fmt.Sprintf("row-%d", index),
			PrevHash: prev,
			RowHash:  hash,
			Payload:  payload,
		})
		prev = hash
	}
	return chain
}

// addCheckpoint signs an anchor over the chain head at seq, the way the
// sealer does.
func (c *fakeChain) addCheckpoint(t *testing.T, seq int64, chainHash string) {
	t.Helper()
	payload := map[string]interface{}{
		"schema":     "preloop.audit.chain_checkpoint/v1",
		"seq":        seq,
		"chain_hash": chainHash,
		"row_count":  seq,
		"taken_at":   "2026-09-10T12:00:00Z",
	}
	digest, err := verify.DigestOf(payload)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(c.private,
		verify.SignedBytes("preloop.audit.chain_checkpoint/v1", digest, "2026-09-10T12:00:00Z"))
	c.checkpoints = append(c.checkpoints, chainCheckpoint{
		Seq:            seq,
		ChainHash:      chainHash,
		RowCount:       seq,
		CheckpointedAt: "2026-09-10T12:00:00Z",
		SigningKeyID:   "key-1",
		Signature:      base64.StdEncoding.EncodeToString(signature),
		SignedPayload:  payload,
		Digest:         digest,
		SignatureDocument: &verify.SignatureDocument{
			Schema:      "preloop.signature/v1",
			Algorithm:   verify.AlgorithmEd25519,
			KeyID:       "key-1",
			PayloadType: "preloop.audit.chain_checkpoint/v1",
			Digest:      digest,
			SignedAt:    "2026-09-10T12:00:00Z",
			Signature:   base64.StdEncoding.EncodeToString(signature),
		},
	})
}

func (c *fakeChain) serve(t *testing.T) *httptest.Server {
	t.Helper()
	head := int64(len(c.entries))
	headHash := verify.GenesisHash
	if head > 0 {
		headHash = c.entries[head-1].RowHash
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case auditChainStatusPath:
			_ = json.NewEncoder(w).Encode(chainStatus{
				Enabled: true, HeadSeq: head, HeadHash: headHash,
				SealedRows: head, CheckpointInterval: 1000, ActiveKeyID: "key-1",
			})
		case auditChainVerifyPath:
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"status": c.verdict, "checked_rows": head, "start_seq": 1,
				"end_seq": head, "head_seq": head, "unsealed_rows": 0,
			})
		case auditChainSegmentPath:
			after := int64(0)
			_, _ = fmt.Sscanf(r.URL.Query().Get("after_seq"), "%d", &after)
			entries := []verify.SegmentEntry{}
			for _, entry := range c.entries {
				if entry.Seq > after && int64(len(entries)) < 3 {
					entries = append(entries, entry)
				}
			}
			_ = json.NewEncoder(w).Encode(verify.Segment{
				AccountID: "acct", RowDomain: verify.RowDomainV1,
				AfterSeq: after, HeadSeq: head, GenesisHash: verify.GenesisHash,
				Entries: entries, HasMore: after+int64(len(entries)) < head,
			})
		case auditChainCheckpointsPath:
			_ = json.NewEncoder(w).Encode(c.checkpoints)
		case signingKeysPath:
			_ = json.NewEncoder(w).Encode(c.keys)
		case serverVersionPath:
			c.versionHits++
			if c.serverVersion == "" {
				http.NotFound(w, r)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]string{"server_version": c.serverVersion})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

// pointCLIAt aims the CLI at a local test server and nothing else.
func pointCLIAt(t *testing.T, url string) {
	t.Helper()
	testenv.SetHome(t, t.TempDir())
	oldToken, oldURL := FlagToken, FlagURL
	FlagToken, FlagURL = "test-token", url
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
}

func runAudit(t *testing.T, args ...string) (string, error) {
	t.Helper()
	var out bytes.Buffer
	auditVerifyCmd.SetOut(&out)
	auditVerifyCmd.SetErr(&out)
	t.Cleanup(func() {
		auditStartSeq, auditEndSeq, auditJSON = 0, 0, false
	})
	err := runAuditVerify(auditVerifyCmd, args)
	return out.String(), err
}

func TestAuditVerifyWalksEveryPageAndReportsIntact(t *testing.T) {
	chain := newFakeChain(t, 7)
	pointCLIAt(t, chain.serve(t).URL)

	out, err := runAudit(t)

	if err != nil {
		t.Fatalf("a good chain failed: %v\n%s", err, out)
	}
	if !strings.Contains(out, "Chain intact: 7 rows, sequences 1 to 7") {
		t.Fatalf("output = %q", out)
	}
	if chain.versionHits != 0 {
		t.Fatalf("a clean walk called /version %d times", chain.versionHits)
	}
	// The honesty line is part of the output, not a footnote in the docs.
	if !strings.Contains(out, "does not show they were true when they were written") {
		t.Fatalf("missing the scope statement: %q", out)
	}
}

func TestAuditVerifyReportsTheFirstBrokenRow(t *testing.T) {
	chain := newFakeChain(t, 8)
	// The server serves a row whose content no longer matches its hash.
	chain.entries[3].Payload["status"] = "failure"
	pointCLIAt(t, chain.serve(t).URL)

	out, err := runAudit(t)

	if err == nil {
		t.Fatalf("a broken chain exited zero:\n%s", out)
	}
	if !strings.Contains(out, "CHAIN BROKEN after 3 good rows") {
		t.Fatalf("output = %q", out)
	}
	if !strings.Contains(out, "row_hash_mismatch at sequence 4") || !strings.Contains(out, "row-4") {
		t.Fatalf("output = %q", out)
	}
}

func TestAuditVerifyContradictsAServerThatClaimsAllIsWell(t *testing.T) {
	chain := newFakeChain(t, 5)
	chain.entries[1].Payload["action"] = "something_else"
	chain.verdict = "ok" // the server insists

	pointCLIAt(t, chain.serve(t).URL)

	out, err := runAudit(t)

	if err == nil {
		t.Fatal("the CLI accepted the server's verdict over its own walk")
	}
	if !strings.Contains(out, `The server reports "ok" and this local walk reports "broken"`) {
		t.Fatalf("the disagreement was not reported: %q", out)
	}
	if strings.Contains(out, "preloop update") {
		t.Fatalf("update hint without a newer server version: %q", out)
	}
}

func TestAuditVerifySuggestsUpdateWhenTheCLIIsOlder(t *testing.T) {
	chain := newFakeChain(t, 5)
	chain.entries[1].Payload["action"] = "something_else"
	chain.verdict = "ok"
	chain.serverVersion = "9.9.9"
	old := version.Version
	version.Version = "0.1.0"
	t.Cleanup(func() { version.Version = old })
	pointCLIAt(t, chain.serve(t).URL)

	out, err := runAudit(t)

	if err == nil {
		t.Fatal("a broken chain exited zero")
	}
	if !strings.Contains(out, "Trust the walk") || !strings.Contains(out, "preloop update") {
		t.Fatalf("output = %q", out)
	}
	if chain.versionHits != 1 {
		t.Fatalf("version endpoint hits = %d, want 1", chain.versionHits)
	}
}

func TestAuditVerifyJSONDoesNotCheckTheServerVersion(t *testing.T) {
	chain := newFakeChain(t, 5)
	chain.entries[1].Payload["action"] = "something_else"
	chain.verdict = "ok"
	chain.serverVersion = "9.9.9"
	auditJSON = true
	pointCLIAt(t, chain.serve(t).URL)

	out, err := runAudit(t)

	if err == nil {
		t.Fatal("a broken chain exited zero")
	}
	if chain.versionHits != 0 {
		t.Fatalf("json mode called /version: %s", out)
	}
}

func TestCheckpointDecodeKeepsFractionalZero(t *testing.T) {
	raw := []byte(`{
		"seq": 1,
		"chain_hash": "abc",
		"row_count": 1,
		"checkpointed_at": "2026-09-10T12:00:00Z",
		"signing_key_id": "key-1",
		"signature": "",
		"signed_payload": {"schema":"preloop.audit.chain_checkpoint/v1","amount":0.0,"seq":1},
		"digest": ""
	}`)
	var checkpoint chainCheckpoint
	if err := json.Unmarshal(raw, &checkpoint); err != nil {
		t.Fatal(err)
	}
	got, err := verify.DigestOf(checkpoint.SignedPayload)
	if err != nil {
		t.Fatal(err)
	}
	wantBody, err := verify.CanonicalFromRaw([]byte(
		`{"amount":0.0,"schema":"preloop.audit.chain_checkpoint/v1","seq":1}`))
	if err != nil {
		t.Fatal(err)
	}
	body, err := verify.CanonicalJSON(checkpoint.SignedPayload)
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != string(wantBody) {
		t.Fatalf("canonical\n got  %s\n want %s", body, wantBody)
	}
	if got != verify.DigestOfBytes(wantBody) {
		t.Fatalf("digest %s", got)
	}
}

func TestAuditVerifyChecksCheckpointSignatures(t *testing.T) {
	chain := newFakeChain(t, 6)
	chain.addCheckpoint(t, 5, chain.entries[4].RowHash)
	pointCLIAt(t, chain.serve(t).URL)

	out, err := runAudit(t)

	if err != nil {
		t.Fatalf("verify failed: %v\n%s", err, out)
	}
	if !strings.Contains(out, "Checkpoint at sequence 5: verified") {
		t.Fatalf("output = %q", out)
	}
}

func TestAuditVerifyFailsWhenACheckpointAnchorsOtherRows(t *testing.T) {
	chain := newFakeChain(t, 6)
	// A checkpoint kept from before, over a head the rows served now do not
	// produce. This is the case a rewritten chain cannot talk its way out of.
	chain.addCheckpoint(t, 5, "cc"+strings.Repeat("dd", 31))
	pointCLIAt(t, chain.serve(t).URL)

	out, err := runAudit(t)

	if err == nil {
		t.Fatalf("a contradicted checkpoint passed:\n%s", out)
	}
	if !strings.Contains(out, "Checkpoint at sequence 5: FAILED") {
		t.Fatalf("output = %q", out)
	}
}

func TestAuditVerifyEmitsJSONWithItsScope(t *testing.T) {
	chain := newFakeChain(t, 4)
	pointCLIAt(t, chain.serve(t).URL)
	auditJSON = true

	out, err := runAudit(t)

	if err != nil {
		t.Fatal(err)
	}
	var report map[string]interface{}
	if err := json.Unmarshal([]byte(out), &report); err != nil {
		t.Fatalf("not JSON: %v\n%s", err, out)
	}
	if report["status"] != "ok" || report["checked_rows"].(float64) != 4 {
		t.Fatalf("report = %#v", report)
	}
	if _, ok := report["proves"]; !ok {
		t.Fatal("the JSON verdict does not say what it proves")
	}
}

func TestAuditVerifyStopsAtTheRequestedEndSequence(t *testing.T) {
	chain := newFakeChain(t, 9)
	pointCLIAt(t, chain.serve(t).URL)
	auditEndSeq = 5

	out, err := runAudit(t)

	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "sequences 1 to 5") {
		t.Fatalf("output = %q", out)
	}
}

func TestAuditVerifyOnAnEmptyChainSaysSo(t *testing.T) {
	chain := newFakeChain(t, 0)
	pointCLIAt(t, chain.serve(t).URL)

	out, err := runAudit(t)

	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "Nothing to verify") {
		t.Fatalf("output = %q", out)
	}
}

func TestAuditVerifyRejectsAForgedGenesisPrevHash(t *testing.T) {
	chain := newFakeChain(t, 4)
	bogus := strings.Repeat("ab", 32)
	prev := bogus
	for i := range chain.entries {
		chain.entries[i].PrevHash = prev
		chain.entries[i].Payload["prev_hash"] = prev
		hash, err := verify.RowHash(verify.RowDomainV1, chain.entries[i].Payload)
		if err != nil {
			t.Fatal(err)
		}
		chain.entries[i].RowHash = hash
		prev = hash
	}
	pointCLIAt(t, chain.serve(t).URL)

	out, err := runAudit(t)

	if err == nil {
		t.Fatalf("a forged genesis prev_hash passed:\n%s", out)
	}
	if !strings.Contains(out, "prev_hash_mismatch at sequence 1") {
		t.Fatalf("output = %q", out)
	}
}

func TestAuditVerifyReportsUnwalkedCheckpointsHonestly(t *testing.T) {
	chain := newFakeChain(t, 6)
	chain.addCheckpoint(t, 6, chain.entries[5].RowHash)
	pointCLIAt(t, chain.serve(t).URL)
	auditJSON = true
	auditEndSeq = 3

	out, err := runAudit(t)

	if err != nil {
		t.Fatalf("an out-of-range checkpoint failed the walk: %v\n%s", err, out)
	}
	var report map[string]interface{}
	if err := json.Unmarshal([]byte(out), &report); err != nil {
		t.Fatalf("not JSON: %v\n%s", err, out)
	}
	if report["status"] != "ok" {
		t.Fatalf("status = %#v", report["status"])
	}
	checkpoints, _ := report["checkpoints"].([]interface{})
	if len(checkpoints) != 1 {
		t.Fatalf("checkpoints = %#v", report["checkpoints"])
	}
	checked, _ := checkpoints[0].(map[string]interface{})
	if checked["matches_local_rows"] != false {
		t.Fatalf("unwalked checkpoint claimed a local match: %#v", checked)
	}
	detail, _ := checked["detail"].(string)
	if !strings.Contains(detail, "outside the walked range") {
		t.Fatalf("detail = %q", detail)
	}
	if checked["signature_ok"] != true {
		t.Fatalf("signature should still verify: %#v", checked)
	}
}
