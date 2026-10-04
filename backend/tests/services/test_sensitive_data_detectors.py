"""Table-driven tests for the shared sensitive-data detector library (#1121).

Every fixture is synthetic: documented test numbers (Visa 4111..., the IBAN
registry example, RFC 5737 addresses) or values built to pass or fail the
public checksum.
"""

from __future__ import annotations

import os
import random
import time

import pytest

from preloop.services.sensitive_data.detectors import (
    BUILTIN_TYPE_IDS,
    CustomPattern,
    DetectorConfig,
    KeywordList,
    Match,
    UnsafePatternError,
    compile_keyword_pattern,
    compile_safe_regex,
    detect,
    list_types,
    register_detector,
    reset_detectors,
    resolve_overlaps,
    types_found,
)


def _types(text: str, *selected: str, **config) -> list[str]:
    cfg = DetectorConfig(types=tuple(selected) or None, **config)
    return types_found(detect(text, cfg))


POSITIVE_CASES = [
    ("email", "Contact alice@example.com for the report."),
    ("email", "mail: first.last+tag@sub.example.co.uk"),
    ("phone", "call (415) 555-2671 today"),
    ("phone", "E.164: +14155552671"),
    ("phone", "UK: +44 20 7946 0958"),
    ("phone", "DE national: 030 1234567"),
    ("phone", "FR national: 06 12 34 56 78"),
    ("credit_card", "card 4111 1111 1111 1111"),
    ("credit_card", "amex 3782-822463-10005"),
    ("iban", "IBAN DE89 3704 0044 0532 0130 00"),
    ("iban", "GB82WEST12345698765432"),
    ("ip_address", "host 203.0.113.7 responded"),
    ("ip_address", "v6 2001:db8::1 responded"),
    ("ip_address", "v6 full 2001:0db8:85a3:0000:0000:8a2e:0370:7334"),
    ("date_of_birth", "DOB: 1980-05-01"),
    ("date_of_birth", "born on 12 March 1975"),
    ("date_of_birth", "Geburtsdatum 01.05.1980"),
    ("date_of_birth", "03/04/1990 (date of birth)"),
    ("national_id", "SSN 123-45-6789"),
    ("national_id", "Steuer-ID 36574261809"),
    ("national_id", "NINO AB 12 34 56 C"),
    ("national_id", "NIR 2 55 08 14 168 025 38"),
    ("national_id", "BSN 123456782"),
    ("medical_record_number", "MRN: 00123456"),
    ("medical_record_number", "Patient ID AB-12345"),
    ("person_name", "patient: Jane Doe"),
    ("person_name", "Dr. Alice Smith attended"),
    ("person_name", "Name: Jean-Luc Picard"),
]

NEGATIVE_CASES = [
    ("email", "no at sign here example.com"),
    ("phone", "order number 12345"),
    ("phone", "year 2024 and 1999"),
    ("credit_card", "card 4111 1111 1111 1112"),  # fails Luhn
    ("credit_card", "1234567890123"),  # 13 digits, not Luhn
    ("iban", "DE89 3704 0044 0532 0130 01"),  # bad mod-97
    ("iban", "DE00 0000"),
    ("ip_address", "999.1.1.1"),
    ("ip_address", "version 1.2.3"),
    ("ip_address", "time 12:30"),
    ("date_of_birth", "Meeting on 2024-05-01 at noon"),  # no DOB keyword
    ("date_of_birth", "invoice dated 01.05.1980"),
    ("national_id", "000-12-3456"),  # invalid SSN area
    ("national_id", "Steuer-ID 12345678901"),  # structure rule fails
    ("national_id", "NINO BG 12 34 56 C"),  # forbidden prefix
    ("national_id", "NINO QQ 12 34 56 C"),  # Q is not a valid first letter
    ("national_id", "BSN 123456789"),  # 11-proef fails
    ("medical_record_number", "record of 00123456 visits"),
    ("person_name", "the patient was discharged"),
]


@pytest.mark.parametrize(("type_id", "text"), POSITIVE_CASES)
def test_builtin_type_positive(type_id: str, text: str) -> None:
    assert type_id in _types(text, type_id), f"{type_id} missed in {text!r}"


@pytest.mark.parametrize(("type_id", "text"), NEGATIVE_CASES)
def test_builtin_type_negative(type_id: str, text: str) -> None:
    assert type_id not in _types(text, type_id), f"{type_id} false positive {text!r}"


def test_every_builtin_type_has_a_positive_case() -> None:
    covered = {type_id for type_id, _ in POSITIVE_CASES}
    assert covered == set(BUILTIN_TYPE_IDS)


