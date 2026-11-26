package redact

import (
	"regexp"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/redact/redacttest"
)

func mustRedact(t *testing.T, input string, opts Options) Result {
	t.Helper()
	result, err := Text(input, opts)
	if err != nil {
		t.Fatalf("Text: %v", err)
	}
	return result
}

func TestVendoredGitleaksRulesLoad(t *testing.T) {
	count, err := RuleCount()
	if err != nil {
		t.Fatalf("rules: %v", err)
	}
	// The vendored file defines 222 rules; one is path-only and finds no text.
	if count != 221 {
		t.Fatalf("active gitleaks rules = %d, want 221", count)
	}
}

func TestPlantedSecretsOfEveryClassAreRedacted(t *testing.T) {
	for _, planted := range redacttest.PlantedSecrets() {
		t.Run(planted.Class, func(t *testing.T) {
			input := "# Deploy helper\n\nRun this first:\n\n" + planted.Line + "\n\nThen continue.\n"
			result := mustRedact(t, input, Options{Path: "SKILL.md"})
			if result.Withheld != "" {
				t.Fatalf("unexpectedly withheld: %s", result.Withheld)
			}
			if strings.Contains(result.Text, planted.Secret) {
				t.Fatalf("secret survived redaction: %q", result.Text)
			}
			if result.Total == 0 || !strings.Contains(result.Text, "[REDACTED:") {
				t.Fatalf("no typed placeholder written: %q", result.Text)
			}
			if !strings.Contains(result.Text, "# Deploy helper") || !strings.Contains(result.Text, "Then continue.") {
				t.Fatalf("surrounding text was lost: %q", result.Text)
			}
		})
	}
}

func TestAllPlantedSecretsInOneFile(t *testing.T) {
	var lines []string
	for _, planted := range redacttest.PlantedSecrets() {
		lines = append(lines, planted.Line)
	}
	result := mustRedact(t, strings.Join(lines, "\n"), Options{Path: "scripts/deploy.sh"})
	for _, planted := range redacttest.PlantedSecrets() {
		if strings.Contains(result.Text, planted.Secret) {
			t.Errorf("%s: secret survived in combined file", planted.Class)
		}
	}
	sum := 0
	for kind, count := range result.Counts {
		if !regexp.MustCompile(`^[a-z0-9-]+$`).MatchString(kind) {
			t.Errorf("placeholder type %q is not a plain identifier", kind)
		}
		sum += count
	}
	if sum != result.Total || strings.Count(result.Text, "[REDACTED:") != result.Total {
		t.Fatalf("counts %v (total %d) do not match %d placeholders", result.Counts, result.Total, strings.Count(result.Text, "[REDACTED:"))
	}
}

func TestPlaceholderNamesTheRule(t *testing.T) {
	cases := map[string]string{
		"known credential format (aws)":         "aws-access-token",
		"known credential format (github)":      "github-pat",
		"authorization header (basic)":          TypeAuthorizationHeader,
		"url with embedded credentials":         TypeURLCredentials,
		"secret-named assignment (low entropy)": TypeSecretAssignment,
		"high-entropy token":                    TypeHighEntropy,
	}
	for _, planted := range redacttest.PlantedSecrets() {
		want, ok := cases[planted.Class]
		if !ok {
			continue
		}
		result := mustRedact(t, planted.Line, Options{})
		if result.Counts[want] == 0 {
			t.Errorf("%s: counts %v, want a %s placeholder", planted.Class, result.Counts, want)
		}
	}
}

func TestPrivateKeyBlockWithholdsTheWholeText(t *testing.T) {
	for name, input := range map[string]string{
		"complete block":  "notes\n" + redacttest.PlantedPrivateKey() + "more\n",
		"truncated block": "-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----\nb3BlbnNzaC1rZXk\n",
		"putty":           "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\n",
	} {
		result := mustRedact(t, input, Options{})
		if result.Withheld != WithheldPrivateKey || result.Text != "" {
			t.Errorf("%s: withheld=%q text=%q", name, result.Withheld, result.Text)
		}
	}
}

