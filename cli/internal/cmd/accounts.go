package cmd

import (
	"fmt"
	"net/http"
	"os"
	"strings"
	"text/tabwriter"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

var (
	// FlagProfile selects a named profile for this invocation.
	FlagProfile string
	// FlagAccount selects an account (by slug) of the profile.
	FlagAccount string
)

// membership is one entry of GET /api/v1/me/memberships.
type membership struct {
	AccountID       string  `json:"account_id"`
	AccountName     string  `json:"account_name"`
	Slug            string  `json:"slug"`
	ParentAccountID *string `json:"parent_account_id"`
	Role            string  `json:"role"`
	IsCurrent       bool    `json:"is_current"`
}

// switchAccountResponse is POST /api/v1/auth/switch-account.
type switchAccountResponse struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
}

var accountsCmd = &cobra.Command{
	Use:   "accounts",
	Short: "List and switch the accounts you belong to",
	Long: `List and switch the accounts you belong to on this server.

Each account keeps its own token pair in the selected profile. Use the
global --account <slug> flag to run one command in another account without
switching, and --profile <name> (or PRELOOP_PROFILE) to use another profile.`,
}

var accountsListCmd = &cobra.Command{
	Use:   "list",
	Short: "List the accounts you belong to",
	Args:  cobra.NoArgs,
	RunE:  runAccountsList,
}

var accountsSwitchCmd = &cobra.Command{
	Use:   "switch <slug>",
	Short: "Switch the current account of this profile",
	Args:  cobra.ExactArgs(1),
	RunE:  runAccountsSwitch,
}

var accountsCurrentCmd = &cobra.Command{
	Use:   "current",
	Short: "Show the current profile and account",
	Args:  cobra.NoArgs,
	RunE:  runAccountsCurrent,
}

func init() {
	accountsCmd.AddCommand(accountsListCmd)
	accountsCmd.AddCommand(accountsSwitchCmd)
	accountsCmd.AddCommand(accountsCurrentCmd)
}

// membershipSlug is the slug an account is stored under: the server's slug,
// else the account id (always a valid name for UUIDs).
func membershipSlug(m membership) string {
	if m.Slug != "" {
		return strings.ToLower(m.Slug)
	}
	return strings.ToLower(m.AccountID)
}

func fetchMemberships(client *api.Client) ([]membership, error) {
	var raw itemList[membership]
	if err := client.Get("/api/v1/me/memberships", &raw); err != nil {
		if api.IsStatus(err, http.StatusNotFound) {
			return nil, fmt.Errorf("this server does not support multiple accounts")
		}
		return nil, err
	}
	return raw.items, nil
}

func runAccountsList(cmd *cobra.Command, _ []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	memberships, err := fetchMemberships(client)
	if err != nil {
		return err
	}
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	stored, _, _ := config.StoredAccounts()
	signedIn := map[string]bool{}
	for _, s := range stored {
		signedIn[s.Slug] = true
	}

	w := tabwriter.NewWriter(cmd.OutOrStdout(), 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "\tSLUG\tNAME\tROLE\tSESSION")
	for _, m := range memberships {
		slug := membershipSlug(m)
		marker := ""
		if (cfg.Account != "" && cfg.Account == slug) || (cfg.Account == "" && m.IsCurrent) {
			marker = "*"
		}
		session := ""
		if signedIn[slug] {
			session = "stored"
		}
		name := m.AccountName
		if m.ParentAccountID != nil && *m.ParentAccountID != "" {
			name = "  " + name
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\n", marker, slug, name, m.Role, session)
	}
	return w.Flush()
}

func runAccountsSwitch(cmd *cobra.Command, args []string) error {
	want := strings.ToLower(strings.TrimSpace(args[0]))
	// The exchange uses the profile's current session, not --account.
	config.Select(FlagProfile, "")
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	memberships, err := fetchMemberships(client)
	if err != nil {
		return err
	}
	var target *membership
	for i := range memberships {
		if membershipSlug(memberships[i]) == want || strings.EqualFold(memberships[i].AccountID, want) {
			target = &memberships[i]
			break
		}
	}
	if target == nil {
		return fmt.Errorf("you are not a member of an account %q; see `preloop accounts list`", want)
	}
	slug := membershipSlug(*target)
	if err := config.ValidateName(slug); err != nil {
		return err
	}

	var pair switchAccountResponse
	if err := client.Post("/api/v1/auth/switch-account", map[string]string{"account_id": target.AccountID}, &pair); err != nil {
		return err
	}
	if pair.AccessToken == "" || pair.RefreshToken == "" {
		return fmt.Errorf("the server did not return a token pair for %q", slug)
	}
	if err := config.SaveAccount(slug, config.AccountEntry{
		AccessToken:  pair.AccessToken,
		RefreshToken: pair.RefreshToken,
		AccountID:    target.AccountID,
		Name:         target.AccountName,
	}); err != nil {
		return err
	}
	fmt.Fprintf(cmd.OutOrStdout(), "Switched to %s (%s)\n", target.AccountName, slug)
	return nil
}

func runAccountsCurrent(cmd *cobra.Command, _ []string) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	fmt.Fprintf(out, "Profile: %s\n", cfg.Profile)
	fmt.Fprintf(out, "Account: %s\n", describeAccount(cfg))
	fmt.Fprintf(out, "API URL: %s\n", cfg.APIURL)
	if cfg.AccountMissing {
		fmt.Fprintf(out, "No stored session for %q; run `preloop accounts switch %s`\n", cfg.Account, cfg.Account)
	}
	return nil
}

// describeAccount names the selected account for status output.
func describeAccount(cfg *config.Config) string {
	switch {
	case cfg.Account == "":
		return "(the account you signed in to)"
	case cfg.AccountName != "":
		return fmt.Sprintf("%s (%s)", cfg.AccountName, cfg.Account)
	default:
		return cfg.Account
	}
}

// applySelection hands the global --profile and --account flags to config.
func applySelection() {
	config.Select(FlagProfile, FlagAccount)
	if verbose && (FlagProfile != "" || FlagAccount != "") {
		fmt.Fprintf(os.Stderr, "Using profile %q account %q\n", FlagProfile, FlagAccount)
	}
}