def test_match_spans_point_at_the_value() -> None:
    text = "Contact alice@example.com now"
    (match,) = detect(text, DetectorConfig(types=("email",)))
    assert text[match.start : match.end] == "alice@example.com"
    assert 0 < match.confidence <= 1.0


def test_national_id_locale_filter() -> None:
    text = "SSN 123-45-6789 and NINO AB 12 34 56 C"
    all_matches = detect(text, DetectorConfig(types=("national_id",)))
    assert len(all_matches) == 2
    uk_only = detect(text, DetectorConfig(types=("national_id",), locales=("uk",)))
    assert len(uk_only) == 1
    assert text[uk_only[0].start : uk_only[0].end] == "AB 12 34 56 C"


def test_overlaps_resolve_longest_first() -> None:
    text = "card 4111 1111 1111 1111"
    matches = detect(text, DetectorConfig(types=("phone", "credit_card")))
    assert [m.type for m in matches] == ["credit_card"]


def test_resolve_overlaps_prefers_confidence_on_equal_length() -> None:
    kept = resolve_overlaps(
        [Match("a", 0, 5, 0.5), Match("b", 0, 5, 0.9), Match("c", 10, 12, 1.0)]
    )
    assert [(m.type, m.start) for m in kept] == [("b", 0), ("c", 10)]


def test_default_config_scans_every_builtin_type() -> None:
    text = "alice@example.com DOB: 1980-05-01 IBAN DE89 3704 0044 0532 0130 00"
    assert set(_types(text)) == {"email", "date_of_birth", "iban"}


def test_empty_text_returns_no_matches() -> None:
    assert detect("") == []
    assert detect(None) == []  # type: ignore[arg-type]


class TestCustomPatterns:
    @pytest.mark.parametrize(
        "pattern",
        ["(a+)+", r"(\w*\s?)*", "(x{2,}){3,}", "((a)+)+", r"(?:\d{3}-)+\d{4}"],
    )
    def test_nested_quantifier_is_rejected(self, pattern: str) -> None:
        with pytest.raises(UnsafePatternError, match="nested quantifier"):
            compile_safe_regex(pattern)

    @pytest.mark.parametrize(
        "pattern",
        ["(ab)+c", "a+b+", "[a+]+", "(?:a|b)+", "(a+)b+", "(?P<x>ab)+", "(?i)abc+"],
    )
    def test_safe_patterns_compile(self, pattern: str) -> None:
        assert compile_safe_regex(pattern).pattern == pattern

    def test_length_cap(self) -> None:
        with pytest.raises(UnsafePatternError, match="exceeds"):
            compile_safe_regex("a" * 513)

    def test_invalid_flag_and_regex(self) -> None:
        with pytest.raises(UnsafePatternError, match="Unknown regex flag"):
            compile_safe_regex("abc", ["z"])
        with pytest.raises(UnsafePatternError, match="Invalid regex"):
            compile_safe_regex("abc(")

    def test_valid_custom_pattern_matches_as_its_own_type(self) -> None:
        cfg = DetectorConfig(
            types=("employee_id",),
            custom_patterns=(CustomPattern("employee_id", r"EMP-\d{6}", ("i",)),),
        )
        text = "badge emp-123456 issued"
        (match,) = detect(text, cfg)
        assert match.type == "employee_id"
        assert text[match.start : match.end] == "emp-123456"

    def test_custom_pattern_is_included_in_the_default_selection(self) -> None:
        cfg = DetectorConfig(custom_patterns=(CustomPattern("badge", r"B-\d{4}"),))
        assert "badge" in _types(
            "badge B-1234", **{"custom_patterns": cfg.custom_patterns}
        )

    def test_from_mapping_builds_custom_entries(self) -> None:
        cfg = DetectorConfig.from_mapping(
            {
                "locales": ["DE"],
                "custom_patterns": [{"name": "emp", "regex": r"EMP-\d+"}],
                "keywords": [{"name": "codes", "terms": ["Phoenix"]}],
            }
        )
        assert cfg.locales == ("de",)
        assert cfg.custom_names() == ["emp", "codes"]
        assert cfg.types is None


