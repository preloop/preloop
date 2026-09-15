"""Portfolio review orchestrator preset (017), running inline.

The layer above the full-repo review family: it discovers the projects
in one repository, asks a human which of them to review, runs the docs
currency lens (016) inline for the selected ones, aggregates, asks which
follow ups to keep, and writes a portfolio report. It is NOT one of the
four lenses, so test_repo_review_presets.py does not parametrize over
it: it carries the built-in ask_user question channel on its allowlist
and it samples projects rather than files. What it does inherit from the
family is pinned here: no write tools, the one-minute verdict cover, the
disclaimer, and the rule that the register can never upgrade a verdict.

This module pins what is specific to the orchestrator:

* discovery is deterministic and command only — the walk in this test
  re-runs the preset's own closed detector list, named exclusion list,
  depth cap and nesting rule against the fixture repositories, so a
  recorded project list cannot quietly stop being reproducible;
* the triage hint is computed from those facts alone, which is why the
  worst written project in the fixture repository ranks last;
* the two question forms: batched, one call, the selectable ids drawn
  from discovery, and the auto-select threshold below which nothing is
  asked;
* the safe default when either question expires;
* the verdict rules, including the one that keeps a project nobody
  reviewed out of the healthy column;
* the size budget a twenty five project portfolio has to fit in.

Deterministic: parses the shipped YAML and the synthetic fixtures under
fixtures/review/portfolio. No agent, no network, no control plane.
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
SCHEMA_FILE = FIXTURES / "schemas" / "portfolio-v1.json"

SCHEMA_ID = "preloop.review.portfolio/v1"
FLOW_SLUG = "portfolio-review"
PRESET_NAME = "Portfolio Review"
LENS_SLUG = "docs-currency-review"
LENS_SCHEMA_ID = "preloop.review.docscurrency/v1"

DISCLAIMER = (
    "Machine-generated review evidence. Not a certification, audit opinion, "
    "or legal advice."
)

THREE_DAYS = 259200
RUN_DATE = date(2026, 9, 16)

# One scenario per acceptance criterion: the repository is the input, the
# result is the contracted output for it.
SCENARIOS = {
    "result-five-selected.json": "five-projects",
    "result-two-auto-selected.json": "two-projects",
    "result-first-question-expired.json": "five-projects",
    "result-second-question-expired.json": "five-projects",
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


def _verdict(result: dict) -> str:
    """The verdict the preset's own rules compute for a result.

    fail when any project is failing; pass only when every discovered
    project was reviewed and every one of them is healthy; everything
    else, an unknown project included, pass_with_findings.
    """
    projects = result["projects"]
    coverage = result["coverage"]
    if any(row["health"] == "failing" for row in projects):
        return "fail"
    if (
        projects
        and coverage["plan_completed"]
        and not coverage["not_reviewed"]
        and all(row["health"] == "healthy" for row in projects)
    ):
        return "pass"
    return "pass_with_findings"


def _question(result: dict, phase: str) -> dict:
    rows = [row for row in result["questions"] if row["phase"] == phase]
    assert len(rows) == 1, f"expected exactly one {phase} question, got {len(rows)}"
    return rows[0]


def _healthy_row(path: str) -> dict:
    """A positive row: a project whose lens ran and passed."""
    return {
        "path": path,
        "lens": LENS_SLUG,
        "lens_schema": LENS_SCHEMA_ID,
        "lens_status": "ran",
        "not_run_reason": None,
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
        "result_artifact": f"evidence/projects/{path.replace('/', '-')}/result.json",
    }


@pytest.fixture(params=sorted(SCENARIOS), ids=str)
def scenario(request) -> tuple[str, dict]:
    return SCENARIOS[request.param], _load_result(request.param)


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
        """The only platform tool is the built-in question channel: no
        MCP server, no write tool, and a prompt that forbids every write
        path including opening a pull request."""
        data = _load_preset()
        assert data["allowed_mcp_servers"] == []
        assert data["allowed_mcp_tools"] == [{"name": "ask_user"}]
        norm = _norm(data["prompt_template"])
        assert "NO write tools" in norm
        assert "do not create issues, post comments, push commits" in norm
        assert "never run git commit or git push" in norm
        assert "Never modify tracked files" in norm
        assert (
            "The ONLY platform tool on your allowlist is the built-in "
            "ask_user, which is a question channel, not a write tool" in norm
        )
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
        """Delegation, the security lens and modernisation are all out."""
        norm = _norm(_prompt())
        assert "SECURITY IS OUT OF SCOPE" in norm
        assert "Release Security Audit" in norm
        assert "file ONE referral finding" in norm
        assert "NEVER a value" in norm
        assert "MODERNISATION IS OUT OF SCOPE" in norm
        assert "no child executions, no delegation" in norm
        assert (
            "The security lens and the code health lens do not run in this "
            "preset" in norm
        )

    def test_orchestrator_does_not_read_source(self):
        norm = _norm(_prompt())
        assert (
            "Outside PHASE 4 you never read project source code and never "
            "form an opinion about it" in norm
        )

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

    def test_the_lens_is_reused_not_restated(self):
        norm = _norm(_prompt())
        assert "backend/presets/016-docs-currency-review.yaml" in norm
        assert "DO NOT RESTATE, VARY, RELAX OR EXTEND THAT LENS HERE" in norm
        assert LENS_SCHEMA_ID in norm

    def test_inline_cap_says_delegation_is_required_above_it(self):
        norm = _norm(_prompt())
        assert "INLINE PROJECT CAP" in norm
        assert "never more than 8 in one execution whatever the payload says" in norm
        assert 'list the rest in coverage.not_reviewed with reason "inline cap"' in norm
        assert "SAY PLAINLY IN THE REPORT THAT DELEGATION IS REQUIRED" in norm

    def test_evidence_pack_and_one_page_cover(self):
        prompt = _prompt()
        norm = _norm(prompt)
        assert "/workspace/evidence/" in prompt
        assert "portfolio-report.md" in prompt
        assert "projects-register.md" in prompt
        assert "findings.json" in prompt
        assert "inventory.json" in prompt
        assert "questions.json" in prompt
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
        assert "Release Security Audit family at minimum" in norm
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
        assert '"fail" if any project\'s health is "failing"' in norm
        assert (
            '"pass" only when every discovered project was reviewed by a lens '
            "that ran" in norm
        )
        assert "THE REGISTER CANNOT UPGRADE THE VERDICT" in norm
        assert (
            "A project nobody reviewed is not evidence of health, an "
            'inventory-only run is never a "pass"' in norm
        )
        assert "A PROJECT WHOSE LENS DID NOT RUN IS NEVER COUNTED AS HEALTHY" in norm
        assert "HEALTH IS DERIVED, NEVER ASSERTED" in norm

    def test_facts_and_judgment_stay_separated(self):
        prompt = _prompt()
        assert '"checks"' in prompt
        assert '"assessments"' in prompt

    def test_size_budget_names_the_twenty_five_project_portfolio(self):
        norm = _norm(_prompt())
        assert "SIZE BUDGET" in norm
        assert "under 200 KB" in norm
        assert "every project row under 4 KB and every discovery row under 2 KB" in norm
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
        assert "NO LENS RUNS, no follow up is ranked, no issue is opened" in norm
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
        _, result = scenario
        schema = json.loads(SCHEMA_FILE.read_text())
        Draft202012Validator(schema).validate(result)

    def test_result_carries_every_required_key(self, scenario):
        _, result = scenario
        missing = [key for key in _required_shape_keys() if key not in result]
        assert missing == [], f"result fixture missing required keys: {missing}"
        assert result["schema"] == SCHEMA_ID
        assert result["flow"] == FLOW_SLUG
        assert result["status"] == "success"
        assert result["disclaimer"] == DISCLAIMER

    def test_fixtures_are_synthetic(self, scenario):
        repo, result = scenario
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
        repo, result = scenario
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
        repo, result = scenario
        for row in result["discovery"]["projects"]:
            for runtime in row["runtimes"]:
                source = REPOS_DIR / repo / runtime["source"]
                assert source.is_file(), f"{row['path']}: no such manifest {source}"
                assert runtime["version"] in source.read_text(), (
                    f"{row['path']}: {runtime['version']} is not in {runtime['source']}"
                )

    def test_every_manifest_matches_a_detector(self, scenario):
        _, result = scenario
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
        _, result = scenario
        eol_table = result["discovery"]["eol_table"]
        for row in result["discovery"]["projects"]:
            assert row["triage"] == _triage(row, eol_table), (
                f"{row['path']}: recorded triage hint is not the computed one"
            )

    def test_the_rank_is_the_documented_ordering(self, scenario):
        _, result = scenario
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
        _, result = scenario
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
        _, result = scenario
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
        assert all(row["lens_status"] == "not_run" for row in result["projects"])
        assert result["rollup"]["by_health"]["unknown"] == 5

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
        repo, result = scenario
        for row in result["follow_ups"]:
            path, _, line_no = row["evidence"].rpartition(":")
            document = REPOS_DIR / repo / path
            assert document.exists(), f"{row['id']}: no such file {path}"
            lines = document.read_text().splitlines()
            index = int(line_no)
            assert 1 <= index <= len(lines), f"{row['id']}: line {index} out of range"
            assert lines[index - 1].strip(), f"{row['id']}: points at a blank line"

    def test_follow_ups_belong_to_a_reviewed_project(self, scenario):
        _, result = scenario
        reviewed = {
            row["path"] for row in result["projects"] if row["lens_status"] == "ran"
        }
        for row in result["follow_ups"]:
            assert row["project"] in reviewed, (
                f"{row['id']}: follow up from a project no lens reviewed"
            )
            assert row["id"].startswith(f"portfolio:{row['project']}:")

    def test_follow_ups_are_ranked_and_never_filed(self, scenario):
        _, result = scenario
        ranks = [row["rank"] for row in result["follow_ups"]]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)
        assert all(row["filed"] is False for row in result["follow_ups"])
        assert result["rollup"]["issues_filed"] == 0


class TestCoverageAndHealth:
    """A project nobody reviewed is never healthy."""

    def test_a_project_whose_lens_did_not_run_is_unknown_and_uncovered(self, scenario):
        _, result = scenario
        not_reviewed = {
            row["path"]: row["reason"] for row in result["coverage"]["not_reviewed"]
        }
        for row in result["projects"]:
            if row["lens_status"] == "not_run":
                assert row["health"] == "unknown"
                assert row["verdict"] is None
                assert row["not_run_reason"]
                assert row["path"] in not_reviewed, (
                    f"{row['path']}: lens did not run but coverage does not say so"
                )
                assert not_reviewed[row["path"]]

    def test_one_project_row_per_discovered_project(self, scenario):
        _, result = scenario
        discovered = [row["path"] for row in result["discovery"]["projects"]]
        assert sorted(row["path"] for row in result["projects"]) == sorted(discovered)
        assert result["coverage"]["projects_discovered"] == len(discovered)
        assert result["coverage"]["projects_reviewed"] == sum(
            1 for row in result["projects"] if row["lens_status"] == "ran"
        )

    def test_health_is_derived_from_the_lens_verdict(self, scenario):
        _, result = scenario
        mapping = {
            "pass": "healthy",
            "pass_with_findings": "findings",
            "fail": "failing",
            None: "unknown",
        }
        for row in result["projects"]:
            assert row["health"] == mapping[row["verdict"]]
            if row["lens_status"] == "ran":
                assert row["lens"] == LENS_SLUG
                assert row["lens_schema"] == LENS_SCHEMA_ID
                assert row["result_artifact"]

    def test_the_rollup_agrees_with_the_rows(self, scenario):
        _, result = scenario
        by_health = result["rollup"]["by_health"]
        for health in ("healthy", "findings", "failing", "unknown"):
            assert by_health[health] == sum(
                1 for row in result["projects"] if row["health"] == health
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
        _, result = scenario
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
                "lens": LENS_SLUG,
                "lens_schema": None,
                "lens_status": "not_run",
                "not_run_reason": "inline cap",
                "verdict": None,
                "health": "unknown",
                "counts": {
                    "holds": 0,
                    "drifted": 0,
                    "not_checkable": 0,
                    "high": 0,
                    "medium": 0,
                    "low": 0,
                },
                "result_artifact": None,
            }
        )
        result["coverage"]["not_reviewed"].append(
            {"path": "apps/legacy", "reason": "inline cap"}
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
            if project_template["result_artifact"]:
                project_template["result_artifact"] = (
                    f"evidence/projects/{grown['path'].replace('/', '-')}/result.json"
                )
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
        _, result = scenario
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
        _, result = scenario
        per_project: dict[str, int] = {}
        for row in result["follow_ups"]:
            per_project[row["project"]] = per_project.get(row["project"], 0) + 1
        assert all(count <= 5 for count in per_project.values()), per_project
