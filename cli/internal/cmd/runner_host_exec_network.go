package cmd

import "fmt"

const (
	// hostExecCapabilityNetwork advertises that a profile's runs have
	// outbound network access.
	hostExecCapabilityNetwork = "host_network"
	hostExecNetworkNotAllowed = "host_network_not_allowed"
)

// hostExecProfileHasNetwork reports whether runs of the profile reach the
// network. Codex sandboxes workspace-write runs without network unless the
// profile sets allow_network; the other native harnesses do not sandbox
// network, so they always have it.
func hostExecProfileHasNetwork(profile hostExecProfile, harness string) bool {
	if harness == hostExecHarnessCodex {
		return profile.AllowNetwork
	}
	return harness != ""
}

// validateHostExecNetworkJob refuses a job that needs network on a profile
// whose runs have none, instead of letting the agent fail on DNS mid-run.
// A read-only review never has network (Codex's read-only sandbox), so it
// cannot require it either.
func validateHostExecNetworkJob(job map[string]any, profile hostExecProfile, harness string, readOnly bool) error {
	needs, _ := job["host_exec_network"].(bool)
	if !needs {
		return nil
	}
	if readOnly {
		return fmt.Errorf("%s: read-only review cannot require network", hostExecNetworkNotAllowed)
	}
	if hostExecProfileHasNetwork(profile, harness) {
		return nil
	}
	return fmt.Errorf(
		"%s: the flow needs network access but host profile %q runs without it; set \"allow_network\": true on the profile in %s",
		hostExecNetworkNotAllowed, profile.Name, hostExecProfilesFileName,
	)
}
