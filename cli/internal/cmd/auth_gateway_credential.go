package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

// authGatewayCredentialCmd is the Claude Desktop inference credential helper.
// Desktop reads stdout (trimmed) as a single bare token, so this command must
// never print anything else to stdout. Diagnostics go to stderr.
var authGatewayCredentialCmd = &cobra.Command{
	Use:   "gateway-credential",
	Short: "Print a per-user Preloop API key for a model gateway client",
	Long: `Print the signed-in user's Preloop API key for a model gateway client, in
the format a credential helper must print (a single bare token on stdout).

The key is minted on first use, cached in ~/.preloop/gateway-credentials with
mode 0600, and re-minted when it was revoked. Claude Desktop runs this command
through inferenceCredentialHelper when it is configured by
'preloop agents onboard "Claude Desktop" --model-route direct'.

Exits non-zero, with nothing on stdout, when you are not signed in.

Example:
  preloop auth gateway-credential --client claude-desktop`,
	Args: cobra.NoArgs,
	RunE: runAuthGatewayCredential,
}

var supportedGatewayCredentialClients = map[string]string{
	"claude-desktop": "Claude Desktop",
}

type cachedGatewayCredential struct {
	KeyID  string `json:"key_id"`
	Key    string `json:"key"`
	APIURL string `json:"api_url"`
	Client string `json:"client"`
}

func init() {
	authGatewayCredentialCmd.Flags().String("client", "", "gateway client the credential is for (claude-desktop)")
	authCmd.AddCommand(authGatewayCredentialCmd)
}

func isGatewayCredentialCommand(cmd *cobra.Command) bool {
	return cmd != nil && cmd.Name() == "gateway-credential" && cmd.Parent() != nil && cmd.Parent().Name() == "auth"
}

func runAuthGatewayCredential(cmd *cobra.Command, _ []string) error {
	clientName, _ := cmd.Flags().GetString("client")
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to load config: %w", err)
	}
	if strings.TrimSpace(cfg.AccessToken) == "" {
		return errors.New("not signed in: run `preloop login` first")
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	dir, err := config.GetConfigDir()
	if err != nil {
		return err
	}
	token, err := gatewayCredential(client, filepath.Join(dir, "gateway-credentials"), clientName, time.Now, os.Stderr)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(cmd.OutOrStdout(), token)
	return err
}

// gatewayCredential returns a cached per-user key, verifying it still exists,
// or mints a new one. stderr receives diagnostics only.
func gatewayCredential(client *api.Client, cacheDir, clientName string, now func() time.Time, stderr io.Writer) (string, error) {
	label, ok := supportedGatewayCredentialClients[strings.TrimSpace(clientName)]
	if !ok {
		return "", fmt.Errorf("--client must be one of: claude-desktop (got %q)", clientName)
	}
	cachePath := filepath.Join(cacheDir, clientName+".json")
	baseURL := strings.TrimRight(client.BaseURL(), "/")

	if cached, err := loadCachedGatewayCredential(cachePath); err == nil && cached.Key != "" && cached.APIURL == baseURL {
		var summary map[string]interface{}
		err := client.Get("/api/v1/auth/api-keys/"+url.PathEscape(cached.KeyID), &summary)
		if err == nil {
			return cached.Key, nil
		}
		var apiErr *api.APIError
		if !errors.As(err, &apiErr) {
			// Preloop is unreachable: hand Desktop the cached key rather than
			// failing; the gateway call will report the real problem.
			fmt.Fprintf(stderr, "preloop: could not verify the cached %s key: %v\n", label, err)
			return cached.Key, nil
		}
		if apiErr.StatusCode != http.StatusNotFound {
			return "", fmt.Errorf("could not verify the cached %s key: %w", label, err)
		}
		fmt.Fprintf(stderr, "preloop: cached %s key %s was revoked; minting a new one\n", label, cached.KeyID)
	}

	host, _ := os.Hostname()
	name := fmt.Sprintf("%s gateway (%s) %s", label, strings.TrimSpace(host), now().UTC().Format("20060102-150405"))
	if len(name) > 100 {
		name = name[:100]
	}
	var created struct {
		ID  string `json:"id"`
		Key string `json:"key"`
	}
	if err := client.Post("/api/v1/auth/api-keys", map[string]interface{}{"name": name, "scopes": []string{}}, &created); err != nil {
		return "", fmt.Errorf("could not mint a %s gateway key: %w", label, err)
	}
	if created.Key == "" {
		return "", fmt.Errorf("the server returned no key value for %s", created.ID)
	}
	if err := saveCachedGatewayCredential(cachePath, cachedGatewayCredential{KeyID: created.ID, Key: created.Key, APIURL: baseURL, Client: clientName}); err != nil {
		fmt.Fprintf(stderr, "preloop: could not cache the %s key: %v\n", label, err)
	}
	return created.Key, nil
}

func loadCachedGatewayCredential(path string) (cachedGatewayCredential, error) {
	var cached cachedGatewayCredential
	data, err := os.ReadFile(path)
	if err != nil {
		return cached, err
	}
	err = json.Unmarshal(data, &cached)
	return cached, err
}

func saveCachedGatewayCredential(path string, cached cachedGatewayCredential) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.Marshal(cached)
	if err != nil {
		return err
	}
	return routeWriteFile(path, data, 0o600)
}
