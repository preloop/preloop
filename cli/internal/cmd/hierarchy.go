package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strings"
	"text/tabwriter"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

// Commands for account hierarchies (subaccounts, sharing) and tag based
// access rules. The server side comes from an extension plugin, so every
// group here is gated on a /features capability (see capabilities.go).

// itemList decodes either a bare JSON list or {"items": [...]}.
type itemList[T any] struct{ items []T }

func (l *itemList[T]) UnmarshalJSON(data []byte) error {
	var bare []T
	if err := json.Unmarshal(data, &bare); err == nil {
		l.items = bare
		return nil
	}
	var wrapped struct {
		Items []T `json:"items"`
	}
	if err := json.Unmarshal(data, &wrapped); err != nil {
		return err
	}
	l.items = wrapped.Items
	return nil
}

// shareableKinds are the resource kinds a parent account can share.
var shareableKinds = []string{"ai_model", "mcp_server", "managed_agent", "flow", "runner_pool", "policy"}

func validKind(kind string) error {
	for _, k := range shareableKinds {
		if k == kind {
			return nil
		}
	}
	return fmt.Errorf("unknown kind %q (one of %s)", kind, strings.Join(shareableKinds, ", "))
}

// tagKeyPattern and maxTagValue match the server's tag limits.
var tagKeyPattern = regexp.MustCompile(`^[a-z0-9._/-]{1,63}$`)

const maxTagValue = 128

// parseTagPairs turns key=value arguments into a tag map.
func parseTagPairs(pairs []string) (map[string]string, error) {
	tags := map[string]string{}
	for _, pair := range pairs {
		key, value, ok := strings.Cut(pair, "=")
		key = strings.TrimSpace(key)
		if !ok || key == "" {
			return nil, fmt.Errorf("tag %q: expected key=value", pair)
		}
		if !tagKeyPattern.MatchString(key) {
			return nil, fmt.Errorf("tag key %q: use lowercase letters, digits and . _ / - (at most 63)", key)
		}
		if len(value) > maxTagValue {
			return nil, fmt.Errorf("tag %q: value longer than %d characters", key, maxTagValue)
		}
		tags[key] = value
	}
	return tags, nil
}

// notFoundAsMissing reports an item 404 as "not found in this account".
// The server answers 404 for another account's ids too, so the message
// never claims the id exists elsewhere.
func notFoundAsMissing(err error, what string) error {
	if api.IsStatus(err, http.StatusNotFound) {
		return fmt.Errorf("%s not found in this account", what)
	}
	return err
}

// currentAccountID is the id of the account the session acts in.
func currentAccountID(client *api.Client) (string, error) {
	if cfg, err := config.Load(); err == nil && cfg.AccountID != "" && FlagToken == "" && os.Getenv(config.EnvToken) == "" {
		return cfg.AccountID, nil
	}
	var me struct {
		AccountID string `json:"account_id"`
	}
	if err := client.Get("/api/v1/auth/users/me", &me); err != nil {
		return "", err
	}
	if me.AccountID == "" {
		return "", fmt.Errorf("the server did not report the current account")
	}
	return me.AccountID, nil
}

func hierarchyClient() (*api.Client, string, error) {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return nil, "", err
	}
	accountID, err := currentAccountID(client)
	if err != nil {
		return nil, "", err
	}
	return client, accountID, nil
}

func accountPath(accountID, rest string) string {
	return "/api/v1/accounts/" + url.PathEscape(accountID) + rest
}

// ---------------------------------------------------------------------------
// subaccounts (account_hierarchy)

type subaccount struct {
	ID   string            `json:"id"`
	Name string            `json:"name"`
	Slug string            `json:"slug"`
	Tags map[string]string `json:"tags"`
}

var subaccountTags []string

