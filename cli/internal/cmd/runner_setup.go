package cmd

import (
	"bufio"
	"fmt"
	"io"
	"net/url"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

const (
	runnerSetupAsk           = "ask"
	runnerSetupInformInstall = "inform_install"
	runnerSetupSkip          = "skip"

	// runnerSetupManualCommand is the command printed when this process
	// cannot finish the install. Windows without elevation prints it as
	// its own line.
	runnerSetupManualCommand = "preloop runner setup"

	// personalRunnersConsentURL is the consent wording for a personal
	// runner. The page is published with the personal runners security
	// note (#1485). The CLI links it even before that page is live.
	personalRunnersConsentURL = "https://docs.preloop.ai/security/personal-runners/"

	runnerRequiredDefault  = "Your organisation requires a Preloop runner on member machines."
	runnerForbiddenDefault = "A Preloop runner is not allowed for this account."
)

// runnerPolicy is the runner_policy object on GET /api/v1/users/me.
// A nil policy, or a policy with an empty requirement, means optional.
type runnerPolicy struct {
	Requirement          string   `json:"requirement"`
	Capabilities         []string `json:"capabilities"`
	MandatedCapabilities []string `json:"mandated_capabilities"`
	GraceUntil           *string  `json:"grace_until"`
	CanDecide            *bool    `json:"can_decide"`
	Message              *string  `json:"message"`
}

func (p *runnerPolicy) canDecide() bool {
	if p == nil || p.CanDecide == nil {
		return true
	}
	return *p.CanDecide
}

func (p *runnerPolicy) message() string {
	if p == nil || p.Message == nil {
		return ""
	}
	return strings.TrimSpace(*p.Message)
}

func (p *runnerPolicy) requirement() string {
	if p == nil || strings.TrimSpace(p.Requirement) == "" {
		return "optional"
	}
	return p.Requirement
}

// runnerSetupEnv is the invocation around decideRunnerSetup.
// OnDemand is true for `preloop runner setup` and false for the post-login step.
type runnerSetupEnv struct {
	Headless       bool
	TokenAuth      bool
	EnvAuth        bool
	NoRunnerFlag   bool
	NoRunnerEnv    bool
	PromptAnswered bool
	Force          bool
	OnDemand       bool
}

type runnerSetupDecision struct {
	Action string
	Reason string
}

// decideRunnerSetup chooses ask, inform_install, or skip.
// Skip reasons: no_runner_flag, no_runner_env, token_auth, env_auth,
// headless, not_tty, already_installed, forbidden, prompt_answered.
// can_decide false still asks. A nil policy is optional.
// Login skip conditions (non-TTY, headless, token, env auth, --no-runner)
// apply before required, so an unattended login never installs.
func decideRunnerSetup(
	policy *runnerPolicy,
	env runnerSetupEnv,
	tty bool,
	installed bool,
) runnerSetupDecision {
	if !env.OnDemand {
		switch {
		case env.NoRunnerFlag:
			return runnerSetupDecision{Action: runnerSetupSkip, Reason: "no_runner_flag"}
		case env.NoRunnerEnv:
			return runnerSetupDecision{Action: runnerSetupSkip, Reason: "no_runner_env"}
		case env.TokenAuth:
			return runnerSetupDecision{Action: runnerSetupSkip, Reason: "token_auth"}
		case env.EnvAuth:
			return runnerSetupDecision{Action: runnerSetupSkip, Reason: "env_auth"}
		case env.Headless:
			return runnerSetupDecision{Action: runnerSetupSkip, Reason: "headless"}
		case !tty:
			return runnerSetupDecision{Action: runnerSetupSkip, Reason: "not_tty"}
		}
	}
	if installed {
		return runnerSetupDecision{Action: runnerSetupSkip, Reason: "already_installed"}
	}
	switch policy.requirement() {
	case "forbidden":
		return runnerSetupDecision{Action: runnerSetupSkip, Reason: "forbidden"}
	case "required":
		return runnerSetupDecision{Action: runnerSetupInformInstall}
	}
	if !env.OnDemand && env.PromptAnswered && !env.Force {
		return runnerSetupDecision{Action: runnerSetupSkip, Reason: "prompt_answered"}
	}
	if !tty {
		return runnerSetupDecision{Action: runnerSetupSkip, Reason: "not_tty"}
	}
	return runnerSetupDecision{Action: runnerSetupAsk}
}

var runnerSetupCmd = &cobra.Command{
	Use:   "setup",
	Short: "Install the runner service for this login",
	Long: `Install the Preloop runner as a background service, using the account
runner policy from the server.

optional asks first (the default answer is no). required installs without
a question. forbidden prints one line and does not install.

The service starts on login, connects outbound to this instance, and can
run flow executions on this machine. It reports installed agent harnesses
and never reads harness credentials. Remote sessions stay off until enabled
per harness. Remove it with 'preloop runner disable'.

On Windows, run this from an elevated PowerShell.`,
	RunE: runRunnerSetup,
}

func runRunnerSetup(cmd *cobra.Command, args []string) error {
	out := cmd.OutOrStdout()
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil || strings.TrimSpace(cfg.AccessToken) == "" {
		fmt.Fprintln(out, "Log in first with: preloop login")
		return fmt.Errorf("not logged in")
	}
	tty := loginTerminal()
	return performRunnerSetup(runnerSetupRequest{
		Out:      out,
		In:       cmd.InOrStdin(),
		Policy:   fetchRunnerPolicy(),
		Instance: cfg.APIURL,
		TTY:      &tty,
		Env: runnerSetupEnv{
			OnDemand: true,
			Force:    true,
		},
	})
}

// offerRunnerAfterLogin runs the post-login runner step. It never returns
// an error: install and wait failures are printed.
func offerRunnerAfterLogin(out io.Writer, user *UserInfo, instance string, headless bool) {
	var policy *runnerPolicy
	if user != nil {
		policy = user.RunnerPolicy
	}
	tty := loginTerminal()
	_ = performRunnerSetup(runnerSetupRequest{
		Out:      out,
		In:       os.Stdin,
		Policy:   policy,
		Instance: instance,
		TTY:      &tty,
		Env: runnerSetupEnv{
			Headless:       headless || loginHeadless,
			TokenAuth:      strings.TrimSpace(loginToken) != "" || strings.TrimSpace(FlagToken) != "",
			EnvAuth:        strings.TrimSpace(os.Getenv(config.EnvToken)) != "",
			NoRunnerFlag:   loginNoRunner,
			NoRunnerEnv:    os.Getenv("PRELOOP_NO_RUNNER") == "1",
			PromptAnswered: config.RunnerPromptAnswered(),
			Force:          loginForce,
		},
	})
}

func loginTerminal() bool {
	return stdinIsTerminal() && stdoutIsTerminal()
}

type runnerSetupRequest struct {
	Out      io.Writer
	In       io.Reader
	Policy   *runnerPolicy
	Env      runnerSetupEnv
	Instance string
	// TTY, when non-nil, overrides terminal detection.
	TTY *bool
	// Installed, when non-nil, overrides the service check.
	Installed func() bool
}

func (r runnerSetupRequest) tty() bool {
	if r.TTY != nil {
		return *r.TTY
	}
	return loginTerminal()
}

func (r runnerSetupRequest) installed() bool {
	if r.Installed != nil {
		return r.Installed()
	}
	return runnerServiceInstalled()
}

func performRunnerSetup(req runnerSetupRequest) error {
	if req.Out == nil {
		req.Out = os.Stdout
	}
	if req.In == nil {
		req.In = os.Stdin
	}
	decision := decideRunnerSetup(req.Policy, req.Env, req.tty(), req.installed())
	switch decision.Action {
	case runnerSetupSkip:
		return handleRunnerSetupSkip(req, decision.Reason)
	case runnerSetupInformInstall:
		fmt.Fprintln(req.Out, runnerRequiredNotice(req.Policy))
		if caps := req.Policy.mandated(); len(caps) > 0 {
			fmt.Fprintf(req.Out, "Required capabilities: %s\n", strings.Join(caps, ", "))
		}
		fmt.Fprintln(req.Out, runnerSetupConsequences(req.Instance))
		return installRunnerFromSetup(req)
	default:
		fmt.Fprintln(req.Out, runnerSetupConsequences(req.Instance))
		answer, err := promptForTextInput(
			bufio.NewReader(req.In),
			req.Out,
			"Install a Preloop runner on this machine? [y/N] ",
		)
		if err != nil {
			fmt.Fprintf(
				req.Out,
				"Could not read the answer. Install it later with: %s\n",
				runnerSetupManualCommand,
			)
			return nil
		}
		if !runnerSetupAffirmative(answer) {
			if err := config.SetRunnerPromptAnsweredNow(); err != nil {
				fmt.Fprintf(req.Out, "Could not record the answer: %v\n", err)
			}
			fmt.Fprintln(req.Out, runnerDeclinedMessage(req.Policy.canDecide()))
			return nil
		}
		return installRunnerFromSetup(req)
	}
}

func (p *runnerPolicy) mandated() []string {
	if p == nil {
		return nil
	}
	return p.MandatedCapabilities
}

func handleRunnerSetupSkip(req runnerSetupRequest, reason string) error {
	switch reason {
	case "forbidden":
		fmt.Fprintln(req.Out, runnerForbiddenNotice(req.Policy))
	case "not_tty":
		if req.Env.OnDemand {
			fmt.Fprintln(
				req.Out,
				"A terminal is required to choose a runner install. Run this from an interactive shell.",
			)
		}
	case "already_installed":
		if req.Env.OnDemand {
			fmt.Fprintln(req.Out, "A Preloop runner service is already installed.")
		}
	}
	return nil
}

func installRunnerFromSetup(req runnerSetupRequest) error {
	if runnerInstallNeedsElevation() {
		fmt.Fprintln(req.Out, runnerElevationMessage())
		return nil
	}
	if err := config.SetRunnerLabels(personalRunnerLabels()); err != nil {
		fmt.Fprintf(req.Out, "Could not save runner labels: %v\n", err)
	}
	if caps := req.Policy.mandated(); len(caps) > 0 {
		if err := config.SetRunnerMandatedCapabilities(caps); err != nil {
			fmt.Fprintf(req.Out, "Could not save mandated capabilities: %v\n", err)
		}
	}
	if err := setupInstallRunner(req.Out); err != nil {
		fmt.Fprintf(
			req.Out,
			"Could not install the Preloop runner: %v\nInstall it later with: %s\n",
			err,
			runnerSetupManualCommand,
		)
		return nil
	}
	started := true
	if err := setupStartRunner(); err != nil {
		fmt.Fprintf(
			req.Out,
			"Runner installed but it did not start: %v\nStart it with: preloop runner start\n",
			err,
		)
		started = false
	}
	if err := config.SetRunnerPromptAnsweredNow(); err != nil {
		fmt.Fprintf(req.Out, "Could not record the answer: %v\n", err)
	}
	// A start failure already told the user what to do. Waiting for the
	// runner to report online would only block login until the timeout.
	if started {
		waitForRunnerOnline(req.Out)
	}
	return nil
}

func runnerSetupAffirmative(answer string) bool {
	switch strings.ToLower(strings.TrimSpace(answer)) {
	case "y", "yes":
		return true
	default:
		return false
	}
}

func runnerSetupConsequences(instance string) string {
	target := strings.TrimSpace(instance)
	if target == "" {
		target = "your Preloop instance"
	}
	return fmt.Sprintf(`A background service is installed and starts when you log in.
It connects outbound only, to %s.
It can run flow executions on this machine.
It reports installed agent harnesses (names, versions, and whether each one is signed in). It never reads or forwards harness credentials.
Remote sessions stay off until you enable them for a harness.
Remove it with: preloop runner disable
What the runner is allowed to do: %s`, target, personalRunnersConsentURL)
}

func runnerRequiredNotice(policy *runnerPolicy) string {
	if msg := policy.message(); msg != "" {
		return oneLine(msg)
	}
	return runnerRequiredDefault
}

func runnerForbiddenNotice(policy *runnerPolicy) string {
	if msg := policy.message(); msg != "" {
		return oneLine(msg)
	}
	return runnerForbiddenDefault
}

func runnerDeclinedMessage(canDecide bool) string {
	if !canDecide {
		return "Runner not installed. Ask your account admin if this machine should run one."
	}
	return "Runner not installed. Install it later with: " + runnerSetupManualCommand
}

func runnerElevationMessage() string {
	return "Windows needs an elevated prompt to install the Preloop runner service.\n" +
		"Open an elevated PowerShell and run:\n" +
		runnerSetupManualCommand
}

func oneLine(text string) string {
	return strings.Join(strings.Fields(text), " ")
}

func personalRunnerLabels() []string {
	return []string{
		"personal",
		"os:" + runtime.GOOS,
		"arch:" + runtime.GOARCH,
	}
}

func fetchRunnerPolicy() *runnerPolicy {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return nil
	}
	var user UserInfo
	if err := client.Get(userInfoPath, &user); err != nil {
		return nil
	}
	return user.RunnerPolicy
}

