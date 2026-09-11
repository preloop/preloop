package verify

import (
	"bytes"
	"encoding/json"
	"reflect"
	"testing"
)

// FuzzCanonicalJSON exercises the bytes accepted from audit/evidence exports.
// A successful parse must consume one valid JSON document; canonicalization
// must preserve the decoded value and produce stable bytes for hashing.
func FuzzCanonicalJSON(f *testing.F) {
	f.Fuzz(func(t *testing.T, raw []byte) {
		if len(raw) > 64*1024 {
			t.Skip()
		}
		value, err := DecodeCanonical(raw)
		if err != nil {
			if json.Valid(raw) {
				t.Fatalf("valid JSON was rejected: %v", err)
			}
			return
		}
		if !json.Valid(raw) {
			t.Fatal("decoder accepted malformed or trailing JSON")
		}
		canonical, err := CanonicalJSON(value)
		if err != nil || !json.Valid(canonical) {
			t.Fatalf("canonicalization did not produce valid JSON: %v", err)
		}
		decoded, err := DecodeCanonical(canonical)
		if err != nil || !reflect.DeepEqual(value, decoded) {
			t.Fatalf("canonicalization changed the decoded value: %v", err)
		}
		again, err := CanonicalFromRaw(canonical)
		if err != nil || !bytes.Equal(canonical, again) {
			t.Fatalf("canonicalization is not idempotent: %v", err)
		}
	})
}
