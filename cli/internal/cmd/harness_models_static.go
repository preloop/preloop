package cmd

// Static model lists shipped with the CLI for harnesses that cannot list
// their models non-interactively (Copilot CLI has no `models` command). The
// harness still decides: a model it does not offer fails fast with the
// existing "--model ... not available" mapping. Inventory reports these with
// source "static". Keep the lists short and update them by PR.
var harnessStaticModels = map[string][]string{
	"copilot_cli": {
		"auto",
		"claude-sonnet-4.6",
		"claude-sonnet-4.5",
		"claude-haiku-4.5",
		"gpt-5.2",
		"gpt-5-mini",
	},
	"cursor_cli": {
		"auto",
	},
}

// harnessStaticModelsFor returns a copy of the static list for a harness.
// The version is accepted so a list can be keyed by CLI version later.
func harnessStaticModelsFor(harness, _ string) []string {
	return append([]string(nil), harnessStaticModels[harness]...)
}