func newSubaccountsCmd() *cobra.Command {
	group := &cobra.Command{
		Use:   "subaccounts",
		Short: "Manage the subaccounts of the current account",
	}
	list := &cobra.Command{
		Use:   "list",
		Short: "List subaccounts",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			client, accountID, err := hierarchyClient()
			if err != nil {
				return err
			}
			var subs itemList[subaccount]
			if err := client.Get(accountPath(accountID, "/subaccounts"), &subs); err != nil {
				return err
			}
			w := tabwriter.NewWriter(cmd.OutOrStdout(), 0, 0, 2, ' ', 0)
			fmt.Fprintln(w, "ID\tNAME\tTAGS")
			for _, s := range subs.items {
				fmt.Fprintf(w, "%s\t%s\t%s\n", s.ID, s.Name, formatTagMap(s.Tags))
			}
			return w.Flush()
		},
	}
	create := &cobra.Command{
		Use:   "create <name>",
		Short: "Create a subaccount",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			tags, err := parseTagPairs(subaccountTags)
			if err != nil {
				return err
			}
			client, accountID, err := hierarchyClient()
			if err != nil {
				return err
			}
			var created subaccount
			body := map[string]any{"name": args[0], "tags": tags}
			if err := client.Post(accountPath(accountID, "/subaccounts"), body, &created); err != nil {
				return err
			}
			fmt.Fprintf(cmd.OutOrStdout(), "Created %s (%s)\n", created.Name, created.ID)
			return nil
		},
	}
	create.Flags().StringArrayVar(&subaccountTags, "tag", nil, "tag as key=value (repeatable)")

	rename := &cobra.Command{
		Use:   "rename <id> <name>",
		Short: "Rename a subaccount",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, accountID, err := hierarchyClient()
			if err != nil {
				return err
			}
			path := accountPath(accountID, "/subaccounts/"+url.PathEscape(args[0]))
			if err := client.Patch(path, map[string]any{"name": args[1]}, nil); err != nil {
				return notFoundAsMissing(err, "subaccount "+args[0])
			}
			fmt.Fprintf(cmd.OutOrStdout(), "Renamed %s to %s\n", args[0], args[1])
			return nil
		},
	}
	detach := &cobra.Command{
		Use:   "detach <id>",
		Short: "Detach a subaccount into a standalone account",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, accountID, err := hierarchyClient()
			if err != nil {
				return err
			}
			path := accountPath(accountID, "/subaccounts/"+url.PathEscape(args[0])+"/detach")
			if err := client.Post(path, map[string]any{}, nil); err != nil {
				return notFoundAsMissing(err, "subaccount "+args[0])
			}
			fmt.Fprintf(cmd.OutOrStdout(), "Detached %s\n", args[0])
			return nil
		},
	}
	remove := &cobra.Command{
		Use:   "delete <id>",
		Short: "Delete a subaccount",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, accountID, err := hierarchyClient()
			if err != nil {
				return err
			}
			path := accountPath(accountID, "/subaccounts/"+url.PathEscape(args[0]))
			if err := client.Delete(path, nil); err != nil {
				return notFoundAsMissing(err, "subaccount "+args[0])
			}
			fmt.Fprintf(cmd.OutOrStdout(), "Deleted %s\n", args[0])
			return nil
		},
	}
	group.AddCommand(list, create, rename, detach, remove)
	return gateOnCapability(group, capabilityAccountHierarchy)
}

// ---------------------------------------------------------------------------
// share (account_hierarchy)

type shareTarget struct {
	Type          string   `json:"type"`
	SubaccountIDs []string `json:"subaccount_ids,omitempty"`
	Key           string   `json:"key,omitempty"`
	Value         string   `json:"value,omitempty"`
}

type share struct {
	ID           string      `json:"id"`
	ResourceType string      `json:"resource_type"`
	ResourceID   string      `json:"resource_id"`
	Target       shareTarget `json:"target"`
}

// shareRequest is the body of POST /shares: a share without its id.
type shareRequest struct {
	ResourceType string      `json:"resource_type"`
	ResourceID   string      `json:"resource_id"`
	Target       shareTarget `json:"target"`
}

func (t shareTarget) String() string {
	switch t.Type {
	case "selected":
		return "subaccounts " + strings.Join(t.SubaccountIDs, ",")
	case "tag":
		return "tag " + t.Key + "=" + t.Value
	default:
		return "all subaccounts"
	}
}

var (
	shareSubaccounts []string
	shareTag         string
)

// buildShareTarget reads --subaccount and --tag; neither means all.
func buildShareTarget(subaccounts []string, tag string) (shareTarget, error) {
	if len(subaccounts) > 0 && tag != "" {
		return shareTarget{}, fmt.Errorf("use either --subaccount or --tag, not both")
	}
	if len(subaccounts) > 0 {
		return shareTarget{Type: "selected", SubaccountIDs: subaccounts}, nil
	}
	if tag != "" {
		tags, err := parseTagPairs([]string{tag})
		if err != nil {
			return shareTarget{}, err
		}
		for k, v := range tags {
			return shareTarget{Type: "tag", Key: k, Value: v}, nil
		}
	}
	return shareTarget{Type: "all"}, nil
}

