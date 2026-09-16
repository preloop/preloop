"""Portfolio review orchestrator preset (017), fanning out to children.

The layer above the full-repo review family: it discovers the projects
in one repository, asks a human which of them to review, starts one
child execution per selected project per lens, parks while they run,
aggregates what their result envelopes said, asks which follow ups to
keep, and writes a portfolio report. It is NOT one of the four lenses,
so test_repo_review_presets.py does not parametrize over it: it carries
the built-in ask_user, run_flow and get_execution tools on its allowlist
and it samples projects rather than files. What it does inherit from the
family is pinned here: no write tools, the one-minute verdict cover, the
disclaimer, the not_checkable vocabulary, and the rule that the register
can never upgrade a verdict.

This module pins what is specific to the orchestrator:

* discovery is deterministic and command only — the walk in this test
  re-runs the preset's own closed detector list, named exclusion list,
  depth cap, nesting rule and SBOM lookup against the fixture
  repositories, so a recorded project list cannot quietly stop being
  reproducible;
* the triage hint is computed from those facts alone, which is why the
  worst written project in the fixture repository ranks last;
* the fan out: one child per selected project per lens, the payload each
  child is started with, the callable list a lens has to be on, the caps
  that stop a fan out and the coverage they have to declare;
* the aggregation: a lens row is what a child reported, a failed,
  refused, expired or not_checkable row is never healthy, and a
  project's cost is its children's recorded cost;
* the two question forms: batched, one call, the selectable ids drawn
  from discovery, and the auto-select threshold below which nothing is
  asked;
* the safe default when either question expires;
* the verdict rules, including the one that keeps a project nobody
  reviewed out of the healthy column;
* the size budget a twenty five project portfolio has to fit in.

Deterministic: parses the shipped YAML and the synthetic fixtures under
fixtures/review/portfolio, and validates every child completion record
against the frozen delegation shapes. No agent, no network, no control
plane.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
PRESET_FILE = "017-portfolio-review.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "review" / "portfolio"
REPOS_DIR = FIXTURES / "repos"
RESULTS_DIR = FIXTURES / "results"
CHILDREN_DIR = FIXTURES / "children"
SCHEMA_FILE = FIXTURES / "schemas" / "portfolio-v1.json"

SCHEMA_ID = "preloop.review.portfolio/v1"
FLOW_SLUG = "portfolio-review"
PRESET_NAME = "Portfolio Review"
LENS_SLUG = "docs-currency-review"
LENS_SCHEMA_ID = "preloop.review.docscurrency/v1"
SECURITY_LENS_SLUG = "release-security-audit"

# The callable list the preset declares, and the schema each lens emits.
# A lens outside this mapping cannot be started at all.
LENS_SCHEMAS = {
    "docs-currency-review": "preloop.review.docscurrency/v1",
    "repo-code-health-review": "preloop.review.codehealth/v1",
    "release-security-audit": "preloop.cra.releaseaudit/v1",
}

# The SBOM names PHASE 1 looks for, project-local, closed list.
SBOM_GLOBS = (
    "*.spdx.json",
    "*.cdx.json",
    "*.spdx",
    "bom.json",
    "sbom*.json",
    "sbom*.xml",
)

NOT_CHECKABLE_REASON = "no SBOM available"
SBOM_FOLLOW_UP_TITLE = "add SBOM generation to this project's build"
SBOM_FOLLOW_UP_SLUG = "add-sbom-generation"

DISCLAIMER = (
    "Machine-generated review evidence. Not a certification, audit opinion, "
    "or legal advice."
)

THREE_DAYS = 259200
RUN_DATE = date(2026, 9, 16)

# One scenario per acceptance criterion: the repository is the input, the
# result is the contracted output for it, and the children file is the
# fan out the platform recorded for that run.
SCENARIOS = {
    "result-five-selected.json": "five-projects",
    "result-two-auto-selected.json": "two-projects",
    "result-first-question-expired.json": "five-projects",
    "result-second-question-expired.json": "five-projects",
    "result-child-failed.json": "five-projects",
    "result-child-cap.json": "five-projects",
    "result-security-lenses.json": "five-projects",
}

# The preset's closed detector list, mirrored here so the walk below is
# the preset's walk. test_detector_list_is_the_one_the_preset_declares
# fails if the two ever drift apart.
DETECTORS = {
    "package.json": "node",
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "requirements.txt": "python",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "jvm",
    "build.gradle": "jvm",
    "build.gradle.kts": "jvm",
    "build.sbt": "scala",
    "composer.json": "php",
    "Gemfile": "ruby",
    "*.gemspec": "ruby",
    "*.csproj": "dotnet",
    "*.fsproj": "dotnet",
    "*.sln": "dotnet",
    "CMakeLists.txt": "cpp",
    "pubspec.yaml": "dart",
    "mix.exs": "elixir",
    "Package.swift": "swift",
    "deno.json": "deno",
    "deno.jsonc": "deno",
}

# The preset's named exclusion list, mirrored for the same reason.
EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "vendor",
    "third_party",
    "bower_components",
    "dist",
    "build",
    "out",
    "target",
    "bin",
    "obj",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".gradle",
    ".m2",
    ".next",
    ".nuxt",
    ".svelte-kit",
    "site-packages",
    "coverage",
    "htmlcov",
    "Pods",
    ".terraform",
    ".idea",
    ".vscode",
    ".cache",
    ".direnv",
}

DEPTH_CAP = 3

# The triage rules, weights and reason names the preset documents.
TRIAGE_RULES = (
    ("no_commits_12m", 1),
    ("no_readme", 2),
    ("no_architecture_doc", 1),
    ("no_ci", 1),
    ("no_tests_dir", 1),
    ("no_licence", 1),
)


def _norm(text: str) -> str:
    """Collapse whitespace so asserts survive YAML line wrapping."""
    return " ".join(text.split())


def _load_preset() -> dict:
    path = PRESETS_DIR / PRESET_FILE
    assert path.exists(), f"Missing preset file: {path}"
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def _prompt() -> str:
    return _load_preset()["prompt_template"]


def _required_shape_keys() -> list[str]:
    """Top-level keys of the YAML "Required shape" block (the contract)."""
    prompt = _prompt()
    marker = f"Required shape ({SCHEMA_ID}):"
    start = prompt.find(marker)
    assert start != -1, f"prompt missing {marker!r}"
    brace = prompt.find("{", start)
    depth = 0
    in_str = False
    keys: list[str] = []
    index = brace
    while index < len(prompt):
        char = prompt[index]
        if in_str:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_str = False
            index += 1
            continue
        if char == '"':
            end = prompt.index('"', index + 1)
            if depth == 1 and prompt[end + 1 :].lstrip().startswith(":"):
                keys.append(prompt[index + 1 : end])
            index = end + 1
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                break
        index += 1
    assert keys, "Required shape had no top-level keys"
    return keys


def _load_result(name: str) -> dict:
    path = RESULTS_DIR / name
    assert path.exists(), f"Missing result fixture: {path}"
    return json.loads(path.read_text())


def _load_children(name: str) -> dict:
    """The fan out the platform recorded for one result fixture."""
    path = CHILDREN_DIR / name.replace("result-", "children-")
    assert path.exists(), f"Missing children fixture: {path}"
    return json.loads(path.read_text())


def _matching_detectors(names: list[str]) -> list[str]:
    """Detector filenames a directory listing matches, sorted."""
    matched = []
    for name in names:
        for detector in DETECTORS:
            if detector.startswith("*."):
                if name.endswith(detector[1:]):
                    matched.append(name)
                    break
            elif name == detector:
                matched.append(name)
                break
    return sorted(matched)


def _stack_of(filename: str) -> str:
    for detector, stack in DETECTORS.items():
        if detector.startswith("*."):
            if filename.endswith(detector[1:]):
                return stack
        elif filename == detector:
            return stack
    raise AssertionError(f"no stack for {filename}")


def _file_count(project: Path) -> int:
    """Files under a project, excluded directories not counted."""
    total = 0
    for path in project.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(project).parts[:-1]
        if any(part in EXCLUDED_DIRS for part in parts):
            continue
        total += 1
    return total


def _sbom_paths(project: Path, root: Path) -> list[str]:
    """SBOM artifacts under one project, by name only, project-local."""
    found: set[str] = set()
    for pattern in SBOM_GLOBS:
        for path in project.rglob(pattern):
            if not path.is_file():
                continue
            parts = path.relative_to(project).parts[:-1]
            if any(part in EXCLUDED_DIRS for part in parts):
                continue
            found.add(path.relative_to(root).as_posix())
    return sorted(found)


def _facts(project: Path) -> dict:
    """The five documentation and tooling booleans, project-local only."""
    names = sorted(entry.name for entry in project.iterdir())
    docs = project / "docs"
    doc_names = sorted(entry.name for entry in docs.iterdir()) if docs.is_dir() else []
    return {
        "has_readme": any(name.startswith("README") for name in names),
        "has_architecture_doc": (
            any(name.startswith("ARCHITECTURE") for name in names)
            or any(name.startswith("architecture") for name in doc_names)
            or (docs / "adr").is_dir()
        ),
        "has_ci": (
            ".gitlab-ci.yml" in names
            or (project / ".github" / "workflows").is_dir()
            or "Jenkinsfile" in names
            or (project / ".circleci" / "config.yml").is_file()
            or "azure-pipelines.yml" in names
        ),
        "has_tests_dir": any(
            (project / name).is_dir() for name in ("tests", "test", "spec")
        ),
        "has_licence": any(
            name.startswith(("LICENSE", "LICENCE", "COPYING")) for name in names
        ),
    }


def _walk(repo: str) -> dict:
    """Re-run the preset's discovery walk over a fixture repository.

    Exclusion list first, depth cap second, detector list third, and a
    discovered project is never descended into.
    """
    root = REPOS_DIR / repo
    projects: list[dict] = []
    excluded: list[str] = []
    truncated: list[str] = []

    def visit(directory: Path, depth: int) -> None:
        names = sorted(entry.name for entry in directory.iterdir())
        files = sorted(entry.name for entry in directory.iterdir() if entry.is_file())
        manifests = _matching_detectors(files)
        rel = directory.relative_to(root).as_posix()
        if manifests and rel != ".":
            projects.append(
                {
                    "path": rel,
                    "stacks": sorted({_stack_of(name) for name in manifests}),
                    "manifests": [f"{rel}/{name}" for name in manifests],
                    "file_count": _file_count(directory),
                    "sbom_paths": _sbom_paths(directory, root),
                    "has_sbom": bool(_sbom_paths(directory, root)),
                    **_facts(directory),
                }
            )
            return
        subdirectories = [
            directory / name for name in names if (directory / name).is_dir()
        ]
        kept = []
        for subdirectory in subdirectories:
            if subdirectory.name in EXCLUDED_DIRS:
                excluded.append(subdirectory.relative_to(root).as_posix())
                continue
            kept.append(subdirectory)
        if depth == DEPTH_CAP:
            if kept:
                truncated.append(rel)
            return
        for subdirectory in kept:
            visit(subdirectory, depth + 1)

    visit(root, 0)
    return {
        "projects": sorted(projects, key=lambda row: row["path"]),
        "excluded_dirs": sorted(excluded),
        "dirs_truncated_at_cap": sorted(truncated),
    }


def _triage(project: dict, eol_table: object) -> dict:
    """The preset's triage hint, recomputed from the recorded facts."""
    score = 0
    reasons = []
    age = (RUN_DATE - date.fromisoformat(project["last_commit_date"])).days
    for name, points, floor in (
        ("stale_365", 3, 365),
        ("stale_180", 2, 180),
        ("stale_90", 1, 90),
    ):
        if age >= floor:
            score += points
            reasons.append(name)
            break
    if project["commits_12m"] == 0:
        score += 1
        reasons.append("no_commits_12m")
    for flag, points in (
        ("has_readme", 2),
        ("has_architecture_doc", 1),
        ("has_ci", 1),
        ("has_tests_dir", 1),
        ("has_licence", 1),
    ):
        if not project[flag]:
            score += points
            reasons.append(f"no_{flag[4:]}")
    if eol_table == "payload" and any(
        runtime["name"] in {"java", "python", "node"}
        and runtime["version"].lstrip(">=~^ ") in {"8", "3.8", "14"}
        for runtime in project["runtimes"]
    ):
        score += 3
        reasons.append("eol_runtime")
    band = "high" if score >= 6 else "medium" if score >= 3 else "low"
    return {"score": score, "band": band, "reasons": reasons}


