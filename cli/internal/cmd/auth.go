package cmd

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/telemetry"
	"github.com/preloop/preloop/cli/internal/version"
)

const (
	// OAuth paths
	authorizePath = "/oauth/authorize"
	tokenPath     = "/oauth/token"
	userInfoPath  = "/api/v1/users/me"

	cliOAuthClientID       = "cli"
	manualOAuthRedirectURI = "urn:ietf:wg:oauth:2.0:oob"
)

var errLoopbackCallbackUnavailable = errors.New("loopback callback unavailable")

// TokenResponse represents the OAuth token response.
type TokenResponse struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
	ExpiresIn    int    `json:"expires_in"`
	TokenType    string `json:"token_type"`
}

// UserInfo represents the authenticated user's information.
type UserInfo struct {
	ID           string `json:"id"`
	Email        string `json:"email"`
	Name         string `json:"name"`
	Organization string `json:"organization"`
}

// authCmd represents the auth command group.
var authCmd = &cobra.Command{
	Use:   "auth",
	Short: "Manage authentication",
	Long:  `Manage authentication with your Preloop account.`,
}

// authLoginCmd represents the auth login command.
var authLoginCmd = &cobra.Command{
	Use:   "login",
	Short: "Authenticate with Preloop",
	Long: `Authenticate with your Preloop account.

With --token: saves the provided API token directly (no server required).
Without --token: starts OAuth authentication. The CLI automatically uses a
loopback callback on local machines and a copy/paste flow on SSH or headless hosts.

Examples:
  preloop login --token <your-token>
  preloop login --token <your-token> --url http://localhost:8000
  PRELOOP_URL=https://review.preloop.ai preloop login --headless
  preloop login
  preloop login --headless`,
	RunE: runAuthLogin,
}

// loginCmd is a root-level alias for auth login.
var loginCmd = &cobra.Command{
	Use:   "login",
	Short: "Authenticate with Preloop",
	Long:  authLoginCmd.Long,
	RunE:  runAuthLogin,
}

// signupCmd opens the OAuth flow but lands the browser on the signup page so
// the user can create an account first. Once the account is created and the
// user is logged in, the existing OAuth consent flow takes over and finishes
// authenticating the CLI - the same loopback (or copy/paste) callback as
// 'preloop login'.
var signupCmd = &cobra.Command{
	Use:   "signup",
	Short: "Create a Preloop account and authenticate the CLI",
	Long: `Open the Preloop sign-up page in a browser and authenticate this CLI.

This is the same end-to-end OAuth flow as 'preloop login', except that the
browser is sent directly to the sign-up page instead of the sign-in page.
After the user creates their account (or signs in if they already have one),
the OAuth authorization page completes the CLI login automatically.

Examples:
  preloop signup
  PRELOOP_URL=https://review.preloop.ai preloop signup`,
	RunE: runAuthSignup,
}

// authSignupCmd is the 'preloop auth signup' subcommand alias for symmetry
// with 'preloop auth login'.
var authSignupCmd = &cobra.Command{
	Use:   "signup",
	Short: signupCmd.Short,
	Long:  signupCmd.Long,
	RunE:  runAuthSignup,
}

// authLogoutCmd represents the auth logout command.
var authLogoutCmd = &cobra.Command{
	Use:   "logout",
	Short: "Log out of Preloop",
	Long:  `Log out of your Preloop account and remove stored credentials.`,
	RunE:  runAuthLogout,
}

// authStatusCmd represents the auth status command.
var authStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show authentication status",
	Long:  `Show the current authentication status and logged-in user.`,
	RunE:  runAuthStatus,
}

// authTokenCmd represents the auth token command.
var authTokenCmd = &cobra.Command{
	Use:   "token",
	Short: "Print current access token",
	Long: `Print the current access token for use in scripts and automation.

This command outputs only the token with no additional formatting,
making it suitable for use in shell scripts or as input to other commands.

Examples:
  # Use token in a curl command
  curl -H "Authorization: Bearer $(preloop auth token)" http://localhost:8000/api/v1/users/me

  # Export as environment variable
  export PRELOOP_TOKEN=$(preloop auth token)`,
	RunE: runAuthToken,
}

var (
	loginToken    string
	loginHeadless bool
	loginLoopback bool
	loginCode     string
	// signupRequested tracks whether the current OAuth login flow was
	// initiated via 'preloop signup'. When true, the browser is sent to the
	// sign-up page first instead of the sign-in page.
	signupRequested bool
)

