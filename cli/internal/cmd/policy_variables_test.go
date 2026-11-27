package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
)

func TestPolicyApplyVariables(t *testing.T) {
	for _, extension := range []string{"yaml", "json"} {
		for _, dryRun := range []bool{false, true} {
			for _, preserve := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/dry=%t/preserve=%t", extension, dryRun, preserve), func(t *testing.T) {
					t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
					testenv.SetTempHome(t)
					value := "quoted \"value\": first\nsecond\\line ${UNCHANGED}"
					t.Setenv("PRELOOP_POLICY_TEST_VALUE", value)
					t.Setenv("PRELOOP_POLICY_EMPTY", "")
					if preserve {
						if err := os.Unsetenv("PRELOOP_POLICY_TEST_VALUE"); err != nil {
							t.Fatal(err)
						}
					}
					content := `{"metadata":{"name":"${PRELOOP_POLICY_TEST_VALUE}"},"tools":[],"extra":["${PRELOOP_POLICY_EMPTY}","$PATH",123,true]}`
					file := filepath.Join(t.TempDir(), "policy."+extension)
					if err := os.WriteFile(file, []byte(content), 0600); err != nil {
						t.Fatal(err)
					}
					requests := 0
					server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						if r.URL.Path == policiesDiffPath {
							// Apply previews removals first; no changes here.
							_, _ = io.WriteString(w, `{"has_changes":false,"summary":""}`)
							return
						}
						requests++
						wantPath := policiesUploadPath
						if dryRun {
							wantPath = policiesValidatePath
						}
						if r.URL.Path != wantPath {
							t.Errorf("path = %s, want %s", r.URL.Path, wantPath)
						}
						if err := r.ParseMultipartForm(1 << 20); err != nil {
							t.Error(err)
							return
						}
						defer r.MultipartForm.RemoveAll()
						if !dryRun && (r.FormValue("dry_run") != "false" || r.FormValue("resolve_env") != "false") {
							t.Errorf("unexpected form fields: %v", r.Form)
						}
						part, _, err := r.FormFile("file")
						if err != nil {
							t.Error(err)
							return
						}
						defer part.Close()
						body, _ := io.ReadAll(part)
						var got struct {
							Metadata struct{ Name string }
							Extra    []interface{}
						}
						if extension == "json" {
							err = json.Unmarshal(body, &got)
						} else {
							err = yaml.Unmarshal(body, &got)
						}
						if err != nil {
							t.Errorf("decode upload: %v", err)
						}
						want, empty := value, ""
						if preserve {
							want, empty = "${PRELOOP_POLICY_TEST_VALUE}", "${PRELOOP_POLICY_EMPTY}"
							if string(body) != content {
								t.Error("unresolved upload changed the original file")
							}
						}
						if got.Metadata.Name != want || len(got.Extra) != 4 || got.Extra[0] != empty || got.Extra[1] != "$PATH" || got.Extra[3] != true {
							t.Errorf("unexpected uploaded values: %+v", got)
						}
						w.Header().Set("Content-Type", "application/json")
						_, _ = io.WriteString(w, `{"success":true,"is_valid":true,"policy_name":"Example"}`)
					}))
					defer server.Close()
					originalURL, originalToken := FlagURL, FlagToken
					FlagURL, FlagToken = server.URL, "test-token"
					t.Cleanup(func() { FlagURL, FlagToken = originalURL, originalToken })
					cmd := &cobra.Command{}
					cmd.Flags().Bool("dry-run", dryRun, "")
					cmd.Flags().Bool("no-resolve-env", preserve, "")
					if err := runPolicyApply(cmd, []string{file}); err != nil {
						t.Fatal(err)
					}
					if requests != 1 {
						t.Fatalf("requests = %d, want 1", requests)
					}
				})
			}
		}
	}
}

func TestPolicyApplyUnsetVariables(t *testing.T) {
	for _, dryRun := range []bool{false, true} {
		t.Run(fmt.Sprint(dryRun), func(t *testing.T) {
			t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
			testenv.SetTempHome(t)
			for _, name := range []string{"PRELOOP_POLICY_MISSING_A", "PRELOOP_POLICY_MISSING_B"} {
				t.Setenv(name, "")
				if err := os.Unsetenv(name); err != nil {
					t.Fatal(err)
				}
			}
			file := filepath.Join(t.TempDir(), "policy.yaml")
			if err := os.WriteFile(file, []byte("metadata:\n  name: ${PRELOOP_POLICY_MISSING_A}/${PRELOOP_POLICY_MISSING_B}\n"), 0600); err != nil {
				t.Fatal(err)
			}
			cmd := &cobra.Command{}
			cmd.Flags().Bool("dry-run", dryRun, "")
			cmd.Flags().Bool("no-resolve-env", false, "")
			err := runPolicyApply(cmd, []string{file})
			if err == nil || !strings.Contains(err.Error(), "PRELOOP_POLICY_MISSING_A") || !strings.Contains(err.Error(), "PRELOOP_POLICY_MISSING_B") {
				t.Fatalf("expected unset variable names, got %v", err)
			}
		})
	}
}

