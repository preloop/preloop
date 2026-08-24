"""Tests for flow preset loading functionality."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from preloop.flow_presets import (
    _derive_slug,
    _extract_order,
    _load_yaml_file,
    load_flow_presets,
)
from preloop.utils.hashing import compute_content_hash


def _load_from(dirs: list[Path]):
    """Load presets from the given directory layers with a fresh cache."""

    with patch("preloop.flow_presets.PRESETS_DIRS", dirs):
        load_flow_presets.cache_clear()
        result = load_flow_presets()
    load_flow_presets.cache_clear()
    return result


class TestExtractOrder:
    """Tests for _extract_order helper function."""

    def test_numeric_prefix(self):
        """Test extraction of numeric prefix from filename."""
        assert _extract_order("01-issue-triage") == 1
        assert _extract_order("10-pr-reviewer") == 10
        assert _extract_order("99-custom-flow") == 99

    def test_no_numeric_prefix(self):
        """Test fallback when no numeric prefix exists."""
        assert _extract_order("issue-triage") == 9999
        assert _extract_order("custom") == 9999

    def test_invalid_prefix(self):
        """Test fallback for non-numeric prefix."""
        assert _extract_order("abc-flow") == 9999


class TestLoadYamlFile:
    """Tests for _load_yaml_file helper function."""

    def test_valid_yaml(self, tmp_path: Path):
        """Test loading a valid YAML file."""
        yaml_file = tmp_path / "test.yml"
        yaml_file.write_text(
            """
name: Test Flow
description: A test flow
trigger_event_types:
  - push
"""
        )
        result = _load_yaml_file(yaml_file)
        assert result["name"] == "Test Flow"
        assert result["description"] == "A test flow"
        assert result["is_preset"] is True  # Default added

    def test_is_preset_default(self, tmp_path: Path):
        """Test that is_preset defaults to True."""
        yaml_file = tmp_path / "test.yml"
        yaml_file.write_text("name: Test\n")
        result = _load_yaml_file(yaml_file)
        assert result["is_preset"] is True

    def test_is_preset_explicit_false(self, tmp_path: Path):
        """Test that explicit is_preset: false is preserved."""
        yaml_file = tmp_path / "test.yml"
        yaml_file.write_text("name: Test\nis_preset: false\n")
        result = _load_yaml_file(yaml_file)
        assert result["is_preset"] is False

    def test_invalid_yaml_syntax(self, tmp_path: Path):
        """Test error handling for invalid YAML syntax."""
        yaml_file = tmp_path / "invalid.yml"
        yaml_file.write_text("name: [unclosed bracket")
        with pytest.raises(ValueError, match="Failed to parse preset file"):
            _load_yaml_file(yaml_file)

    def test_non_mapping_yaml(self, tmp_path: Path):
        """Test error handling when YAML is not a mapping."""
        yaml_file = tmp_path / "list.yml"
        yaml_file.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="must define a mapping"):
            _load_yaml_file(yaml_file)


class TestLoadFlowPresets:
    """Tests for load_flow_presets function."""

    def test_empty_presets_dir(self, tmp_path: Path):
        """Test that empty presets directory returns empty list."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        with patch("preloop.flow_presets.PRESETS_DIRS", [presets_dir]):
            # Clear the lru_cache
            load_flow_presets.cache_clear()
            result = load_flow_presets()
            assert result == []

    def test_missing_presets_dir(self, tmp_path: Path):
        """Test that missing presets directory returns empty list (open source default)."""
        nonexistent_dir = tmp_path / "nonexistent"

        with patch("preloop.flow_presets.PRESETS_DIRS", [nonexistent_dir]):
            load_flow_presets.cache_clear()
            result = load_flow_presets()
            assert result == []

    def test_loads_yaml_files(self, tmp_path: Path):
        """Test that YAML files are loaded correctly."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        # Create test preset files
        (presets_dir / "01-first.yml").write_text("name: First Flow\n")
        (presets_dir / "02-second.yaml").write_text("name: Second Flow\n")

        with patch("preloop.flow_presets.PRESETS_DIRS", [presets_dir]):
            load_flow_presets.cache_clear()
            result = load_flow_presets()

            assert len(result) == 2
            assert result[0]["name"] == "First Flow"
            assert result[1]["name"] == "Second Flow"

    def test_ordering_by_numeric_prefix(self, tmp_path: Path):
        """Test that presets are ordered by numeric prefix."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        # Create files in non-sequential order
        (presets_dir / "10-last.yml").write_text("name: Last\n")
        (presets_dir / "01-first.yml").write_text("name: First\n")
        (presets_dir / "05-middle.yml").write_text("name: Middle\n")

        with patch("preloop.flow_presets.PRESETS_DIRS", [presets_dir]):
            load_flow_presets.cache_clear()
            result = load_flow_presets()

            assert len(result) == 3
            assert result[0]["name"] == "First"
            assert result[1]["name"] == "Middle"
            assert result[2]["name"] == "Last"


