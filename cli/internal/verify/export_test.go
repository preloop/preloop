package verify

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"testing"
)

// buildExport packs an archive the way build_period_export does: a manifest
// that digests every member, plus a detached signature over the manifest
// bytes as packed.
func buildExport(t *testing.T, members map[string][]byte, signer ed25519.PrivateKey) []byte {
	t.Helper()
	entries := []interface{}{}
	for _, name := range sortedNames(members) {
		entries = append(entries, map[string]interface{}{
			"name":       name,
			"size_bytes": len(members[name]),
			"sha256":     DigestOfBytes(members[name]),
		})
	}
	membersDigest, err := DigestOf(entries)
	if err != nil {
		t.Fatal(err)
	}
	manifest := map[string]interface{}{
		"schema":         "preloop.retention.period_export_manifest/v1",
		"account_id":     "acct",
		"members":        entries,
		"members_digest": membersDigest,
	}
	manifestBody, err := CanonicalJSON(manifest)
	if err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{ManifestMember: manifestBody}
	for name, body := range members {
		files[name] = body
	}
	if signer != nil {
		digest := DigestOfBytes(manifestBody)
		signature := ed25519.Sign(signer, SignedBytes(PayloadPeriodExport, digest, pythonSignedAt))
		document := map[string]interface{}{
			"schema":       "preloop.signature/v1",
			"algorithm":    AlgorithmEd25519,
			"key_id":       "key-1",
			"payload_type": PayloadPeriodExport,
			"digest":       digest,
			"signed_at":    pythonSignedAt,
			"signature":    base64.StdEncoding.EncodeToString(signature),
		}
		body, err := json.Marshal(document)
		if err != nil {
			t.Fatal(err)
		}
		files[SignatureMember] = body
	}
	return packTar(t, files)
}

func sortedNames(members map[string][]byte) []string {
	names := make([]string, 0, len(members))
	for name := range members {
		names = append(names, name)
	}
	for i := 0; i < len(names); i++ {
		for j := i + 1; j < len(names); j++ {
			if names[j] < names[i] {
				names[i], names[j] = names[j], names[i]
			}
		}
	}
	return names
}