func init() {
	authCmd.AddCommand(authLoginCmd)
	authCmd.AddCommand(authSignupCmd)
	authCmd.AddCommand(authLogoutCmd)
	authCmd.AddCommand(authStatusCmd)
	authCmd.AddCommand(authTokenCmd)

	configureLoginFlags(authLoginCmd)
	configureLoginFlags(authSignupCmd)
	configureLoginFlags(loginCmd)
	configureLoginFlags(signupCmd)
}

// runAuthSignup is the entry point for 'preloop signup'. It mirrors
// runAuthLogin but flips the signup intent flag so that the OAuth authorize
// URL routes the browser to the sign-up page first.
func runAuthSignup(cmd *cobra.Command, args []string) error {
	signupRequested = true
	defer func() { signupRequested = false }()
	return runAuthLogin(cmd, args)
}

// runAuthLogin handles both token-based and OAuth login.
// If --token is provided, it saves the token directly.
// Otherwise, it falls back to the OAuth browser flow.
func runAuthLogin(cmd *cobra.Command, args []string) error {
	token := loginToken

	// Also accept the global --token flag as a fallback
	if token == "" {
		token = FlagToken
	}

	if token != "" {
		return runTokenLogin(token)
	}

	if loginLoopback && loginHeadless {
		return fmt.Errorf("--loopback and --headless cannot be used together")
	}
	if loginLoopback && loginCode != "" {
		return fmt.Errorf("--code can only be used with the headless login flow")
	}

	baseURL, err := resolveConfiguredAPIURL()
	if err != nil {
		return err
	}

	if shouldUseHeadlessOAuth() {
		return runHeadlessOAuthLogin(baseURL)
	}

	if err := runLoopbackOAuthLogin(baseURL); err != nil {
		if errors.Is(err, errLoopbackCallbackUnavailable) && !loginLoopback {
			fallbackReason := err
			if unwrapped := errors.Unwrap(err); unwrapped != nil {
				fallbackReason = unwrapped
			}
			fmt.Printf("Loopback callback unavailable: %v\n", fallbackReason)
			fmt.Println("Falling back to the headless copy/paste login flow...")
			return runHeadlessOAuthLogin(baseURL)
		}
		return err
	}

	return nil
}

// runTokenLogin saves a token directly to the config file.
func runTokenLogin(token string) error {
	apiURL, err := resolveConfiguredAPIURL()
	if err != nil {
		return err
	}

	client := api.NewClientWithToken(apiURL, token)
	var userInfo UserInfo
	if err := client.Get(userInfoPath, &userInfo); err != nil {
		if err := config.SetTokens(token, ""); err != nil {
			return fmt.Errorf("failed to save token: %w", err)
		}
		if err := config.SetAPIURL(apiURL); err != nil {
			return fmt.Errorf("failed to save API URL: %w", err)
		}

		fmt.Println("Token saved (could not verify — server may be unreachable)")
		fmt.Printf("  API URL: %s\n", apiURL)
		return nil
	}

	if err := config.SetTokens(token, ""); err != nil {
		return fmt.Errorf("failed to save token: %w", err)
	}
	if err := config.SetAPIURL(apiURL); err != nil {
		return fmt.Errorf("failed to save API URL: %w", err)
	}

	fmt.Println("Authenticated successfully!")
	fmt.Printf("  User:    %s (%s)\n", userInfo.Name, userInfo.Email)
	if userInfo.Organization != "" {
		fmt.Printf("  Org:     %s\n", userInfo.Organization)
	}
	fmt.Printf("  API URL: %s\n", apiURL)

	telemetry.SendConversion(apiURL, token, version.Version, "Login")

	return nil
}

func configureLoginFlags(command *cobra.Command) {
	command.Flags().StringVar(&loginToken, "token", "", "API access token (skip OAuth and save directly)")
	command.Flags().BoolVar(&loginHeadless, "headless", false, "use copy/paste OAuth for SSH or no-GUI environments")
	command.Flags().BoolVar(&loginLoopback, "loopback", false, "force the local loopback callback OAuth flow")
	command.Flags().StringVar(&loginCode, "code", "", "authorization code from a previous headless OAuth login")
}

func shouldUseHeadlessOAuth() bool {
	if loginHeadless || loginCode != "" {
		return true
	}
	if loginLoopback {
		return false
	}

	if isSSHSession() {
		return true
	}

	if runtime.GOOS == "linux" {
		return os.Getenv("DISPLAY") == "" && os.Getenv("WAYLAND_DISPLAY") == ""
	}

	return false
}

