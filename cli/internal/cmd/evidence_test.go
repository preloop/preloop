package cmd

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/verify"
)

const testSignedAt = "2026-09-10T12:00:00Z"

// signedExport packs a period export the way the backend does and signs the
// manifest bytes with a throwaway key. packedRows is what actually lands in
// the archive, which is normally the same bytes the manifest digests and is
// deliberately different when a test wants an edited member.
func signedExport(t *testing.T, private ed25519.PrivateKey, rows, packedRows string) []byte {
	t.Helper()
	members := map[string][]byte{"audit/audit_log.jsonl": []byte(packedRows)}
	entries := []interface{}{map[string]interface{}{
		"name":       "audit/audit_log.jsonl",
		"size_bytes": len(rows),
		"sha256":     verify.DigestOfBytes([]byte(rows)),
	}}
	membersDigest, err := verify.DigestOf(entries)
	if err != nil {
		t.Fatal(err)
	}
	manifest := map[string]interface{}{
		"schema":         "preloop.retention.period_export_manifest/v1",
		"members":        entries,
		"members_digest": membersDigest,
	}
	manifestBody, err := verify.CanonicalJSON(manifest)
	if err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{verify.ManifestMember: manifestBody}
	for name, body := range members {
		files[name] = body
	}
	digest := verify.DigestOfBytes(manifestBody)
	signature := ed25519.Sign(private,
		verify.SignedBytes(verify.PayloadPeriodExport, digest, testSignedAt))
	document, err := json.Marshal(map[string]interface{}{
		"schema":       "preloop.signature/v1",
		"algorithm":    verify.AlgorithmEd25519,
		"key_id":       "key-1",
		"payload_type": verify.PayloadPeriodExport,
		"digest":       digest,
		"signed_at":    testSignedAt,
		"signature":    base64.StdEncoding.EncodeToString(signature),
	})
	if err != nil {
		t.Fatal(err)
	}
	files[verify.SignatureMember] = document
	return tarGz(t, files)
}