def _health(lens_row: dict) -> str:
    """The health the preset derives from one lens row.

    Only a lens that ran carries a verdict; everything else, a failed,
    refused, expired or not_checkable row included, is unknown.
    """
    if lens_row["lens_status"] != "ran":
        return "unknown"
    return {
        "pass": "healthy",
        "pass_with_findings": "findings",
        "fail": "failing",
    }[lens_row["verdict"]]


def _project_status(project: dict) -> str:
    """The project status the preset's rules derive from its lens rows."""
    healths = {_health(row) for row in project["lenses"]}
    statuses = {row["lens_status"] for row in project["lenses"]}
    if "failing" in healths:
        return "failing"
    if statuses - {"ran"}:
        return "unknown"
    if "findings" in healths:
        return "findings"
    return "healthy"


def _verdict(result: dict) -> str:
    """The verdict the preset's own rules compute for a result.

    fail when any project is failing; pass only when every discovered
    project was reviewed by a lens that ran and every one of them is
    healthy; everything else, an unknown project included,
    pass_with_findings.
    """
    projects = result["projects"]
    coverage = result["coverage"]
    if any(row["status"] == "failing" for row in projects):
        return "fail"
    if (
        projects
        and coverage["plan_completed"]
        and not coverage["not_reviewed"]
        and all(row["status"] == "healthy" for row in projects)
    ):
        return "pass"
    return "pass_with_findings"


def _question(result: dict, phase: str) -> dict:
    rows = [row for row in result["questions"] if row["phase"] == phase]
    assert len(rows) == 1, f"expected exactly one {phase} question, got {len(rows)}"
    return rows[0]


def _healthy_row(path: str) -> dict:
    """A positive row: a project whose only lens ran and passed."""
    slug = path.replace("/", "-")
    return {
        "path": path,
        "status": "healthy",
        "cost_usd": 0.2,
        "lenses": [
            {
                "lens": LENS_SLUG,
                "lens_schema": LENS_SCHEMA_ID,
                "lens_status": "ran",
                "reason": None,
                "verdict": "pass",
                "health": "healthy",
                "counts": {
                    "holds": 4,
                    "drifted": 0,
                    "not_checkable": 0,
                    "high": 0,
                    "medium": 0,
                    "low": 0,
                },
                "child": {
                    "execution_id": f"child-{slug}",
                    "state": "SUCCEEDED",
                    "cost_usd": 0.2,
                    "label": f"{path}|{LENS_SLUG}",
                },
                "result_artifact": (
                    f"evidence/projects/{slug}/{LENS_SLUG}/result.json"
                ),
            }
        ],
    }


def _lens_rows(result: dict) -> list[tuple[str, dict]]:
    """Every lens row in a result, paired with its project path."""
    return [
        (project["path"], row)
        for project in result["projects"]
        for row in project["lenses"]
    ]


def _child_records(children: dict) -> dict:
    """Completion records keyed by child execution id."""
    return children["children"]


def _cost_of(record: dict) -> float | None:
    return record["metadata"].get("preloop.ai/cost")


@pytest.fixture(params=sorted(SCENARIOS), ids=str)
def scenario(request) -> tuple[str, dict, dict]:
    return (
        SCENARIOS[request.param],
        _load_result(request.param),
        _load_children(request.param),
    )


