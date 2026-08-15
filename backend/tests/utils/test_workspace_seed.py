"""Tests for trigger-payload workspace seeding (utils.workspace_seed)."""

import base64

import pytest

from preloop.utils.workspace_seed import (
    MAX_SEED_FILES,
    MAX_TOTAL_SEED_ENCODED_BYTES,
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
            # `.git` must be rejected at ANY depth: repos may be cloned into
            # sub-paths of /workspace, so nested `.git` writes could plant
            # executable git hooks inside a real repository.
            "client/.git/hooks/post-commit",
            "repo/client/.git/config",
            "deep/.git/HEAD",
            "a/b/.git",
            "a/b/../.git/config",
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

    def test_gitignore_like_names_allowed(self):
        """Only `.git` segments are blocked, not names that merely start
        with `.git` (e.g. `.gitignore`, `.github/`)."""
        files = parse_workspace_files(
            _payload(
                {"path": ".gitignore", "content_base64": _b64(b"x")},
                {"path": "client/.gitignore", "content_base64": _b64(b"x")},
                {"path": ".github/workflows/ci.yml", "content_base64": _b64(b"x")},
            )
        )
        assert [f.path for f in files] == [
            ".gitignore",
            "client/.gitignore",
            ".github/workflows/ci.yml",
        ]

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

    def test_total_cap_enforced_on_encoded_size(self):
        """The cap applies to the base64-ENCODED size (what is embedded in
        the container launch command / K8s Job spec), across all files."""
        # Each file's encoded form is just over half the cap.
        decoded_half = (MAX_TOTAL_SEED_ENCODED_BYTES // 2 // 4) * 3 + 3
        half = _b64(b"x" * decoded_half)
        assert len(half) > MAX_TOTAL_SEED_ENCODED_BYTES // 2
        with pytest.raises(WorkspaceSeedError, match="inline cap"):
            parse_workspace_files(
                _payload(
                    {"path": "a.bin", "content_base64": half},
                    {"path": "b.bin", "content_base64": half},
                )
            )

    def test_decoded_size_at_old_cap_now_rejected(self):
        """1 MiB of decoded content encodes to ~1.33 MiB — over the encoded
        cap. Guards against regressing to a decoded-size check."""
        content = _b64(b"x" * MAX_TOTAL_SEED_ENCODED_BYTES)
        assert len(content) > MAX_TOTAL_SEED_ENCODED_BYTES
        with pytest.raises(WorkspaceSeedError, match="inline cap"):
            parse_workspace_files(
                _payload({"path": "a.bin", "content_base64": content})
            )

    def test_single_file_at_encoded_cap_allowed(self):
        # Decoded size chosen so the encoded form is exactly at the cap.
        decoded = MAX_TOTAL_SEED_ENCODED_BYTES // 4 * 3
        content = _b64(b"x" * decoded)
        assert len(content) == MAX_TOTAL_SEED_ENCODED_BYTES
        files = parse_workspace_files(
            _payload({"path": "a.bin", "content_base64": content})
        )
        assert files[0].decoded_size == decoded

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
        assert "w=/workspace" in shell
        assert "__pl_seed fixtures/input.json" in shell
        assert _b64(b"{}") in shell
        # Runtime symlink-containment guard is present.
        assert "cd -P" in shell
        assert "set -e" in shell
        assert '[ -L "$t" ]' in shell

    def test_paths_are_shell_quoted(self):
        files = parse_workspace_files(
            _payload({"path": "with space/f'ile.txt", "content_base64": _b64(b"x")})
        )
        shell = build_workspace_seed_shell(files)
        # shlex.quote wraps the path and escapes the single quote.
        assert "'with space/f'\"'\"'ile.txt'" in shell


class TestWorkspaceSeedShellRuntime:
    """Functional tests: run the generated block in a real POSIX shell
    against a temp workspace root, including symlink-escape attempts."""

    @staticmethod
    def _run(shell: str):
        import subprocess

        return subprocess.run(["sh", "-c", shell], capture_output=True, text=True)

    def _shell_for(self, root, *entries) -> str:
        files = parse_workspace_files(_payload(*entries))
        return build_workspace_seed_shell(files, workspace_root=str(root))

    def test_materializes_files(self, tmp_path):
        shell = self._shell_for(
            tmp_path,
            {"path": "fixtures/input.json", "content_base64": _b64(b'{"k": 1}')},
            {"path": "top.txt", "content_base64": _b64(b"hello")},
        )
        result = self._run(shell)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "fixtures/input.json").read_bytes() == b'{"k": 1}'
        assert (tmp_path / "top.txt").read_bytes() == b"hello"

    def test_symlinked_parent_escape_refused(self, tmp_path):
        """A cloned repo may contain `fixtures -> <outside>`; the write must
        be refused, not follow the link out of the workspace root."""
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (workspace / "fixtures").symlink_to(outside)

        shell = self._shell_for(
            workspace,
            {"path": "fixtures/input.json", "content_base64": _b64(b"pwned")},
        )
        result = self._run(shell)
        assert result.returncode != 0
        assert "resolves outside" in result.stderr
        assert not (outside / "input.json").exists()

    def test_deep_symlinked_ancestor_escape_refused(self, tmp_path):
        """Symlink higher up the (existing) ancestor chain is also caught,
        and no directories are created outside the root by mkdir -p."""
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (workspace / "a").symlink_to(outside)

        shell = self._shell_for(
            workspace,
            {"path": "a/b/c/input.json", "content_base64": _b64(b"pwned")},
        )
        result = self._run(shell)
        assert result.returncode != 0
        assert "resolves outside" in result.stderr
        assert not (outside / "b").exists()

    def test_symlinked_target_file_refused(self, tmp_path):
        """`> target` must not write through a symlink left by the clone."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"original")
        (workspace / "config.json").symlink_to(secret)

        shell = self._shell_for(
            workspace,
            {"path": "config.json", "content_base64": _b64(b"pwned")},
        )
        result = self._run(shell)
        assert result.returncode != 0
        assert "refusing to write through symlink" in result.stderr
        assert secret.read_bytes() == b"original"

    def test_symlink_inside_workspace_is_contained(self, tmp_path):
        """Symlinked parents that stay inside the root are allowed (the
        guard is about containment, not about symlinks per se)."""
        workspace = tmp_path / "workspace"
        (workspace / "real").mkdir(parents=True)
        (workspace / "alias").symlink_to(workspace / "real")

        shell = self._shell_for(
            workspace,
            {"path": "alias/file.txt", "content_base64": _b64(b"ok")},
        )
        result = self._run(shell)
        assert result.returncode == 0, result.stderr
        assert (workspace / "real/file.txt").read_bytes() == b"ok"

    def test_overwrites_regular_file(self, tmp_path):
        (tmp_path / "existing.txt").write_bytes(b"old")
        shell = self._shell_for(
            tmp_path, {"path": "existing.txt", "content_base64": _b64(b"new")}
        )
        result = self._run(shell)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "existing.txt").read_bytes() == b"new"

    def test_binary_content_roundtrip(self, tmp_path):
        payload = bytes(range(256))
        shell = self._shell_for(
            tmp_path, {"path": "bin/blob.dat", "content_base64": _b64(payload)}
        )
        result = self._run(shell)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "bin/blob.dat").read_bytes() == payload

    def test_quoted_paths_roundtrip(self, tmp_path):
        shell = self._shell_for(
            tmp_path, {"path": "with space/f'ile.txt", "content_base64": _b64(b"x")}
        )
        result = self._run(shell)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "with space" / "f'ile.txt").read_bytes() == b"x"


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