func isSSHSession() bool {
	return os.Getenv("SSH_CONNECTION") != "" ||
		os.Getenv("SSH_CLIENT") != "" ||
		os.Getenv("SSH_TTY") != ""
}

func buildAuthorizationURL(baseURL, redirectURI, state string) string {
	values := url.Values{}
	values.Set("client_id", cliOAuthClientID)
	values.Set("response_type", "code")
	values.Set("redirect_uri", redirectURI)
	values.Set("state", state)
	if signupRequested {
		// The frontend OAuth consent view honors 'signup=1' to send
		// unauthenticated users to the registration page instead of the
		// sign-in page.
		values.Set("signup", "1")
	}
	return fmt.Sprintf("%s%s?%s", baseURL, authorizePath, values.Encode())
}

func runLoopbackOAuthLogin(baseURL string) error {
	state, err := generateState()
	if err != nil {
		return fmt.Errorf("failed to generate state: %w", err)
	}

	codeChan := make(chan string, 1)
	errChan := make(chan error, 1)

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return fmt.Errorf("%w: %v", errLoopbackCallbackUnavailable, err)
	}

	successRedirectURL := buildPostAuthRedirectURL(baseURL)

	server := &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			handleOAuthCallback(w, r, state, successRedirectURL, codeChan, errChan)
		}),
	}
	go func() {
		if serveErr := server.Serve(listener); serveErr != nil && serveErr != http.ErrServerClosed {
			errChan <- fmt.Errorf("callback server error: %w", serveErr)
		}
	}()
	defer server.Shutdown(context.Background()) //nolint:errcheck

	addr, ok := listener.Addr().(*net.TCPAddr)
	if !ok {
		return fmt.Errorf("failed to determine callback address")
	}
	redirectURI := fmt.Sprintf("http://127.0.0.1:%d/callback", addr.Port)
	authURL := buildAuthorizationURL(baseURL, redirectURI, state)

	fmt.Println("Opening browser for authentication...")
	fmt.Printf("If the browser doesn't open, please visit:\n%s\n\n", authURL)

	if err := openBrowser(authURL); err != nil {
		fmt.Printf("Warning: Could not open browser: %v\n", err)
	}

	fmt.Println("Waiting for authentication...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	var code string
	select {
	case code = <-codeChan:
	case err := <-errChan:
		return err
	case <-ctx.Done():
		return fmt.Errorf("authentication timed out")
	}

	return finishOAuthLogin(baseURL, code, redirectURI)
}

func runHeadlessOAuthLogin(baseURL string) error {
	state, err := generateState()
	if err != nil {
		return fmt.Errorf("failed to generate state: %w", err)
	}

	redirectURI := manualOAuthRedirectURI
	authURL := buildAuthorizationURL(baseURL, redirectURI, state)

	code := strings.TrimSpace(loginCode)
	if code == "" {
		fmt.Println("Open this URL in a browser to sign in:")
		fmt.Println(authURL)
		fmt.Println()
		fmt.Println("After approving access, copy the one-time code shown by Preloop and paste it here.")

		if !stdinIsTerminal() {
			return fmt.Errorf(
				"cannot prompt for an authorization code without a terminal - rerun with --code <value>",
			)
		}

		reader := bufio.NewReader(os.Stdin)
		code, err = promptForTextInput(reader, os.Stdout, "Authorization code: ")
		if err != nil {
			return fmt.Errorf("failed to read authorization code: %w", err)
		}
	}
	if code == "" {
		return fmt.Errorf("authorization code is required")
	}

	return finishOAuthLogin(baseURL, code, redirectURI)
}