class TestPresetDefinition:
    """What the preset file itself promises."""

    def test_identity_and_family_membership(self):
        data = _load_preset()
        assert data["slug"] == FLOW_SLUG
        assert data["name"] == PRESET_NAME
        assert data["is_preset"] is True
        assert data["icon"]
        assert data["agent_type"] == "codex"
        assert data["agent_config"]["sandbox_type"] == "exec"
        assert data["agent_config"]["enable_auto_lint"] is False
        assert data["git_clone_config"] is None
        assert data["trigger_config"] is None
        assert data["trigger_event_source"] is None
        assert data["trigger_event_types"] is None
        assert SCHEMA_ID in data["description"]
        assert SCHEMA_ID in data["prompt_template"]
        assert f'"flow": "{FLOW_SLUG}"' in data["prompt_template"]
        assert DISCLAIMER in data["prompt_template"]

    def test_declares_no_write_tools(self):
        """Three platform tools, none of them a write tool: the question
        channel, the delegation call and the child read. No MCP server,
        and a prompt that forbids every write path including opening a
        pull request."""
        data = _load_preset()
        assert data["allowed_mcp_servers"] == []
        assert data["allowed_mcp_tools"] == [
            {"name": "ask_user"},
            {"name": "run_flow"},
            {"name": "get_execution"},
        ]
        norm = _norm(data["prompt_template"])
        assert "NO write tools" in norm
        assert "do not create issues, post comments, push commits" in norm
        assert "never run git commit or git push" in norm
        assert "Never modify tracked files" in norm
        assert (
            "The ONLY platform tools on your allowlist are the built-in "
            "ask_user, which is a question channel, run_flow, which starts "
            "one of the read-only review lenses named on this flow's callable "
            "list, and get_execution, which reads a child you started" in norm
        )
        assert "None of the three writes anything outside Preloop" in norm
        assert "you never open a pull request" in norm

    def test_nothing_is_ever_filed(self):
        """Approving a follow up is not filing it: this preset has no
        write tools, so the filed counters are pinned at zero."""
        norm = _norm(_prompt())
        assert "NOTHING IS EVER FILED BY THIS PRESET" in norm
        assert "rollup.issues_filed is always 0" in norm
        assert "follow_ups[].filed is always false" in norm
        assert "Approving is not filing and you never claim otherwise" in norm

    def test_out_of_scope_is_stated(self):
        """Security work belongs to a lens, modernisation to nobody here."""
        norm = _norm(_prompt())
        assert "SECURITY IS ONE OF THE LENSES, NOT YOUR JOB" in norm
        assert "Release Security Audit" in norm
        assert "You never do that work yourself" in norm
        assert "file ONE referral finding" in norm
        assert "NEVER a value" in norm
        assert "MODERNISATION IS OUT OF SCOPE" in norm
        assert "never fix, upgrade, refactor or rewrite anything" in norm

    def test_orchestrator_does_not_read_source(self):
        norm = _norm(_prompt())
        assert (
            "YOU NEVER READ PROJECT SOURCE CODE AND NEVER FORM AN OPINION "
            "ABOUT IT" in norm
        )
        assert "the assessment itself belongs to the child executions" in norm

    def test_detector_list_is_the_one_the_preset_declares(self):
        """The walk in this module mirrors the preset's closed detector
        list; if the YAML grows a detector, this test fails until the
        mirror is updated."""
        prompt = _prompt()
        for detector, stack in DETECTORS.items():
            assert detector in prompt, f"detector missing from the preset: {detector}"
            assert f"-> {stack}" in prompt, f"stack missing from the preset: {stack}"
        norm = _norm(prompt)
        assert (
            "A PROJECT IS A DIRECTORY CONTAINING AT LEAST ONE MANIFEST OR "
            "BUILD DESCRIPTOR FROM THIS CLOSED DETECTOR LIST" in norm
        )
        assert "A detector you invent is a bug" in norm

    def test_exclusion_list_is_the_one_the_preset_declares(self):
        prompt = _prompt()
        for directory in EXCLUDED_DIRS:
            assert directory in prompt, (
                f"exclusion missing from the preset: {directory}"
            )
        norm = _norm(prompt)
        assert "NAMED EXCLUSION LIST" in norm
        assert "a vendored package.json is not a project" in norm

    def test_depth_cap_and_nesting_rule_are_declared_and_reported(self):
        norm = _norm(_prompt())
        assert "max_depth: how many directory levels below root_path" in norm
        assert "default 3" in norm
        assert "DEPTH CAP" in norm
        assert "discovery.dirs_truncated_at_cap" in norm
        assert "A truncated branch is a coverage statement" in norm
        assert "NESTING RULE" in norm
        assert "not a sixth project" in norm

    def test_discovery_records_the_named_per_project_facts(self):
        prompt = _prompt()
        for field in (
            "path",
            "stacks",
            "manifests",
            "runtimes",
            "last_commit_date",
            "commits_12m",
            "file_count",
            "has_readme",
            "has_architecture_doc",
            "has_ci",
            "has_tests_dir",
            "has_licence",
        ):
            assert field in prompt, f"discovery field missing: {field}"
        norm = _norm(prompt)
        assert "COMMANDS ONLY, DETERMINISTIC" in norm
        assert "no model judgment at all" in norm
        assert (
            "two runs over the same commit must produce the same list in the "
            "same order" in norm
        )

    def test_triage_hint_is_deterministic_and_never_an_opinion(self):
        prompt = _prompt()
        norm = _norm(prompt)
        assert "TRIAGE HINT (DETERMINISTIC FACTS ONLY)" in prompt
        assert "THE HINT IS COMPUTED FROM THE PHASE 1 FACTS AND NOTHING ELSE" in norm
        assert (
            "It is never your opinion of the code, never a quality score, and "
            "never the output of reading a source file" in norm
        )
        assert (
            "A badly written project that is fresh, documented, tested and "
            "supported scores zero" in norm
        )
        for rule, points in (("stale_365", 3), ("stale_180", 2), ("stale_90", 1)):
            assert f"{rule} +{points}" in norm, f"missing staleness rule {rule}"
        for rule, points in TRIAGE_RULES:
            assert f"{rule} +{points}" in norm, f"missing triage rule {rule}"
        assert "eol_runtime +3" in norm
        assert (
            "The staleness rules are exclusive: at most one of stale_365, "
            "stale_180, stale_90 fires" in norm
        )
        assert "Band: high for 6 or more, medium for 3 to 5, low for 2 or less" in norm
        assert (
            "Rank the projects by (score descending, last_commit_date "
            "ascending, path ascending)" in norm
        )

    def test_end_of_life_is_never_recalled_from_memory(self):
        norm = _norm(_prompt())
        assert "eol_runtimes" in norm
        assert (
            "Delivered, it is the ONLY source for the end-of-life part of a "
            "triage hint" in norm
        )
        assert "You never decide from memory that a runtime is end of life" in norm
        assert "no delivered table means eol_runtime never fires" in norm

    def test_the_lenses_are_reused_not_restated(self):
        """Each lens owns its claim types, severities and verdict rules;
        this preset only decides which projects they run on."""
        norm = _norm(_prompt())
        for preset_file in (
            "backend/presets/016-docs-currency-review.yaml",
            "009-repo-code-health-review.yaml",
            "006-release-security-audit.yaml",
        ):
            assert preset_file in norm, f"lens definition not cited: {preset_file}"
        assert "DO NOT RESTATE, VARY, RELAX OR EXTEND A LENS" in norm
        assert (
            "this preset only decides which projects they run on and reports "
            "what they said" in norm
        )
        for schema_id in LENS_SCHEMAS.values():
            assert schema_id in norm, f"lens schema not named: {schema_id}"

    def test_the_caps_are_declared_with_their_ceilings(self):
        """A project cap and a child cap, each with the hard ceiling it
        cannot be raised past, and a coverage statement rather than a
        failure when one of them stops the fan out."""
        norm = _norm(_prompt())
        assert (
            "max_projects: how many selected projects this run fans out for, "
            "default 5, hard cap 12 (PROJECT CAP, PHASE 4)" in norm
        )
        assert (
            "max_children: the ceiling on child executions this run starts, "
            "default 20, hard cap 25" in norm
        )
        assert "PROJECT CAP: max_projects selected projects" in norm
        assert "CHILD CAP: max_children child executions" in norm
        assert "Planned calls past either cap ARE NOT MADE" in norm
        assert "record each of them in fan_out.children_over_cap" in norm
        assert (
            "put every project that got no lens run at all into "
            'coverage.not_reviewed with reason "child cap" (or "project cap")' in norm
        )
        assert "set coverage.plan_completed false, and FINISH THE REPORT ANYWAY" in norm
        assert "A ceiling is a coverage statement, not a failure" in norm

    def test_evidence_pack_and_one_page_cover(self):
        prompt = _prompt()
        norm = _norm(prompt)
        assert "/workspace/evidence/" in prompt
        assert "portfolio-report.md" in prompt
        assert "projects-register.md" in prompt
        assert "findings.json" in prompt
        assert "inventory.json" in prompt
        assert "questions.json" in prompt
        assert "children.json" in prompt
        assert (
            "children.json: one row per planned call: project, lens, whether "
            "it was made, the child execution id, its final state, its cost "
            "and the refusal reason when there is one" in norm
        )
        assert (
            "projects/<project slug>/<lens slug>/result.json: the child's own "
            "result envelope, copied verbatim" in norm
        )
        assert "MUST OPEN" in prompt
        assert "one-page cover" in prompt
        assert "at the TOP of the report" in norm
        assert "Verdict sentence first" in prompt
        for box in (
            'BOX 1 — "What we checked"',
            'BOX 2 — "What we did NOT check"',
            'BOX 3 — "What you should do next week"',
        ):
            assert box in prompt, f"missing cover box: {box}"
        assert "HONESTY RAIL" in prompt
        assert "may only summarize" in norm
        assert "No new claims" in prompt
        assert "May not be empty if anything was out of scope" in norm
        assert "Strictly one page" in norm
        assert (
            "the lenses this run did not start at all: architecture, standards "
            "compliance, and any lens the payload named that is not on the "
            "callable list" in norm
        )
        assert (
            "every child that failed, was refused or expired, every project "
            "whose security row is not_checkable for want of an SBOM" in norm
        )
        assert (
            "the fan out table (one row per planned call with its child "
            "execution id, state and cost)" in norm
        )
        assert "As your FINAL action, write /workspace/result.json" in prompt

    def test_completion_status_and_verdict_vocabulary(self):
        prompt = _prompt()
        norm = _norm(prompt)
        assert '"status": "success" | "error"' in norm
        assert '"status" is REQUIRED — it is the flow completion signal' in norm
        assert "regardless of the verdict" in norm
        assert "including the inventory-only run a question expiry produces" in norm
        assert '"pass" | "pass_with_findings" | "fail"' in prompt

    def test_verdict_rules_are_stated(self):
        norm = _norm(_prompt())
        assert "Verdict, computed from LENS RESULTS AND COVERAGE ONLY" in norm
        assert '"fail" if any project\'s status is "failing"' in norm
        assert (
            '"pass" only when every discovered project was reviewed by a lens '
            "that ran" in norm
        )
        assert (
            "any failed, refused or expired child and any truncated coverage, "
            'is "pass_with_findings"' in norm
        )
        assert "THE REGISTER CANNOT UPGRADE THE VERDICT" in norm
        assert (
            "A project nobody reviewed is not evidence of health, an "
            'inventory-only run is never a "pass"' in norm
        )
        assert "A PROJECT NO LENS REVIEWED IS NEVER COUNTED AS HEALTHY" in norm
        assert "HEALTH IS DERIVED, NEVER ASSERTED" in norm
        assert (
            "A FAILED, REFUSED, EXPIRED OR NOT_CHECKABLE LENS IS NEVER A PASS" in norm
        )

    def test_facts_and_judgment_stay_separated(self):
        prompt = _prompt()
        assert '"checks"' in prompt
        assert '"assessments"' in prompt

    def test_size_budget_names_the_twenty_five_project_portfolio(self):
        norm = _norm(_prompt())
        assert "SIZE BUDGET" in norm
        assert "under 200 KB" in norm
        assert (
            "Keep every project row under 4 KB, lens rows and child records "
            "included, and every discovery row under 2 KB" in norm
        )
        assert "A 25 PROJECT PORTFOLIO MUST STILL FIT" in norm
        assert "null rather than inventing values" in norm

    def test_url_hygiene(self):
        prompt = _prompt()
        norm = _norm(prompt)
        assert "URL HYGIENE" in prompt
        assert "169.254.169.254" in prompt
        assert "loopback, private-range, link-local" in norm
        assert "never fetch them anyway" in norm

    def test_one_repository_per_run(self):
        prompt = _prompt()
        norm = _norm(prompt)
        assert "exactly one per run" in norm
        assert "HEAD commit SHA (40 hex)" in norm
        assert "target_repo_path" in prompt
        assert "repository_url" in prompt


class TestQuestionForms:
    """Two batched questions, a long window, and a stated safe default."""

    def test_the_declared_window_matches_the_flow_field(self):
        data = _load_preset()
        assert data["approval_window_seconds"] == THREE_DAYS
        assert f"timeout_seconds: {THREE_DAYS}" in data["prompt_template"]

    def test_both_questions_are_one_batched_call(self):
        norm = _norm(_prompt())
        assert "make EXACTLY ONE ask_user call for the whole portfolio, batched" in norm
        assert "NEVER one call per project, never a second round" in norm
        assert "never ask a human to type JSON into free text" in norm
        assert "Otherwise make EXACTLY ONE ask_user call, batched, with the" in norm
        assert (
            "Call the tool by the exact namespaced name your tool catalog "
            "lists for the preloop MCP server" in norm
        )
        assert (
            "a routing failure is not an answer, it fails closed like an expiry" in norm
        )

    def test_the_questions_use_items_and_an_input_schema(self):
        prompt = _prompt()
        norm = _norm(prompt)
        assert "Pass ONE ROW PER DISCOVERED PROJECT in items, in rank order" in norm
        for fragment in (
            '"id": "<project path, exactly as discovery recorded it>"',
            '"severity": "high|medium|low" (the triage band)',
            '"selected": {"type": "array", "title": "Projects to review"',
            '"items": {"enum": [<the item ids>]}',
            '"approved": {"type": "array", "title": "Follow ups to keep"',
            '"id": {"type": "string", "enum": [<the item ids>]}',
            '"x-autofill": "author"',
            '"x-autofill": "date"',
        ):
            assert fragment in prompt, f"missing question form fragment: {fragment}"
        assert "THE SELECTABLE IDS ARE EXACTLY THE DISCOVERED PROJECT PATHS" in norm

    def test_a_long_window_parks_the_run(self):
        norm = _norm(_prompt())
        assert "PARKS the execution" in norm
        assert "holds no container and no budget" in norm
        assert "_answers_prompt" in norm
        assert "RESUMED AFTER A HUMAN DECISION" in norm
        assert "do not wait for a tool result that will not come" in norm
        assert "Do NOT parse prose: the array is the answer" in norm

    def test_below_the_threshold_nothing_is_asked(self):
        norm = _norm(_prompt())
        assert "BELOW THE THRESHOLD, DO NOT ASK" in norm
        assert "auto_select_threshold: default 3" in norm
        assert (
            'the selection source is "auto_below_threshold", and no question '
            "is asked" in norm
        )

    def test_the_first_safe_default_is_inventory_only(self):
        norm = _norm(_prompt())
        assert "SAFE DEFAULT ON EXPIRY: INVENTORY ONLY" in norm
        assert "NO CHILD IS STARTED, no follow up is ranked, no issue is opened" in norm
        assert (
            "every discovered project is reported with no lens row that ran "
            'and status "unknown"' in norm
        )
        assert "NAMES THE DEADLINE THAT PASSED" in norm
        assert (
            "Never re-ask, never assume a selection, never treat silence as "
            '"review everything"' in norm
        )

    def test_the_second_safe_default_keeps_nothing_and_still_reports(self):
        norm = _norm(_prompt())
        assert (
            "SAFE DEFAULT ON EXPIRY: KEEP NOTHING, AND THE REPORT STILL LANDS" in norm
        )
        assert 'leaves EVERY candidate with status "unapproved"' in norm
        assert "nothing is filed, nothing is opened" in norm
        assert "approval is recorded, not executed" in norm


