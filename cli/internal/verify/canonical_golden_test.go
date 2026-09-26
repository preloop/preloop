package verify

import (
	"encoding/json"
	"os"
	"testing"
)

// goldenCase is one fixture the Python sealer hashed. The payload is the
// JSON object; sha256 is sha256(row_domain + canonical JSON).
type goldenCase struct {
	Name      string          `json:"name"`
	Payload   json.RawMessage `json:"payload"`
	Canonical string          `json:"canonical"`
	SHA256    string          `json:"sha256"`
}

type goldenFile struct {
	RowDomain string       `json:"row_domain"`
	Cases     []goldenCase `json:"cases"`
}

func TestCanonicalGoldenMatchesPython(t *testing.T) {
	raw, err := os.ReadFile("testdata/canonical-golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var file goldenFile
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	if file.RowDomain != RowDomainV1 {
		t.Fatalf("row domain %q", file.RowDomain)
	}
	if len(file.Cases) == 0 {
		t.Fatal("golden file has no cases")
	}
	for _, item := range file.Cases {
		t.Run(item.Name, func(t *testing.T) {
			gotCanon, err := CanonicalFromRaw(item.Payload)
			if err != nil {
				t.Fatal(err)
			}
			if string(gotCanon) != item.Canonical {
				t.Fatalf("canonical\n got  %s\n want %s", gotCanon, item.Canonical)
			}
			payload, err := DecodeCanonical(item.Payload)
			if err != nil {
				t.Fatal(err)
			}
			gotHash, err := RowHash(file.RowDomain, payload)
			if err != nil {
				t.Fatal(err)
			}
			if gotHash != item.SHA256 {
				t.Fatalf("sha256 = %s, want %s", gotHash, item.SHA256)
			}
		})
	}
}

func TestSegmentDecodePreservesFractionalZero(t *testing.T) {
	// A segment as the API serves it: 0.0 is a float token, not an integer.
	raw := []byte(`{
		"account_id":"11111111-2222-4333-8444-555555555555",
		"row_domain":"preloop.audit.chain/v1\n",
		"after_seq":0,
		"head_seq":1,
		"pruned_below_seq":0,
		"genesis_hash":"0000000000000000000000000000000000000000000000000000000000000000",
		"entries":[{
			"seq":1,
			"row_id":"row-1",
			"prev_hash":"0000000000000000000000000000000000000000000000000000000000000000",
			"row_hash":"ignored",
			"payload":{"action":"model_gateway_request","details":{"budget":{"flow_current_spend_usd":0.0,"estimated_request_cost_usd":1.0,"note":null}},"seq":1}
		}],
		"has_more":false,
		"note":""
	}`)
	var segment Segment
	if err := json.Unmarshal(raw, &segment); err != nil {
		t.Fatal(err)
	}
	body, err := CanonicalJSON(segment.Entries[0].Payload)
	if err != nil {
		t.Fatal(err)
	}
	const want = `{"action":"model_gateway_request","details":{"budget":{"estimated_request_cost_usd":1.0,"flow_current_spend_usd":0.0,"note":null}},"seq":1}`
	if string(body) != want {
		t.Fatalf("canonical\n got  %s\n want %s", body, want)
	}
}
