package cmd

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestExtensionDetailsEnvelopeDowngrade(t *testing.T) {
	for _, accepted := range []int{3, 2} {
		t.Run(fmt.Sprint(accepted), func(t *testing.T) {
			extensions := []inventoryExtension{{Kind: "skill", ID: "careful", Details: &inventoryExtensionDetails{Description: "Review changes"}}}
			report := discoveryReportRequest{Candidates: []discoveryReportCandidate{{Extensions: &extensions}}, Evidence: &inventoryReportEvidence{SchemaVersion: 4, Applications: []inventoryReportApplication{{Extensions: &extensions}}}}
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				var got discoveryReportRequest
				if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
					t.Error(err)
					return
				}
				if got.Evidence.SchemaVersion > accepted {
					w.WriteHeader(422)
					fmt.Fprint(w, `{"detail":[{"loc":["body","evidence","schema_version"],"type":"literal_error"}]}`)
					return
				}
				if accepted == 3 {
					if (*got.Candidates[0].Extensions)[0].Details != nil || (*got.Evidence.Applications[0].Extensions)[0].Details != nil {
						t.Error("details survived downgrade")
					}
				} else if got.Candidates[0].Extensions != nil {
					t.Error("v3 fields survived v2 downgrade")
				}
				fmt.Fprint(w, `{"received":1}`)
			}))
			defer server.Close()
			if err := postInventoryReport(api.NewClientWithToken(server.URL, "synthetic-token"), report, &discoveryReportResponse{}); err != nil {
				t.Fatal(err)
			}
			if calls != 5-accepted || report.Evidence.SchemaVersion != 4 || extensions[0].Details == nil {
				t.Fatal("downgrade mutated caller or retried incorrectly")
			}
		})
	}
}