class TestPresetLoadsIntoTheCatalogue:
    """The gallery entry: the loader must pick 017 up and expose it."""

    def test_preset_appears_in_the_catalogue_with_its_gallery_fields(self):
        from unittest.mock import patch

        from preloop.flow_presets import load_flow_presets

        load_flow_presets.cache_clear()
        try:
            with patch("preloop.flow_presets.PRESETS_DIRS", [PRESETS_DIR]):
                catalog = load_flow_presets()
        finally:
            load_flow_presets.cache_clear()

        entries = [entry for entry in catalog if entry["name"] == PRESET_NAME]
        assert len(entries) == 1, f"{PRESET_NAME} not in the catalogue exactly once"
        entry = entries[0]
        # What the picker renders.
        assert entry["description"]
        assert entry["icon"]
        assert entry["prompt_template"]
        assert entry["is_preset"] is True
        # Slug is loader-internal identity and never reaches consumers.
        assert "slug" not in entry


class TestResultSchema:
    """The new schema, and the fixtures that have to satisfy it."""

    def test_schema_is_a_valid_json_schema(self):
        schema = json.loads(SCHEMA_FILE.read_text())
        Draft202012Validator.check_schema(schema)

    def test_schema_required_matches_the_yaml_contract(self):
        schema = json.loads(SCHEMA_FILE.read_text())
        assert schema["required"] == _required_shape_keys()
        extra = set(schema["properties"]) - set(_required_shape_keys())
        assert extra == set(), f"schema invents fields the YAML does not name: {extra}"

    def test_result_validates_against_the_schema(self, scenario):
        _, result, children = scenario
        schema = json.loads(SCHEMA_FILE.read_text())
        Draft202012Validator(schema).validate(result)

    def test_result_carries_every_required_key(self, scenario):
        _, result, children = scenario
        missing = [key for key in _required_shape_keys() if key not in result]
        assert missing == [], f"result fixture missing required keys: {missing}"
        assert result["schema"] == SCHEMA_ID
        assert result["flow"] == FLOW_SLUG
        assert result["status"] == "success"
        assert result["disclaimer"] == DISCLAIMER

    def test_fixtures_are_synthetic(self, scenario):
        repo, result, children = scenario
        assert result["note"] == "synthetic fixture"
        blob = "\n".join(
            path.read_text()
            for path in sorted((REPOS_DIR / repo).rglob("*"))
            if path.is_file()
        ).lower()
        assert "synthetic fixture" in blob


class TestDiscovery:
    """Deterministic, command only, and reproducible from the tree."""

    def test_the_recorded_projects_are_the_ones_the_walk_finds(self, scenario):
        repo, result, children = scenario
        walked = {row["path"]: row for row in _walk(repo)["projects"]}
        recorded = {row["path"]: row for row in result["discovery"]["projects"]}
        assert sorted(recorded) == sorted(walked)
        assert result["discovery"]["count"] == len(recorded)
        for path, row in recorded.items():
            found = walked[path]
            for field in (
                "stacks",
                "manifests",
                "file_count",
                "has_readme",
                "has_architecture_doc",
                "has_ci",
                "has_tests_dir",
                "has_licence",
            ):
                assert row[field] == found[field], f"{path}: {field} drifted"

    def test_five_projects_and_none_of_the_excluded_directories(self):
        result = _load_result("result-five-selected.json")
        walked = _walk("five-projects")
        assert [row["path"] for row in walked["projects"]] == [
            "legacy/inventory-web",
            "libs/shared-utils",
            "services/billing-api",
            "services/notifications",
            "tools/report-cli",
        ]
        assert result["discovery"]["count"] == 5
        # Every trap really is in the tree, and none of them is a project.
        traps = [
            ".venv/pyproject.toml",
            "build/Cargo.toml",
            "libs/vendor/pom.xml",
            "node_modules/left-pad/package.json",
            "services/notifications/node_modules/left-pad/package.json",
            "services/notifications/ui-kit/package.json",
            "platform/edge/gateway/proxy/go.mod",
        ]
        for trap in traps:
            assert (REPOS_DIR / "five-projects" / trap).is_file(), f"missing {trap}"
            parent = str(Path(trap).parent)
            assert parent not in [row["path"] for row in walked["projects"]]
        assert walked["excluded_dirs"] == result["discovery"]["excluded_dirs"]
        assert set(result["discovery"]["excluded_dirs"]) == {
            ".venv",
            "build",
            "libs/vendor",
            "node_modules",
        }

    def test_the_depth_cap_is_reported_not_silent(self):
        result = _load_result("result-five-selected.json")
        walked = _walk("five-projects")
        assert result["discovery"]["depth_cap"] == DEPTH_CAP
        assert result["discovery"]["depth_cap_hit"] is True
        assert (
            walked["dirs_truncated_at_cap"]
            == result["discovery"]["dirs_truncated_at_cap"]
            == ["platform/edge/gateway"]
        )

    def test_a_nested_manifest_does_not_make_a_second_project(self):
        """ui-kit lives inside a discovered project, so it is part of it:
        it is not discovered, but its file is counted."""
        result = _load_result("result-five-selected.json")
        rows = {row["path"]: row for row in result["discovery"]["projects"]}
        assert "services/notifications/ui-kit" not in rows
        assert rows["services/notifications"]["manifests"] == [
            "services/notifications/package.json"
        ]
        assert rows["services/notifications"]["file_count"] == 5

    def test_declared_runtimes_are_read_out_of_the_manifest_they_cite(self, scenario):
        repo, result, children = scenario
        for row in result["discovery"]["projects"]:
            for runtime in row["runtimes"]:
                source = REPOS_DIR / repo / runtime["source"]
                assert source.is_file(), f"{row['path']}: no such manifest {source}"
                assert runtime["version"] in source.read_text(), (
                    f"{row['path']}: {runtime['version']} is not in {runtime['source']}"
                )

    def test_every_manifest_matches_a_detector(self, scenario):
        _, result, children = scenario
        for row in result["discovery"]["projects"]:
            for manifest in row["manifests"]:
                assert _matching_detectors([Path(manifest).name]), (
                    f"{manifest} matches no detector"
                )
            assert row["stacks"] == sorted(
                {_stack_of(Path(manifest).name) for manifest in row["manifests"]}
            )


class TestTriageHint:
    """Computed from deterministic facts, never from the code."""

    def test_the_recorded_hint_is_the_one_the_rules_compute(self, scenario):
        _, result, children = scenario
        eol_table = result["discovery"]["eol_table"]
        for row in result["discovery"]["projects"]:
            assert row["triage"] == _triage(row, eol_table), (
                f"{row['path']}: recorded triage hint is not the computed one"
            )

    def test_the_rank_is_the_documented_ordering(self, scenario):
        _, result, children = scenario
        rows = result["discovery"]["projects"]
        expected = sorted(
            rows,
            key=lambda row: (
                -row["triage"]["score"],
                row["last_commit_date"],
                row["path"],
            ),
        )
        assert [row["path"] for row in expected] == [
            row["path"] for row in sorted(rows, key=lambda row: row["rank"])
        ]
        assert [row["rank"] for row in sorted(rows, key=lambda r: r["rank"])] == list(
            range(1, len(rows) + 1)
        )

    def test_a_badly_written_fresh_project_ranks_below_a_stale_one(self):
        """The fixture that keeps the hint honest: report-cli is the worst
        written project in the repository and the last one a human is asked
        about, because nothing in the hint has read it."""
        result = _load_result("result-five-selected.json")
        rows = {row["path"]: row for row in result["discovery"]["projects"]}
        fresh = rows["tools/report-cli"]
        stale = rows["legacy/inventory-web"]
        source = (REPOS_DIR / "five-projects" / "tools/report-cli/main.go").read_text()
        # The fixture really is badly written.
        assert "handels" in source  # spelling
        assert "reviewr" in source  # spelling
        assert source.count("if ") >= 3  # nested branching in one function
        assert fresh["triage"]["score"] == 0
        assert fresh["triage"]["band"] == "low"
        assert fresh["rank"] > stale["rank"]
        assert stale["triage"]["band"] == "high"

    def test_the_hint_reasons_come_from_the_closed_vocabulary(self, scenario):
        _, result, children = scenario
        allowed = {
            "stale_365",
            "stale_180",
            "stale_90",
            "no_commits_12m",
            "no_readme",
            "no_architecture_doc",
            "no_ci",
            "no_tests_dir",
            "no_licence",
            "eol_runtime",
        }
        for row in result["discovery"]["projects"]:
            unknown = set(row["triage"]["reasons"]) - allowed
            assert unknown == set(), f"{row['path']}: invented triage reasons {unknown}"