var (
	runnerOnlineWait   = 30 * time.Second
	runnerOnlinePoll   = time.Second
	runnerSetupSleep   = time.Sleep
	setupInstallRunner = installRunnerService
	setupStartRunner   = startInstalledRunner
)

func startInstalledRunner() error {
	// writeLaunchdPlist already loads the agent, which starts it.
	if runtime.GOOS == "darwin" {
		return nil
	}
	return runnerServiceAction("start")
}

type runnerOnlineView struct {
	Status           string                `json:"status"`
	HarnessInventory *harnessInventoryView `json:"harness_inventory"`
}

type harnessInventoryView struct {
	Entries []harnessInventoryEntry `json:"entries"`
}

type harnessInventoryEntry struct {
	Harness     string `json:"harness"`
	DisplayName string `json:"display_name"`
	Version     string `json:"version"`
	LoginState  string `json:"login_state"`
}

func waitForRunnerOnline(out io.Writer) {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		fmt.Fprintln(out, runnerNotOnlineYet)
		return
	}
	deadline := time.Now().Add(runnerOnlineWait)
	for {
		if view, ok := readRunnerOnline(client); ok {
			printRunnerOnline(out, view)
			return
		}
		if !time.Now().Before(deadline) {
			break
		}
		runnerSetupSleep(runnerOnlinePoll)
	}
	fmt.Fprintln(out, runnerNotOnlineYet)
}

