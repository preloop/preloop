"""One Markdown fence around a native final answer is removed; nothing else is."""

import json
from pathlib import Path
from typing import Any

import pytest

from preloop.services.native_answer import MARKDOWN_FENCE, parse_native_answer
from preloop.services.native_result import (
    NATIVE_CANDIDATES_KEY,
    NATIVE_RESULT_BUNDLE_PATH,
    native_result_capture_script,
)
from preloop.services.native_result_repair import repair_paths, repair_prompt

DOC = {"status": "success", "summary": "Looks good", "count": 2}
BODY = json.dumps(DOC, indent=2)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "summary"],
    "properties": {"status": {"enum": ["success"]}, "summary": {"type": "string"}},
}

#: (answer, expected document, unwrapped?) — None as document means rejected.
CASES: list[tuple[str, Any, bool]] = [
    (json.dumps(DOC), DOC, False),
    (f"```json\n{BODY}\n```", DOC, True),
    (f"```\n{BODY}\n```", DOC, True),
    (f"```JSON\n{BODY}\n```", DOC, True),
    (f"```Json\r\n{BODY}\r\n```", DOC, True),
    (f"\n\n  ```json\n{BODY}\n```  \n", DOC, True),
    (
        '```json\n{"status": "success", "summary": "use ```code``` here"}\n```',
        {"status": "success", "summary": "use ```code``` here"},
        True,
    ),
    (f"```json\n{BODY}\n```\n```json\n{BODY}\n```", None, False),
    (f"Here is the review:\n```json\n{BODY}\n```", None, False),
    (f"```json\n{BODY}\n```\nLet me know if you need more.", None, False),
    ('```json\n{"status": "success",}\n```', None, False),
    ("```json\n{'status': 'success'}\n```", None, False),
    ('```json\n// note\n{"status": "success"}\n```', None, False),
    (f"```python\n{BODY}\n```", None, False),
    ("not json at all", None, False),
]
IDS = [
    "bare",
    "fenced-json",
    "fenced-no-tag",
    "uppercase-tag",
    "crlf",
    "surrounding-whitespace",
    "backticks-in-string",
    "two-blocks",
    "prose-before",
    "prose-after",
    "fenced-invalid",
    "single-quotes",
    "comment",
    "other-language",
    "prose",
]


@pytest.mark.parametrize("answer,document,unwrapped", CASES, ids=IDS)
def test_parse_native_answer(answer: str, document: Any, unwrapped: bool) -> None:
    if document is None:
        with pytest.raises(ValueError):
            parse_native_answer(answer)
        return
    assert parse_native_answer(answer) == (
        document,
        MARKDOWN_FENCE if unwrapped else None,
    )
    assert parse_native_answer(answer.encode()) == parse_native_answer(answer)


def _capture(tmp_path: Path, answer: str) -> dict[str, Any]:
    (tmp_path / ".preloop-native-result.json").write_text(answer)
    exec(native_result_capture_script().replace("/workspace", str(tmp_path)), {})
    written = tmp_path / Path(NATIVE_RESULT_BUNDLE_PATH).name
    return json.loads(written.read_text())[NATIVE_CANDIDATES_KEY]


@pytest.mark.parametrize("answer,document,unwrapped", CASES, ids=IDS)
def test_checker_and_controller_capture_agree(
    tmp_path: Path, answer: str, document: Any, unwrapped: bool
) -> None:
    """The in-container checker and the capture read every answer the same way."""
    candidates = _capture(tmp_path, answer)
    prompt = repair_prompt(answer, SCHEMA, "Review")
    if document is None:
        assert candidates["native"] is None
        assert candidates["native_error"] == "not_json"
        assert "native_unwrapped" not in candidates
        assert repair_paths(answer, SCHEMA) == ["$"]
        assert "Not valid JSON" in prompt
        return
    assert candidates["native"] == document
    assert "native_error" not in candidates
    assert candidates.get("native_unwrapped") == (MARKDOWN_FENCE if unwrapped else None)
    # The unwrapped document is then held to the schema like any other.
    assert repair_paths(answer, SCHEMA) == repair_paths(json.dumps(document), SCHEMA)
    assert "Not valid JSON" not in prompt


def test_fenced_document_that_breaks_the_schema_still_needs_repair() -> None:
    answer = '```json\n{"status": "failed"}\n```'
    assert sorted(repair_paths(answer, SCHEMA)) == ["$.status", "$.summary"]


def test_repair_prompt_says_what_was_wrong_with_a_non_json_answer() -> None:
    answer = f"Here is the review:\n```json\n{BODY}\n```"
    prompt = repair_prompt(answer, SCHEMA, "Review")
    assert (
        "- $: Your answer was wrapped in a Markdown code fence or contained text "
        "outside the JSON. Reply with the JSON object only: no backticks, no "
        "prose. Not valid JSON: "
    ) in prompt
    # The previous answer is still quoted back.
    assert answer in prompt