class TestSelectionQuestion:
    """One batched question, with the discovered paths as its ids."""

    def test_five_projects_produce_one_question_with_five_rows(self):
        result = _load_result("result-five-selected.json")
        discovered = [row["path"] for row in result["discovery"]["projects"]]
        assert len(discovered) == 5
        assert result["selection"]["auto_select_threshold"] == 3
        asked = [row for row in result["questions"] if row["asked"]]
        selection = _question(result, "selection")
        assert selection["asked"] is True
        assert len(selection["items"]) == 5
        assert sorted(selection["selectable_ids"]) == sorted(discovered)
        assert sorted(selection["items"]) == sorted(discovered)
        # Batched: one selection question for the whole portfolio, never one
        # per project.
        assert [row["phase"] for row in asked].count("selection") == 1
        assert result["selection"]["source"] == "human"
        assert selection["answered_by"]

    def test_the_rows_are_in_rank_order(self):
        result = _load_result("result-five-selected.json")
        ranked = [
            row["path"]
            for row in sorted(
                result["discovery"]["projects"], key=lambda row: row["rank"]
            )
        ]
        assert _question(result, "selection")["items"] == ranked

    def test_two_projects_below_the_threshold_ask_nothing(self):
        result = _load_result("result-two-auto-selected.json")
        assert result["discovery"]["count"] == 2
        assert result["selection"]["auto_select_threshold"] == 3
        selection = _question(result, "selection")
        assert selection["asked"] is False
        assert selection["status"] == "not_asked"
        assert selection["reason"] == (
            "2 projects discovered, below the auto select threshold of 3"
        )
        # The selection source records why nobody was asked.
        assert result["selection"]["source"] == "auto_below_threshold"
        assert result["selection"]["selected"] == [
            row["path"] for row in result["discovery"]["projects"]
        ]
        assert result["coverage"]["projects_reviewed"] == 2

    def test_every_result_records_both_question_phases(self, scenario):
        _, result, children = scenario
        assert [row["phase"] for row in result["questions"]] == [
            "selection",
            "follow_ups",
        ]
        for row in result["questions"]:
            if not row["asked"]:
                assert row["status"] == "not_asked"
                assert row["reason"], f"{row['phase']}: not asked without a reason"


class TestFirstQuestionExpired:
    """The safe default: an inventory, and a run that still completed."""

    @pytest.fixture()
    def result(self) -> dict:
        return _load_result("result-first-question-expired.json")

    def test_selection_source_and_deadline_are_recorded(self, result):
        assert result["selection"]["source"] == "expired_default"
        assert result["selection"]["selected"] == []
        selection = _question(result, "selection")
        assert selection["asked"] is True
        assert selection["status"] == "expired"
        assert selection["expires_at"] == "2026-09-19T09:02:00Z"
        assert selection["answered_by"] is None
        # The report names the deadline that passed.
        check = next(
            row
            for row in result["checks"]
            if row["name"] == "selection_question_answered"
        )
        assert check["passed"] is False
        assert selection["expires_at"] in check["details"]

    def test_zero_reviews_zero_issues_and_a_successful_run(self, result):
        assert result["status"] == "success"
        assert result["coverage"]["projects_reviewed"] == 0
        assert result["rollup"]["issues_filed"] == 0
        assert result["follow_ups"] == []
        assert all(row["lens_status"] == "not_run" for _, row in _lens_rows(result))
        assert result["rollup"]["by_health"]["unknown"] == 5

    def test_no_child_execution_was_started(self, result):
        """The expiry is a safe default, not a fan out nobody asked for."""
        children = _load_children("result-first-question-expired.json")
        assert children["calls"] == []
        assert children["children"] == {}
        fan_out = result["fan_out"]
        assert fan_out["children_planned"] == 0
        assert fan_out["children_started"] == 0
        assert fan_out["parked"] is False
        assert result["rollup"]["children_started"] == 0
        assert result["rollup"]["children_cost_usd"] == 0

    def test_an_inventory_is_never_a_pass(self, result):
        assert result["verdict"] == "pass_with_findings"
        assert _verdict(result) == "pass_with_findings"

    def test_the_second_question_is_not_asked(self, result):
        follow_ups = _question(result, "follow_ups")
        assert follow_ups["asked"] is False
        assert follow_ups["reason"] == "no follow up candidates: no lens ran"


class TestSecondQuestionExpired:
    """The report still lands, with nothing approved and nothing filed."""

    @pytest.fixture()
    def result(self) -> dict:
        return _load_result("result-second-question-expired.json")

    def test_every_candidate_is_unapproved(self, result):
        assert result["follow_ups"], "the fixture needs candidates to leave unapproved"
        for row in result["follow_ups"]:
            assert row["status"] == "unapproved"
            assert row["approved_by"] is None
            assert row["approved_at"] is None
            assert row["filed"] is False
        assert result["rollup"]["follow_ups_approved"] == 0
        assert result["rollup"]["follow_ups_total"] == len(result["follow_ups"])

    def test_nothing_is_filed(self, result):
        assert result["rollup"]["issues_filed"] == 0

    def test_the_full_report_still_lands(self, result):
        assert result["status"] == "success"
        assert result["coverage"]["projects_reviewed"] == 3
        assert result["artifacts"]["report"] == "evidence/portfolio-report.md"
        assert result["verdict"] == _verdict(result) == "fail"
        expired = _question(result, "follow_ups")
        assert expired["status"] == "expired"
        assert expired["expires_at"]


class TestFollowUps:
    """Candidates a lens finding supports, ranked, never filed."""

    def test_follow_ups_point_at_a_real_line_of_the_repository(self, scenario):
        repo, result, children = scenario
        for row in result["follow_ups"]:
            path, _, line_no = row["evidence"].rpartition(":")
            document = REPOS_DIR / repo / path
            assert document.exists(), f"{row['id']}: no such file {path}"
            lines = document.read_text().splitlines()
            index = int(line_no)
            assert 1 <= index <= len(lines), f"{row['id']}: line {index} out of range"
            assert lines[index - 1].strip(), f"{row['id']}: points at a blank line"

    def test_follow_ups_belong_to_a_lens_row_that_reported(self, scenario):
        """A follow up needs a lens row behind it: a lens that ran, or the
        one exception the preset names, the security row that could not
        run for want of an SBOM."""
        _, result, children = scenario
        rows: dict[tuple[str, str], dict] = {
            (path, row["lens"]): row for path, row in _lens_rows(result)
        }
        for row in result["follow_ups"]:
            assert row["id"].startswith(f"portfolio:{row['project']}:")
            lens_row = rows.get((row["project"], row["lens"]))
            assert lens_row is not None, (
                f"{row['id']}: no {row['lens']} row for {row['project']}"
            )
            if row["id"].endswith(SBOM_FOLLOW_UP_SLUG):
                assert lens_row["lens_status"] == "not_checkable"
                continue
            assert lens_row["lens_status"] == "ran", (
                f"{row['id']}: follow up from a lens that did not run"
            )

    def test_every_follow_up_repeats_a_finding_a_child_reported(self, scenario):
        """Aggregation, not authorship: each follow up is a finding out of
        the child's own result envelope, pointer included."""
        _, result, children = scenario
        findings: dict[tuple[str, str], dict] = {}
        for call in children["calls"]:
            child = call["child_execution_id"]
            if child is None:
                continue
            for artifact in _child_records(children)[child].get("artifacts", []):
                envelope = artifact["parts"][0]["data"]
                for finding in envelope.get("findings", []):
                    findings[(call["project"], finding["id"])] = finding
        for row in result["follow_ups"]:
            slug = row["id"].rsplit(":", 1)[1]
            if slug == SBOM_FOLLOW_UP_SLUG:
                continue
            finding = findings.get((row["project"], slug))
            assert finding is not None, (
                f"{row['id']}: no child of this run reported that finding"
            )
            assert row["title"] == finding["title"]
            assert row["severity"] == finding["severity"]
            assert row["evidence"] == finding["evidence"]

    def test_follow_ups_are_ranked_and_never_filed(self, scenario):
        _, result, children = scenario
        ranks = [row["rank"] for row in result["follow_ups"]]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)
        assert all(row["filed"] is False for row in result["follow_ups"])
        assert result["rollup"]["issues_filed"] == 0


class TestCoverageAndHealth:
    """A project nobody reviewed is never healthy."""

    def test_a_project_no_lens_reviewed_is_unknown_and_uncovered(self, scenario):
        _, result, children = scenario
        not_reviewed = {
            row["path"]: row["reason"] for row in result["coverage"]["not_reviewed"]
        }
        for project in result["projects"]:
            ran = [row for row in project["lenses"] if row["lens_status"] == "ran"]
            if ran:
                continue
            assert project["status"] == "unknown"
            assert project["path"] in not_reviewed, (
                f"{project['path']}: no lens ran but coverage does not say so"
            )
            assert not_reviewed[project["path"]]

    def test_one_project_row_per_discovered_project(self, scenario):
        _, result, children = scenario
        discovered = [row["path"] for row in result["discovery"]["projects"]]
        assert sorted(row["path"] for row in result["projects"]) == sorted(discovered)
        assert result["coverage"]["projects_discovered"] == len(discovered)
        assert result["coverage"]["projects_reviewed"] == sum(
            1
            for project in result["projects"]
            if any(row["lens_status"] == "ran" for row in project["lenses"])
        )
        assert result["coverage"]["lens_runs_completed"] == sum(
            1 for _, row in _lens_rows(result) if row["lens_status"] == "ran"
        )

    def test_health_is_derived_from_the_lens_verdict(self, scenario):
        _, result, children = scenario
        for path, row in _lens_rows(result):
            assert row["health"] == _health(row), f"{path}: health is asserted"
            if row["lens_status"] == "ran":
                assert row["lens_schema"] == LENS_SCHEMAS[row["lens"]]
                assert row["result_artifact"]
                assert row["reason"] is None
            else:
                assert row["verdict"] is None
                assert row["health"] == "unknown"
                assert row["reason"], f"{path}: {row['lens_status']} without a reason"

    def test_project_status_is_derived_from_its_lens_rows(self, scenario):
        _, result, children = scenario
        for project in result["projects"]:
            assert project["status"] == _project_status(project), (
                f"{project['path']}: recorded status is not the derived one"
            )
            if project["status"] == "healthy":
                assert all(
                    row["lens_status"] == "ran" and row["verdict"] == "pass"
                    for row in project["lenses"]
                )

    def test_the_rollup_agrees_with_the_rows(self, scenario):
        _, result, children = scenario
        by_health = result["rollup"]["by_health"]
        for health in ("healthy", "findings", "failing", "unknown"):
            assert by_health[health] == sum(
                1 for row in result["projects"] if row["status"] == health
            )
        by_severity = result["rollup"]["by_severity"]
        for severity in ("high", "medium", "low"):
            assert by_severity[severity] == sum(
                1 for row in result["follow_ups"] if row["severity"] == severity
            )
        assert result["rollup"]["follow_ups_total"] == len(result["follow_ups"])
        assert result["rollup"]["follow_ups_approved"] == sum(
            1 for row in result["follow_ups"] if row["status"] == "approved"
        )

    def test_the_verdict_is_the_one_the_rules_compute(self, scenario):
        _, result, children = scenario
        assert result["verdict"] == _verdict(result)

    def test_a_complete_clean_portfolio_is_the_only_pass(self):
        result = _load_result("result-two-auto-selected.json")
        assert result["verdict"] == "pass"
        assert result["coverage"]["not_reviewed"] == []
        assert result["rollup"]["by_health"]["unknown"] == 0