func packTar(t *testing.T, files map[string][]byte) []byte {
	t.Helper()
	var buffer bytes.Buffer
	gz := gzip.NewWriter(&buffer)
	writer := tar.NewWriter(gz)
	for _, name := range sortedNames(files) {
		header := &tar.Header{Name: name, Mode: 0o600, Size: int64(len(files[name]))}
		if err := writer.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(files[name]); err != nil {
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

func testKeyPair(t *testing.T) (ed25519.PrivateKey, PublicKey) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	return private, PublicKey{
		KeyID:     "key-1",
		Algorithm: AlgorithmEd25519,
		PublicKey: base64.StdEncoding.EncodeToString(public),
		Active:    true,
	}
}

func TestAWellFormedExportChecksOutAndVerifies(t *testing.T) {
	private, public := testKeyPair(t)
	archive := buildExport(t, map[string][]byte{
		"audit/audit_log.jsonl":  []byte("{\"action\":\"a\"}\n"),
		"holds/legal_hold.jsonl": []byte(""),
	}, private)

	result, err := ReadExport(archive)
	if err != nil {
		t.Fatal(err)
	}

	if !result.ContentOK() {
		t.Fatalf("problems on a good archive: %v", result.Problems)
	}
	if result.Signature == nil {
		t.Fatal("no signature found")
	}
	if err := CheckSignature(*result.Signature, public, result.ManifestSha256); err != nil {
		t.Fatalf("signature refused: %v", err)
	}
}

func TestAnAlteredMemberIsCaughtWithoutAnyKey(t *testing.T) {
	private, _ := testKeyPair(t)
	archive := buildExport(t, map[string][]byte{
		"audit/audit_log.jsonl": []byte("{\"decision\":\"allow\"}\n"),
	}, private)
	// Rebuild the archive with one byte of the member changed and the
	// manifest left alone, which is the cheapest tamper available.
	tampered := buildTamperedExport(t, archive)
	after, err := ReadExport(tampered)
	if err != nil {
		t.Fatal(err)
	}

	if after.ContentOK() {
		t.Fatal("an altered member passed the digest check")
	}
	if len(after.Problems) == 0 || after.Members[0].OK {
		t.Fatalf("problems = %v", after.Problems)
	}
}

// buildTamperedExport re-packs an archive with one member's bytes changed
// and the manifest untouched.
func buildTamperedExport(t *testing.T, archive []byte) []byte {
	t.Helper()
	members, err := untar(archive)
	if err != nil {
		t.Fatal(err)
	}
	members["audit/audit_log.jsonl"] = bytes.Replace(
		members["audit/audit_log.jsonl"], []byte("allow"), []byte("deny_"), 1)
	return packTar(t, members)
}

func TestAMemberTheManifestNeverListedIsReported(t *testing.T) {
	private, _ := testKeyPair(t)
	archive := buildExport(t, map[string][]byte{"audit/audit_log.jsonl": []byte("x")}, private)
	members, err := untar(archive)
	if err != nil {
		t.Fatal(err)
	}
	members["extra/notes.txt"] = []byte("added later")

	result, err := ReadExport(packTar(t, members))
	if err != nil {
		t.Fatal(err)
	}

	// An unlisted file is outside everything the signature covers, so it is
	// reported rather than ignored.
	if result.ContentOK() {
		t.Fatal("an unlisted member passed")
	}
}

func TestAMemberRemovedFromTheArchiveIsReported(t *testing.T) {
	private, _ := testKeyPair(t)
	archive := buildExport(t, map[string][]byte{
		"audit/audit_log.jsonl":  []byte("x"),
		"holds/legal_hold.jsonl": []byte("y"),
	}, private)
	members, err := untar(archive)
	if err != nil {
		t.Fatal(err)
	}
	delete(members, "holds/legal_hold.jsonl")

	result, err := ReadExport(packTar(t, members))
	if err != nil {
		t.Fatal(err)
	}

	if result.ContentOK() {
		t.Fatal("a missing member passed")
	}
}

func TestARewrittenManifestBreaksTheSignature(t *testing.T) {
	private, public := testKeyPair(t)
	archive := buildExport(t, map[string][]byte{"audit/audit_log.jsonl": []byte("allow")}, private)
	members, err := untar(archive)
	if err != nil {
		t.Fatal(err)
	}
	// The full forgery: change the member and repair the manifest around it.
	members["audit/audit_log.jsonl"] = []byte("deny_")
	manifest, err := DecodeCanonical(members[ManifestMember])
	if err != nil {
		t.Fatal(err)
	}
	object := manifest.(map[string]interface{})
	entries := object["members"].([]interface{})
	entries[0].(map[string]interface{})["sha256"] = DigestOfBytes(members["audit/audit_log.jsonl"])
	digest, err := DigestOf(entries)
	if err != nil {
		t.Fatal(err)
	}
	object["members_digest"] = digest
	rebuilt, err := CanonicalJSON(object)
	if err != nil {
		t.Fatal(err)
	}
	members[ManifestMember] = rebuilt

	result, err := ReadExport(packTar(t, members))
	if err != nil {
		t.Fatal(err)
	}

	// The archive is now internally consistent, which is exactly why the
	// signature is the part that matters.
	if !result.ContentOK() {
		t.Fatalf("expected a self-consistent forgery, problems: %v", result.Problems)
	}
	if err := CheckSignature(*result.Signature, public, result.ManifestSha256); err == nil {
		t.Fatal("a forged manifest verified")
	}
}

func TestAnArchiveWithoutASignatureIsStillReadable(t *testing.T) {
	archive := buildExport(t, map[string][]byte{"audit/audit_log.jsonl": []byte("x")}, nil)

	result, err := ReadExport(archive)
	if err != nil {
		t.Fatal(err)
	}

	if result.Signature != nil {
		t.Fatal("found a signature that was never made")
	}
	if !result.ContentOK() {
		t.Fatalf("problems = %v", result.Problems)
	}
}

func TestSomethingThatIsNotAnArchiveIsAnError(t *testing.T) {
	if _, err := ReadExport([]byte("this is not a tar.gz")); err == nil {
		t.Fatal("expected an error")
	}
}

func TestAnArtifactMemberIsDigestedWithoutBeingHeld(t *testing.T) {
	previous := maxHeldMemberBytes
	maxHeldMemberBytes = 1024
	t.Cleanup(func() { maxHeldMemberBytes = previous })
	private, _ := testKeyPair(t)
	screenshot := bytes.Repeat([]byte{0x89, 'P', 'N', 'G'}, 4096)
	archive := buildExport(t, map[string][]byte{
		"artifacts/manifest.json":    []byte("[]"),
		"artifacts/s-1/a-1-shot.png": screenshot,
	}, private)

	result, err := ReadExportFrom(bytes.NewReader(archive))
	if err != nil {
		t.Fatal(err)
	}
	if !result.ContentOK() {
		t.Fatalf("problems: %v", result.Problems)
	}
	if result.ArchiveSha256 != DigestOfBytes(archive) {
		t.Fatal("streamed archive digest differs from the digest of the bytes")
	}
	found := false
	for _, member := range result.Members {
		if member.Name == "artifacts/s-1/a-1-shot.png" {
			found = member.OK && member.Size == len(screenshot)
		}
	}
	if !found {
		t.Fatalf("artifact member not verified: %+v", result.Members)
	}
}

func TestAStreamPastTheReadBudgetIsRefused(t *testing.T) {
	previous := maxStreamedBytes
	maxStreamedBytes = 1024
	t.Cleanup(func() { maxStreamedBytes = previous })
	private, _ := testKeyPair(t)
	archive := buildExport(t, map[string][]byte{
		"artifacts/s-1/a-1-big.bin": bytes.Repeat([]byte("x"), 4096),
	}, private)

	if _, err := ReadExportFrom(bytes.NewReader(archive)); err == nil {
		t.Fatal("an archive past the budget was read")
	}
}

// The server refuses exports whose artifacts exceed
// RETENTION_EXPORT_MAX_ARTIFACT_BYTES (default 2 GiB). The verifier has to
// read at least that much, or the documented check fails on a valid bundle.
func TestTheReadBudgetCoversTheServerArtifactCap(t *testing.T) {
	const serverDefaultArtifactCap int64 = 2 << 30
	if maxStreamedBytes <= serverDefaultArtifactCap {
		t.Fatalf("verifier budget %d does not cover the server cap %d", maxStreamedBytes, serverDefaultArtifactCap)
	}
}

// untar expands a gzipped tar into memory for tests that repack an archive.
// It is test-only: the verifier itself streams (ReadExportFrom).
func untar(archive []byte) (map[string][]byte, error) {
	gz, err := gzip.NewReader(bytes.NewReader(archive))
	if err != nil {
		return nil, fmt.Errorf("not a gzip archive: %w", err)
	}
	defer func() { _ = gz.Close() }()
	reader := tar.NewReader(gz)
	members := map[string][]byte{}
	budget := int64(64 << 20)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("not a tar archive: %w", err)
		}
		if header.Typeflag != tar.TypeReg {
			continue
		}
		body, err := io.ReadAll(io.LimitReader(reader, budget+1))
		if err != nil {
			return nil, fmt.Errorf("cannot read member %q: %w", header.Name, err)
		}
		budget -= int64(len(body))
		if budget < 0 {
			return nil, errors.New("archive expands past the size a verifier will hold in memory")
		}
		members[header.Name] = body
	}
	return members, nil
}
