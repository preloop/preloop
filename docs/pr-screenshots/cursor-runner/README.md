# Cursor runner console walk

Screenshots from a real local Preloop stack with a fresh account and a personal
macOS runner. The runner's installed Cursor CLI reported `signed_in`, profile
`cursor`, model `auto`, `flow_execution_consent: ask_me`, and
`accept_untagged: false`. Account and display names are generic local fixtures.
No API responses were mocked for this walk.

| Screenshot | Verified interaction |
| --- | --- |
| [Agents](agents.png) | Cursor appears in the managed agent list. |
| [Cursor agent](cursor-agent-create-flow.png) | Visible flow and runner links without Agent Control. |
| [Flow consent](flow-cursor-consent.png) | Cursor selects the personal runner and explains `ask_me` and unmetered usage. |
| [Existing agent](flow-existing-agent.png) | Explains why Cursor is absent and switches back to the runner route. |
| [Runners](runners-cursor.png) | Signed-in inventory entry links to a Cursor flow on this runner. |
| [Runner to flow](runner-to-flow.png) | The runner link preselects Cursor and displays its owner's consent. |

The stock reviewer was also triggered manually against an open Azure Repos
test PR. The owner approved its actual consent request; execution then failed
with `host_checkout_not_allowed` before Cursor started. This is a measured
limitation, not a successful end-to-end review. See the
[setup and limitations guide](../../guide/cursor-runner-review.md).