const runnerNotOnlineYet = "Runner installed. It has not reported online yet. Check with: preloop runner status"

func readRunnerOnline(client *api.Client) (runnerOnlineView, bool) {
	state, err := readRunnerState()
	if err != nil || state.ID == "" {
		return runnerOnlineView{}, false
	}
	var view runnerOnlineView
	path := "/api/v1/runners/" + url.PathEscape(state.ID)
	if err := client.Get(path, &view); err != nil {
		return runnerOnlineView{}, false
	}
	switch view.Status {
	case "online", "busy":
		return view, true
	default:
		return runnerOnlineView{}, false
	}
}

func printRunnerOnline(out io.Writer, view runnerOnlineView) {
	fmt.Fprintln(out, "Runner online.")
	if view.HarnessInventory == nil {
		return
	}
	for _, entry := range view.HarnessInventory.Entries {
		name := entry.DisplayName
		if name == "" {
			name = entry.Harness
		}
		if name == "" {
			continue
		}
		fmt.Fprintf(out, "  %s", name)
		if entry.Version != "" {
			fmt.Fprintf(out, " %s", entry.Version)
		}
		if entry.Harness != "" && entry.Harness != name {
			fmt.Fprintf(out, " (%s)", entry.Harness)
		}
		if entry.LoginState != "" {
			fmt.Fprintf(out, ", %s", entry.LoginState)
		}
		fmt.Fprintln(out)
	}
}
