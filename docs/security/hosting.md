# Hosting and subprocessors

Editions: Cloud. This page describes where Preloop Cloud (preloop.ai) runs
and which third parties process data for it. It does not apply to
self-hosted Preloop: a self-hosted install sends nothing to us except the
optional daily version check described in
[Your data](security-privacy.md#your-data), and nothing to the companies
listed here unless you configure them yourself.

Last reviewed: 3 November 2026.

## Where Preloop Cloud runs

| What | Provider | Location |
| --- | --- | --- |
| Application, gateway, database and console | Hetzner Online GmbH | Falkenstein, Germany (datacenter FSN1-DC8) |
| Flow executions (the containers that run agents for hosted flows) | Google Cloud | Belgium (`europe-west1-b`) |
| Database backups (continuous archive, 7 day retention policy) | Amazon Web Services, S3 | United States (`us-east-1`) |
| Error reports from the application | Hetzner Online GmbH | Finland |

Flows routed to a [private runner](../guide/runners/quickstart-linux.md)
run on your machine, not on the flow execution host.

## Subprocessors

| Subprocessor | Purpose | Data it receives |
| --- | --- | --- |
| Hetzner Online GmbH | Hosting | Everything Preloop Cloud stores |
| Google Cloud | Flow execution host | Flow prompts, workspace files and repository checkouts while a hosted flow runs |
| Amazon Web Services | Database backup storage | A compressed copy of the database |
| Brevo | Transactional email | Recipient address and message content: invitations, password resets, and approval requests (tool name, agent name, summary, agent reasoning and redacted tool arguments) |
| Apple Push Notification service | Mobile push to iOS devices | Approval request notifications: tool name, agent name, agent reasoning, summary and redacted tool arguments |
| Firebase Cloud Messaging (Google) | Mobile push to Android devices | The same approval request notification content |
| Model providers you select | Inference on Preloop-hosted model credentials | The prompts and tool results of requests you send to that model |

Preloop-hosted model credentials are configured today for DeepSeek, Google
(Gemini API), OpenAI and Alibaba Cloud Model Studio. A request goes only to
the provider of the model you picked. When you bring your own provider key,
that provider is your processor, not ours.

Slack, Mattermost and other chat or webhook destinations are configured by
you and receive only what you route to them. They are not our
subprocessors.

## Roles

- For the records your agents produce (sessions, tool calls, approvals,
  audit rows, evidence packs), you are the controller and Preloop is the
  processor.
- For account and billing data (your login, team membership, invoices),
  Preloop is the controller. Card payments are handled by Stripe.

A data processing agreement is signed at contract.

## Related

- [Security and privacy](security-privacy.md)
- [Credentials at rest](credentials-at-rest.md)
- [Sample signed evidence pack](sample-evidence-pack.md)
