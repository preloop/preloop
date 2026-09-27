# CRA Article 14 reporting runbooks

Operational fill-in templates for the three reports CRA Article 14 requires
from a manufacturer once it becomes aware of an actively exploited
vulnerability in its product or of a severe incident having an impact on the
security of its product: an early warning within 24 hours, a vulnerability or
incident notification within 72 hours, and a final report (14 days after a
corrective or mitigating measure is available for a vulnerability, one month
after the notification for a severe incident).

This document is procedure, not legal advice. It does not decide whether a
specific event is reportable and it does not restate the regulation as a legal
conclusion. The report is filed by a person with authority to sign it, through
the official submission channel (the single reporting platform operated by
ENISA, routed to the CSIRT designated as coordinator). Nothing in the Preloop
product files anything on its own; see the
[Article 14 section of the audit preset guide](../guide/flows/security-audit-presets.md#cra-article-14-reporting).

## Roles

Fill in the names for your deployment once and keep them current. The roles
are the stable part; the names rotate.

| Role | Responsibility | Assigned to |
| --- | --- | --- |
| Incident owner | Runs the clock, owns the timeline, signs the reports | `____________` |
| Security triage | Confirms exploitation evidence and affectedness | `____________` |
| Release manager | Owns the corrective release and its evidence pack | `____________` |
| Communications | User-facing advisories, coordinated disclosure | `____________` |

One person may hold several roles. The incident owner must be reachable within
the 24 hour window; publish an escalation path (paging, backup owner) next to
this table in your internal copy.

## Becoming aware: what starts the clock

The clocks in Article 14 run from the moment the manufacturer becomes aware.
Any of the following counts as a candidate awareness event and must reach the
incident owner the same day:

- A `reportable_candidate` assessment in the `reporting` block of a Release
  Security Audit or SBOM Exploit Check result (`result.json`).
- A KEV-listed CVE id in `art14_candidates` of an audit result.
- A vulnerability report to [security@preloop.ai](mailto:security@preloop.ai)
  claiming active exploitation.
- Exploitation observed in operations (logs, alerts, a compromised
  deployment).

Record the awareness timestamp in UTC immediately. Every template below
carries it, and the audit result records it per candidate as `discovered_at`
with the derived `deadlines` (`early_warning_24h`, `notification_72h`,
`final_report_14d`).

## Decision points

Work through these two questions before filing anything. Record the answer
and its basis in the template; if either answer is "undetermined", file the
early warning anyway when the deadline would otherwise lapse, and say so in
the report.

1. **Is the vulnerability actively exploited?** Evidence sources, strongest
   first: own telemetry or a confirmed compromise; a KEV listing
   (`exploited_evidence: kev` on the audit candidate); a vendor advisory
   (`exploited_evidence: vendor_advisory`); a credible external report. A
   vulnerability that is merely severe but not exploited does not start the
   Article 14 clock; it goes through the normal fix-and-disclose path.
2. **Is it instead (or also) a severe incident having an impact on the
   security of the product?** A compromise of build or release
   infrastructure, of the update channel, or of hosted customer data may be
   reportable as a severe incident even when no single CVE is involved. The
   incident path uses the same three templates with the incident wording and
   a one month final report deadline.

Affectedness is the third input: the audit candidate carries
`affected.value` (`true` / `false` / `"undetermined"`) with its source
(`vex`, `reachability`, `manual`). A delivered VEX `not_affected` statement
with a machine-readable justification is evidence the product is not
affected; verify the justification still holds before relying on it.

## Where the evidence comes from

Each audit run produces an evidence pack (result JSON, findings list, SBOMs,
VEX documents, gap register). The templates below name the field they are
filled from, so a rehearsal or a real filing is a lookup, not an
investigation:

| Template field | Source |
| --- | --- |
| Vulnerability id | `reporting.candidates[].id` in `result.json` |
| Exploitation evidence | `reporting.candidates[].exploited_evidence`, KEV snapshot (`kev_snapshot_date`, `kev_source_url`) |
| Affectedness and basis | `reporting.candidates[].affected` (`value`, `source`, `detail`), `vex_status` |
| Awareness time and deadlines | `reporting.candidates[].discovered_at`, `reporting.candidates[].deadlines` |
| Affected component and version | the release SBOMs in the evidence pack (`sbom/*.cdx.json`) |
| Corrective measure evidence | the release notes, `SHA256SUMS`, build attestation of the fixing release |

## Template 1: early warning (within 24 hours of awareness)

Purpose: tell the CSIRT that an actively exploited vulnerability (or a severe
incident) exists. It is short by design; do not wait for analysis to finish.

```text
EARLY WARNING (CRA Article 14)
Status of this report:            [initial | update to report filed at <UTC time>]
Manufacturer:                     [legal entity name and contact]
Single point of contact:          [name, role, e-mail, phone]
Product:                          [product name and affected version range]
Type:                             [actively exploited vulnerability | severe incident]
Vulnerability id (if assigned):   [CVE/advisory id, or "not yet assigned"]
Became aware at (UTC):            [timestamp, and how awareness arose]
Exploitation evidence:            [kev | vendor advisory | own telemetry | external report]
Member States where the product
is made available (if known):     [list, or "distributed generally via public download"]
Cross-border relevance (if any):  [known impact outside the filing state, or "none known"]
Requested confidentiality:        [handling expectation for this report]
Filed by:                         [name, role]  Filed at (UTC): [timestamp]
```

Owner: incident owner. Decision needed before filing: question 1 (or 2)
above answered "yes" or deadline about to lapse with the answer still
"undetermined".

## Template 2: vulnerability notification (within 72 hours of awareness)

Purpose: the substance. Everything from the early warning plus what is known
by now. Mark clearly which statements are still preliminary.

```text
VULNERABILITY NOTIFICATION (CRA Article 14)
Reference to early warning:       [id or timestamp of template 1 filing]
Product and affected versions:    [from the release SBOM of each affected release]
General nature of the exploit:    [attack vector, preconditions, observed use]
Nature of the vulnerability:      [class, component, severity score if scored]
Affectedness determination:       [affected.value with source: vex | reachability | manual]
Corrective or mitigating measures
taken by the manufacturer:        [fix shipped / in progress, workaround, none yet]
Measures users can take:          [upgrade target, configuration change, mitigation]
Sensitivity of this information:  [how sensitive the manufacturer considers it]
Updated exploitation picture:     [changes since the early warning]
Filed by:                         [name, role]  Filed at (UTC): [timestamp]
```

Owner: incident owner, with security triage supplying the affectedness
determination and the release manager supplying the measures section.

## Template 3: final report (within 14 days of a corrective or mitigating measure being available; one month after the notification for a severe incident)

Purpose: close the loop. Filed once the fix or mitigation exists.

```text
FINAL REPORT (CRA Article 14)
Reference:                        [ids of the template 1 and 2 filings]
Description of the vulnerability: [component, versions, class]
Severity and impact:              [score and basis; observed impact on deployments]
Malicious actor information:      [what is known about who exploited it, if anything]
Details of the security update or
other corrective measures:        [fixing release and date, SHA256SUMS digest,
                                   build attestation reference, advisory link]
How users obtain the fix:         [update channel, upgrade instructions]
Residual risk:                    [what remains for users who cannot upgrade]
Filed by:                         [name, role]  Filed at (UTC): [timestamp]
```

Owner: incident owner signs; release manager attaches the corrective measure
evidence (release notes, checksums, attestation) from the release evidence
pack.

## Rehearsal checklist

Run this drill at least twice a year, and after any change to roles or
tooling. It uses a synthetic candidate, files nothing, and should finish
inside one hour.

- [ ] Pick a past audit result (`result.json`) and locate the `reporting`
      block and `art14_candidates`.
- [ ] Confirm the roles table above names reachable people; page the incident
      owner through the published escalation path and measure response time.
- [ ] Take one finding (or invent one) and answer decision points 1 and 2 in
      writing, citing the evidence fields.
- [ ] Fill template 1 completely from the evidence pack within 30 minutes.
- [ ] Fill template 2, marking every field that would still be preliminary at
      the 72 hour mark.
- [ ] Verify the submission channel: the current URL of the single reporting
      platform and which CSIRT acts as coordinator for the filing entity, and
      that whoever files has working credentials.
- [ ] Verify the evidence pack retention: the drill's chosen `result.json`
      and its tarball are still retrievable.
- [ ] Record the drill date, participants and findings in the internal
      incident log, and fix whatever the drill surfaced.

## Retention

Keep every filed report, the evidence pack it was filled from, and the drill
records for the support period stated in [SECURITY.md](../../SECURITY.md).
The run-over-run audit trail (scheduled re-audits) is the record that
vulnerability handling continued between releases.
