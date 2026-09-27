package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/config"
)

// Server capabilities that gate command groups. They come from the
// `features` map of GET /api/v1/features and are off unless the server
// reports a literal true (an extension plugin turns them on).
const (
	capabilityMultiAccount     = "multi_account"
	capabilityAccountHierarchy = "account_hierarchy"
	capabilityABACRules        = "abac_rules"
)

// capabilityAnnotation marks a command group as gated on a capability.
const capabilityAnnotation = "preloop.capability"

// capabilityLookupTimeout bounds the /features call made to render help, so
// `preloop --help` against an unreachable server stays fast.
const capabilityLookupTimeout = 2 * time.Second

// featuresFetcher returns the features map. Tests replace it.
var featuresFetcher = fetchFeatures

var (
	featuresOnce   sync.Once
	featuresCached map[string]any
)

// resetCapabilityCache forgets the cached features (tests).
func resetCapabilityCache() {
	featuresOnce = sync.Once{}
	featuresCached = nil
}

func serverFeatures() map[string]any {
	featuresOnce.Do(func() {
		features, err := featuresFetcher()
		if err != nil {
			if verbose {
				fmt.Fprintf(rootCmd.ErrOrStderr(), "Warning: could not read server features: %v\n", err)
			}
			features = nil
		}
		featuresCached = features
	})
	return featuresCached
}

// capabilityOn reports whether the server reports capability as on. An
// unreachable server means off: hiding a command is always safe.
func capabilityOn(capability string) bool {
	on, _ := serverFeatures()[capability].(bool)
	return on
}

// fetchFeatures reads GET /api/v1/features. The endpoint is public, but the
// gated commands all need a session, so without one there is nothing to
// reveal and no reason to reach the network.
func fetchFeatures() (map[string]any, error) {
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil {
		return nil, err
	}
	if cfg.AccessToken == "" {
		return nil, nil
	}
	client := &http.Client{Timeout: capabilityLookupTimeout}
	resp, err := client.Get(cfg.APIURL + "/api/v1/features")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("features: status %d", resp.StatusCode)
	}
	var body struct {
		Features map[string]any `json:"features"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&body); err != nil {
		return nil, fmt.Errorf("features: %w", err)
	}
	return body.Features, nil
}

// gateOnCapability hides a command group unless the server has the
// capability, and makes every command in it fail with a clear message when
// run against a server without it.
func gateOnCapability(group *cobra.Command, capability string) *cobra.Command {
	group.Hidden = true
	if group.Annotations == nil {
		group.Annotations = map[string]string{}
	}
	group.Annotations[capabilityAnnotation] = capability
	var wrap func(*cobra.Command)
	wrap = func(cmd *cobra.Command) {
		if run := cmd.RunE; run != nil {
			cmd.RunE = func(c *cobra.Command, args []string) error {
				if !capabilityOn(capability) {
					return fmt.Errorf(
						"`preloop %s` is not available on this server (capability %q is off)",
						group.Name(), capability,
					)
				}
				return run(c, args)
			}
		}
		for _, child := range cmd.Commands() {
			wrap(child)
		}
	}
	wrap(group)
	return group
}

// revealCapabilityCommands shows the gated groups whose capability is on.
// It runs only when help is rendered, so ordinary commands never pay for
// the /features call.
func revealCapabilityCommands(root *cobra.Command) {
	for _, cmd := range root.Commands() {
		capability, gated := cmd.Annotations[capabilityAnnotation]
		if gated {
			cmd.Hidden = !capabilityOn(capability)
		}
	}
}

// installCapabilityHelp wraps the root help so gated groups appear in
// `preloop --help` (and bare `preloop`) exactly when the server has them.
func installCapabilityHelp(root *cobra.Command) {
	defaultHelp := root.HelpFunc()
	root.SetHelpFunc(func(cmd *cobra.Command, args []string) {
		if cmd == root {
			revealCapabilityCommands(root)
		}
		defaultHelp(cmd, args)
	})
}
