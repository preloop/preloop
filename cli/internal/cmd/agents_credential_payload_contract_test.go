package cmd

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"testing"
	"time"
)

// These tests pin the CLI's credential_payload producers to the server's
// write-time contract in backend/preloop/schemas/ai_model.py
// (validate_credential_payload). The server answers 422 to a payload that
// breaks it, so onboarding and sync-credentials must always satisfy it.

var serverOAuthPayloadContract = map[string]struct {
	required []string
	optional []string
}{
	"oauth_openai_codex": {
		required: []string{"access", "refresh", "account_id", "expires"},
	},
	"oauth_anthropic_claude_code": {
		required: []string{"access"},
		optional: []string{"refresh", "expires"},
	},
}

// maxServerOAuthPayloadEpochMillis mirrors the server's _MAX_EPOCH_MILLIS
// (year 5138): larger values are micro- or nanoseconds and are rejected.
const maxServerOAuthPayloadEpochMillis int64 = 100_000_000_000_000

var serverOAuthPayloadAliases = []string{"access_token", "refresh_token", "expires_at"}

// serverOAuthPayloadProblems mirrors the server rule on the JSON wire form of
// a payload and returns every violation, sorted.
func serverOAuthPayloadProblems(t *testing.T, credentialType string, payload map[string]interface{}) []string {
	t.Helper()
	contract, ok := serverOAuthPayloadContract[credentialType]
	if !ok {
		t.Fatalf("no server contract for %q", credentialType)
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	var wire map[string]interface{}
	if err := decoder.Decode(&wire); err != nil {
		t.Fatalf("decode payload: %v", err)
	}

	problems := []string{}
	for _, alias := range serverOAuthPayloadAliases {
		if _, present := wire[alias]; present {
			problems = append(problems, "alias "+alias)
		}
	}
	for _, key := range contract.required {
		if _, present := wire[key]; !present {
			problems = append(problems, "missing "+key)
		}
	}
	for _, key := range append(append([]string{}, contract.required...), contract.optional...) {
		value, present := wire[key]
		if !present {
			continue
		}
		if key == "expires" {
			number, isNumber := value.(json.Number)
			parsed, intErr := number.Int64()
			switch {
			case !isNumber || intErr != nil:
				problems = append(problems, "expires not an integer")
			case parsed < minOAuthPayloadEpochMillis || parsed > maxServerOAuthPayloadEpochMillis:
				problems = append(problems, fmt.Sprintf("expires %d not epoch millis", parsed))
			}
			continue
		}
		text, isString := value.(string)
		if !isString || strings.TrimSpace(text) == "" {
			problems = append(problems, key+" empty")
		}
	}
	sort.Strings(problems)
	return problems
}

func TestCodexOAuthPayloadFromAuthJSONSatisfiesServerContract(t *testing.T) {
	expiry := time.Now().UTC().Add(time.Hour).Unix()
	access := codexTestJWT(t, map[string]interface{}{
		"exp": expiry,
		"https://api.openai.com/auth": map[string]interface{}{
			"chatgpt_account_id": "chatgpt-account",
		},
	})
	blob := fmt.Sprintf(
		`{"tokens":{"access_token":%q,"refresh_token":"codex-refresh","id_token":"codex-id"}}`,
		access,
	)
	credential := parseCodexOAuthCredentialBlob([]byte(blob), 0)
	if credential == nil {
		t.Fatal("expected a parsed Codex credential")
	}

	payload := credential.Payload()

	if problems := serverOAuthPayloadProblems(t, "oauth_openai_codex", payload); len(problems) > 0 {
		t.Fatalf("Codex payload breaks the server contract: %v (payload keys %v)", problems, sortedKeys(payload))
	}
	if payload["expires"] != expiry*1000 {
		t.Fatalf("expires = %v, want %d", payload["expires"], expiry*1000)
	}
}

func TestCodexOAuthPayloadScalesSecondsExpiry(t *testing.T) {
	credential := &codexOAuthCredential{
		AccessToken:  "codex-access",
		RefreshToken: "codex-refresh",
		ExpiresAtMS:  1_893_456_000,
		AccountID:    "chatgpt-account",
	}

	payload := credential.Payload()

	if problems := serverOAuthPayloadProblems(t, "oauth_openai_codex", payload); len(problems) > 0 {
		t.Fatalf("Codex payload breaks the server contract: %v", problems)
	}
	if payload["expires"] != int64(1_893_456_000_000) {
		t.Fatalf("expires = %v, want seconds scaled to millis", payload["expires"])
	}
}

func TestCodexOAuthPayloadWithoutAccountIDIsRejectedByServerContract(t *testing.T) {
	// The server needs account_id for every upstream call; a token without the
	// ChatGPT account claim cannot be used, so the push must fail loudly.
	credential := &codexOAuthCredential{
		AccessToken:  "codex-access",
		RefreshToken: "codex-refresh",
		ExpiresAtMS:  time.Now().UTC().Add(time.Hour).UnixMilli(),
	}

	problems := serverOAuthPayloadProblems(t, "oauth_openai_codex", credential.Payload())

	if len(problems) != 1 || problems[0] != "missing account_id" {
		t.Fatalf("problems = %v, want only missing account_id", problems)
	}
}

func TestClaudeOAuthPayloadFromCredentialsFileSatisfiesServerContract(t *testing.T) {
	expiresAt := time.Now().UTC().Add(time.Hour).UnixMilli()
	blob := fmt.Sprintf(
		`{"claudeAiOauth":{"accessToken":"sk-ant-oat01-live","refreshToken":"sk-ant-ort01-live","expiresAt":%d}}`,
		expiresAt,
	)
	credential := parseClaudeOAuthCredentialBlob(blob, 0)
	if credential == nil {
		t.Fatal("expected a parsed Claude credential")
	}

	payload := credential.Payload()

	if problems := serverOAuthPayloadProblems(t, "oauth_anthropic_claude_code", payload); len(problems) > 0 {
		t.Fatalf("Claude payload breaks the server contract: %v", problems)
	}
	if payload["expires"] != expiresAt {
		t.Fatalf("expires = %v, want %d", payload["expires"], expiresAt)
	}
}

func TestClaudeOAuthPayloadScalesSecondsExpiresAt(t *testing.T) {
	blob := `{"access_token":"sk-ant-oat01-live","refresh_token":"sk-ant-ort01-live","expires_at":1893456000}`
	credential := parseClaudeOAuthCredentialBlob(blob, 0)
	if credential == nil {
		t.Fatal("expected a parsed Claude credential")
	}

	payload := credential.Payload()

	if problems := serverOAuthPayloadProblems(t, "oauth_anthropic_claude_code", payload); len(problems) > 0 {
		t.Fatalf("Claude payload breaks the server contract: %v", problems)
	}
	if payload["expires"] != int64(1_893_456_000_000) {
		t.Fatalf("expires = %v, want seconds scaled to millis", payload["expires"])
	}
}

func TestServerContractMirrorRejectsImplausibleExpiry(t *testing.T) {
	for name, expires := range map[string]int64{
		"seconds":      1_893_456_000,
		"microseconds": 1_893_456_000_000_000,
	} {
		payload := map[string]interface{}{"access": "sk-ant-oat01-live", "expires": expires}
		problems := serverOAuthPayloadProblems(t, "oauth_anthropic_claude_code", payload)
		if len(problems) != 1 || !strings.Contains(problems[0], "not epoch millis") {
			t.Fatalf("%s: problems = %v, want one expiry problem", name, problems)
		}
	}
}

func TestClaudeAccessOnlyPayloadsSatisfyServerContract(t *testing.T) {
	// Onboarding falls back to {"access": token} when only a bare long-lived
	// OAuth token is available, and Payload() omits empty refresh/expiry.
	for name, payload := range map[string]map[string]interface{}{
		"onboarding fallback": {"access": "sk-ant-oat01-long-lived"},
		"payload without refresh": (&claudeOAuthCredential{
			AccessToken: "sk-ant-oat01-long-lived",
		}).Payload(),
	} {
		if problems := serverOAuthPayloadProblems(t, "oauth_anthropic_claude_code", payload); len(problems) > 0 {
			t.Fatalf("%s breaks the server contract: %v", name, problems)
		}
	}
}

func sortedKeys(payload map[string]interface{}) []string {
	keys := make([]string, 0, len(payload))
	for key := range payload {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
