package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestRunnerDiscoveryContentPrivacy(t *testing.T) {
	for _, mode := range []string{"enabled", "device", "flag", "environment", "account", "excluded", "all-users", "offline"} {
		t.Run(mode, func(t *testing.T) {
			resetInventoryCommand(t)
			home, secrets := contentHome(t)
			testenv.SetHome(t, home)
			oldProbes, oldProfiles := inventoryProbes, inventoryProfiles
			inventoryProbes = inventoryTestDependencies(t, home)
			inventoryProfiles.Elevated = func() bool { return mode == "all-users" }
			inventoryProfiles.Profiles = func() ([]inventoryProfile, bool, error) {
				return []inventoryProfile{{Home: home}}, false, nil
			}
			t.Cleanup(func() {
				inventoryProbes, inventoryProfiles = oldProbes, oldProfiles
				discoveryNoExtensionContent = false
				runnerFingerprint.Store("")
			})
			switch mode {
			case "device":
				writeInventoryFixture(t, home, ".preloop/config.yaml", "discovery:\n  extension_content: false\n")
			case "flag":
				discoveryNoExtensionContent = true
			case "environment":
				t.Setenv(contentDisableEnv, "1")
			case "excluded":
				writeInventoryFixture(t, home, ".preloop/config.yaml", "discovery:\n  extension_content_exclude: ['*']\n")
			}
			content := newContentServer(t)
			content.collect = mode != "account"
			if mode == "offline" {
				content.status = 503
			}
			reports := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer runner-only" {
					t.Error("content used another credential")
				}
				prefix := api.RESTPrefix + "/runners/fixture-runner/"
				switch r.URL.Path {
				case prefix + "discovery-reports":
					reports++
					w.WriteHeader(202)
					_ = json.NewEncoder(w).Encode(map[string]int{"received": 1})
				case prefix + "extension-content/missing":
					r.URL.Path = contentMissingPath
					content.server.Config.Handler.ServeHTTP(w, r)
				case prefix + "extension-content":
					r.URL.Path = contentUploadPath
					content.server.Config.Handler.ServeHTTP(w, r)
				default:
					t.Errorf("unexpected route: %s", r.URL.Path)
					http.NotFound(w, r)
				}
			}))
			defer server.Close()
			client := runnerDiscoveryClient{api.NewClientWithToken(server.URL, "runner-only"), "fixture-runner"}
			err := runRunnerDiscoveryReport(client, testDiscoverySalt, "fixture-device")
			if (err != nil) != (mode == "offline") {
				t.Fatalf("unexpected report error: %v", err)
			}
			if reports != 1 {
				t.Fatal("missing inventory report")
			}
			wantUpload := mode == "enabled" || mode == "all-users" || mode == "excluded"
			if (len(content.uploads) > 0) != wantUpload {
				t.Fatalf("content uploads = %d, expected upload = %v", len(content.uploads), wantUpload)
			}
			for _, secret := range secrets {
				if bytes.Contains(content.all(), []byte(secret)) {
					t.Fatal("planted secret reached runner upload")
				}
			}
			if mode == "excluded" && bytes.Contains(content.all(), []byte("Read before running.")) {
				t.Fatal("excluded content was uploaded")
			}
			if mode == "device" || mode == "flag" || mode == "environment" {
				if len(content.queries) != 0 {
					t.Fatal("device opt-out negotiated content")
				}
			}
		})
	}
}