func TestPolicyApplyVariableFlag(t *testing.T) {
	if flag := policyApplyCmd.Flags().Lookup("no-resolve-env"); flag == nil || flag.DefValue != "false" {
		t.Fatal("apply must offer --no-resolve-env, disabled by default")
	}
}

func TestPolicyVariableScalarValues(t *testing.T) {
	for _, value := range []string{"true", "123", "null", "", "${PRELOOP_POLICY_OTHER}"} {
		t.Run(value, func(t *testing.T) {
			t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
			t.Setenv("PRELOOP_POLICY_TEST_VALUE", value)
			t.Setenv("PRELOOP_POLICY_OTHER", "second-value")
			content := []byte("metadata:\n  name: &name ${PRELOOP_POLICY_TEST_VALUE}\n  description: *name\n  tags: [\"${PRELOOP_POLICY_TEST_VALUE}\"]\n")
			resolved, err := resolvePolicyVariables(content, "policy.yaml")
			if err != nil {
				t.Fatal(err)
			}
			var got map[string]map[string]interface{}
			if err := yaml.Unmarshal(resolved, &got); err != nil {
				t.Fatal(err)
			}
			metadata := got["metadata"]
			if metadata["name"] != value || metadata["description"] != value || metadata["tags"].([]interface{})[0] != value {
				t.Fatalf("string values changed type or resolved twice: %v", metadata)
			}
		})
	}
}

func TestPolicyVariablesKeepJSONNumbers(t *testing.T) {
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	t.Setenv("PRELOOP_POLICY_TEST_VALUE", "example")
	content := []byte(`{"metadata":{"name":"${PRELOOP_POLICY_TEST_VALUE}"},"number":123456789012345678901234567890}`)
	resolved, err := resolvePolicyVariables(content, "policy.json")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(resolved), `"number":123456789012345678901234567890`) {
		t.Fatalf("JSON number changed: %s", resolved)
	}
}

func TestPolicyDiffVariables(t *testing.T) {
	for _, preserve := range []bool{false, true} {
		t.Run(fmt.Sprint(preserve), func(t *testing.T) {
			t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
			testenv.SetTempHome(t)
			t.Setenv("PRELOOP_POLICY_TEST_VALUE", "operator-value")
			file := filepath.Join(t.TempDir(), "policy.yaml")
			if err := os.WriteFile(file, []byte("metadata:\n  name: ${PRELOOP_POLICY_TEST_VALUE}\n"), 0600); err != nil {
				t.Fatal(err)
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != policiesDiffPath {
					t.Errorf("path = %s, want diff", r.URL.Path)
				}
				part, _, err := r.FormFile("file")
				if err != nil {
					t.Error(err)
					return
				}
				defer part.Close()
				defer r.MultipartForm.RemoveAll()
				body, _ := io.ReadAll(part)
				want := "operator-value"
				if preserve {
					want = "${PRELOOP_POLICY_TEST_VALUE}"
				}
				if !strings.Contains(string(body), want) {
					t.Errorf("diff input = %s, want %s", body, want)
				}
				w.Header().Set("Content-Type", "application/json")
				_, _ = io.WriteString(w, `{"has_changes":false,"changes":[],"summary":"No changes"}`)
			}))
			defer server.Close()
			originalURL, originalToken := FlagURL, FlagToken
			FlagURL, FlagToken = server.URL, "test-token"
			t.Cleanup(func() { FlagURL, FlagToken = originalURL, originalToken })
			cmd := &cobra.Command{}
			cmd.Flags().Bool("no-resolve-env", preserve, "")
			if err := runPolicyDiff(cmd, []string{file}); err != nil {
				t.Fatal(err)
			}
		})
	}
	if flag := policyDiffCmd.Flags().Lookup("no-resolve-env"); flag == nil || flag.DefValue != "false" {
		t.Fatal("diff must offer --no-resolve-env, disabled by default")
	}
}

func TestPolicyDryRunValidationDiagnostics(t *testing.T) {
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	testenv.SetTempHome(t)
	file := filepath.Join(t.TempDir(), "policy.yaml")
	if err := os.WriteFile(file, []byte("tools: []\n"), 0600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == policiesDiffPath {
			_, _ = io.WriteString(w, `{"has_changes":false,"summary":""}`)
			return
		}
		if r.URL.Path != policiesValidatePath {
			t.Errorf("path = %s, want validation", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"is_valid":false,"errors":[{"path":"$.metadata","message":"Field required","value":null}]}`)
	}))
	defer server.Close()
	originalURL, originalToken := FlagURL, FlagToken
	FlagURL, FlagToken = server.URL, "test-token"
	t.Cleanup(func() { FlagURL, FlagToken = originalURL, originalToken })
	cmd := &cobra.Command{}
	cmd.Flags().Bool("dry-run", true, "")
	cmd.Flags().Bool("no-resolve-env", false, "")
	var runErr error
	output := captureStdout(t, func() error {
		runErr = runPolicyApply(cmd, []string{file})
		return nil
	})
	if runErr == nil || runErr.Error() != "validation failed" {
		t.Fatalf("expected validation error, got %v", runErr)
	}
	if !strings.Contains(output, "  Field required\n") || strings.Contains(output, `"errors"`) {
		t.Fatalf("expected readable diagnostic, got %s", output)
	}
}