func TestRedactionIsDeterministic(t *testing.T) {
	var lines []string
	for _, planted := range redacttest.PlantedSecrets() {
		lines = append(lines, planted.Line)
	}
	input := strings.Join(lines, "\n")
	first := mustRedact(t, input, Options{Path: "a.md", Home: "/Users/jane"})
	for i := 0; i < 5; i++ {
		again := mustRedact(t, input, Options{Path: "a.md", Home: "/Users/jane"})
		if again.Text != first.Text || again.Total != first.Total {
			t.Fatal("redaction output changed between runs")
		}
	}
}

func TestOrdinarySkillTextIsLeftReadable(t *testing.T) {
	input := strings.Join([]string{
		"---",
		"name: careful",
		"description: Safety guardrails for destructive commands.",
		"---",
		"# Careful",
		"",
		"Set `GITHUB_TOKEN=$GITHUB_TOKEN` or pass `--token <token>` before running.",
		"The request sends `Authorization: Bearer ${API_TOKEN}` to https://api.example.com/v1.",
		"Use `export API_KEY=\"$(pass show api)\"` and keep max_tokens: 4096.",
		"Password: required for the first login.",
		"author: Jane Doe",
		"Clone with git@github.com:example/repo.git or https://github.com/example/repo.git.",
		"Run scripts/check-everything-before-deploy.sh then read docs/guide/agent-discovery-reporting.md.",
	}, "\n")
	result := mustRedact(t, input, Options{Path: "SKILL.md"})
	if result.Text != input {
		t.Fatalf("readable text was altered (%v):\n%s", result.Counts, result.Text)
	}
}

func TestHomePathsBecomePlaceholders(t *testing.T) {
	input := "bash /Users/jane/.claude/hooks/guard.sh && cat /home/sam/notes.txt && type C:\\Users\\Jane\\x.txt"
	result := mustRedact(t, input, Options{Home: "/Users/jane"})
	for _, leaked := range []string{"jane", "sam", "Jane"} {
		if strings.Contains(result.Text, leaked) {
			t.Errorf("login %q survived: %s", leaked, result.Text)
		}
	}
	if result.Counts[TypeHomePath] != 3 {
		t.Fatalf("home-path count = %d, want 3 (%s)", result.Counts[TypeHomePath], result.Text)
	}
	if !strings.Contains(result.Text, "/.claude/hooks/guard.sh") {
		t.Fatalf("path below the home directory was lost: %s", result.Text)
	}
}

func TestOverlappingFindingsCollapse(t *testing.T) {
	planted := redacttest.PlantedSecrets()[1]
	input := "GITHUB_TOKEN=" + planted.Secret
	result := mustRedact(t, input, Options{})
	if result.Total != 1 || result.Counts["github-pat"] != 1 {
		t.Fatalf("want one github-pat placeholder, got %v in %q", result.Counts, result.Text)
	}
	if result.Text != "GITHUB_TOKEN=[REDACTED:github-pat]" {
		t.Fatalf("unexpected text %q", result.Text)
	}
}

func TestGitleaksAllowlistsAreHonoured(t *testing.T) {
	// The generic rule ignores values that are plain identifiers, and the
	// global allowlist ignores variable references.
	input := "api_key = some_function_name\ntoken = ${TOKEN}\n"
	result := mustRedact(t, input, Options{})
	if strings.Contains(result.Text, "[REDACTED:generic-api-key]") {
		t.Fatalf("allowlisted value flagged by gitleaks rule: %q", result.Text)
	}
}