func finishOAuthLogin(baseURL, code, redirectURI string) error {
	fmt.Println("Exchanging code for tokens...")
	tokenResp, err := exchangeCodeForTokens(baseURL, code, redirectURI)
	if err != nil {
		return fmt.Errorf("failed to exchange code for tokens: %w", err)
	}

	client := api.NewClientWithToken(baseURL, tokenResp.AccessToken)
	var userInfo UserInfo
	if err := client.Get(userInfoPath, &userInfo); err != nil {
		if err := config.SetTokens(tokenResp.AccessToken, tokenResp.RefreshToken); err != nil {
			return fmt.Errorf("failed to save tokens: %w", err)
		}
		if err := config.SetAPIURL(baseURL); err != nil {
			return fmt.Errorf("failed to save API URL: %w", err)
		}

		fmt.Println("\nSuccessfully authenticated!")
		fmt.Printf("API URL: %s\n", baseURL)
		return nil
	}

	if err := config.SetTokens(tokenResp.AccessToken, tokenResp.RefreshToken); err != nil {
		return fmt.Errorf("failed to save tokens: %w", err)
	}
	if err := config.SetAPIURL(baseURL); err != nil {
		return fmt.Errorf("failed to save API URL: %w", err)
	}

	fmt.Println("\nSuccessfully authenticated!")
	fmt.Printf("Logged in as: %s (%s)\n", userInfo.Name, userInfo.Email)
	if userInfo.Organization != "" {
		fmt.Printf("Organization: %s\n", userInfo.Organization)
	}
	fmt.Printf("API URL: %s\n", baseURL)

	telemetry.SendConversion(
		baseURL, tokenResp.AccessToken, version.Version, conversionEventName(),
	)

	return nil
}

// conversionEventName maps the current auth flow to its analytics conversion
// (names match the web funnel goals byte-for-byte).
func conversionEventName() string {
	if signupRequested {
		return "Signup"
	}
	return "Login"
}

func stdinIsTerminal() bool {
	stat, err := os.Stdin.Stat()
	if err != nil {
		return false
	}
	return (stat.Mode() & os.ModeCharDevice) != 0
}

// runAuthLogout clears stored credentials.
func runAuthLogout(cmd *cobra.Command, args []string) error {
	if !config.IsAuthenticated() {
		fmt.Println("Not currently logged in")
		return nil
	}

	if err := config.Clear(); err != nil {
		return fmt.Errorf("failed to clear credentials: %w", err)
	}

	fmt.Println("Successfully logged out")
	return nil
}

// runAuthStatus shows the current authentication status.
func runAuthStatus(cmd *cobra.Command, args []string) error {
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to load config: %w", err)
	}

	if cfg.AccessToken == "" {
		fmt.Println("Not authenticated")
		fmt.Println("Run 'preloop login --token <your-token>' to authenticate")
		return nil
	}

	// Create API client and fetch user info
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to initialize API client: %w", err)
	}
	if FlagToken == "" && cfg.RefreshToken != "" {
		if err := client.RefreshAccessToken(); err != nil {
			fmt.Println("Authenticated (stored login could not be refreshed)")
			fmt.Printf("  API URL: %s\n", cfg.APIURL)
			fmt.Printf("  Error:   %v\n", err)
			fmt.Println("Run 'preloop login' to re-authenticate")
			return nil
		}
	}

	var userInfo UserInfo
	if err := client.Get(userInfoPath, &userInfo); err != nil {
		fmt.Println("Authenticated (token may be invalid or server unreachable)")
		fmt.Printf("  API URL: %s\n", cfg.APIURL)
		fmt.Printf("  Error:   %v\n", err)
		fmt.Println("Run 'preloop login --token <your-token>' to re-authenticate")
		return nil
	}

	fmt.Println("Authenticated")
	fmt.Printf("  User:    %s\n", userInfo.Name)
	fmt.Printf("  Email:   %s\n", userInfo.Email)
	if userInfo.Organization != "" {
		fmt.Printf("  Org:     %s\n", userInfo.Organization)
	}
	fmt.Printf("  API URL: %s\n", cfg.APIURL)

	return nil
}

// runAuthToken prints the current access token.
func runAuthToken(cmd *cobra.Command, args []string) error {
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to load config: %w", err)
	}

	if cfg.AccessToken == "" {
		return fmt.Errorf("not authenticated - run 'preloop login --token <your-token>' first")
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to initialize API client: %w", err)
	}
	if FlagToken == "" && cfg.RefreshToken != "" {
		if err := client.RefreshAccessToken(); err != nil {
			return fmt.Errorf("stored login expired - run 'preloop login' again: %w", err)
		}
	}

	// Print just the token with no newline for scripting
	fmt.Print(client.Token())
	return nil
}

// buildPostAuthRedirectURL returns the Preloop console URL the browser should
// land on after the loopback OAuth callback succeeds. This brings the user to
// the agents page so they can immediately see any onboarded agents instead of
// being left on a generic localhost success page.
func buildPostAuthRedirectURL(baseURL string) string {
	trimmed := strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if trimmed == "" {
		return ""
	}
	return trimmed + "/console/agents?cli=connected"
}

