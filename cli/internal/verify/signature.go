package verify

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
)

// The signature wire format, documented here because a verifier has to
// reproduce it byte for byte.
const (
	SignatureDomain      = "preloop.signature/v1\n"
	PayloadPeriodExport  = "preloop.retention.period_export_manifest/v1"
	PayloadEvidencePack  = "preloop.cra.evidence_manifest/v1"
	PayloadAuditCheckpnt = "preloop.audit.chain_checkpoint/v1"
	AlgorithmEd25519     = "ed25519"
)

// ErrNoKey is returned when nothing published a public key for the key id a
// signature names. It is a distinct error because it is not a failed
// verification: it is a verification that could not be performed.
var ErrNoKey = errors.New("no public key for this key id")

// SignatureDocument is a detached signature as it appears in signature.json,
// on a receipt, or beside a checkpoint.
type SignatureDocument struct {
	Schema      string                 `json:"schema"`
	Algorithm   string                 `json:"algorithm"`
	KeyID       string                 `json:"key_id"`
	PayloadType string                 `json:"payload_type"`
	Digest      string                 `json:"digest"`
	SignedAt    string                 `json:"signed_at"`
	Signature   string                 `json:"signature"`
	Payload     map[string]interface{} `json:"payload,omitempty"`
}

// PublicKey is one published key, public half only.
type PublicKey struct {
	KeyID     string `json:"key_id"`
	Algorithm string `json:"algorithm"`
	PublicKey string `json:"public_key"`
	Active    bool   `json:"active"`
	CreatedAt string `json:"created_at"`
	RetiredAt string `json:"retired_at"`
}

// KeyList is the response of the signing keys endpoint.
type KeyList struct {
	ActiveKeyID       string      `json:"active_key_id"`
	SignatureSchema   string      `json:"signature_schema"`
	SignedBytesFormat string      `json:"signed_bytes_format"`
	Keys              []PublicKey `json:"keys"`
}

// Find returns the published key with this id.
func (l KeyList) Find(keyID string) (PublicKey, bool) {
	for _, key := range l.Keys {
		if key.KeyID == keyID {
			return key, true
		}
	}
	return PublicKey{}, false
}

// SignedBytes rebuilds the exact bytes a signature covers.
func SignedBytes(payloadType, digest, signedAt string) []byte {
	return []byte(SignatureDomain + payloadType + "\n" + digest + "\n" + signedAt)
}

// DigestOf is the sha256 over the canonical JSON of a payload, which is the
// only thing a Preloop signature ever covers.
func DigestOf(payload interface{}) (string, error) {
	body, err := CanonicalJSON(payload)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:]), nil
}

// DigestOfBytes is the sha256 of bytes exactly as they are held, used for the
// manifest of a period export, which is signed as packed.
func DigestOfBytes(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

// CheckSignature verifies a detached signature against a published key.
//
// expectedDigest is what the caller computed for itself. Passing "" skips
// that comparison and checks only that the document is internally consistent,
// which proves far less: a well formed signature over a digest nobody
// recomputed says only that we signed something.
func CheckSignature(doc SignatureDocument, key PublicKey, expectedDigest string) error {
	if doc.Algorithm != AlgorithmEd25519 {
		return fmt.Errorf("unsupported signature algorithm %q", doc.Algorithm)
	}
	if key.PublicKey == "" {
		return ErrNoKey
	}
	if expectedDigest != "" && doc.Digest != expectedDigest {
		return fmt.Errorf(
			"the signature covers digest %s but the bytes here digest to %s",
			doc.Digest, expectedDigest,
		)
	}
	raw, err := base64.StdEncoding.DecodeString(key.PublicKey)
	if err != nil {
		return fmt.Errorf("published public key is not base64: %w", err)
	}
	if len(raw) != ed25519.PublicKeySize {
		return fmt.Errorf("published public key is %d bytes, want %d", len(raw), ed25519.PublicKeySize)
	}
	signature, err := base64.StdEncoding.DecodeString(doc.Signature)
	if err != nil {
		return fmt.Errorf("signature is not base64: %w", err)
	}
	if !ed25519.Verify(ed25519.PublicKey(raw), SignedBytes(doc.PayloadType, doc.Digest, doc.SignedAt), signature) {
		return errors.New("signature does not verify against this key")
	}
	return nil
}