class TestVerdictHonesty:
    """The family rule: a positive row can never raise a verdict."""

    def test_healthy_rows_cannot_rescue_a_failing_portfolio(self):
        result = _load_result("result-five-selected.json")
        assert _verdict(result) == "fail"
        result["projects"].extend(
            _healthy_row(f"extra/project-{index}") for index in range(20)
        )
        assert _verdict(result) == "fail"

    def test_healthy_rows_cannot_clear_an_unreviewed_project(self):
        result = _load_result("result-two-auto-selected.json")
        assert _verdict(result) == "pass"
        result["projects"].append(
            {
                "path": "apps/legacy",
                "status": "unknown",
                "cost_usd": 0.0,
                "lenses": [
                    {
                        "lens": LENS_SLUG,
                        "lens_schema": None,
                        "lens_status": "not_run",
                        "reason": "child cap",
                        "verdict": None,
                        "health": "unknown",
                        "counts": {"high": 0, "medium": 0, "low": 0},
                        "child": None,
                        "result_artifact": None,
                    }
                ],
            }
        )
        result["coverage"]["not_reviewed"].append(
            {"path": "apps/legacy", "reason": "child cap"}
        )
        assert _verdict(result) == "pass_with_findings"
        result["projects"].extend(
            _healthy_row(f"extra/project-{index}") for index in range(20)
        )
        assert _verdict(result) == "pass_with_findings"

    def test_truncated_coverage_cannot_pass(self):
        result = _load_result("result-two-auto-selected.json")
        result["coverage"]["plan_completed"] = False
        assert _verdict(result) == "pass_with_findings"

    def test_approved_follow_ups_never_raise_the_verdict(self):
        result = _load_result("result-second-question-expired.json")
        assert _verdict(result) == "fail"
        for row in result["follow_ups"]:
            row["status"] = "approved"
            row["approved_by"] = "user-2f0b1d4c"
            row["approved_at"] = "2026-09-16T11:12:00Z"
        assert _verdict(result) == "fail"


class TestSizeBudget:
    """A twenty five project portfolio has to fit the documented cap."""

    RESULT_CAP_BYTES = 200 * 1000
    PROJECT_ROW_CAP_BYTES = 4 * 1000
    DISCOVERY_ROW_CAP_BYTES = 2 * 1000

    def _grown_to(self, count: int) -> dict:
        """The five-project result, grown to a `count` project portfolio."""
        result = _load_result("result-five-selected.json")
        discovery_rows = list(result["discovery"]["projects"])
        project_rows = list(result["projects"])
        follow_ups = list(result["follow_ups"])
        index = 0
        while len(discovery_rows) < count:
            template = discovery_rows[index % len(result["discovery"]["projects"])]
            grown = json.loads(json.dumps(template))
            grown["path"] = f"{template['path']}-{index}"
            grown["rank"] = len(discovery_rows) + 1
            grown["manifests"] = [
                manifest.replace(template["path"], grown["path"])
                for manifest in template["manifests"]
            ]
            grown["runtimes"] = [
                {
                    **runtime,
                    "source": runtime["source"].replace(
                        template["path"], grown["path"]
                    ),
                }
                for runtime in template["runtimes"]
            ]
            discovery_rows.append(grown)
            project_template = json.loads(
                json.dumps(result["projects"][index % len(result["projects"])])
            )
            project_template["path"] = grown["path"]
            slug = grown["path"].replace("/", "-")
            for lens_row in project_template["lenses"]:
                if lens_row["result_artifact"]:
                    lens_row["result_artifact"] = (
                        f"evidence/projects/{slug}/{lens_row['lens']}/result.json"
                    )
                if lens_row["child"]:
                    lens_row["child"]["label"] = f"{grown['path']}|{lens_row['lens']}"
            project_rows.append(project_template)
            follow_ups.extend(
                {
                    **json.loads(json.dumps(candidate)),
                    "id": f"portfolio:{grown['path']}:{candidate['id'].rsplit(':', 1)[1]}",
                    "project": grown["path"],
                    "rank": len(follow_ups) + position + 1,
                }
                for position, candidate in enumerate(result["follow_ups"])
            )
            index += 1
        result["discovery"]["projects"] = discovery_rows
        result["discovery"]["count"] = len(discovery_rows)
        result["projects"] = project_rows
        result["follow_ups"] = follow_ups
        return result

    def test_a_twenty_five_project_result_stays_under_the_cap(self):
        result = self._grown_to(25)
        assert len(result["projects"]) == 25
        size = len(json.dumps(result).encode())
        assert size < self.RESULT_CAP_BYTES, (
            f"a 25 project portfolio serializes to {size} bytes, over the "
            f"{self.RESULT_CAP_BYTES} byte cap the preset documents"
        )

    def test_every_row_stays_inside_its_documented_budget(self, scenario):
        _, result, children = scenario
        for row in result["projects"]:
            size = len(json.dumps(row).encode())
            assert size < self.PROJECT_ROW_CAP_BYTES, (
                f"{row['path']}: project row is {size} bytes"
            )
        for row in result["discovery"]["projects"]:
            size = len(json.dumps(row).encode())
            assert size < self.DISCOVERY_ROW_CAP_BYTES, (
                f"{row['path']}: discovery row is {size} bytes"
            )

    def test_at_most_five_follow_ups_per_project(self, scenario):
        _, result, children = scenario
        per_project: dict[str, int] = {}
        for row in result["follow_ups"]:
            per_project[row["project"]] = per_project.get(row["project"], 0) + 1
        assert all(count <= 5 for count in per_project.values()), per_project


class TestFanOutDeclaration:
    """What the preset promises about delegation, before any run."""

    def test_the_callable_list_is_explicit(self):
        """The flow names the lenses it may start, with a bound on each.
        A lens outside the list cannot be started at all: the preset says
        so, and the platform refuses the call in the same words."""
        data = _load_preset()
        callable_flows = data["callable_flows"]
        assert [entry["flow"] for entry in callable_flows] == list(LENS_SCHEMAS)
        for entry in callable_flows:
            assert entry["max_children"] == 12
            assert 0 < entry["max_usd_per_child"] <= 3.0
        norm = _norm(data["prompt_template"])
        assert "THE CALLABLE LENSES ARE EXACTLY THESE" in norm
        assert "the payload cannot add to the list" in norm
        for lens, schema_id in LENS_SCHEMAS.items():
            assert f"{lens} -> {schema_id}" in norm, f"lens table missing {lens}"
        assert "A LENS ABSENT FROM THAT LIST IS REFUSED, NEVER SILENTLY SKIPPED" in norm
        assert (
            'record it in fan_out.lenses_refused with reason "not on the '
            'callable list"' in norm
        )
        assert "The platform enforces the same rule server side" in norm
        assert "TASK_STATE_REJECTED" in norm
        assert 'preloop.ai/refusalReason "flow_not_callable"' in norm

    def test_the_child_payload_is_the_repository_project_and_depth(self):
        """One call per project per lens, carrying the repository path
        selector, the project path and the depth knob, and nothing else."""
        norm = _norm(_prompt())
        assert "ONE CHILD EXECUTION PER PROJECT PER LENS" in norm
        assert (
            'run_flow( flow: "<lens slug>", payload: {"target_repo_path": '
            '"<the checkout this run used>", "project_path": "<the path '
            'discovery recorded>", "depth": "<the depth knob, unchanged>"}, '
            'label: "<project path>|<lens slug>", max_cost_usd: '
            "<max_cost_usd_per_child, default 2.0>, timeout_seconds: "
            "<child_timeout_seconds, default 3600>)" in norm
        )
        assert "THAT PAYLOAD AND NOTHING ELSE" in norm
        assert "Never send model or harness overrides" in norm
        assert "THE PLAN, DETERMINISTIC" in norm
        assert (
            "For each selected project IN RANK ORDER, for each chosen lens in "
            "the declared order, one planned call" in norm
        )

    def test_one_wait_covers_the_whole_fan_out_and_parks_the_run(self):
        norm = _norm(_prompt())
        assert "Pass wait: true ON THE LAST CALL ONLY" in norm
        assert "a wait per call would serialise a fan out" in norm
        assert "wait: true suspends this execution on WAITING_FOR_CHILDREN" in norm
        assert "the timeout budget pauses while they run" in norm
        assert "RESUMED AFTER THE FLOWS YOU STARTED FINISHED" in norm
        assert "the full records are in the trigger payload under children" in norm
        assert "DO NOT START THESE FLOWS AGAIN" in norm
        assert "You resume ONCE for the fan out" in norm
        assert "A run parks more than once over its life" in norm
        assert "TERMINAL IS NOT SUCCESSFUL" in norm

    def test_a_refusal_is_an_answer_and_a_ceiling_is_declared(self):
        norm = _norm(_prompt())
        assert "A REFUSAL IS AN ANSWER, NOT AN ERROR" in norm
        assert (
            "record the reason on that lens row, never retry it, never work "
            "around it, and never count the project as reviewed" in norm
        )
        assert "CEILINGS THIS PRESET EXPECTS" in norm
        assert (
            "direct children of one execution: 25 (FLOW_DELEGATION_MAX_CHILDREN)"
            in norm
        )
        assert "this flow's callable list entry per lens: 12 children" in norm
        assert "a child of this run is depth 1, the instance cap is 2" in norm
        assert "FLOW_DELEGATION_MAX_TREE_USD (50 USD by default)" in norm
        assert (
            "a parked parent waits FLOW_DELEGATION_CHILD_WAIT_SECONDS (6 hours)" in norm
        )
        assert "Never present a ceiling as a failure and never hide one" in norm

    def test_the_lens_status_vocabulary_is_closed_and_says_not_checkable(self):
        norm = _norm(_prompt())
        assert "LENS_STATUS, CLOSED VOCABULARY" in norm
        for status in (
            "ran",
            "failed",
            "refused",
            "expired",
            "not_checkable",
            "not_run",
        ):
            assert f'"{status}"' in norm, f"lens status missing: {status}"
        assert 'THE WORD IS "not_checkable", NEVER "skipped"' in norm
        assert "a skipped check reads as a choice and this is a missing input" in norm

    def test_the_security_lens_is_gated_on_an_sbom(self):
        norm = _norm(_prompt())
        assert "THE SECURITY LENS NEEDS AN SBOM" in norm
        assert (
            "It is planned for a project only when PHASE 1 recorded one under "
            "that project path (has_sbom true)" in norm
        )
        assert "Otherwise NO CHILD IS STARTED for it" in norm
        assert (
            'lens_status "not_checkable" with a non empty reason, "no SBOM '
            'available"' in norm
        )
        assert "A not_checkable row is never a pass" in norm
        assert "THE MISSING SBOM IS A FOLLOW UP, NOT A BLANK" in norm
        assert (
            'id "portfolio:<project path>:add-sbom-generation", title "add SBOM '
            "generation to this project's build\"" in norm
        )
        assert (
            "ONE PER PROJECT, NEVER TWO, AND NEVER FOR A PROJECT WHOSE SBOM "
            "WAS FOUND" in norm
        )
        assert "sbom_paths and has_sbom" in norm
        for pattern in SBOM_GLOBS:
            assert pattern in norm, f"SBOM name missing from the preset: {pattern}"
        assert "SIBLING PROJECT'S SBOM IS NOT THIS PROJECT'S SBOM" in norm

    def test_cost_is_the_child_s_own_record(self):
        norm = _norm(_prompt())
        assert "COST IS THE CHILD'S, NEVER AN ESTIMATE" in norm
        assert (
            "A project's cost_usd is the sum of the recorded cost of that "
            "project's children, exactly as the completion records report it "
            "(preloop.ai/cost)" in norm
        )
        assert "a refused call cost nothing and contributes nothing" in norm
        assert "a cost the records do not carry is null, never a guess" in norm
        assert "rollup.children_cost_usd is the same sum over every child" in norm


