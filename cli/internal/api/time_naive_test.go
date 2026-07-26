package api

import (
	"encoding/json"
	"testing"
)

func TestAPITimeParsesNaiveAndRFC3339(t *testing.T) {
	cases := []string{
		`"2026-07-25T22:09:24.940820"`,
		`"2026-07-25T22:09:24"`,
		`"2026-07-25T22:09:24.940820Z"`,
		`"2026-07-25T22:09:24+02:00"`,
		`null`,
		`""`,
	}
	for _, c := range cases {
		var ts Time
		if err := json.Unmarshal([]byte(c), &ts); err != nil {
			t.Errorf("failed to parse %s: %v", c, err)
		}
	}
	var ts Time
	if err := json.Unmarshal([]byte(`"2026-07-25T22:09:24.940820"`), &ts); err != nil {
		t.Fatal(err)
	}
	if ts.IsZero() || ts.Location().String() != "UTC" {
		t.Errorf("naive timestamp should parse as UTC, got %v", ts)
	}
}
