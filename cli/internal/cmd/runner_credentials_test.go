package cmd

import (
	"encoding/base64"
	"net/url"
	"strings"
	"testing"
)

func TestRunnerMasksEncodedExecutionCredentials(t *testing.T) {
	secret := "synthetic-runner/credential+value=123456789"
	forms := []string{secret, base64.StdEncoding.EncodeToString([]byte(secret)), base64.RawURLEncoding.EncodeToString([]byte(secret)), url.QueryEscape(secret), url.PathEscape(secret)}
	b := &runnerLogBuffer{secrets: []string{secret}}
	for _, value := range forms {
		_, _ = b.Write([]byte(value[:7]))
		_, _ = b.Write([]byte(value[7:] + "\n"))
	}
	b.finish()
	for _, line := range b.pending {
		if strings.TrimSpace(line) != "[REDACTED]" {
			t.Fatal("output was not masked")
		}
	}
}

func TestRunnerBootstrapWrapsImageSetup(t *testing.T) {
	args := dockerRunArgs("example.com/image:tag", map[string]string{"PRELOOP_OUTPUT_BOOTSTRAP": "wrapper"}, runnerDockerOpts{Launch: true, PreserveEntrypoint: true})
	if !strings.Contains(strings.Join(args, " "), "--entrypoint python3") {
		t.Fatal("image setup must pass through the output wrapper")
	}
}

func TestHostCodexExcludesExecutionSecretsFromCommands(t *testing.T) {
	installCodexHostHook(t)
	t.Setenv("PRELOOP_URL", "https://example.com")
	args, _, _, _, err := prepareCodexHostExec(hostExecProfile{}, map[string]any{"prompt": "task"}, t.TempDir(), t.TempDir(), "synthetic-scoped-token")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(args, " "), "shell_environment_policy.exclude=") {
		t.Fatal("missing shell credential exclusions")
	}
}

func TestRunnerMasksToolEncodingVariantsAndStatusOutput(t *testing.T) {
	secret := "synthetic-runner/credential+value=with space"
	encoded := strings.ReplaceAll(url.QueryEscape(secret), "+", "%20")
	lowerEscapes := strings.NewReplacer("%2F", "%2f", "%2B", "%2b", "%3D", "%3d").Replace(encoded)
	for _, value := range []string{encoded, lowerEscapes} {
		if maskRunnerCredentials(value, []string{secret}) != "[REDACTED]" {
			t.Fatal("URL-encoded credential was not masked")
		}
	}
	buffer := &runnerLogBuffer{secrets: []string{secret}}
	buffer.note("status " + secret)
	if len(buffer.pending) != 1 || buffer.pending[0] != "status [REDACTED]" {
		t.Fatal("runner status output was not masked")
	}
}
