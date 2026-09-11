package verify

import (
	"encoding/base64"
	"testing"
)

// Fixtures produced by the backend itself (json.dumps with sort_keys=True,
// separators=(",", ":"), ensure_ascii=False). If Go and Python ever disagree
// about a byte, every digest downstream disagrees too, so these are pinned
// rather than derived.
const (
	pythonSimple  = `{"a":"x","b":1}`
	pythonEscapes = "{\"back\":\"a\\\\b\",\"ctrl\":\"\\u0000\\u001f\",\"ls\":\"\u2028\",\"nl\":\"l1\\nl2\",\"quote\":\"he said \\\"hi\\\"\",\"tab\":\"\\t\",\"unicode\":\"héllo → ✓\"}"
	pythonNumbers = `{"nested":{"z":[{"k":"v"}]},"null":null,"nums":[1,-2,3.5,1e+30,0.1],"true":true}`
	pythonHTML    = `{"html":"<a href=\"x\">&amp;</a>"}`

	// One sealed audit row, exactly as the chain hashes it.
	pythonRowPayload = `{"account_id":"11111111-2222-4333-8444-555555555555","action":"tool_call_approved","details":{"amount":1000,"note":"héllo → ✓","quote":"he said \"hi\""},"prev_hash":"0000000000000000000000000000000000000000000000000000000000000000","resource_id":"send_payment","resource_type":"tool","seq":1,"status":"success","timestamp":"2026-09-10T12:00:00Z","user_id":null}`
	pythonRowHash    = "ae72cf4cfed8f69d496bfdbdb2a882c2b649490aff71d8a227111fb682a684a3"

	// A period export manifest, its digest, and a real Ed25519 signature made
	// by the backend's signing helper over the documented signed bytes.
	pythonManifest       = `{"members":[{"name":"a.jsonl","sha256":"abababababababababababababababababababababababababababababababab","size_bytes":3}],"schema":"preloop.retention.period_export_manifest/v1"}`
	pythonManifestDigest = "0817faab16f36165276b90f9abe2c3c0e05985bdebec816f12c6a106e50f1c55"
	pythonPublicKey      = "qIM9T+5jtMNY1L4e/QiY6q/Axe9dIKfefLKKiejuJCQ="
	pythonSignature      = "eCHzwR9c4CQFHj5NbTJRYfzhflMRZzoEiL2EDOSZWt4AMf2qYwqilhvCPuiJrY6ijhR9g9nJeI/+ZVkhCm4IDg=="
	pythonSignedAt       = "2026-09-10T12:00:00Z"
)

func canonicalOf(t *testing.T, raw string) string {
	t.Helper()
	out, err := CanonicalFromRaw([]byte(raw))
	if err != nil {
		t.Fatalf("canonicalise %q: %v", raw, err)
	}
	return string(out)
}

func TestCanonicalJSONReproducesThePlatformsBytes(t *testing.T) {
	for _, fixture := range []string{
		pythonSimple, pythonEscapes, pythonNumbers, pythonHTML, `[]`, `{}`,
		pythonRowPayload, pythonManifest,
	} {
		if got := canonicalOf(t, fixture); got != fixture {
			t.Fatalf("canonical JSON differs from the platform\n want %s\n got  %s", fixture, got)
		}
	}
}

func TestCanonicalJSONSortsKeysAndDropsWhitespace(t *testing.T) {
	got := canonicalOf(t, "{\n  \"b\": 1,\n  \"a\": \"x\"\n}")

	if got != pythonSimple {
		t.Fatalf("got %s", got)
	}
}

func TestRowHashMatchesTheServersHashForTheSameRow(t *testing.T) {
	payload, err := DecodeCanonical([]byte(pythonRowPayload))
	if err != nil {
		t.Fatal(err)
	}

	got, err := RowHash(RowDomainV1, payload)
	if err != nil {
		t.Fatal(err)
	}

	if got != pythonRowHash {
		t.Fatalf("row hash = %s, want %s", got, pythonRowHash)
	}
}