class TestFanOut:
    """The recorded fan out: one child per selected project per lens."""

    def test_three_projects_one_lens_start_three_children(self):
        """The first acceptance criterion: three selected projects and one
        lens produce exactly three child executions, each carrying its own
        project path in the payload."""
        result = _load_result("result-five-selected.json")
        children = _load_children("result-five-selected.json")
        assert result["fan_out"]["lenses"] == [LENS_SLUG]
        assert len(result["selection"]["selected"]) == 3
        calls = [call for call in children["calls"] if call["made"]]
        assert len(calls) == 3
        assert len(_child_records(children)) == 3
        assert result["fan_out"]["children_planned"] == 3
        assert result["fan_out"]["children_started"] == 3
        payload_paths = []
        for call in calls:
            arguments = call["call"]["arguments"]
            assert arguments["flow"] == LENS_SLUG
            assert set(arguments["payload"]) == {
                "target_repo_path",
                "project_path",
                "depth",
            }
            assert (
                arguments["payload"]["target_repo_path"]
                == (result["inputs_declared"]["target_repo_path"])
            )
            assert arguments["payload"]["depth"] == result["inputs_declared"]["depth"]
            assert arguments["label"] == f"{call['project']}|{LENS_SLUG}"
            payload_paths.append(arguments["payload"]["project_path"])
        assert sorted(payload_paths) == sorted(result["selection"]["selected"])

    def test_one_call_per_selected_project_per_chosen_lens(self, scenario):
        _, result, children = scenario
        discovered = {row["path"] for row in result["discovery"]["projects"]}
        planned = [(call["project"], call["lens"]) for call in children["calls"]]
        assert len(planned) == len(set(planned)), "a project and lens pair was "
        for project, lens in planned:
            assert project in discovered
            assert lens in LENS_SCHEMAS, f"{lens} is not on the callable list"
            assert lens in result["fan_out"]["lenses"]
        assert len(planned) == result["fan_out"]["children_planned"]
        assert len(planned) == result["coverage"]["lens_runs_planned"]

    def test_the_payload_of_every_call_is_the_documented_one(self, scenario):
        _, result, children = scenario
        for call in children["calls"]:
            if not call["made"]:
                assert call["call"] is None
                continue
            arguments = call["call"]["arguments"]
            assert call["call"]["tool"] == "run_flow"
            assert set(arguments["payload"]) == {
                "target_repo_path",
                "project_path",
                "depth",
            }
            assert arguments["payload"]["project_path"] == call["project"]
            assert (
                arguments["max_cost_usd"]
                <= (result["fan_out"]["max_cost_usd_per_child"])
            )
            assert arguments["timeout_seconds"] > 0

    def test_only_the_last_call_waits(self, scenario):
        """One wait covers the whole fan out; a wait per call would run
        the children one after another."""
        _, result, children = scenario
        made = [call for call in children["calls"] if call["made"]]
        waits = [
            index
            for index, call in enumerate(made)
            if call["call"]["arguments"].get("wait")
        ]
        if not made:
            assert waits == []
            return
        assert waits == [len(made) - 1], "wait: true is not on the last call only"
        assert result["fan_out"]["parked"] is True

    def test_the_parent_resumes_with_one_row_per_child(self, scenario):
        """One aggregated lens row per child execution, no more and no
        less, each naming the child it came from."""
        _, result, children = scenario
        records = _child_records(children)
        rows_with_children = [
            row for _, row in _lens_rows(result) if row["child"] is not None
        ]
        assert len(rows_with_children) == len(records)
        seen = [row["child"]["execution_id"] for row in rows_with_children]
        assert sorted(seen) == sorted(records)
        assert len(set(seen)) == len(seen), "a child was aggregated twice"
        for path, row in _lens_rows(result):
            if row["child"] is None:
                continue
            assert row["child"]["label"] == f"{path}|{row['lens']}"

    def test_the_counters_add_up(self, scenario):
        _, result, children = scenario
        fan_out = result["fan_out"]
        rollup = result["rollup"]
        assert fan_out["children_planned"] == (
            fan_out["children_started"] + len(fan_out["children_over_cap"])
        )
        assert fan_out["children_started"] == len(_child_records(children))
        assert rollup["children_started"] == fan_out["children_started"]
        assert rollup["children_started"] == (
            rollup["children_succeeded"]
            + rollup["children_failed"]
            + rollup["children_refused"]
            + rollup["children_expired"]
        )

    def test_child_records_match_the_frozen_delegation_shapes(self, scenario):
        """The fixtures are the platform's own records, not a restatement
        of them: they validate against the delegation schemas of #633."""
        from preloop.a2a.delegation import (
            validate_delegation_request,
            validate_delegation_task,
        )

        _, result, children = scenario
        for record in _child_records(children).values():
            validate_delegation_task(record)
            assert record["metadata"]["preloop.ai/depth"] == 1
        for call in children["calls"]:
            if call["request"] is None:
                continue
            validate_delegation_request(call["request"])
            assert call["request"]["metadata"]["preloop.ai/depth"] == 1

    def test_the_child_state_on_a_row_is_the_state_the_record_reports(self, scenario):
        _, result, children = scenario
        records = _child_records(children)
        for _path, row in _lens_rows(result):
            if row["child"] is None:
                continue
            record = records[row["child"]["execution_id"]]
            assert row["child"]["state"] == record["metadata"]["preloop.ai/status"]
            assert row["child"]["cost_usd"] == _cost_of(record)


class TestChildOutcomes:
    """A child that failed is reported as failed, never as healthy."""

    @pytest.fixture()
    def result(self) -> dict:
        return _load_result("result-child-failed.json")

    @pytest.fixture()
    def children(self) -> dict:
        return _load_children("result-child-failed.json")

    def test_a_failed_child_is_reported_as_failed_and_never_healthy(
        self, result, children
    ):
        project = next(
            row for row in result["projects"] if row["path"] == "services/billing-api"
        )
        row = next(
            lens for lens in project["lenses"] if lens["lens_status"] == "failed"
        )
        record = _child_records(children)[row["child"]["execution_id"]]
        assert record["status"]["state"] == "TASK_STATE_FAILED"
        assert record["metadata"]["preloop.ai/status"] == "FAILED"
        assert record.get("artifacts") == []
        assert row["verdict"] is None
        assert row["health"] == "unknown"
        assert row["reason"]
        assert row["child"]["execution_id"] in row["reason"]
        assert project["status"] != "healthy"
        assert project["status"] == "unknown"
        assert result["rollup"]["children_failed"] == 1
        assert result["verdict"] != "pass"

    def test_a_failed_child_leaves_its_project_uncovered(self, result):
        not_reviewed = {
            row["path"]: row["reason"] for row in result["coverage"]["not_reviewed"]
        }
        assert "services/billing-api" in not_reviewed
        assert "failed" in not_reviewed["services/billing-api"]
        assert result["coverage"]["plan_completed"] is False
        assert result["coverage"]["lens_runs_completed"] == 1

    def test_an_expired_child_is_expired_not_missing(self, result, children):
        project = next(
            row for row in result["projects"] if row["path"] == "services/notifications"
        )
        row = project["lenses"][0]
        assert row["lens_status"] == "expired"
        assert row["health"] == "unknown"
        assert row["reason"]
        assert row["child"]["execution_id"] in result["fan_out"]["wait_expired"]
        assert result["rollup"]["children_expired"] == 1
        assert project["status"] == "unknown"

    def test_the_run_still_completed(self, result):
        assert result["status"] == "success"
        assert result["artifacts"]["children"] == "evidence/children.json"
        assert result["verdict"] == _verdict(result)