// The shapes below came out of a security review of the first version of the
// redactor: each was uploaded unredacted.
func TestSecretShapesFoundInReview(t *testing.T) {
	secret := "hunter2" + "secret"
	cases := map[string][2]string{
		"oauth scheme":           {"Authorization: OAuth 2f9c1aab" + "77e04d21", "2f9c1aab77e04d21"},
		"bot scheme":             {"Authorization: Bot hunter2" + "hunter2xyz", "hunter2hunter2xyz"},
		"short vendor scheme":    {"Authorization: SSWS 00abc" + "DEFghi", "00abcDEFghi"},
		"python annotation":      {"password: str = \"" + secret + "\"", secret},
		"typescript annotation":  {"const token: string = \"" + secret + "\";", secret},
		"triple quotes":          {"PASSWORD = \"\"\"" + secret + "\"\"\"", secret},
		"backticks":              {"const password = `" + secret + "`", secret},
		"hash rocket":            {"'password' => '" + secret + "',", secret},
		"tilde value":            {"DB_PASSWORD=~Tilde" + "123!", "~Tilde123!"},
		"bytes literal":          {"password = b\"" + secret + "\"", secret},
		"escaped quote":          {"PASSWORD=\"abc\\\"REAL" + "PART99\"", "REALPART99"},
		"short yaml value":       {"password: hunter" + "2", "hunter2"},
		"letters only yaml":      {"db_password: correct" + "horsebattery", "correcthorsebattery"},
		"unclosed angle":         {"password=\"<S3cret" + "Passw0rd\"", "S3cretPassw0rd"},
		"at sign in url":         {"postgres://admin:p@ssw0" + "rdXY@db.example.com/x", "ssw0rdXY"},
		"slash in url":           {"postgres://admin:pa/ssw0" + "rd@db.example.com/x", "pa/ssw0rd"},
		"armor with tab":         {"-----BEGIN\tRSA PRIVATE KEY-----\nMIIEvQ\n", "MIIEvQ"},
		"armor split over lines": {"-----BEGIN OPENSSH\nPRIVATE KEY-----\nb3Blbn\n", "b3Blbn"},
		"armor with two spaces":  {"-----BEGIN  EC  PRIVATE  KEY-----\nMHcCAQ\n", "MHcCAQ"},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			result := mustRedact(t, "# Notes\n\n"+tc[0]+"\n\nMore text.\n", Options{Path: "SKILL.md"})
			if strings.Contains(result.Text, tc[1]) {
				t.Fatalf("secret survived: %q", result.Text)
			}
			if result.Withheld == "" && result.Total == 0 {
				t.Fatalf("nothing was redacted: %q", result.Text)
			}
		})
	}
}

func TestGitleaksPathAllowlistsDoNotSwitchRulesOff(t *testing.T) {
	// gitleaks skips whole directories in a repository scan. The collector
	// has already chosen the file, so its name must not disable any rule.
	key := "AKIA" + "Z5GMXQ7T" + "LPW3HRBD"
	for _, name := range []string{"SKILL.md", "node_modules/x/README.md", "vendor/github.com/a/b.go", "assets/logo.svg", "package-lock.json"} {
		result := mustRedact(t, "key "+key+" end", Options{Path: name})
		if strings.Contains(result.Text, key) {
			t.Errorf("%s: a credential survived because of the file name", name)
		}
	}
}

func TestReadableShapesStayAfterTheStricterRules(t *testing.T) {
	input := strings.Join([]string{
		"password: required",
		"token: string",
		"api_key: str",
		"secret: true",
		"Password: required for the first login.",
		"if [[ \"$token\" =~ ^abc ]]; then echo ok; fi",
		"if token == other: pass",
		"Install from https://unpkg.com:443/pkg@1.2.3/index.js or https://example.com/a@b.",
		"export API_TOKEN=${API_TOKEN}",
		"token = <your-token>",
		"Authorization: Bearer $TOKEN",
		"max_tokens = 4096",
	}, "\n")
	result := mustRedact(t, input, Options{Path: "SKILL.md"})
	if result.Text != input {
		t.Fatalf("readable text was altered (%v):\n%s", result.Counts, result.Text)
	}
}
