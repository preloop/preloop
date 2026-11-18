"""Conservative command-log evidence of reading a bound review change.

Stdlib only: the same matcher runs in the hosted wrapper and the controller,
so both decide from the same inputs: the command text and its exit code.
This is an inspection floor, not proof of full diff coverage or review quality.

A command counts when one of its segments prints the bound patch or a changed
file, and that segment's own success follows from the command's exit code
being 0. Accepted reads:

* ``git diff`` of the bound range: ``<base>...<head>``, or, when the bound base
  is the merge base, ``<base>..<head>`` and ``<base> <head>``. Revisions are
  full SHAs, abbreviations of at least seven hex digits, or the
  ``$PRELOOP_REVIEW_BASE_SHA`` / ``$PRELOOP_REVIEW_HEAD_SHA`` variables. Only
  options that keep the whole patch are allowed (rename and copy detection,
  context lines, colour, diff algorithm, external diff and textconv off).
  A pathspec after ``--`` may be ``.`` or name a changed file.
* ``git show <base|head>:<changed file>``.
* ``cat``, ``nl``, ``head``, ``tail`` or ``sed -n '<range>p'`` of a changed file.

The segment must not be piped or have its standard output redirected, and its
success must be implied by the exit code: it is the first segment or follows
``;`` or ``&&``, and only ``&&`` follows it. Earlier ``;`` segments may fail
without effect on it. Commands with substitutions, subshells, background
jobs, input redirection, output written to a file, or shell state changes
(``export``, ``source``, ``eval``, aliases, variable assignments) never count.
Literal ``HEAD`` never names the head: the agent owns the checkout and could
have moved it.
"""

import json
import re
import shlex
from pathlib import PurePosixPath
from typing import Any

#: ``git diff`` options that never drop part of the patch from the output.
DIFF_OPTIONS = frozenset(
    {
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--color=never",
        "--no-renames",
        "--binary",
        "--full-index",
        "-p",
        "-u",
        "--patch",
        "--find-renames",
        "--find-copies",
        "--find-copies-harder",
        "-M",
        "-C",
        "--minimal",
        "--patience",
        "--histogram",
        "--indent-heuristic",
        "--no-indent-heuristic",
    }
)
DIFF_OPTION_PATTERN = re.compile(
    r"(?:-U|--unified=)\d+"
    r"|(?:-M|-C)\d+%?"
    r"|--find-(?:renames|copies)=\d+%?"
    r"|--diff-algorithm=(?:default|myers|minimal|patience|histogram)"
)

#: Shell builtins that change state a later segment depends on.
STATE_CHANGING = frozenset(
    {
        "export",
        "source",
        ".",
        "eval",
        "exec",
        "alias",
        "unset",
        "declare",
        "typeset",
        "readonly",
        "local",
        "function",
        "pushd",
        "popd",
        "shopt",
        "trap",
        "enable",
        "hash",
        # Wrappers run another command, which may be one of the above.
        "command",
        "builtin",
        "env",
        "nohup",
        "timeout",
        "stdbuf",
        "nice",
        "setsid",
        "xargs",
        "sudo",
        "su",
    }
)

#: Words that make the rest of a segment conditional or grouped.
CONTROL_WORDS = frozenset(
    {
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "select",
        "{",
        "}",
        "!",
        "time",
        "coproc",
    }
)

#: Commands that write a file named in their arguments.
FILE_WRITERS = frozenset({"tee", "sponge"})

OPERATORS = frozenset({";", "\n", "&&", "||"})
REDIRECTS = frozenset({">", ">>", ">&", "&>", "&>>"})


def _revision(token: str, full: str) -> bool:
    """Whether ``token`` names ``full``: the SHA or an unambiguous abbreviation.

    Git refuses an abbreviation that matches two commits, so on a successful
    command a prefix of at least seven hex digits names the bound commit.
    """
    return bool(full) and (
        token == full
        or (
            re.fullmatch(r"[0-9a-f]{7,64}", token) is not None
            and full.startswith(token)
        )
    )