// handleOAuthCallback handles the OAuth callback from the browser.
//
// On success the browser is redirected to successRedirectURL (typically the
// Preloop console agents page) so the user lands somewhere useful instead of
// staring at a localhost page. If successRedirectURL is empty the legacy
// inline success card is rendered instead.
func handleOAuthCallback(w http.ResponseWriter, r *http.Request, expectedState string, successRedirectURL string, codeChan chan<- string, errChan chan<- error) {
	query := r.URL.Query()

	// Check for errors
	if errMsg := query.Get("error"); errMsg != "" {
		errDesc := query.Get("error_description")
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprintf(w, "<html><body><h1>Authentication Failed</h1><p>%s: %s</p><p>You can close this window.</p></body></html>", html.EscapeString(errMsg), html.EscapeString(errDesc)) //nolint:errcheck
		errChan <- fmt.Errorf("OAuth error: %s - %s", errMsg, errDesc)
		return
	}

	// Verify state
	state := query.Get("state")
	if state != expectedState {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprintf(w, "<html><body><h1>Authentication Failed</h1><p>Invalid state parameter</p><p>You can close this window.</p></body></html>") //nolint:errcheck
		errChan <- fmt.Errorf("invalid state parameter")
		return
	}

	// Get authorization code
	code := query.Get("code")
	if code == "" {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprintf(w, "<html><body><h1>Authentication Failed</h1><p>No authorization code received</p><p>You can close this window.</p></body></html>") //nolint:errcheck
		errChan <- fmt.Errorf("no authorization code received")
		return
	}

	if successRedirectURL != "" {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Location", successRedirectURL)
		w.WriteHeader(http.StatusFound)
		safeRedirectURL := html.EscapeString(successRedirectURL)
		_, _ = fmt.Fprintf(w, `<!doctype html>
<html><head>
<meta http-equiv="refresh" content="0; url=%s">
<title>Preloop CLI connected</title>
</head><body>
<p>CLI connected. <a href="%s">Continue to Preloop</a>...</p>
</body></html>`, safeRedirectURL, safeRedirectURL)
		codeChan <- code
		return
	}

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = fmt.Fprintf(w, `<html>
<head>
	<style>
		body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: linear-gradient(135deg, #667eea 0%%, #764ba2 100%%); }
		.card { background: white; padding: 40px; border-radius: 16px; text-align: center; box-shadow: 0 10px 40px rgba(0,0,0,0.2); }
		h1 { color: #333; margin-bottom: 10px; }
		p { color: #666; }
		.checkmark { font-size: 48px; margin-bottom: 20px; }
	</style>
</head>
<body>
	<div class="card">
		<div class="checkmark">✓</div>
		<h1>Authentication Successful</h1>
		<p>You can close this window and return to the terminal.</p>
	</div>
</body>
</html>`)

	codeChan <- code
}

// exchangeCodeForTokens exchanges the authorization code for access and refresh tokens.
func exchangeCodeForTokens(baseURL, code, redirectURI string) (*TokenResponse, error) {
	tokenURL := baseURL + tokenPath

	data := url.Values{}
	data.Set("grant_type", "authorization_code")
	data.Set("code", code)
	data.Set("redirect_uri", redirectURI)

	tokenReq, err := http.NewRequest(
		http.MethodPost, tokenURL, strings.NewReader(data.Encode()),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to build token request: %w", err)
	}
	tokenReq.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	version.SetClientIdentityHeaders(tokenReq.Header)

	resp, err := http.DefaultClient.Do(tokenReq)
	if err != nil {
		return nil, fmt.Errorf("token request failed: %w", err)
	}
	defer resp.Body.Close() //nolint:errcheck

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("token request failed with status %d", resp.StatusCode)
	}

	var tokenResp TokenResponse
	if err := json.NewDecoder(resp.Body).Decode(&tokenResp); err != nil {
		return nil, fmt.Errorf("failed to decode token response: %w", err)
	}

	return &tokenResp, nil
}

// generateState generates a random state string for CSRF protection.
func generateState() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.URLEncoding.EncodeToString(b), nil
}

// openBrowser opens the default browser to the specified URL.
func openBrowser(rawURL string) error {
	var cmd *exec.Cmd

	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", rawURL)
	case "linux":
		cmd = exec.Command("xdg-open", rawURL)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", rawURL)
	default:
		return fmt.Errorf("unsupported platform: %s", runtime.GOOS)
	}

	return cmd.Start()
}
