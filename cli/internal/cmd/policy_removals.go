package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/spf13/cobra"
)

// policyRemoval is one thing an apply would take away from an existing tool.
// A tool entry replaces the tool's whole configuration, so an approval
// workflow or condition the file does not repeat is removed (#2558).
type policyRemoval struct {
	Tool string
	// Workflow is the approval workflow that would be dropped, if any.
	Workflow string
	// Condition describes a condition that would be dropped, if any.
	Condition string
}

func (r policyRemoval) String() string {
	if r.Workflow != "" {
		return fmt.Sprintf("REMOVES approval workflow %q from tool %s", r.Workflow, r.Tool)
	}
	return fmt.Sprintf("REMOVES condition from tool %s: %s", r.Tool, r.Condition)
}

const policyToolPathPrefix = "$.tools[name="

// policyToolRemovals lists the approval workflows and conditions that the
// diff's tool modifications would drop.
func policyToolRemovals(diff *PolicyDiff) []policyRemoval {
	if diff == nil {
		return nil
	}
	var removals []policyRemoval
	for _, change := range diff.Changes {
		if change.Operation != "modify" || !strings.HasPrefix(change.Path, policyToolPathPrefix) {
			continue
		}
		tool := strings.TrimSuffix(strings.TrimPrefix(change.Path, policyToolPathPrefix), "]")
		oldTool, _ := change.OldValue.(map[string]interface{})
		newTool, _ := change.NewValue.(map[string]interface{})
		if oldTool == nil {
			continue
		}
		oldWorkflow, _ := oldTool["approval_workflow"].(string)
		newWorkflow, _ := newTool["approval_workflow"].(string)
		if oldWorkflow != "" && newWorkflow == "" {
			removals = append(removals, policyRemoval{Tool: tool, Workflow: oldWorkflow})
		}
		kept := map[string]bool{}
		for _, condition := range policyConditions(newTool) {
			kept[policyCanonicalJSON(condition)] = true
		}
		for _, condition := range policyConditions(oldTool) {
			if !kept[policyCanonicalJSON(condition)] {
				removals = append(removals, policyRemoval{Tool: tool, Condition: describePolicyCondition(condition)})
			}
		}
	}
	sort.SliceStable(removals, func(i, j int) bool { return removals[i].Tool < removals[j].Tool })
	return removals
}

func policyConditions(tool map[string]interface{}) []interface{} {
	if tool == nil {
		return nil
	}
	conditions, _ := tool["conditions"].([]interface{})
	return conditions
}

// policyCanonicalJSON encodes a decoded JSON value with sorted map keys.
func policyCanonicalJSON(value interface{}) string {
	data, err := json.Marshal(value)
	if err != nil {
		return fmt.Sprintf("%v", value)
	}
	return string(data)
}

func describePolicyCondition(condition interface{}) string {
	fields, ok := condition.(map[string]interface{})
	if !ok {
		return policyCanonicalJSON(condition)
	}
	expression, _ := fields["expression"].(string)
	action, _ := fields["action"].(string)
	if action == "" {
		action = "require_approval"
	}
	return fmt.Sprintf("%s (%s)", expression, action)
}

func printPolicyRemovals(out io.Writer, removals []policyRemoval) {
	if len(removals) == 0 {
		return
	}
	fmt.Fprintf(out, "\nRemovals (%d): a tool entry replaces that tool's whole configuration.\n", len(removals)) //nolint:errcheck
	for _, removal := range removals {
		fmt.Fprintf(out, "  ! %s\n", removal) //nolint:errcheck
	}
	fmt.Fprintln(out, "  Repeat the existing approval_workflow and conditions in the file to keep them.") //nolint:errcheck
}

// confirmPolicyRemovals decides whether an apply that removes approval
// workflows or conditions may proceed. --yes and PRELOOP_CONFIRM proceed; a
// terminal is asked; anything else stops so unattended runs never hang and
// never drop a workflow silently.
func confirmPolicyRemovals(cmd *cobra.Command, out io.Writer, removals []policyRemoval) error {
	yes, _ := cmd.Flags().GetBool("yes")
	if yes {
		fmt.Fprintln(out, "Proceeding with the removals (--yes).") //nolint:errcheck
		return nil
	}
	if !nonInteractiveAutoConfirm() && !stdinIsTerminal() {
		return fmt.Errorf("policy would remove %d approval workflow(s) or condition(s); nothing was applied. Re-run with --yes to apply anyway", len(removals))
	}
	confirmed, err := confirmAction(cmd.InOrStdin(), out, fmt.Sprintf("Apply this policy and make %d removal(s)? (y/N): ", len(removals)))
	if err != nil {
		return fmt.Errorf("failed to read confirmation: %w", err)
	}
	if !confirmed {
		return fmt.Errorf("apply cancelled; nothing was applied")
	}
	return nil
}