func TestRowHashChangesWhenTheDomainDoes(t *testing.T) {
	payload, _ := DecodeCanonical([]byte(pythonRowPayload))

	// Domain separation is the reason a signature over one payload type
	// cannot be replayed as another. If the domain were ignored, this would
	// come back equal.
	got, err := RowHash("preloop.audit.chain/v2\n", payload)
	if err != nil {
		t.Fatal(err)
	}

	if got == pythonRowHash {
		t.Fatal("the domain separator is not being hashed")
	}
}

func TestDigestOfMatchesTheServerForAManifest(t *testing.T) {
	manifest, _ := DecodeCanonical([]byte(pythonManifest))

	digest, err := DigestOf(manifest)
	if err != nil {
		t.Fatal(err)
	}

	if digest != pythonManifestDigest {
		t.Fatalf("digest = %s, want %s", digest, pythonManifestDigest)
	}
}

func pythonSignedDocument() SignatureDocument {
	return SignatureDocument{
		Schema:      "preloop.signature/v1",
		Algorithm:   AlgorithmEd25519,
		KeyID:       "key-1",
		PayloadType: PayloadPeriodExport,
		Digest:      pythonManifestDigest,
		SignedAt:    pythonSignedAt,
		Signature:   pythonSignature,
	}
}

func pythonKey() PublicKey {
	return PublicKey{KeyID: "key-1", Algorithm: AlgorithmEd25519, PublicKey: pythonPublicKey, Active: true}
}

func TestASignatureMadeByTheBackendVerifiesHere(t *testing.T) {
	if err := CheckSignature(pythonSignedDocument(), pythonKey(), pythonManifestDigest); err != nil {
		t.Fatalf("a genuine signature was refused: %v", err)
	}
}

func TestASignatureIsRefusedForOtherBytes(t *testing.T) {
	document := pythonSignedDocument()

	err := CheckSignature(document, pythonKey(), "ff"+pythonManifestDigest[2:])

	if err == nil {
		t.Fatal("a digest that is not the signed one was accepted")
	}
}

func TestASignatureCannotBeReplayedAsAnotherPayloadType(t *testing.T) {
	document := pythonSignedDocument()
	document.PayloadType = PayloadEvidencePack

	// The payload type is inside the signed bytes precisely so that a
	// manifest signature cannot be presented as an evidence pack signature.
	if err := CheckSignature(document, pythonKey(), pythonManifestDigest); err == nil {
		t.Fatal("a signature was accepted under a different payload type")
	}
}

func TestASignatureIsRefusedWhenTheTimestampIsChanged(t *testing.T) {
	document := pythonSignedDocument()
	document.SignedAt = "2026-09-11T12:00:00Z"

	if err := CheckSignature(document, pythonKey(), pythonManifestDigest); err == nil {
		t.Fatal("signed_at is not covered by the signature")
	}
}

func TestAnotherKeyDoesNotVerifyThisSignature(t *testing.T) {
	other := pythonKey()
	raw, _ := base64.StdEncoding.DecodeString(pythonPublicKey)
	raw[0] ^= 0xff
	other.PublicKey = base64.StdEncoding.EncodeToString(raw)

	if err := CheckSignature(pythonSignedDocument(), other, pythonManifestDigest); err == nil {
		t.Fatal("a wrong key verified the signature")
	}
}

func TestAMissingKeyIsNotAPass(t *testing.T) {
	err := CheckSignature(pythonSignedDocument(), PublicKey{}, pythonManifestDigest)

	if err == nil {
		t.Fatal("verification with no key must not succeed")
	}
}

func TestGarbageInThePlaceOfASignatureIsRefused(t *testing.T) {
	document := pythonSignedDocument()
	document.Signature = "not base64 at all !!"

	if err := CheckSignature(document, pythonKey(), pythonManifestDigest); err == nil {
		t.Fatal("a malformed signature was accepted")
	}
}

func TestDecodeCanonicalConsumesOneDocument(t *testing.T) {
	for _, raw := range []string{`{}[]`, `1 true`, `null false`, `{"a":1}garbage`} {
		t.Run(raw, func(t *testing.T) {
			if _, err := DecodeCanonical([]byte(raw)); err == nil {
				t.Fatal("trailing content was silently ignored")
			}
		})
	}
	if got := canonicalOf(t, " \n\t"+pythonSimple+"\r\n\t "); got != pythonSimple {
		t.Fatalf("valid whitespace changed canonical bytes: %q", got)
	}
}
