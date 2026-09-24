package cmd

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

const (
	copilotCommand     = "copilot"
	copilotInstallHint = "npm install -g @github/copilot"

	copilotProviderOpenAI    = "openai"
	copilotProviderAnthropic = "anthropic"

	copilotEnvProviderType = "COPILOT_PROVIDER_TYPE"
	copilotEnvProviderURL  = "COPILOT_PROVIDER_BASE_URL"
	copilotEnvProviderKey  = "COPILOT_PROVIDER_API_KEY"
	copilotEnvModel        = "COPILOT_MODEL"
)

// Named failure modes for the launcher. Missing binary, credential, or model
// must not fall through to an unconfigured `copilot` process (that would use
// GitHub-hosted models).
var (
	errCopilotBinaryMissing     = errors.New("copilot binary not found")
	errCopilotCredentialMissing = errors.New("copilot credential missing")
	errCopilotModelMissing      = errors.New("copilot model alias missing")
)

// copilotCmd launches the GitHub Copilot CLI under Preloop BYOK.
//
// Interactive mode is a TTY passthrough: stdin/stdout/stderr stay attached.
// Extra args after Preloop flags are passed through to `copilot`. Global
// Preloop flags (--token, --url) belong before `copilot`, same as `preloop
// cursor`.
var copilotCmd = &cobra.Command{
	Use:   "copilot [flags] [copilot-args...]",
	Short: "Run GitHub Copilot CLI through the Preloop model gateway",
	Long: `Run the GitHub Copilot CLI (copilot) with BYOK environment variables
pointed at the Preloop model gateway.

Sets COPILOT_PROVIDER_TYPE, COPILOT_PROVIDER_BASE_URL, COPILOT_PROVIDER_API_KEY,
and COPILOT_MODEL before exec. OpenAI-family aliases use {PRELOOP_URL}/openai/v1;
Anthropic-family aliases use {PRELOOP_URL}/anthropic. The API key is a Preloop
bearer credential (managed-agent durable key when enrolled, otherwise the
current login token) — never a raw upstream provider key.

  preloop copilot
  preloop copilot --model openai/gpt-5
  preloop copilot --model anthropic/claude-sonnet-4-5 --provider anthropic
  preloop --url https://preloop.example.com copilot --model openai/gpt-5

--model is required when no enrolled Copilot CLI managed-agent alias exists
(onboarding is separate; see preloop agents onboard "Copilot CLI"). --provider
forces openai or anthropic; otherwise an alias whose normalized form starts
with anthropic/ selects the Anthropic base, and everything else defaults to
openai.

Missing copilot on PATH, missing credential, or missing model alias exits with
a named error and does not launch Copilot.

copilot flags after 'copilot' are passed through. Global Preloop flags
(--token, --url) belong before 'copilot'.`,
	Args:               cobra.ArbitraryArgs,
	DisableFlagParsing: true,
	SilenceErrors:      true,
	RunE:               runCopilotLauncher,
}

// lookupEnrolledCopilotModelAlias is a seam for tests. Production resolves
// LatestModelAlias from a managed agent named like "Copilot CLI".
var lookupEnrolledCopilotModelAlias = defaultLookupEnrolledCopilotModelAlias

// resolveCopilotCredential is a seam for tests. Production uses the same
// login resolution path as `preloop cursor` (flag / env / config).
var resolveCopilotCredential = defaultResolveCopilotCredential

// resolveCopilotBaseURL is a seam for tests.
var resolveCopilotBaseURL = defaultResolveCopilotBaseURL

func runCopilotLauncher(cmd *cobra.Command, args []string) error {
	if len(args) == 1 && (args[0] == "--help" || args[0] == "-h") {
		return cmd.Help()
	}
	opts, err := parseCopilotArgs(args)
	if err != nil {
		if errors.Is(err, errCopilotHelp) {
			return cmd.Help()
		}
		fmt.Fprintln(cmd.ErrOrStderr(), err)
		return err
	}
	launch, err := prepareCopilotLaunch(opts)
	if err != nil {
		fmt.Fprintln(cmd.ErrOrStderr(), err)
		return err
	}
	return printCopilotPreloopError(
		cmd.ErrOrStderr(),
		runCopilot(launch.Bin, launch.Args, launch.Env, os.Stdin, os.Stdout, os.Stderr),
	)
}

// printCopilotPreloopError writes Preloop-side failures to stderr. Child
// wrapProcessExit errors stay silent: copilot already wrote its diagnostics.
func printCopilotPreloopError(w io.Writer, err error) error {
	if err == nil {
		return nil
	}
	var coded *processExitError
	if errors.As(err, &coded) {
		return err
	}
	fmt.Fprintln(w, err)
	return err
}