func newShareCmd() *cobra.Command {
	group := &cobra.Command{
		Use:   "share",
		Short: "Share resources with subaccounts",
		Long: `Share a resource of the current account with its subaccounts.

Kinds: ` + strings.Join(shareableKinds, ", ") + `.
Shared resources are read-only in subaccounts and never expose credentials.`,
	}
	list := &cobra.Command{
		Use:   "list <kind> <id>",
		Short: "List the shares of a resource",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := validKind(args[0]); err != nil {
				return err
			}
			client, accountID, err := hierarchyClient()
			if err != nil {
				return err
			}
			query := "?resource_type=" + url.QueryEscape(args[0]) + "&resource_id=" + url.QueryEscape(args[1])
			var shares itemList[share]
			if err := client.Get(accountPath(accountID, "/shares"+query), &shares); err != nil {
				return err
			}
			w := tabwriter.NewWriter(cmd.OutOrStdout(), 0, 0, 2, ' ', 0)
			fmt.Fprintln(w, "ID\tTARGET")
			for _, s := range shares.items {
				if s.ResourceID != "" && s.ResourceID != args[1] {
					continue
				}
				fmt.Fprintf(w, "%s\t%s\n", s.ID, s.Target)
			}
			return w.Flush()
		},
	}
	add := &cobra.Command{
		Use:   "add <kind> <id>",
		Short: "Share a resource (all subaccounts unless --subaccount or --tag)",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := validKind(args[0]); err != nil {
				return err
			}
			target, err := buildShareTarget(shareSubaccounts, shareTag)
			if err != nil {
				return err
			}
			client, accountID, err := hierarchyClient()
			if err != nil {
				return err
			}
			body := shareRequest{ResourceType: args[0], ResourceID: args[1], Target: target}
			var created share
			if err := client.Post(accountPath(accountID, "/shares"), body, &created); err != nil {
				return notFoundAsMissing(err, args[0]+" "+args[1])
			}
			fmt.Fprintf(cmd.OutOrStdout(), "Shared %s %s with %s (%s)\n", args[0], args[1], target, created.ID)
			return nil
		},
	}
	add.Flags().StringArrayVar(&shareSubaccounts, "subaccount", nil, "share with this subaccount id (repeatable)")
	add.Flags().StringVar(&shareTag, "tag", "", "share with subaccounts tagged key=value")

	remove := &cobra.Command{
		Use:   "rm <share-id>",
		Short: "Stop a share",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, accountID, err := hierarchyClient()
			if err != nil {
				return err
			}
			if err := client.Delete(accountPath(accountID, "/shares/"+url.PathEscape(args[0])), nil); err != nil {
				return notFoundAsMissing(err, "share "+args[0])
			}
			fmt.Fprintf(cmd.OutOrStdout(), "Stopped share %s\n", args[0])
			return nil
		},
	}
	group.AddCommand(list, add, remove)
	return gateOnCapability(group, capabilityAccountHierarchy)
}

// ---------------------------------------------------------------------------
// tags (abac_rules)

type resourceTags struct {
	Tags         map[string]string `json:"tags"`
	GovernedKeys []string          `json:"governed_keys"`
	// Version identifies the tag set that was read. The write sends it back
	// and the server answers 409 when the set changed in between, so two
	// editors cannot silently overwrite each other.
	Version *string `json:"version"`
}

func formatTagMap(tags map[string]string) string {
	keys := make([]string, 0, len(tags))
	for k := range tags {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+tags[k])
	}
	return strings.Join(parts, " ")
}

func tagsPath(kind, id string) string {
	return "/api/v1/tags/" + url.PathEscape(kind) + "/" + url.PathEscape(id)
}