def _parse(command: str) -> list[tuple[str | None, list[list[str]], str | None]] | None:
    """Split a command into and-or list members.

    Returns:
        ``(operator before, pipeline stages, operator after)`` per member, each
        stage its words with harmless redirections removed, or None when the
        command uses a form whose effect is not visible in its text.
    """
    if re.search(r"(?:^|\s)\d+\s+[<>&]", command):
        # ``2 >file`` passes ``2`` as an argument and moves standard output;
        # the tokens alone cannot tell it from ``2>file``.
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    members: list[tuple[str | None, list[list[str]], str | None]] = []
    stages: list[list[str]] = [[]]
    words = stages[0]
    before: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if "`" in token or "$(" in token or "<" in token:
            return None
        if token in OPERATORS or token == "|":
            if not words:
                if token == "\n":
                    # A blank line, or a line continued after an operator.
                    index += 1
                    continue
                return None
            if token == "|":
                words = []
                stages.append(words)
            else:
                operator = ";" if token == "\n" else token
                members.append((before, stages, operator))
                before = operator
                words = []
                stages = [words]
            index += 1
            continue
        if token in REDIRECTS or (
            token.isdigit()
            and index + 2 < len(tokens)
            and tokens[index + 1] in REDIRECTS
        ):
            fd = token if token.isdigit() else None
            operator = tokens[index + 1] if fd else token
            target_index = index + 2 if fd else index + 1
            if target_index >= len(tokens):
                return None
            target = tokens[target_index]
            index = target_index + 1
            stderr_only = fd == "2"
            if operator == ">&" and target.isdigit():
                # Duplicating a descriptor writes no file; ``2>&1`` keeps the
                # output visible. Moving standard output is marked below.
                if not stderr_only:
                    words.append("\0stdout")
                continue
            if target != "/dev/null":
                return None
            if not stderr_only:
                words.append("\0stdout")
            continue
        if token in {"&", "(", ")"} or set(token) <= set(";&|<>()"):
            return None
        words.append(token)
        index += 1
    if words:
        members.append((before, stages, None))
    elif members:
        # A trailing newline or ``;`` ends the list.
        if before in {"&&", "||"}:
            return None
    return members


def command_reads_change(
    command: Any,
    *,
    base: str,
    head: str,
    files: list[str],
    root: str = "",
    merge_base: str = "",
) -> bool:
    """Recognize a read of the bound patch or of a controller-listed changed file.

    The rule is in the module docstring. Shell wrappers are unwrapped, never
    executed; nothing is inferred from output.

    Args:
        command: The command text as the agent ran it.
        base: Bound base SHA.
        head: Bound head SHA.
        files: Paths the bound change touches, relative to the repository.
        root: The reviewed checkout, the agent's starting directory.
        merge_base: Merge base of ``base`` and ``head``, when known.

    Returns:
        True when a segment whose success the exit code implies read the change.
    """
    if not isinstance(command, str) or not base or not head:
        return False
    try:
        outer = shlex.split(command)
    except ValueError:
        return False
    if len(outer) == 3 and PurePosixPath(outer[0]).name in {"bash", "sh", "zsh"}:
        if outer[1] in {"-c", "-lc"}:
            return command_reads_change(
                outer[2],
                base=base,
                head=head,
                files=files,
                root=root,
                merge_base=merge_base,
            )
    members = _parse(command)
    if not members:
        return False
    for _, stages, _ in members:
        for stage in stages:
            words = [w for w in stage if not w.startswith("\0")]
            if not words:
                return False
            name = PurePosixPath(words[0]).name
            if (
                name in STATE_CHANGING
                or name in CONTROL_WORDS
                or name in FILE_WRITERS
                or _writes_git_config(words)
                or re.match(r"[A-Za-z_][A-Za-z0-9_]*=", words[0])
            ):
                return False

    cwd: str | None = root
    cd_pending = False
    for position, (before, stages, after) in enumerate(members):
        words = stages[0]
        established = (
            len(stages) == 1
            and "\0stdout" not in words
            and before in {None, ";", "&&"}
            and all(member[2] in {"&&", None} for member in members[position:])
        )
        args = [_substitute(w, base, head) for w in words if not w.startswith("\0")]
        program = PurePosixPath(args[0]).name
        if program == "cd" and len(stages) == 1:
            if len(args) != 2 or ".." in PurePosixPath(args[1]).parts:
                return False
            target = PurePosixPath(args[1])
            if before == "||":
                # Reached only when what came before failed: it may not run.
                cwd = None
            elif target.is_absolute():
                cwd = str(target)
            elif cwd is not None:
                cwd = str(PurePosixPath(cwd) / target) if cwd else str(target)
            cd_pending = True
        elif established and _reads(
            args,
            cwd,
            base=base,
            head=head,
            files=files,
            root=root,
            merge_base=merge_base,
        ):
            return True
        if after in {";", "||"} and cd_pending:
            # A failed ``cd`` does not stop what follows ``;`` or ``||``.
            cwd = None
            cd_pending = False
    return False


def _writes_git_config(words: list[str]) -> bool:
    """Whether a ``git`` call may be a ``git config`` write.

    Any ``config`` word counts, wherever Git's global options put the
    subcommand: listing every global option that takes a value is brittle,
    and rejecting a read-only call here only costs a false negative.
    """
    if PurePosixPath(words[0]).name != "git" or "config" not in words[1:]:
        return False
    rest = words[words.index("config") + 1 :]
    return not any(arg in {"-l", "--list"} or arg.startswith("--get") for arg in rest)


def _substitute(token: str, base: str, head: str) -> str:
    """Expand the review SHA variables the hosted wrapper exports."""
    for name, value in (("BASE_SHA", base), ("HEAD_SHA", head)):
        token = token.replace("${PRELOOP_REVIEW_" + name + "}", value)
        token = token.replace("$PRELOOP_REVIEW_" + name, value)
    return token


