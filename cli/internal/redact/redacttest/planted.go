// Package redacttest holds synthetic credentials for redaction tests.
package redacttest

import "strings"

// PlantedSecret is one synthetic credential used by tests in this package and
// by the collector's end-to-end tests. The values are assembled at run time so
// that no credential-shaped literal sits in the repository.
type PlantedSecret struct {
	// Class is the redaction class the brief for this feature names.
	Class string
	// Line is a line of text that carries the secret in a realistic position.
	Line string
	// Secret is the exact substring that must not survive redaction.
	Secret string
}

func join(parts ...string) string { return strings.Join(parts, "") }

// PlantedSecrets returns one or more synthetic secrets for every class the
// redactor claims to cover. None of them is a live credential.
func PlantedSecrets() []PlantedSecret {
	aws := join("AKIA", "Q3EGUYZ7", "LMNOPQ5X")
	github := join("ghp_", "x7Kq2LmN9pRs4TuV", "w1Xy3Za5Bc6De8Fg0HiJ")
	slack := join("xoxb-", "2847561930", "-", "5839201746352", "-", "kR7mPq2XvL9nTz4BwY6cJh8D")
	stripe := join("sk_", "live_", "4eC39HqLyjWDarjtT1zdp7dcQ9XbN2mK")
	anthropic := join("sk-ant-", "api03-", "q7RkX2mVp9LzT4nYw6BcJ8hDf3GsA5uE1oPiN0xZvKtMrQyWbHgCdFjSlUaOeIX-kR7mPq2XvL9nTz4BwY6c", "-", "J8hDfQAA")
	jwt := join("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUgRG9lIn0", ".", "Xk7mPq2LvR9nTz4BwY6cJh8DfGs3A5uE1oPiN0xZvKt")
	bearer := join("q7RkX2mVp9", "LzT4nYw6Bc", "J8hDf3GsA5")
	basic := join("amFuZTpodW50", "ZXIyLXNlY3JldA==")
	urlPassword := join("s3cr3t", "-Pa55", "-w0rd")
	weak := join("hunter", "2")
	yamlSecret := join("Tr0ub4dor", "-and-3")
	entropy := join("Zk9xQ2mV7pLr4TnY", "w6BcJ8hDf3GsA5uE", "1oPiN0xZ")
	hex := join("9f86d081884c7d65", "9a2feaa0c55ad015", "a3bf4f1b2b0b822c", "d15d6c15b0f00a08")
	return []PlantedSecret{
		{Class: "known credential format (aws)", Line: "aws configure set aws_access_key_id " + aws, Secret: aws},
		{Class: "known credential format (github)", Line: "gh auth login --with-token <<< " + github, Secret: github},
		{Class: "known credential format (slack)", Line: "slack_token: " + slack, Secret: slack},
		{Class: "known credential format (stripe)", Line: "stripe.api_key = \"" + stripe + "\"", Secret: stripe},
		{Class: "known credential format (anthropic)", Line: "export ANTHROPIC_API_KEY=" + anthropic, Secret: anthropic},
		{Class: "known credential format (jwt)", Line: "session cookie value " + jwt + " was captured", Secret: jwt},
		{Class: "authorization header (bearer)", Line: "curl -H \"Authorization: Bearer " + bearer + "\" https://api.example.com/v1/items", Secret: bearer},
		{Class: "authorization header (basic)", Line: "Authorization: Basic " + basic, Secret: basic},
		{Class: "authorization header (api key)", Line: "X-Api-Key: " + bearer[:20] + "zz", Secret: bearer[:20] + "zz"},
		{Class: "url with embedded credentials", Line: "DATABASE_URL=postgres://app_user:" + urlPassword + "@db.internal.example.com:5432/app", Secret: urlPassword},
		{Class: "url with embedded token", Line: "git clone https://" + github + "@github.com/example/repo.git", Secret: github},
		{Class: "secret-named assignment (low entropy)", Line: "export DB_PASSWORD=" + weak, Secret: weak},
		{Class: "secret-named assignment (json)", Line: "  \"clientSecret\": \"" + yamlSecret + "\",", Secret: yamlSecret},
		{Class: "secret-named assignment (yaml)", Line: "api_token: " + yamlSecret + "9", Secret: yamlSecret + "9"},
		{Class: "high-entropy token", Line: "use the value " + entropy + " when the service asks", Secret: entropy},
		{Class: "high-entropy token (hex)", Line: "signing material: " + hex, Secret: hex},
	}
}

// PlantedPrivateKey returns a synthetic PEM private key block. A text that
// contains it must be withheld entirely.
func PlantedPrivateKey() string {
	body := strings.Repeat("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n", 4)
	return join("-----BEGIN ", "RSA PRIVATE KEY", "-----\n", body, "-----END ", "RSA PRIVATE KEY", "-----\n")
}
