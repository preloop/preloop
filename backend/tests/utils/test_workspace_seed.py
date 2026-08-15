"""Tests for trigger-payload workspace seeding (utils.workspace_seed)."""

import base64

import pytest

from preloop.utils.workspace_seed import (
    MAX_SEED_FILES,
    MAX_TOTAL_SEED_BYTES,
    WORKSPACE_FILE_PATHS_KEY,
    WorkspaceSeedError,
    attach_workspace_file_paths,
    build_workspace_seed_shell,
    parse_workspace_files,
    workspace_seed_paths,
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _payload(*entries):
    return {"workspace_files": list(entries)}


class TestParseWorkspaceFiles:
    """Validation of the workspace_files payload declaration."""

    def test_absent_key_returns_empty(self):
        assert parse_workspace_files({"other": 1}) == []
        assert parse_workspace_files(None) == []
        assert parse_workspace_files({}) == []

    def test_valid_files_parse(self):
        files = parse_workspace_files(
            _payload(
                {"path": "fixtures/input.json", "content_base64": _b64(b'{"k": 1}')},
                {"path": "README.md", "content_base64": _b64(b"hello")},
            )
        )
        assert [f.path for f in files] == ["fixtures/input.json", "README.md"]
        assert files[0].decoded_size == len(b'{"k": 1}')

    def test_wrapped_base64_tolerated(self):
        content = _b64(b"x" * 100)
        wrapped = "\n".join(content[i : i + 20] for i in range(0, len(content), 20))
        files = parse_workspace_files(
            _payload({"path": "a.txt", "content_base64": wrapped})
        )
        assert files[0].decoded_size == 100

    def test_non_list_rejected(self):
        with pytest.raises(WorkspaceSeedError, match="must be a list"):
            parse_workspace_files({"workspace_files": {"path": "a"}})

    def test_non_dict_entry_rejected(self):
        with pytest.raises(WorkspaceSeedError, match="must be an object"):
            parse_workspace_files(_payload("a.txt"))

    @pytest.mark.parametrize(
        "path",
        [
            "../escape.txt",
            "a/../../escape.txt",
            "/etc/passwd",
            "..",
            "fixtures/../../escape.txt",
            "~/escape.txt",
            "a\\..\\escape.txt",
            ".git/hooks/post-checkout",
            ".git",
            "",
            "   ",
            ".",
            "a/./..",
            "bad\npath",
            "bad\x00path",
        ],
    )
    def test_unsafe_paths_rejected(self, path):
        """Path-traversal and other unsafe paths must be rejected."""
        with pytest.raises(WorkspaceSeedError):
            parse_workspace_files(
                _payload({"path": path, "content_base64": _b64(b"x")})
            )

    def test_nested_relative_path_allowed(self):
        files = parse_workspace_files(
            _payload({"path": "a/b/../c/file.txt", "content_base64": _b64(b"x")})
        )
        # Normalized, still inside /workspace.
        assert files[0].path == "a/c/file.txt"

    def test_duplicate_paths_rejected(self):
        with pytest.raises(WorkspaceSeedError, match="duplicates path"):
            parse_workspace_files(
                _payload(
                    {"path": "a.txt", "content_base64": _b64(b"1")},
                    {"path": "b/../a.txt", "content_base64": _b64(b"2")},
                )
            )

    def test_invalid_base64_rejected(self):
        with pytest.raises(WorkspaceSeedError, match="not valid base64"):
            parse_workspace_files(
                _payload({"path": "a.txt", "content_base64": "not base64!!!"})
            )

    def test_missing_content_rejected(self):
        with pytest.raises(WorkspaceSeedError, match="content_base64"):
            parse_workspace_files(_payload({"path": "a.txt", "url": "http://x"}))

    def test_total_cap_enforced(self):
        """Total decoded bytes across all files must stay under the cap."""
        half = _b64(b"x" * (MAX_TOTAL_SEED_BYTES // 2 + 1))
        with pytest.raises(WorkspaceSeedError, match="inline cap"):
            parse_workspace_files(
                _payload(
                    {"path": "a.bin", "content_base64": half},
                    {"path": "b.bin", "content_base64": half},
                )
            )

    def test_single_file_at_cap_allowed(self):
        content = _b64(b"x" * MAX_TOTAL_SEED_BYTES)
        files = parse_workspace_files(
            _payload({"path": "a.bin", "content_base64": content})
        )
        assert files[0].decoded_size == MAX_TOTAL_SEED_BYTES

    def test_file_count_cap_enforced(self):
        entries = [
            {"path": f"f{i}.txt", "content_base64": _b64(b"x")}
            for i in range(MAX_SEED_FILES + 1)
        ]
        with pytest.raises(WorkspaceSeedError, match="max is"):
            parse_workspace_files(_payload(*entries))


class TestBuildWorkspaceSeedShell:
    """Generated init-command block."""

    def test_empty_returns_empty_string(self):
        assert build_workspace_seed_shell([]) == ""

    def test_writes_under_workspace(self):
        files = parse_workspace_files(
            _payload({"path": "fixtures/input.json", "content_base64": _b64(b"{}")})
        )
        shell = build_workspace_seed_shell(files)
        assert "mkdir -p /workspace/fixtures" in shell
        assert "base64 -d > /workspace/fixtures/input.json" in shell
        assert _b64(b"{}") in shell

    def test_paths_are_shell_quoted(self):
        files = parse_workspace_files(
            _payload({"path": "with space/f'ile.txt", "content_base64": _b64(b"x")})
        )
        shell = build_workspace_seed_shell(files)
        assert "'/workspace/with space'" in shell
        # shlex.quote escapes the single quote in the filename.
        assert '"\'"' in shell


class TestAttachWorkspaceFilePaths:
    """Audit stamp on the trigger snapshot."""

    def test_paths_stamped_for_valid_declaration(self):
        details = {
            "payload": _payload(
                {"path": "a.txt", "content_base64": _b64(b"1")},
                {"path": "b/c.txt", "content_base64": _b64(b"2")},
            )
        }
        attach_workspace_file_paths(details)
        assert details[WORKSPACE_FILE_PATHS_KEY] == ["a.txt", "b/c.txt"]

    def test_no_stamp_without_workspace_files(self):
        details = {"payload": {"x": 1}}
        attach_workspace_file_paths(details)
        assert WORKSPACE_FILE_PATHS_KEY not in details

    def test_invalid_declaration_left_unstamped(self):
        """Invalid seeds fail later in the orchestrator, not at creation."""
        details = {
            "payload": _payload({"path": "../bad", "content_base64": _b64(b"1")})
        }
        attach_workspace_file_paths(details)
        assert WORKSPACE_FILE_PATHS_KEY not in details

    def test_non_dict_passthrough(self):
        assert attach_workspace_file_paths(None) is None

    def test_workspace_seed_paths_helper(self):
        payload = _payload({"path": "a.txt", "content_base64": _b64(b"1")})
        assert workspace_seed_paths(payload) == ["a.txt"]