class TestKeywords:
    def test_keywords_match_whole_words_only(self) -> None:
        cfg = DetectorConfig(
            types=("codenames",),
            keywords=(KeywordList("codenames", ("Phoenix", "secret sauce")),),
        )
        assert _types("Project Phoenix ships", "codenames", keywords=cfg.keywords) == [
            "codenames"
        ]
        assert (
            _types("phoenixes rise; secretsauce", "codenames", keywords=cfg.keywords)
            == []
        )
        assert _types(
            "the secret sauce recipe", "codenames", keywords=cfg.keywords
        ) == ["codenames"]

    def test_case_sensitivity(self) -> None:
        insensitive = compile_keyword_pattern(["Phoenix"])
        sensitive = compile_keyword_pattern(["Phoenix"], case_sensitive=True)
        assert insensitive.search("phoenix")
        assert not sensitive.search("phoenix")

    def test_punctuated_terms_still_need_token_boundaries(self) -> None:
        pattern = compile_keyword_pattern(["c++"])
        assert pattern.search("we use c++ here")
        assert not pattern.search("abc++ here")

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one term"):
            compile_keyword_pattern(["  "])


class TestRegistry:
    def teardown_method(self) -> None:
        reset_detectors()

    def test_registered_detector_is_selectable_by_name(self) -> None:
        def fake(text: str, _config: DetectorConfig):
            index = text.find("ZZZ")
            if index >= 0:
                yield Match("fake", index, index + 3, 0.4)

        register_detector("fake", fake, label="Fake", description="test only")
        matches = detect("a ZZZ b", DetectorConfig(types=("fake",)))
        assert [m.type for m in matches] == ["fake"]
        assert "fake" in [info.id for info in list_types()]
        assert "fake" in _types("ZZZ")  # part of the default selection

    def test_builtin_names_cannot_be_shadowed(self) -> None:
        with pytest.raises(ValueError, match="built-in"):
            register_detector("email", lambda text, cfg: [])

    def test_reset_drops_registered_detectors(self) -> None:
        register_detector("tmp", lambda text, cfg: [])
        reset_detectors()
        assert "tmp" not in [info.id for info in list_types()]


def test_list_types_includes_configured_custom_entries() -> None:
    cfg = DetectorConfig(
        custom_patterns=(CustomPattern("emp", r"E\d"),),
        keywords=(KeywordList("kw", ("x",)),),
    )
    ids = [info.id for info in list_types(cfg)]
    assert ids[: len(BUILTIN_TYPE_IDS)] == list(BUILTIN_TYPE_IDS)
    assert ids[-2:] == ["emp", "kw"]
    assert all(info.example for info in list_types() if info.builtin)


def _benchmark_text(size: int = 100_000) -> str:
    rng = random.Random(1121)
    words = [
        "lorem",
        "ipsum",
        "dolor",
        "sit",
        "amet",
        "2024-05-01",
        "12:30",
        "order",
        "alice@example.com",
        "+44 20 7946 0958",
        "DE89 3704 0044 0532 0130 00",
        "203.0.113.7",
        "DOB: 1980-05-01",
        "SSN 123-45-6789",
        "patient: Jane Doe",
        "MRN: 00123456",
        "x" * 30,
        "0123456789",
        "999.1.1.1",
        "4111 1111 1111 1111",
    ]
    text = " ".join(rng.choice(words) for _ in range(size // 6))
    return text[:size]


def _coverage_active() -> bool:
    """True under coverage tracing, which slows Python-level loops several-fold."""
    try:
        import coverage

        return coverage.Coverage.current() is not None
    except Exception:  # noqa: BLE001 - coverage is optional
        return False


def _slow_runner(text: str) -> bool:
    """Calibrate on one plain regex pass; a slow or busy runner skips the timing."""
    baseline = min(_timed_regex(text) for _ in range(3))
    return baseline > 0.004


@pytest.mark.benchmark
@pytest.mark.skipif(
    os.environ.get("PRELOOP_SKIP_BENCHMARKS") == "1",
    reason="benchmarks disabled on this runner",
)
def test_benchmark_100kb_all_types_under_50ms() -> None:
    """100 KB with every built-in plus a custom pattern and a keyword list."""
    if _coverage_active():
        pytest.skip("coverage tracing active; timing is not representative")
    cfg = DetectorConfig(
        custom_patterns=(CustomPattern("emp", r"EMP-\d{6}"),),
        keywords=(KeywordList("kw", ("Project Phoenix", "lorem")),),
    )
    text = _benchmark_text()
    assert len(text) == 100_000
    if _slow_runner(text):
        pytest.skip("slow runner; one regex pass over 100 KB exceeds 4 ms")
    detect(text, cfg)  # warm regex caches
    best = min(_timed(text, cfg) for _ in range(3))
    assert best < 0.050, f"detect took {best * 1000:.1f} ms"


def _timed_regex(text: str) -> float:
    import re

    pattern = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
    start = time.perf_counter()
    pattern.findall(text)
    return time.perf_counter() - start


def _timed(text: str, cfg: DetectorConfig) -> float:
    start = time.perf_counter()
    detect(text, cfg)
    return time.perf_counter() - start
