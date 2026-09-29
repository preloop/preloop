# Use Cases: AI Automation with Human Oversight

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Preloop is designed to unlock the power of AI agents while maintaining the safety and quality standards of human engineering. Here are some key use cases where this "human-in-the-loop" approach shines.

## 1. Automated Bug Triage & Preliminary Analysis

**The Challenge**: Engineering teams spend countless hours triaging new bug reports, checking for duplicates, and gathering missing context.

**The Preloop Solution**:

1.  **Trigger**: A new issue is created in Jira or GitHub.
2.  **Agent Action**: An AI agent (e.g., Claude Code) is triggered via a Flow. It reads the issue, searches the vector database for similar past issues, and analyzes the codebase to identify potential causes.
3.  **Outcome**: The agent posts a comment on the issue with a summary, a list of potential duplicates, and a suggested starting point for the fix.
4.  **Human Oversight**: A human engineer reviews the analysis. If the agent proposes a label change or a priority update, the human approves it with a single click.

## 2. Secure Code Refactoring & Modernization

**The Challenge**: Large-scale refactoring (e.g., migrating to a new library version) is tedious and error-prone.

**The Preloop Solution**:

1.  **Trigger**: A scheduled task or a manual trigger initiates the flow.
2.  **Agent Action**: An agent like OpenHands scans the repository, identifies usages of the deprecated library, and generates a Pull Request with the necessary code changes.
3.  **Safety Rail**: The agent attempts to run the test suite.
4.  **Human Oversight**:
    *   **Code Review**: The PR is assigned to a senior engineer.
    *   **Deployment Approval**: If the agent attempts to trigger a deployment pipeline, Preloop intercepts the call and requires explicit approval from a release manager.

## 3. Incident Response Assistance

**The Challenge**: During an incident, speed is critical, but panic can lead to mistakes.

**The Preloop Solution**:

1.  **Trigger**: An alert from PagerDuty or an observability tool.
2.  **Agent Action**: An agent gathers logs, metrics, and recent deployment history relevant to the alert. It drafts a preliminary incident report and posts it to the incident Slack channel.
3.  **Human Oversight**: The on-call engineer reviews the gathered data, verifying the agent's findings before taking remediation actions. The agent is restricted from executing "write" operations (like restarting servers) without explicit human confirmation.

## 4. Documentation Maintenance

**The Challenge**: Documentation often drifts out of sync with the code.

**The Preloop Solution**:

1.  **Trigger**: A Pull Request is merged to the `main` branch.
2.  **Agent Action**: An agent analyzes the code changes and identifies affected documentation pages. It drafts a PR to update the documentation.
3.  **Human Oversight**: A technical writer reviews the proposed documentation changes to ensure clarity and accuracy before merging.

## 5. Release Evidence Legwork for a Human Assessor

**The Challenge**: Product security reviews ask the same questions every release: is this SBOM usable, which components carry known-exploited vulnerabilities, what changed since the last release, and who decided this component was appropriate. Assembling that by hand per release does not scale, and the assessment itself is human work that cannot be delegated to an agent.

**The Preloop Solution**:

1.  **Trigger**: A release tag pushes a webhook from CI with the build's SBOM attached inline.
2.  **Agent Action**: A [security audit preset](flows/security-audit-presets.md) checks SBOM validity and minimum elements, screens components against OSV.dev with a CISA KEV cross-check, computes drift against the previous run, and writes a versioned `result.json` plus a human-readable evidence pack. Deterministic checks stay separate from agent judgment, and every source records a negative control so an empty result is never reported as a clean component.
3.  **Safety Rail**: The audit presets hold no write tools. CI gates the release on the recorded verdict, and the pack is retained whether the gate passes or fails.
4.  **Human Oversight**: A human assessor reads the pack and carries the assessment. For a single integrated component, the due diligence preset does the legwork and then routes the risk decision to a named reviewer through a Preloop approval, so the recorded decision has a human behind it. The packs are machine-generated evidence, not a conformity assessment, not a certification, and not legal advice.