func tarGz(t *testing.T, files map[string][]byte) []byte {
	t.Helper()
	var buffer bytes.Buffer
	gz := gzip.NewWriter(&buffer)
	writer := tar.NewWriter(gz)
	for name, body := range files {
		if err := writer.WriteHeader(&tar.Header{Name: name, Mode: 0o600, Size: int64(len(body))}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(body); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return buffer.Bytes()
}

func writeTemp(t *testing.T, name string, body []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func serveKeys(t *testing.T, public ed25519.PublicKey, receipt interface{}) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == signingKeysPath:
			_ = json.NewEncoder(w).Encode(verify.KeyList{
				ActiveKeyID: "key-1",
				Keys: []verify.PublicKey{{
					KeyID:     "key-1",
					Algorithm: verify.AlgorithmEd25519,
					PublicKey: base64.StdEncoding.EncodeToString(public),
					Active:    true,
				}},
			})
		case strings.HasSuffix(r.URL.Path, "/evidence-status") && receipt != nil:
			_ = json.NewEncoder(w).Encode(receipt)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

func runEvidence(t *testing.T, path string) (string, error) {
	t.Helper()
	var out bytes.Buffer
	evidenceVerifyCmd.SetOut(&out)
	evidenceVerifyCmd.SetErr(&out)
	t.Cleanup(func() {
		evidenceExecutionID, evidencePublicKey = "", ""
		evidenceOffline, evidenceJSON = false, false
	})
	err := runEvidenceVerify(evidenceVerifyCmd, []string{path})
	return out.String(), err
}

func TestEvidenceVerifyAcceptsASignedPeriodExport(t *testing.T) {
	public, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	path := writeTemp(t, "export.tar.gz", signedExport(t, private, "{\"decision\":\"allow\"}\n", "{\"decision\":\"allow\"}\n"))
	pointCLIAt(t, serveKeys(t, public, nil).URL)

	out, verifyErr := runEvidence(t, path)

	if verifyErr != nil {
		t.Fatalf("a genuine export was refused: %v\n%s", verifyErr, out)
	}
	if !strings.Contains(out, "Signature verified with key key-1") {
		t.Fatalf("output = %q", out)
	}
	if !strings.Contains(out, "Not that the records were true when written") {
		t.Fatalf("the scope statement is missing: %q", out)
	}
}

func TestEvidenceVerifyRefusesAnEditedExport(t *testing.T) {
	public, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	// One member edited, manifest untouched: the digest catches it before a
	// key is ever needed.
	edited := signedExport(t, private, "{\"decision\":\"allow\"}\n", "{\"decision\":\"deny_\"}\n")
	path := writeTemp(t, "export.tar.gz", edited)
	pointCLIAt(t, serveKeys(t, public, nil).URL)

	out, verifyErr := runEvidence(t, path)

	if verifyErr == nil {
		t.Fatalf("an edited export passed:\n%s", out)
	}
}

func TestEvidenceVerifyRefusesAnExportSignedByAnotherKey(t *testing.T) {
	_, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	otherPublic, _, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	path := writeTemp(t, "export.tar.gz", signedExport(t, private, "x", "x"))
	pointCLIAt(t, serveKeys(t, otherPublic, nil).URL)

	out, verifyErr := runEvidence(t, path)

	if verifyErr == nil {
		t.Fatalf("a signature from an unpublished key passed:\n%s", out)
	}
	if !strings.Contains(out, "SIGNATURE NOT VERIFIED") {
		t.Fatalf("output = %q", out)
	}
}

func TestEvidenceVerifyUsesAKeyTheCallerBrought(t *testing.T) {
	public, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	path := writeTemp(t, "export.tar.gz", signedExport(t, private, "x", "x"))
	keyPath := writeTemp(t, "key.pub", []byte(base64.StdEncoding.EncodeToString(public)))
	// No server at all: verification against a key you kept yourself is the
	// strong form, and it must not need us.
	pointCLIAt(t, "http://127.0.0.1:1")
	evidencePublicKey = keyPath
	evidenceJSON = true

	out, verifyErr := runEvidence(t, path)

	if verifyErr != nil {
		t.Fatalf("offline verification failed: %v\n%s", verifyErr, out)
	}
	var report evidenceVerifyReport
	if err := json.Unmarshal([]byte(out), &report); err != nil {
		t.Fatalf("not JSON: %v\n%s", err, out)
	}
	if !report.SignatureOK || report.KeySource != "flag" {
		t.Fatalf("report = %#v", report)
	}
}

func TestEvidenceVerifyChecksAnEvidencePackAgainstItsReceipt(t *testing.T) {
	public, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	archive := []byte("evidence pack bytes")
	payload := map[string]interface{}{
		"schema":         verify.PayloadEvidencePack,
		"account_id":     "acct",
		"artifact_id":    "artifact-1",
		"execution_id":   "exec-1",
		"archive_sha256": verify.DigestOfBytes(archive),
		"size_bytes":     len(archive),
		"created_at":     testSignedAt,
	}
	digest, err := verify.DigestOf(payload)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(private, verify.SignedBytes(verify.PayloadEvidencePack, digest, testSignedAt))
	receipt := map[string]interface{}{
		"status":         "available",
		"artifact_id":    "artifact-1",
		"signing_key_id": "key-1",
		"signature": map[string]interface{}{
			"schema":       "preloop.signature/v1",
			"algorithm":    verify.AlgorithmEd25519,
			"key_id":       "key-1",
			"payload_type": verify.PayloadEvidencePack,
			"digest":       digest,
			"signed_at":    testSignedAt,
			"signature":    base64.StdEncoding.EncodeToString(signature),
			"payload":      payload,
		},
	}
	path := writeTemp(t, "evidence.tar.gz", archive)
	pointCLIAt(t, serveKeys(t, public, receipt).URL)
	evidenceExecutionID = "exec-1"

	out, verifyErr := runEvidence(t, path)

	if verifyErr != nil {
		t.Fatalf("a genuine pack was refused: %v\n%s", verifyErr, out)
	}
	if !strings.Contains(out, "Signature verified with key key-1") {
		t.Fatalf("output = %q", out)
	}
}

func TestEvidenceVerifyRefusesAPackThatIsNotTheSignedOne(t *testing.T) {
	public, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	payload := map[string]interface{}{
		"schema":         verify.PayloadEvidencePack,
		"artifact_id":    "artifact-1",
		"archive_sha256": verify.DigestOfBytes([]byte("the pack that was captured")),
	}
	digest, err := verify.DigestOf(payload)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(private, verify.SignedBytes(verify.PayloadEvidencePack, digest, testSignedAt))
	receipt := map[string]interface{}{
		"artifact_id": "artifact-1",
		"signature": map[string]interface{}{
			"algorithm":    verify.AlgorithmEd25519,
			"key_id":       "key-1",
			"payload_type": verify.PayloadEvidencePack,
			"digest":       digest,
			"signed_at":    testSignedAt,
			"signature":    base64.StdEncoding.EncodeToString(signature),
			"payload":      payload,
		},
	}
	path := writeTemp(t, "evidence.tar.gz", []byte("a different pack entirely"))
	pointCLIAt(t, serveKeys(t, public, receipt).URL)
	evidenceExecutionID = "exec-1"

	out, verifyErr := runEvidence(t, path)

	if verifyErr == nil {
		t.Fatalf("the wrong archive passed:\n%s", out)
	}
	if !strings.Contains(out, "but this file is") {
		t.Fatalf("output = %q", out)
	}
}

func TestEvidenceVerifySaysWhenAPackWasNeverSigned(t *testing.T) {
	public, _, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	receipt := map[string]interface{}{"artifact_id": "artifact-1", "signature": nil}
	path := writeTemp(t, "evidence.tar.gz", []byte("bytes"))
	pointCLIAt(t, serveKeys(t, public, receipt).URL)
	evidenceExecutionID = "exec-1"

	out, verifyErr := runEvidence(t, path)

	if verifyErr == nil {
		t.Fatal("an unsigned pack must not report a pass")
	}
	if !strings.Contains(out, "no signature") {
		t.Fatalf("output = %q", out)
	}
}

func TestEvidenceVerifyReportsAMissingFilePlainly(t *testing.T) {
	pointCLIAt(t, "http://127.0.0.1:1")

	_, err := runEvidence(t, filepath.Join(t.TempDir(), "absent.tar.gz"))

	if err == nil || !strings.Contains(err.Error(), "could not read") {
		t.Fatalf("err = %v", err)
	}
}