class TestFlowPresetSchema:
    """Tests to validate flow preset schema requirements."""

    # Required keys that every flow preset should have
    REQUIRED_KEYS = {"name"}

    # Optional but recommended keys
    RECOMMENDED_KEYS = {
        "description",
        "trigger_event_types",  # Use array field
        "prompt_template",
        "agent_type",
    }

    def test_presets_have_required_keys(self, tmp_path: Path):
        """Test that all presets have required keys."""
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()

        # Create a valid preset
        valid_preset = {
            "name": "Test Flow",
            "description": "A test flow",
            "trigger_event_types": ["push"],  # Use array field
            "prompt_template": "Do something",
            "agent_type": "codex",
        }
        (presets_dir / "01-test.yml").write_text(yaml.dump(valid_preset))

        with patch("preloop.flow_presets.PRESETS_DIRS", [presets_dir]):
            load_flow_presets.cache_clear()
            presets = load_flow_presets()

            for preset in presets:
                for key in self.REQUIRED_KEYS:
                    assert key in preset, f"Preset missing required key: {key}"

    def test_actual_presets_directory(self):
        """Test that actual presets directory (if exists) has valid presets.

        This test validates the real presets in the codebase.
        For open source, the directory may be empty which is acceptable.
        """
        load_flow_presets.cache_clear()
        presets = load_flow_presets()

        # Open source may have no presets - that's OK
        # But if presets exist, they must be valid
        for preset in presets:
            # Every preset must have a name
            assert "name" in preset, f"Preset missing 'name' key: {preset}"

            # is_preset should be True (set by default or explicitly)
            assert preset.get("is_preset", True) is True or preset.get("is_preset"), (
                f"Preset should have is_preset=True: {preset.get('name')}"
            )


class TestDeriveSlug:
    """Tests for _derive_slug fallback identity derivation."""

    def test_strips_numeric_prefix(self):
        assert _derive_slug("001-issue-triage-assistant") == "issue-triage-assistant"
        assert _derive_slug("42-pr-reviewer") == "pr-reviewer"

    def test_no_numeric_prefix(self):
        assert _derive_slug("observe-eval") == "observe-eval"
        assert _derive_slug("custom") == "custom"

    def test_bare_numeric_stem(self):
        assert _derive_slug("001") == "001"
        assert _derive_slug("001-") == "001-"


