"""Conservative command-log evidence of reading a bound review change.

Stdlib only: the same matcher runs in the hosted wrapper and the controller.
This is an inspection floor, not proof of full diff coverage or review quality.
"""

import json
import re
import shlex
from pathlib import PurePosixPath
from typing import Any


def command_reads_change(
    command: Any, *, base: str, head: str, files: list[str], root: str = ""
) -> bool:
    """Recognize a patch read or a read of a controller-listed changed file.

    Only simple commands and successful ``&&`` chains qualify. Shell wrappers
    are unwrapped, never executed. Redirections, pipelines, substitutions and
    control flow are deliberately not inferred from text or output.
    """
    if not isinstance(command, str) or not base or not head:
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    if len(tokens) == 3 and PurePosixPath(tokens[0]).name in {"bash", "sh", "zsh"}:
        if tokens[1] in {"-c", "-lc"}:
            return command_reads_change(
                tokens[2], base=base, head=head, files=files, root=root
            )
    if any(
        token in {";", "||", "|", "&", "(", ")"}
        or "<" in token
        or ">" in token
        or "`" in token
        or "$(" in token
        for token in tokens
    ):
        return False
    parts: list[list[str]] = [[]]
    for token in tokens:
        if token == "&&":
            parts.append([])
        else:
            for name, value in (("BASE_SHA", base), ("HEAD_SHA", head)):
                token = token.replace("${PRELOOP_REVIEW_" + name + "}", value)
                token = token.replace("$PRELOOP_REVIEW_" + name, value)
            parts[-1].append(token)

    def changed(path: str) -> bool:
        normalized = str(PurePosixPath(path))
        if ".." in PurePosixPath(path).parts:
            return False
        if root and normalized.startswith(root.rstrip("/") + "/"):
            normalized = normalized[len(root.rstrip("/")) + 1 :]
        return normalized in files

    for args in parts:
        if not args:
            continue
        program, *args = args
        program = PurePosixPath(program).name
        if program == "git":
            if len(args) >= 2 and args[0] == "-C":
                args = args[2:]
            if not args:
                continue
            operation, *args = args
            if operation == "diff":
                # Default output is the patch. Summary-only flags never count.
                allowed = {
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--no-renames",
                    "--binary",
                    "--full-index",
                    "-p",
                    "--patch",
                    "--",
                }
                if any(
                    arg.startswith("-")
                    and arg not in allowed
                    and not re.fullmatch(r"(?:-U|--unified=)\d+", arg)
                    for arg in args
                ):
                    continue
                revisions = args[: args.index("--")] if "--" in args else args
                paths = args[args.index("--") + 1 :] if "--" in args else []
                if f"{base}...{head}" in revisions and (
                    not paths or any(path == "." or changed(path) for path in paths)
                ):
                    return True
            if operation == "show" and len(args) == 1:
                revision, separator, path = args[0].partition(":")
                if separator and revision in {base, head} and changed(path):
                    return True
        elif program in {"cat", "head", "tail", "nl"}:
            if any(arg in {"--help", "--version"} for arg in args):
                continue
            if program in {"head", "tail"} and any(
                arg == "0" or re.fullmatch(r"(?:-|\-n|\-c|--lines=|--bytes=)0+", arg)
                for arg in args
            ):
                continue
            if any(changed(arg) for arg in args if not arg.startswith("-")):
                return True
        elif program == "sed" and len(args) >= 3 and args[0] == "-n":
            if re.fullmatch(r"\d+(?:,\d+)?p", args[1]) and any(
                changed(arg) for arg in args[2:]
            ):
                return True
    return False


def log_reads_change(
    lines: Any, *, base: str, head: str, files: list[str], root: str = ""
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
                item.get("command"), base=base, head=head, files=files, root=root
            ):
                return True
    return False
