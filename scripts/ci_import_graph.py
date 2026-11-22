"""Static import graph from backend test files to the modules they load.

Each ``backend/preloop`` and ``backend/tests`` module is parsed with ``ast``
(never imported). A file's references are split in two:

``top``
    Imports that run when the module is imported: module level, class
    bodies and ``try``/``if`` blocks at that level. In a test file every
    import and every ``preloop.`` string constant (a ``patch`` target) is
    ``top``, because the test runs the function that holds it.
``lazy``
    Imports a source module defers into a function body or a
    ``TYPE_CHECKING`` block, plus dotted ``preloop.`` string constants
    (``importlib.import_module`` arguments, logger names). They run only
    when that code path runs, and they are how the codebase breaks import
    cycles: following them transitively joins nearly every module into one
    component, so selection follows them a bounded number of hops.

Importing ``a.b.c`` also loads the packages ``a`` and ``a.b``, so package
``__init__`` modules are dependencies too. Parsing is cached per file by git
blob id, so a rebuild in CI only parses files whose content changed since the
cached copy.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import subprocess
from typing import TypedDict

BACKEND = "backend"
PACKAGES = ("preloop", "tests")
CACHE_VERSION = 2
_DOTTED = re.compile(r"^preloop(?:\.\w+)+$")


class References(TypedDict):
    """Direct references of one module."""

    top: list[str]
    lazy: list[str]


Graph = dict[str, References]


def module_name(path: str) -> str:
    """Return the dotted import name of a ``backend/`` Python file.

    Args:
        path: Repository-relative path such as ``backend/preloop/a/b.py``.

    Returns:
        The dotted name (``preloop.a.b``); a package ``__init__`` maps to the
        package name.
    """
    parts = Path(path).relative_to(BACKEND).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _relative_base(module: str, is_package: bool, level: int) -> str:
    """Resolve the package a ``from .x import y`` statement is relative to."""
    parts = module.split(".")
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    return ".".join(parts)


def _is_type_checking(node: ast.If) -> bool:
    """Whether an ``if`` tests ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``."""
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def direct_references(
    source: str, module: str, is_package: bool, *, is_test: bool = False
) -> References:
    """List the dotted names a module source refers to.

    Args:
        source: Python source text.
        module: Dotted name of the module, used to resolve relative imports.
        is_package: Whether the source is a package ``__init__``.
        is_test: Treat every reference as ``top`` (a ``backend/tests`` file).

    Returns:
        Sorted ``top`` and ``lazy`` names. ``from a import b`` yields both
        ``a`` and ``a.b``, because ``b`` may be a submodule. Unparseable
        source yields nothing; pytest reports that file's error itself.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {"top": [], "lazy": []}
    top: set[str] = set()
    lazy: set[str] = set()

    def visit(node: ast.AST, deferred: bool) -> None:
        for child in ast.iter_child_nodes(node):
            inner = deferred or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            )
            if isinstance(child, ast.If) and _is_type_checking(child):
                inner = True
            bucket = top if is_test or not deferred else lazy
            if isinstance(child, ast.Import):
                bucket.update(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                base = child.module or ""
                if child.level:
                    prefix = _relative_base(module, is_package, child.level)
                    base = f"{prefix}.{base}" if base else prefix
                if base:
                    bucket.add(base)
                    bucket.update(f"{base}.{alias.name}" for alias in child.names)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                if _DOTTED.match(child.value):
                    (top if is_test else lazy).add(child.value)
            if isinstance(child, ast.If) and _is_type_checking(child):
                visit(child.test, deferred)
                for stmt in child.body:
                    visit_stmt(stmt, True)
                for stmt in child.orelse:
                    visit_stmt(stmt, deferred)
            else:
                visit(child, inner)

    def visit_stmt(stmt: ast.stmt, deferred: bool) -> None:
        wrapper = ast.Module(body=[stmt], type_ignores=[])
        visit(wrapper, deferred)

    visit(tree, False)

    def keep(names: set[str]) -> list[str]:
        return sorted(n for n in names if n.split(".", 1)[0] in PACKAGES)

    return {"top": keep(top), "lazy": keep(lazy - top)}


def python_files(root: Path) -> dict[str, str]:
    """Map backend Python files to a content id.

    Args:
        root: Repository root.

    Returns:
        Repository-relative path to git blob id, or ``"untracked"`` for files
        whose working copy git has not indexed (always reparsed).
    """
    roots = [f"{BACKEND}/{package}" for package in PACKAGES]
    files: dict[str, str] = {}
    staged = subprocess.run(
        ["git", "ls-files", "-s", "--", *roots],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for line in staged.splitlines():
        meta, path = line.split("\t", 1)
        if path.endswith(".py") and (root / path).is_file():
            files[path] = meta.split()[1]
    dirty = subprocess.run(
        ["git", "ls-files", "-m", "-o", "--exclude-standard", "--", *roots],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for path in dirty.splitlines():
        if path.endswith(".py") and (root / path).is_file():
            files[path] = "untracked"
    return files


def build(root: Path, cache_path: Path | None = None) -> Graph:
    """Return each backend module's direct references, reusing the cache.

    Args:
        root: Repository root.
        cache_path: JSON cache of earlier parses; rewritten when it changes.

    Returns:
        Repository-relative file path to its references.
    """
    cache: dict[str, dict[str, object]] = {}
    if cache_path is not None and cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text())
            if data.get("version") == CACHE_VERSION:
                cache = data["files"]
        except (OSError, ValueError, KeyError):
            cache = {}
    graph: Graph = {}
    fresh: dict[str, dict[str, object]] = {}
    for path, blob in sorted(python_files(root).items()):
        hit = cache.get(path)
        if blob != "untracked" and hit and hit.get("blob") == blob:
            refs: References = hit["refs"]  # type: ignore[assignment]
        else:
            refs = direct_references(
                (root / path).read_text(errors="replace"),
                module_name(path),
                path.endswith("__init__.py"),
                is_test=path.startswith(f"{BACKEND}/tests/"),
            )
        graph[path] = refs
        if blob != "untracked":
            fresh[path] = {"blob": blob, "refs": refs}
    if cache_path is not None and fresh != cache:
        cache_path.write_text(
            json.dumps({"version": CACHE_VERSION, "files": fresh}, sort_keys=True)
        )
    return graph


def _parents(name: str) -> list[str]:
    """Return ``a``, ``a.b`` and ``a.b.c`` for ``a.b.c``."""
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _reverse(graph: Graph, kind: str, known: set[str]) -> dict[str, set[str]]:
    """Map each module name to the modules that reference it."""
    reverse: dict[str, set[str]] = {}
    for path, refs in graph.items():
        importer = module_name(path)
        for ref in refs[kind]:  # type: ignore[literal-required]
            # Every known prefix loads. The name itself stays a node even
            # when no module has it (a deleted module), so its old importers
            # still match it.
            for target in {p for p in _parents(ref) if p in known} | {ref}:
                if target != importer:
                    reverse.setdefault(target, set()).add(importer)
    return reverse


def _walk(reverse: dict[str, set[str]], start: set[str]) -> set[str]:
    """Return ``start`` and everything that reaches it in ``reverse``."""
    seen: set[str] = set()
    stack = list(start)
    while stack:
        name = stack.pop()
        if name not in seen:
            seen.add(name)
            stack.extend(reverse.get(name, ()))
    return seen


def importers(graph: Graph, changed: list[str], lazy_hops: int = 1) -> set[str]:
    """Return the backend files that load a changed module.

    Equivalent to computing each file's transitive import closure and keeping
    the files whose closure names a changed module, without materialising
    every closure. ``top`` references are followed transitively; ``lazy``
    references are crossed at most ``lazy_hops`` times on any path.

    Args:
        graph: Output of :func:`build`.
        changed: Dotted names of changed (or deleted) modules.
        lazy_hops: How many deferred imports a dependency chain may cross.

    Returns:
        Repository-relative paths of files that load a changed module,
        including the changed files themselves when they still exist.
    """
    by_name = {module_name(path): path for path in graph}
    known = set(by_name)
    top = _reverse(graph, "top", known)
    lazy = _reverse(graph, "lazy", known)
    reached = _walk(top, set(changed))
    frontier = reached
    for _ in range(lazy_hops):
        crossed = {i for name in frontier for i in lazy.get(name, ())} - reached
        frontier = _walk(top, crossed) - reached
        reached |= frontier
    return {by_name[name] for name in reached if name in by_name}