def _changed(path: str, directory: str | None, files: list[str], root: str) -> bool:
    """Whether ``path``, seen from ``directory``, is a changed file."""
    pure = PurePosixPath(path)
    if ".." in pure.parts:
        return False
    if not pure.is_absolute():
        if directory is None:
            return False
        if directory:
            pure = PurePosixPath(directory) / pure
    normalized = str(pure)
    if root and normalized.startswith(root.rstrip("/") + "/"):
        normalized = normalized[len(root.rstrip("/")) + 1 :]
    return normalized in files


def _reads(
    args: list[str],
    cwd: str | None,
    *,
    base: str,
    head: str,
    files: list[str],
    root: str,
    merge_base: str,
) -> bool:
    """Whether one simple command prints the bound patch or a changed file."""
    program, *args = args
    program = PurePosixPath(program).name
    if program == "git":
        directory = cwd
        while args and args[0] in {"-C", "--no-pager", "-P"}:
            if args[0] == "-C":
                if len(args) < 2:
                    return False
                target = PurePosixPath(args[1])
                if ".." in target.parts:
                    return False
                if target.is_absolute():
                    directory = str(target)
                elif directory is not None:
                    directory = str(PurePosixPath(directory) / target)
                args = args[2:]
            else:
                args = args[1:]
        if not args:
            return False
        operation, *args = args
        if operation == "diff":
            return _diff_reads(
                args,
                directory,
                base=base,
                head=head,
                files=files,
                root=root,
                merge_base=merge_base,
            )
        if operation == "show" and len(args) == 1:
            revision, separator, path = args[0].partition(":")
            return (
                bool(separator)
                and (_revision(revision, base) or _revision(revision, head))
                and _changed(path, "", files, "")
            )
        return False
    if program in {"cat", "head", "tail", "nl"}:
        if any(arg in {"--help", "--version"} for arg in args):
            return False
        if program in {"head", "tail"} and any(
            arg == "0" or re.fullmatch(r"(?:-|\-n|\-c|--lines=|--bytes=)0+", arg)
            for arg in args
        ):
            return False
        return any(
            _changed(arg, cwd, files, root) for arg in args if not arg.startswith("-")
        )
    if program == "sed" and len(args) >= 3 and args[0] == "-n":
        return re.fullmatch(r"\d+(?:,\d+)?p", args[1]) is not None and any(
            _changed(arg, cwd, files, root) for arg in args[2:]
        )
    return False


def _diff_reads(
    args: list[str],
    directory: str | None,
    *,
    base: str,
    head: str,
    files: list[str],
    root: str,
    merge_base: str,
) -> bool:
    """Whether ``git diff <args>`` prints the whole bound patch."""
    if "--" in args:
        split = args.index("--")
        options_and_revisions, paths = args[:split], args[split + 1 :]
    else:
        options_and_revisions, paths = args, []
    revisions = []
    for arg in options_and_revisions:
        if arg.startswith("-"):
            if arg not in DIFF_OPTIONS and not DIFF_OPTION_PATTERN.fullmatch(arg):
                return False
        else:
            revisions.append(arg)
    if "--" not in args:
        # Without ``--`` Git reads the arguments after the revisions as paths.
        count = 1 if revisions and ".." in revisions[0] else 2
        revisions, paths = revisions[:count], revisions[count:]
    if len(revisions) == 1 and "..." in revisions[0]:
        left, right = revisions[0].split("...", 1)
        bound = (_revision(left, base) or _revision(left, merge_base)) and _revision(
            right, head
        )
    elif len(revisions) == 1 and ".." in revisions[0]:
        left, right = revisions[0].split("..", 1)
        bound = _revision(left, merge_base) and _revision(right, head)
    elif len(revisions) == 2:
        bound = _revision(revisions[0], merge_base) and _revision(revisions[1], head)
    else:
        bound = False
    if not bound:
        return False
    return not paths or any(
        (path == "." and directory in {root, ""})
        or _changed(path, directory, files, root)
        for path in paths
    )


def log_reads_change(
    lines: Any,
    *,
    base: str,
    head: str,
    files: list[str],
    root: str = "",
    merge_base: str = "",
) -> bool:
    """Accept only successful completed native command events, never prose."""
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        if type(item.get("exit_code")) is int and item["exit_code"] == 0:
            if command_reads_change(
                item.get("command"),
                base=base,
                head=head,
                files=files,
                root=root,
                merge_base=merge_base,
            ):
                return True
    return False


def inspection_receipt(binding: Any, diff: Any, root: str) -> dict[str, Any]:
    """Bind a server-computed read receipt without retaining command or path text.

    Only the native evidence writer creates this receipt, before privacy
    redaction. Gateway/model claims never enter this channel.
    """
    import hashlib

    context = {
        "base": binding.get("base_sha"),
        "head": binding.get("head_sha"),
        "diff": diff or {},
        "root": root,
    }
    fingerprint = hashlib.sha256(
        json.dumps(context, sort_keys=True).encode()
    ).hexdigest()
    return {"version": 1, "context_sha256": fingerprint, "read": True}