// errCopilotHelp is returned when the user asked for Preloop's help rather
// than copilot's.
var errCopilotHelp = errors.New("copilot help")

type copilotOptions struct {
	model    string
	provider string
	args     []string
}

type copilotLaunch struct {
	Bin  string
	Args []string
	Env  map[string]string
}

func parseCopilotArgs(args []string) (copilotOptions, error) {
	opts := copilotOptions{}
	passthrough := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "--" {
			passthrough = append(passthrough, args[i+1:]...)
			break
		}
		if arg == "--help" || arg == "-h" {
			return opts, errCopilotHelp
		}
		name, value, hasValue := splitCursorRunFlag(arg)
		switch name {
		case "--model", "--provider":
			if !hasValue {
				i++
				if i >= len(args) {
					return opts, fmt.Errorf("%s requires a value", name)
				}
				value = args[i]
			}
			switch name {
			case "--model":
				opts.model = value
			case "--provider":
				opts.provider = value
			}
		default:
			passthrough = append(passthrough, arg)
		}
	}
	opts.args = passthrough
	return opts, nil
}

func prepareCopilotLaunch(opts copilotOptions) (copilotLaunch, error) {
	bin, err := findCopilot()
	if err != nil {
		return copilotLaunch{}, err
	}
	apiKey, err := resolveCopilotCredential()
	if err != nil {
		return copilotLaunch{}, err
	}
	if strings.TrimSpace(apiKey) == "" {
		return copilotLaunch{}, fmt.Errorf(
			"%w: log in with `preloop login` or pass --token / PRELOOP_TOKEN",
			errCopilotCredentialMissing,
		)
	}
	model, err := resolveCopilotModelAlias(opts.model)
	if err != nil {
		return copilotLaunch{}, err
	}
	baseURL, err := resolveCopilotBaseURL()
	if err != nil {
		return copilotLaunch{}, err
	}
	env, err := buildCopilotProviderEnv(baseURL, apiKey, model, opts.provider)
	if err != nil {
		return copilotLaunch{}, err
	}
	return copilotLaunch{Bin: bin, Args: opts.args, Env: env}, nil
}

func findCopilot() (string, error) {
	path, err := resolveRuntimeExecutable(copilotCommand)
	if err != nil {
		return "", fmt.Errorf(
			"%w on %s; install the GitHub Copilot CLI with: %s",
			errCopilotBinaryMissing,
			runtimeExecutableSearchDescription(copilotCommand),
			copilotInstallHint,
		)
	}
	return path, nil
}

func resolveCopilotModelAlias(explicit string) (string, error) {
	if model := strings.TrimSpace(explicit); model != "" {
		return model, nil
	}
	alias, err := lookupEnrolledCopilotModelAlias()
	if err != nil {
		return "", err
	}
	if model := strings.TrimSpace(alias); model != "" {
		return model, nil
	}
	return "", fmt.Errorf(
		"%w: pass --model <gateway-alias>, or onboard Copilot CLI so a managed alias is recorded",
		errCopilotModelMissing,
	)
}

func defaultResolveCopilotCredential() (string, error) {
	hookToken := ""
	if cred, err := resolvePermissionHookCredential(permissionSourceCopilotCLI); err == nil {
		hookToken = strings.TrimSpace(cred.Token)
	}
	loginToken := ""
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err == nil {
		loginToken = strings.TrimSpace(cfg.AccessToken)
	}
	chosen := selectCopilotAPIKey(explicitCopilotToken(), hookToken, loginToken)
	if chosen == "" {
		if err != nil {
			return "", fmt.Errorf("%w: %v", errCopilotCredentialMissing, err)
		}
		return "", fmt.Errorf(
			"%w: log in with `preloop login` or pass --token / PRELOOP_TOKEN",
			errCopilotCredentialMissing,
		)
	}
	return chosen, nil
}

// explicitCopilotToken is the operator's deliberate identity: the --token
// flag, then PRELOOP_TOKEN. A saved login and the enrolled hook credential
// are not explicit.
func explicitCopilotToken() string {
	if token := strings.TrimSpace(FlagToken); token != "" {
		return token
	}
	return strings.TrimSpace(os.Getenv("PRELOOP_TOKEN"))
}

// selectCopilotAPIKey picks the bearer sent as COPILOT_PROVIDER_API_KEY.
// An explicit flag or PRELOOP_TOKEN wins over the enrolled hook credential,
// which wins over the saved login token.
func selectCopilotAPIKey(explicit, hookToken, loginToken string) string {
	if token := strings.TrimSpace(explicit); token != "" {
		return token
	}
	if token := strings.TrimSpace(hookToken); token != "" {
		return token
	}
	return strings.TrimSpace(loginToken)
}