class TestLayeredLoading:
    """Tests for union/override/tombstone semantics across preset dirs."""

    @staticmethod
    def _make_dir(tmp_path: Path, name: str, files: dict) -> Path:
        directory = tmp_path / name
        directory.mkdir()
        for filename, content in files.items():
            (directory / filename).write_text(content)
        return directory

    def test_single_dir_compat(self, tmp_path: Path):
        """A single directory keeps the exact current behavior."""
        oss = self._make_dir(
            tmp_path,
            "oss",
            {
                "01-first.yml": "name: First\n",
                "02-second.yaml": "name: Second\n",
            },
        )
        result = _load_from([oss])
        assert [p["name"] for p in result] == ["First", "Second"]
        # The loader-internal slug identity never leaks into the catalog.
        assert all("slug" not in p for p in result)

    def test_union_of_distinct_slugs(self, tmp_path: Path):
        """Presets with distinct slugs from all dirs appear (union)."""
        oss = self._make_dir(
            tmp_path,
            "oss",
            {
                "001-triage.yaml": "name: Triage\n",
                "004-docs-generator.yaml": "name: Docs Generator\n",
            },
        )
        ee = self._make_dir(tmp_path, "ee", {"007-scanner.yaml": "name: Scanner\n"})
        result = _load_from([oss, ee])
        assert [p["name"] for p in result] == ["Triage", "Docs Generator", "Scanner"]

    def test_override_on_slug_collision(self, tmp_path: Path):
        """A later dir overrides an earlier one when slugs collide."""
        oss = self._make_dir(
            tmp_path,
            "oss",
            {"003-observe-eval.yaml": "name: Observe / Eval\nprompt_template: oss\n"},
        )
        ee = self._make_dir(
            tmp_path,
            "ee",
            {
                "008-observe-eval.yaml": "name: Observe / Eval (EE)\nprompt_template: ee\n"
            },
        )
        result = _load_from([oss, ee])
        assert len(result) == 1
        assert result[0]["name"] == "Observe / Eval (EE)"
        assert result[0]["prompt_template"] == "ee"

    def test_override_via_explicit_slug(self, tmp_path: Path):
        """Explicit slug keys collide even when filenames differ entirely."""
        oss = self._make_dir(
            tmp_path,
            "oss",
            {"001-triage.yaml": "slug: issue-triage\nname: OSS Triage\n"},
        )
        ee = self._make_dir(
            tmp_path,
            "ee",
            {"050-enterprise-triage.yaml": "slug: issue-triage\nname: EE Triage\n"},
        )
        result = _load_from([oss, ee])
        assert len(result) == 1
        assert result[0]["name"] == "EE Triage"
        # The explicit slug key is stripped before the catalog is returned.
        assert "slug" not in result[0]

    def test_tombstone_suppresses_earlier_preset(self, tmp_path: Path):
        """`disabled: true` in a later dir hides the same-slug preset entirely."""
        oss = self._make_dir(
            tmp_path,
            "oss",
            {
                "001-triage.yaml": "name: Triage\n",
                "002-reviewer.yaml": "name: Reviewer\n",
            },
        )
        ee = self._make_dir(
            tmp_path,
            "ee",
            {"001-triage.yaml": "name: Triage\ndisabled: true\n"},
        )
        result = _load_from([oss, ee])
        assert [p["name"] for p in result] == ["Reviewer"]
        # The tombstone marker itself never leaks into the catalog
        assert all("disabled" not in p for p in result)

    def test_stable_ordering_numeric_prefix_then_slug(self, tmp_path: Path):
        """Catalog is sorted by (numeric prefix, slug) across dirs."""
        oss = self._make_dir(
            tmp_path,
            "oss",
            {
                "010-zeta.yaml": "name: Zeta\n",
                "010-alpha.yaml": "name: Alpha\n",
            },
        )
        ee = self._make_dir(tmp_path, "ee", {"005-mid.yaml": "name: Mid\n"})
        result = _load_from([oss, ee])
        assert [p["name"] for p in result] == ["Mid", "Alpha", "Zeta"]

    def test_missing_later_dir_ignored(self, tmp_path: Path):
        """A nonexistent layer is skipped without error."""
        oss = self._make_dir(tmp_path, "oss", {"001-triage.yaml": "name: Triage\n"})
        result = _load_from([oss, tmp_path / "nonexistent"])
        assert [p["name"] for p in result] == ["Triage"]

    def test_invalid_slug_rejected(self, tmp_path: Path):
        yaml_file = tmp_path / "001-bad.yaml"
        yaml_file.write_text("name: Bad\nslug: ''\n")
        with pytest.raises(ValueError, match="invalid 'slug'"):
            _load_yaml_file(yaml_file)

    def test_same_dir_collision_warns_and_later_file_wins(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Two files in ONE directory resolving to the same slug is almost
        certainly a mistake: the later file wins, but a warning is logged so
        the drop is surfaced instead of silently swallowed."""
        oss = self._make_dir(
            tmp_path,
            "oss",
            {
                "001-triage.yaml": "name: Old Triage\n",
                "002-triage.yaml": "name: New Triage\n",
            },
        )
        with caplog.at_level("WARNING", logger="preloop.flow_presets"):
            result = _load_from([oss])
        assert [p["name"] for p in result] == ["New Triage"]
        warnings = [r for r in caplog.records if "slug collision" in r.message]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "002-triage.yaml" in message
        assert "001-triage.yaml" in message
        assert "'triage'" in message

    def test_cross_dir_override_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Cross-directory same-slug override is the intended layering
        feature and must stay silent."""
        oss = self._make_dir(tmp_path, "oss", {"001-triage.yaml": "name: OSS\n"})
        ee = self._make_dir(tmp_path, "ee", {"001-triage.yaml": "name: EE\n"})
        with caplog.at_level("WARNING", logger="preloop.flow_presets"):
            result = _load_from([oss, ee])
        assert [p["name"] for p in result] == ["EE"]
        assert not [r for r in caplog.records if "slug collision" in r.message]

    def test_slug_never_leaks_into_catalog(self, tmp_path: Path):
        """Neither explicit nor filename-derived slugs appear in catalog
        dicts; the loader is the sole owner of the identity."""
        oss = self._make_dir(
            tmp_path,
            "oss",
            {
                "001-explicit.yaml": "slug: my-explicit\nname: Explicit\n",
                "002-derived.yaml": "name: Derived\n",
            },
        )
        result = _load_from([oss])
        assert len(result) == 2
        assert all("slug" not in p for p in result)


class TestSyncPropagationOnDirChange:
    """When a preset's source dir changes (EE override), the loaded content
    changes and the content-hash based sync detects it for propagation."""

    def test_content_hash_changes_when_override_dir_added(self, tmp_path: Path):
        oss = tmp_path / "oss"
        oss.mkdir()
        (oss / "001-triage.yaml").write_text(
            "name: Triage\nprompt_template: oss prompt\n"
        )
        ee = tmp_path / "ee"
        ee.mkdir()
        (ee / "001-triage.yaml").write_text(
            "name: Triage\nprompt_template: ee prompt\n"
        )

        oss_only = _load_from([oss])
        layered = _load_from([oss, ee])

        oss_hash = compute_content_hash(oss_only[0]["prompt_template"])
        layered_hash = compute_content_hash(layered[0]["prompt_template"])

        # Same identity (both files derive slug "triage", so the EE layer
        # overrides), different content: sync (which compares
        # source_prompt_hash against the current preset hash in
        # sync_preset_to_derived_flows) will auto-update non-customized
        # derived flows and notify customized ones.
        assert oss_only[0]["name"] == layered[0]["name"] == "Triage"
        assert len(layered) == 1
        assert oss_hash != layered_hash

    def test_content_hash_stable_without_override(self, tmp_path: Path):
        oss = tmp_path / "oss"
        oss.mkdir()
        (oss / "001-triage.yaml").write_text(
            "name: Triage\nprompt_template: oss prompt\n"
        )
        empty = tmp_path / "ee"
        empty.mkdir()

        oss_only = _load_from([oss])
        layered = _load_from([oss, empty])

        assert compute_content_hash(
            oss_only[0]["prompt_template"]
        ) == compute_content_hash(layered[0]["prompt_template"])


class TestRealPresetSlugs:
    """The shipped preset files must declare unique explicit slugs."""

    def test_shipped_presets_declare_unique_slugs(self):
        from preloop.flow_presets import DEFAULT_PRESETS_DIR

        if not DEFAULT_PRESETS_DIR.exists():
            pytest.skip("no shipped presets")

        slugs = []
        for path in sorted(DEFAULT_PRESETS_DIR.glob("*.y*ml")):
            data = yaml.safe_load(path.read_text())
            assert isinstance(data.get("slug"), str) and data["slug"].strip(), (
                f"{path.name} must declare an explicit slug"
            )
            slugs.append(data["slug"])
        assert len(slugs) == len(set(slugs)), f"duplicate slugs: {slugs}"


# Every shipped preset must declare an explicit result.json completion
# channel in its prompt. Flows confirm completion either by printing the
# FLOW_EXECUTION_SUCCESS sentinel or by writing /workspace/result.json with
# a recognized completion status; presets must not rely on the model
# remembering the sentinel (regression guard for exec 31613e46, where the
# PR reviewer completed a full review then FAILED on the missing sentinel).
# Maps preset file -> the exact status/verdict vocabulary line its prompt
# documents (whitespace-normalized).
PRESET_COMPLETION_MARKERS = {
    "001-issue-triage-assistant.yaml": '"status": "success"',
    "002-pull-request-reviewer.yaml": '"status": "success"',
    "003-observe-eval.yaml": '"status": "pass" | "fail" | "error"',
    "004-sbom-verify.yaml": '"verdict": "pass" | "pass_with_findings" | "fail"',
    "005-sbom-exploit-check.yaml": '"status": "success" | "error"',
    "006-release-security-audit.yaml": (
        '"verdict": "pass" | "pass_with_findings" | "fail"'
    ),
    "007-component-due-diligence.yaml": '"status": "success" | "error"',
}


class TestShippedPresetCompletionContracts:
    """Each shipped preset prompt pins its result.json completion contract."""

    def _shipped_preset_files(self):
        from preloop.flow_presets import DEFAULT_PRESETS_DIR

        if not DEFAULT_PRESETS_DIR.exists():
            pytest.skip("no shipped presets")
        return sorted(DEFAULT_PRESETS_DIR.glob("*.y*ml"))

    def test_every_shipped_preset_has_a_known_completion_marker(self):
        """New presets must register their completion vocabulary here."""
        names = [path.name for path in self._shipped_preset_files()]
        assert names == sorted(PRESET_COMPLETION_MARKERS), (
            "shipped presets and PRESET_COMPLETION_MARKERS out of sync — "
            "every shipped preset needs an explicit result.json completion "
            "contract and a marker entry in this test"
        )

    @pytest.mark.parametrize("filename", sorted(PRESET_COMPLETION_MARKERS))
    def test_prompt_documents_result_json_completion_status(self, filename):
        from preloop.flow_presets import DEFAULT_PRESETS_DIR

        path = DEFAULT_PRESETS_DIR / filename
        if not path.exists():
            pytest.skip(f"{filename} not shipped in this layout")
        prompt = yaml.safe_load(path.read_text())["prompt_template"]
        norm = " ".join(prompt.split())
        assert "/workspace/result.json" in norm, (
            f"{filename} prompt must instruct writing /workspace/result.json"
        )
        assert PRESET_COMPLETION_MARKERS[filename] in norm, (
            f"{filename} prompt must document its completion status vocabulary"
        )

    @pytest.mark.parametrize(
        "filename",
        ["001-issue-triage-assistant.yaml", "002-pull-request-reviewer.yaml"],
    )
    def test_success_and_error_paths_are_both_instructed(self, filename):
        """001/002 use the plain status contract: success on completion,
        error (with a reason) on unrecoverable failure."""
        from preloop.flow_presets import DEFAULT_PRESETS_DIR

        prompt = yaml.safe_load((DEFAULT_PRESETS_DIR / filename).read_text())[
            "prompt_template"
        ]
        norm = " ".join(prompt.split())
        assert '"status": "success"' in norm
        assert '"status": "error"' in norm
        assert '"reason"' in norm
        # The final act framing: writing result.json is the last action.
        assert "Record Completion (MANDATORY FINAL ACT)" in norm