// updateTags reads the tags of a resource, applies change and writes the
// whole set back with the version it read. Keys the parent governs cannot
// be changed here, and a set changed by someone else in between is not
// overwritten.
func updateTags(cmd *cobra.Command, kind, id string, change func(map[string]string) ([]string, error)) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	var current resourceTags
	if err := client.Get(tagsPath(kind, id), &current); err != nil {
		return notFoundAsMissing(err, kind+" "+id)
	}
	if current.Tags == nil {
		current.Tags = map[string]string{}
	}
	touched, err := change(current.Tags)
	if err != nil {
		return err
	}
	governed := map[string]bool{}
	for _, k := range current.GovernedKeys {
		governed[k] = true
	}
	for _, k := range touched {
		if governed[k] {
			return fmt.Errorf("tag %q is set by the parent account and is read-only here", k)
		}
	}
	var saved resourceTags
	body := map[string]any{"tags": current.Tags, "version": current.Version}
	if err := client.Put(tagsPath(kind, id), body, &saved); err != nil {
		if api.IsStatus(err, http.StatusConflict) {
			return fmt.Errorf("the tags of %s %s changed while this command ran; nothing was saved, run it again", kind, id)
		}
		return notFoundAsMissing(err, kind+" "+id)
	}
	if saved.Tags == nil {
		saved.Tags = current.Tags
	}
	fmt.Fprintln(cmd.OutOrStdout(), formatTagMap(saved.Tags))
	return nil
}

func newTagsCmd() *cobra.Command {
	group := &cobra.Command{
		Use:   "tags",
		Short: "Tag resources for access rules",
	}
	list := &cobra.Command{
		Use:   "list <kind> <id>",
		Short: "Show the tags of a resource",
		Args:  cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := api.NewClient(FlagToken, FlagURL)
			if err != nil {
				return err
			}
			var current resourceTags
			if err := client.Get(tagsPath(args[0], args[1]), &current); err != nil {
				return notFoundAsMissing(err, args[0]+" "+args[1])
			}
			governed := map[string]bool{}
			for _, k := range current.GovernedKeys {
				governed[k] = true
			}
			w := tabwriter.NewWriter(cmd.OutOrStdout(), 0, 0, 2, ' ', 0)
			fmt.Fprintln(w, "KEY\tVALUE\tSET BY")
			keys := make([]string, 0, len(current.Tags))
			for k := range current.Tags {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				by := "this account"
				if governed[k] {
					by = "parent (read-only)"
				}
				fmt.Fprintf(w, "%s\t%s\t%s\n", k, current.Tags[k], by)
			}
			return w.Flush()
		},
	}
	set := &cobra.Command{
		Use:   "set <kind> <id> key=value...",
		Short: "Set tags on a resource",
		Args:  cobra.MinimumNArgs(3),
		RunE: func(cmd *cobra.Command, args []string) error {
			pairs, err := parseTagPairs(args[2:])
			if err != nil {
				return err
			}
			return updateTags(cmd, args[0], args[1], func(tags map[string]string) ([]string, error) {
				touched := make([]string, 0, len(pairs))
				for k, v := range pairs {
					tags[k] = v
					touched = append(touched, k)
				}
				return touched, nil
			})
		},
	}
	remove := &cobra.Command{
		Use:   "rm <kind> <id> key...",
		Short: "Remove tags from a resource",
		Args:  cobra.MinimumNArgs(3),
		RunE: func(cmd *cobra.Command, args []string) error {
			keys := args[2:]
			return updateTags(cmd, args[0], args[1], func(tags map[string]string) ([]string, error) {
				for _, k := range keys {
					if _, ok := tags[k]; !ok {
						return nil, fmt.Errorf("tag %q is not set", k)
					}
					delete(tags, k)
				}
				return keys, nil
			})
		},
	}
	group.AddCommand(list, set, remove)
	return gateOnCapability(group, capabilityABACRules)
}

// ---------------------------------------------------------------------------
// access (abac_rules)

type accessRule struct {
	ID          string   `json:"id"`
	Name        string   `json:"name"`
	Effect      string   `json:"effect"`
	Actions     []string `json:"actions"`
	Scope       string   `json:"scope"`
	AccountName string   `json:"account_name"`
}

var (
	rulesFile       string
	explainSubject  string
	explainAction   string
	explainResource string
)

