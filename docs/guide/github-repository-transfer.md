# Moving a GitHub repository to another organisation

A GitHub repository keeps its repository ID when it is transferred to another
owner or renamed. Preloop treats that ID as the repository's identity, so a
moved repository keeps its existing project: the same project ID, issues, flow
triggers, audit records and review history.

## Before you start

1. Give the destination organisation's integration access to the repository.
   For a GitHub App installed on selected repositories, add the repository to
   the installation.
2. Make sure the destination organisation, and the repository if you use
   repository rules, are included in that integration's scope.

Do not register the repository again with `POST /api/v1/projects`. That
returns `409 repository_already_registered` with the existing project ID.

## Preview and apply

Preview the move. Nothing changes:

```bash
curl -X POST "$PRELOOP_URL/api/v1/projects/$PROJECT_ID/transfer" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"organization_id": "'"$DESTINATION_ORG_ID"'"}'
```

The receipt shows the source and destination organisation and repository
name, the changes, anything not carried over, and any manual actions. Apply
it with `"dry_run": false`. Repeating the call returns `status: unchanged`.

The caller needs the `manage_trackers` permission, and both organisations must
belong to the caller's account. Before changing anything, Preloop lists the
repositories the destination integration can see, using that integration's
own credentials. The move is refused unless the repository is among them.

## What moves and what does not

Moves: the project, its issues and history, and the repository owner/name
that cloning and review publication use. Reviews are then published through
the destination organisation's integration.

Not copied: repository rules on the source integration, and anything else
that granted access through the old owner. The receipt lists them under
`not_carried_over`. Re-create any you still want on the destination
deliberately. Flows that pin the old repository URL and repository webhooks
registered by the old integration are listed under `manual_actions`.

## Errors

| `detail.code` | Meaning |
| --- | --- |
| `destination_cannot_see_repository` | Grant the destination integration access to the repository. |
| `destination_out_of_scope` | Include the destination organisation (or repository) in the destination integration's scope. |
| `destination_has_duplicate` | The destination already has a project for this repository. The response names it and its issue count. Delete it if it holds nothing you need, then retry. |
| `destination_integration_inactive` | Reactivate the destination integration. |
| `different_repository_host`, `unsupported_tracker` | Only GitHub repositories on the same GitHub host can be moved. |

## Renames

A rename within the same organisation is picked up by the next tracker
refresh. To refresh it at once, call the same endpoint with the project's
current organisation (`status: updated`).