class TestChildCap:
    """A ceiling is a coverage statement, not a failure."""

    @pytest.fixture()
    def result(self) -> dict:
        return _load_result("result-child-cap.json")

    @pytest.fixture()
    def children(self) -> dict:
        return _load_children("result-child-cap.json")

    def test_the_cap_stops_the_fan_out_at_the_declared_number(self, result, children):
        fan_out = result["fan_out"]
        assert fan_out["child_cap"] == 5
        assert fan_out["children_planned"] == 10
        assert fan_out["children_started"] == fan_out["child_cap"]
        assert len(fan_out["children_over_cap"]) == 5
        assert len(_child_records(children)) == fan_out["child_cap"]

    def test_the_projects_it_never_reached_are_listed_under_coverage(self, result):
        """The fourth acceptance criterion: every project the cap kept the
        run away from is named, with the cap as the reason."""
        not_reviewed = {
            row["path"]: row["reason"] for row in result["coverage"]["not_reviewed"]
        }
        unreached = {
            project["path"]
            for project in result["projects"]
            if not any(row["lens_status"] == "ran" for row in project["lenses"])
        }
        assert unreached == {"libs/shared-utils", "tools/report-cli"}
        for path in unreached:
            assert path in not_reviewed, f"{path} was never reached and is not listed"
            assert not_reviewed[path] == "child cap"
        for row in result["fan_out"]["children_over_cap"]:
            lens_row = next(
                lens
                for project in result["projects"]
                if project["path"] == row["project"]
                for lens in project["lenses"]
                if lens["lens"] == row["lens"]
            )
            assert lens_row["lens_status"] == "not_run"
            assert lens_row["reason"] == "child cap"
            assert lens_row["child"] is None

    def test_the_run_still_succeeds_with_a_complete_report(self, result):
        assert result["status"] == "success"
        assert result["coverage"]["plan_completed"] is False
        assert result["verdict"] == _verdict(result) == "pass_with_findings"
        for artifact in (
            "report",
            "register",
            "findings",
            "inventory",
            "questions",
            "children",
        ):
            assert result["artifacts"][artifact], f"missing artifact: {artifact}"
        assert len(result["projects"]) == result["coverage"]["projects_discovered"]

    def test_a_refused_call_is_recorded_and_never_retried(self, result, children):
        rows = [row for _, row in _lens_rows(result) if row["lens_status"] == "refused"]
        assert len(rows) == 1
        row = rows[0]
        record = _child_records(children)[row["child"]["execution_id"]]
        assert record["status"]["state"] == "TASK_STATE_REJECTED"
        assert record["metadata"]["preloop.ai/refusalReason"] == "budget_exceeded"
        assert "preloop.ai/cost" not in record["metadata"]
        assert row["reason"].startswith("budget_exceeded")
        assert row["health"] == "unknown"
        assert row["child"]["cost_usd"] is None
        assert result["rollup"]["children_refused"] == 1
        attempts = [
            call
            for call in children["calls"]
            if call["project"] == "services/billing-api"
            and call["lens"] == "repo-code-health-review"
        ]
        assert len(attempts) == 1, "a refused call was retried"


class TestSecurityLens:
    """The lens that needs an SBOM, and the follow up an absence buys."""

    @pytest.fixture()
    def result(self) -> dict:
        return _load_result("result-security-lenses.json")

    @pytest.fixture()
    def children(self) -> dict:
        return _load_children("result-security-lenses.json")

    def test_the_security_lens_runs_only_where_an_sbom_exists(self, result, children):
        discovery = {row["path"]: row for row in result["discovery"]["projects"]}
        for project in result["projects"]:
            row = next(
                lens for lens in project["lenses"] if lens["lens"] == SECURITY_LENS_SLUG
            )
            if project["path"] not in result["selection"]["selected"]:
                assert row["lens_status"] == "not_run"
                continue
            if discovery[project["path"]]["has_sbom"]:
                assert row["lens_status"] == "ran"
                assert row["child"] is not None
            else:
                assert row["lens_status"] == "not_checkable"
                assert row["child"] is None, "a child was spent with no SBOM to read"
        started = {
            (call["project"], call["lens"])
            for call in children["calls"]
            if call["made"]
        }
        for project, lens in started:
            if lens == SECURITY_LENS_SLUG:
                assert discovery[project]["has_sbom"] is True

    def test_a_project_without_an_sbom_is_not_checkable_and_never_healthy(self, result):
        """The fifth acceptance criterion, asserted on the literal string
        the family uses: not_checkable, with a reason, never skipped."""
        project = next(
            row for row in result["projects"] if row["path"] == "legacy/inventory-web"
        )
        row = next(
            lens for lens in project["lenses"] if lens["lens"] == SECURITY_LENS_SLUG
        )
        assert row["lens_status"] == "not_checkable"
        assert row["reason"] == NOT_CHECKABLE_REASON
        assert row["reason"].strip()
        assert row["verdict"] is None
        assert row["health"] == "unknown"
        # The docs lens passed on this project: the missing SBOM is what
        # keeps it out of the healthy column.
        assert any(
            lens["lens_status"] == "ran" and lens["verdict"] == "pass"
            for lens in project["lenses"]
        )
        assert project["status"] == "unknown"
        assert result["rollup"]["by_health"]["healthy"] == 0
        # The vocabulary is the family's: a missing input is not a choice.
        prose = json.dumps(
            [result["projects"], result["coverage"], result["follow_ups"]]
        ).lower()
        assert "skipped" not in prose

    def test_exactly_one_add_sbom_follow_up_for_the_project_without_one(self, result):
        """The sixth acceptance criterion: one follow up, never two, and
        none at all for the project that already ships an SBOM."""
        discovery = {row["path"]: row for row in result["discovery"]["projects"]}
        per_project: dict[str, list[dict]] = {}
        for row in result["follow_ups"]:
            if row["id"].endswith(SBOM_FOLLOW_UP_SLUG):
                per_project.setdefault(row["project"], []).append(row)
        assert list(per_project) == ["legacy/inventory-web"]
        rows = per_project["legacy/inventory-web"]
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == "portfolio:legacy/inventory-web:add-sbom-generation"
        assert row["title"] == SBOM_FOLLOW_UP_TITLE
        assert row["lens"] == SECURITY_LENS_SLUG
        assert row["severity"] == "medium"
        assert row["filed"] is False
        # Pointer discipline: the project's own build manifest, file:line.
        path, _, line_no = row["evidence"].rpartition(":")
        assert path in discovery["legacy/inventory-web"]["manifests"]
        assert int(line_no) >= 1
        assert discovery["services/billing-api"]["has_sbom"] is True
        assert not [
            follow_up
            for follow_up in result["follow_ups"]
            if follow_up["project"] == "services/billing-api"
            and follow_up["id"].endswith(SBOM_FOLLOW_UP_SLUG)
        ]

    def test_a_lens_off_the_callable_list_is_refused_not_skipped(
        self, result, children
    ):
        """The eighth acceptance criterion: the payload named a fourth
        lens, and the run recorded a refusal rather than dropping it."""
        refused = result["fan_out"]["lenses_refused"]
        assert refused == [
            {"lens": "standards-compliance-walk", "reason": "not on the callable list"}
        ]
        assert refused[0]["lens"] not in LENS_SCHEMAS
        assert refused[0]["lens"] not in result["fan_out"]["lenses"]
        assert not [
            call for call in children["calls"] if call["lens"] == refused[0]["lens"]
        ], "a lens off the callable list was called anyway"

    def test_the_sbom_facts_are_the_ones_the_tree_holds(self, scenario):
        """Discovery's SBOM lookup is project-local and name based: a
        sibling's SBOM is not this project's SBOM."""
        repo, result, children = scenario
        walked = {row["path"]: row for row in _walk(repo)["projects"]}
        for row in result["discovery"]["projects"]:
            assert row["sbom_paths"] == walked[row["path"]]["sbom_paths"]
            assert row["has_sbom"] is bool(row["sbom_paths"])
            for path in row["sbom_paths"]:
                assert path.startswith(f"{row['path']}/")
                assert (REPOS_DIR / repo / path).is_file()


class TestChildCost:
    """Cost is the child's own record, never an estimate."""

    def test_per_project_cost_is_the_recorded_cost_of_its_children(self, scenario):
        """The seventh acceptance criterion, asserted against the
        execution records rather than against the report's own prose."""
        _, result, children = scenario
        records = _child_records(children)
        for project in result["projects"]:
            expected = 0.0
            for row in project["lenses"]:
                if row["child"] is None:
                    continue
                cost = _cost_of(records[row["child"]["execution_id"]])
                assert row["child"]["cost_usd"] == cost
                expected += cost or 0.0
            assert project["cost_usd"] == pytest.approx(expected), (
                f"{project['path']}: cost is not the sum of its children's"
            )

    def test_the_rollup_cost_is_the_sum_over_every_child(self, scenario):
        _, result, children = scenario
        total = sum(
            _cost_of(record) or 0.0 for record in _child_records(children).values()
        )
        assert result["rollup"]["children_cost_usd"] == pytest.approx(total)
        assert result["rollup"]["children_cost_usd"] == pytest.approx(
            sum(project["cost_usd"] or 0.0 for project in result["projects"])
        )

    def test_a_refused_call_contributes_no_cost(self):
        result = _load_result("result-child-cap.json")
        children = _load_children("result-child-cap.json")
        refused = [
            record
            for record in _child_records(children).values()
            if record["status"]["state"] == "TASK_STATE_REJECTED"
        ]
        assert refused
        for record in refused:
            assert "preloop.ai/cost" not in record["metadata"]
        assert result["rollup"]["children_cost_usd"] == pytest.approx(
            sum(_cost_of(record) or 0.0 for record in _child_records(children).values())
        )