func printRules(w *tabwriter.Writer, rules []accessRule, from string) {
	for _, r := range rules {
		source := from
		if from != "" && r.AccountName != "" {
			source = "inherited from " + r.AccountName
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\n", r.Name, r.Effect, strings.Join(r.Actions, ","), r.Scope, source)
	}
}

func newAccessCmd() *cobra.Command {
	group := &cobra.Command{
		Use:   "access",
		Short: "Manage tag based access rules",
	}
	rules := &cobra.Command{
		Use:   "rules",
		Short: "List and apply access rules",
	}
	list := &cobra.Command{
		Use:   "list",
		Short: "List access rules, including read-only rules from the parent",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			client, err := api.NewClient(FlagToken, FlagURL)
			if err != nil {
				return err
			}
			var set struct {
				Rules     []accessRule      `json:"rules"`
				Inherited []accessRule      `json:"inherited"`
				Modes     map[string]string `json:"modes"`
			}
			if err := client.Get("/api/v1/access/rules", &set); err != nil {
				return err
			}
			w := tabwriter.NewWriter(cmd.OutOrStdout(), 0, 0, 2, ' ', 0)
			fmt.Fprintln(w, "NAME\tEFFECT\tACTIONS\tSCOPE\tSOURCE")
			printRules(w, set.Rules, "")
			printRules(w, set.Inherited, "inherited")
			if err := w.Flush(); err != nil {
				return err
			}
			if len(set.Modes) > 0 {
				actions := make([]string, 0, len(set.Modes))
				for a := range set.Modes {
					actions = append(actions, a)
				}
				sort.Strings(actions)
				fmt.Fprintln(cmd.OutOrStdout(), "\nModes:")
				for _, a := range actions {
					fmt.Fprintf(cmd.OutOrStdout(), "  %s: %s\n", a, set.Modes[a])
				}
			}
			return nil
		},
	}
	apply := &cobra.Command{
		Use:   "apply -f <file.yaml>",
		Short: "Replace this account's rules with the rules in a YAML file",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if rulesFile == "" {
				return fmt.Errorf("-f <file.yaml> is required")
			}
			var data []byte
			var err error
			if rulesFile == "-" {
				data, err = io.ReadAll(cmd.InOrStdin())
			} else {
				data, err = os.ReadFile(rulesFile)
			}
			if err != nil {
				return err
			}
			client, err := api.NewClient(FlagToken, FlagURL)
			if err != nil {
				return err
			}
			if err := client.Post("/api/v1/access/rules/apply", map[string]string{"yaml": string(data)}, nil); err != nil {
				return err
			}
			fmt.Fprintln(cmd.OutOrStdout(), "Applied access rules")
			return nil
		},
	}
	apply.Flags().StringVarP(&rulesFile, "file", "f", "", "YAML file with the rules ('-' for stdin)")
	rules.AddCommand(list, apply)

	explain := &cobra.Command{
		Use:     "explain",
		Short:   "Explain whether a subject may perform an action on a resource",
		Example: `  preloop access explain --subject agent:<id> --action tool:call --resource tool:<id>`,
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if explainSubject == "" || explainAction == "" || explainResource == "" {
				return fmt.Errorf("--subject, --action and --resource are required")
			}
			client, err := api.NewClient(FlagToken, FlagURL)
			if err != nil {
				return err
			}
			var result struct {
				Effect  string   `json:"effect"`
				Reason  string   `json:"reason"`
				RuleIDs []string `json:"rule_ids"`
			}
			body := map[string]string{"subject": explainSubject, "action": explainAction, "resource": explainResource}
			if err := client.Post("/api/v1/access/explain", body, &result); err != nil {
				return err
			}
			out := cmd.OutOrStdout()
			fmt.Fprintf(out, "Decision: %s\n", result.Effect)
			if result.Reason != "" {
				fmt.Fprintf(out, "Reason:   %s\n", result.Reason)
			}
			if len(result.RuleIDs) > 0 {
				fmt.Fprintf(out, "Rules:    %s\n", strings.Join(result.RuleIDs, ", "))
			}
			return nil
		},
	}
	explain.Flags().StringVar(&explainSubject, "subject", "", "subject, for example agent:<id> or user:<id>")
	explain.Flags().StringVar(&explainAction, "action", "", "action, for example tool:call or model:invoke")
	explain.Flags().StringVar(&explainResource, "resource", "", "resource, for example tool:<id>")

	group.AddCommand(rules, explain)
	return gateOnCapability(group, capabilityABACRules)
}