func defaultResolveCopilotBaseURL() (string, error) {
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil {
		return "", err
	}
	base := strings.TrimRight(strings.TrimSpace(cfg.APIURL), "/")
	if base == "" {
		base = strings.TrimRight(api.DefaultBaseURL, "/")
	}
	return base, nil
}

func defaultLookupEnrolledCopilotModelAlias() (string, error) {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil || !client.IsAuthenticated() {
		return "", nil
	}
	agents, err := listManagedAgents(client)
	if err != nil {
		// Network / API failures are not a hard stop when --model can still
		// be supplied; without an enrolled alias the caller reports
		// errCopilotModelMissing.
		return "", nil
	}
	for _, agent := range agents {
		if !isCopilotCLIManagedAgent(agent) {
			continue
		}
		if alias := strings.TrimSpace(agent.LatestModelAlias); alias != "" {
			return alias, nil
		}
	}
	return "", nil
}

func isCopilotCLIManagedAgent(agent managedAgentSummary) bool {
	return strings.EqualFold(strings.TrimSpace(agent.DisplayName), copilotCLIAgentName)
}

// buildCopilotProviderEnv constructs the four BYOK variables Copilot CLI
// reads. providerFlag is openai, anthropic, or empty (infer from alias).
func buildCopilotProviderEnv(baseURL, apiKey, model, providerFlag string) (map[string]string, error) {
	if strings.TrimSpace(apiKey) == "" {
		return nil, fmt.Errorf(
			"%w: log in with `preloop login` or pass --token / PRELOOP_TOKEN",
			errCopilotCredentialMissing,
		)
	}
	if strings.TrimSpace(model) == "" {
		return nil, fmt.Errorf(
			"%w: pass --model <gateway-alias>",
			errCopilotModelMissing,
		)
	}
	provider, err := resolveCopilotProviderType(providerFlag, model)
	if err != nil {
		return nil, err
	}
	return map[string]string{
		copilotEnvProviderType: provider,
		copilotEnvProviderURL:  copilotProviderBaseURL(baseURL, provider),
		copilotEnvProviderKey:  strings.TrimSpace(apiKey),
		copilotEnvModel:        strings.TrimSpace(model),
	}, nil
}

func resolveCopilotProviderType(providerFlag, modelAlias string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(providerFlag)) {
	case "":
		// Infer only when the alias itself names the Anthropic provider
		// prefix (after stripping an optional preloop/ gateway prefix).
		// Do not guess from model product names.
		normalized := strings.ToLower(normalizeGatewayModelAlias(modelAlias))
		if normalized == copilotProviderAnthropic ||
			strings.HasPrefix(normalized, copilotProviderAnthropic+"/") {
			return copilotProviderAnthropic, nil
		}
		return copilotProviderOpenAI, nil
	case copilotProviderOpenAI:
		return copilotProviderOpenAI, nil
	case copilotProviderAnthropic:
		return copilotProviderAnthropic, nil
	default:
		return "", fmt.Errorf(
			"--provider must be %q or %q, got %q",
			copilotProviderOpenAI,
			copilotProviderAnthropic,
			providerFlag,
		)
	}
}

func copilotProviderBaseURL(preloopURL, providerType string) string {
	base := strings.TrimRight(strings.TrimSpace(preloopURL), "/")
	if providerType == copilotProviderAnthropic {
		return base + "/anthropic"
	}
	return base + "/openai/v1"
}

func runCopilot(
	bin string,
	args []string,
	providerEnv map[string]string,
	stdin io.Reader,
	stdout, stderr io.Writer,
) error {
	child := exec.Command(bin, args...)
	child.Stdin = stdin
	child.Stdout = stdout
	child.Stderr = stderr
	child.Env = mergeCopilotEnv(os.Environ(), providerEnv)
	// No SysProcAttr: the child must inherit the launcher's process group
	// so an interactive TTY stays in the foreground (see startClaudeTUI).
	return wrapProcessExit(child.Run())
}

func mergeCopilotEnv(base []string, extras map[string]string) []string {
	keys := map[string]struct{}{
		copilotEnvProviderType: {},
		copilotEnvProviderURL:  {},
		copilotEnvProviderKey:  {},
		copilotEnvModel:        {},
	}
	out := make([]string, 0, len(base)+len(extras))
	for _, entry := range base {
		name, _, ok := strings.Cut(entry, "=")
		if ok {
			if _, drop := keys[name]; drop {
				continue
			}
		}
		out = append(out, entry)
	}
	for _, key := range []string{
		copilotEnvProviderType,
		copilotEnvProviderURL,
		copilotEnvProviderKey,
		copilotEnvModel,
	} {
		if value, ok := extras[key]; ok {
			out = append(out, key+"="+value)
		}
	}
	return out
}
