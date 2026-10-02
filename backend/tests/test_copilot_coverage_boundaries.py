"""The Copilot coverage page must state the shipped host-flow behaviour.

``docs/guide/copilot.md`` is a coverage matrix, so a stale cell is read as
a product boundary. Two cells once said that a private-runner host flow
ignores the flow's MCP settings and that the runner refuses
``--additional-mcp-config`` outright. The runner writes a per-job MCP file
and supplies that flag itself
(``hostExecMCPConfig`` in ``cli/internal/cmd/runner_host_exec_flow.go``);
only an operator-supplied copy in profile ``argv`` is refused
(``copilotManagedFlags`` in ``cli/internal/cmd/runner_host_exec_copilot.go``).
The page must keep those two statements apart, and must keep ticket-level
dollars and publication separate answers from MCP governance.

See GitHub issue 1059.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = REPO_ROOT / "docs" / "guide"
COVERAGE = GUIDE / "copilot.md"
LAUNCHER = GUIDE / "copilot-cli.md"
USAGE_IMPORT = GUIDE / "copilot-usage-import.md"

#: Claims the runner does not implement, in the wording the page used.
STALE_ASSERTIONS = (
    "Flow MCP settings do not apply",
    "Only the runner user's own MCP file",
    "is a flag the runner refuses",
)

#: The replacement coverage, in the order a reader meets it.
FLOW_MCP_FACTS = (
    "preloop-flow",
    "execution-scoped token",
    "--additional-mcp-config",
    "copilotManagedFlags",
    "profile `argv` is refused",
)

#: Boundary wording for the host flow, per the matrix column of that name.
BOUNDARY_FACTS = (
    "create_pull_request",
    "native CLI session resume",
    "not the full ticket-to-PR factory",
)

#: The sentence that marks the tracking issue as open work, not as shipped.
#: It is matched whole, link and marker included, so the check does not
#: depend on where the issue number is first mentioned on the page.
OPEN_ISSUE_TRACKING = (
    "[#1069](https://github.com/preloop/preloop/issues/1069) tracks Bitbucket "
    "publication and feedback continuation for host flows. It is open."
)


def _matrix_rows(text: str) -> list[list[str]]:
    """Return the coverage matrix as a list of split rows.

    Only the first contiguous run of table lines is read, so a later table
    on the page cannot be mistaken for the matrix.
    """
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def _flat(text: str) -> str:
    """Collapse wrapped prose so an assertion is not tied to line breaks."""
    return " ".join(text.split())


def _host_flow_row(rows: list[list[str]]) -> list[str]:
    """Return the matrix row for the private-runner host profile."""
    for row in rows:
        if row and "private-runner host profile" in row[0]:
            return row
    raise AssertionError("the coverage matrix has no private-runner host profile row")


def test_coverage_page_drops_the_stale_host_flow_claims() -> None:
    text = _flat(COVERAGE.read_text(encoding="utf-8"))
    for claim in STALE_ASSERTIONS:
        assert claim not in text, claim


def test_coverage_page_documents_the_flow_scoped_mcp_server() -> None:
    text = _flat(COVERAGE.read_text(encoding="utf-8"))
    for fact in FLOW_MCP_FACTS:
        assert fact in text, fact
    # The operator-facing file stays documented as its own source, without
    # a promise of governance for servers that do not point at Preloop.
    assert "`~/.copilot/mcp-config.json`" in text
    assert "point at Preloop's `/mcp/v1`" in text


def test_managed_flag_refusal_stays_documented() -> None:
    """The runner still refuses an operator-supplied managed flag."""
    coverage = _flat(COVERAGE.read_text(encoding="utf-8"))
    assert "cannot replace the managed server" in coverage
    launcher = _flat(LAUNCHER.read_text(encoding="utf-8"))
    assert "the operator cannot supply or replace the flow's MCP server" in launcher, (
        "the launcher guide must keep naming the managed-flag refusal"
    )


def test_matrix_answers_each_boundary_separately() -> None:
    """A reader can see MCP, sessions, metering, dollars and publication apart."""
    rows = _matrix_rows(COVERAGE.read_text(encoding="utf-8"))
    header, separator, *surfaces = rows
    assert len(separator) == len(header)
    assert header[1:] == [
        "MCP governed",
        "Models metered",
        "Hook sessions",
        "Spend visible",
        "Dollars per ticket",
        "Publication and feedback",
        "Status today",
    ]
    assert surfaces, "the matrix has no surface rows"
    for row in surfaces:
        assert len(row) == len(header), row[0]

    dollars = header.index("Dollars per ticket")
    publication = header.index("Publication and feedback")
    host = _host_flow_row(surfaces)
    assert host[dollars].startswith("No."), host[dollars]
    assert "rejected before the run" in host[publication], host[publication]


def test_host_flow_boundaries_and_open_issue_are_stated() -> None:
    text = _flat(COVERAGE.read_text(encoding="utf-8"))
    for fact in BOUNDARY_FACTS:
        assert fact in text, fact
    # The open issue is referenced as tracked work, never as shipped. Match
    # the tracking sentence rather than a fixed window after the first
    # "#1069" on the page: another cell citing the same issue moves that
    # first mention, and a window anchored to it would then fail while the
    # page is still correct.
    assert OPEN_ISSUE_TRACKING in text, "the page must call #1069 open, not shipped"


def test_import_page_separates_ticket_dollars_from_daily_figures() -> None:
    text = _flat(USAGE_IMPORT.read_text(encoding="utf-8"))
    assert "Not per ticket" in text
    assert "Not per request" in text
    assert "Unknown is not zero" in text
