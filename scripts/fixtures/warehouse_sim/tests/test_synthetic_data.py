"""No real personal data, and pin how the current PII detector sees it."""

import re

import pytest

from scripts.fixtures.warehouse_sim import data

FIXTURE_DIR = data.HERE
TEXT_FILES = [
    p
    for p in FIXTURE_DIR.rglob("*")
    if p.is_file()
    and p.suffix in {".vtt", ".bpmn", ".md", ".py"}
    and "__pycache__" not in p.parts
]

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
# Digit runs that look like phone numbers (7+ digits, optional separators).
PHONE = re.compile(r"\+\d[\d\s\-]{8,}\d")

ALLOWED_PHONES = {"+1 555 010 0199", "+49 7131 1234567"}
PII_TRANSCRIPT = ("sued", "night")


def _all_text():
    return {p: p.read_text(encoding="utf-8") for p in TEXT_FILES}


def test_emails_use_reserved_domains_only():
    for path, text in _all_text().items():
        for domain in EMAIL.findall(text):
            assert domain == "example.com" or domain.endswith(".example.com"), (
                f"{path}: {domain}"
            )


def test_phone_numbers_are_the_documented_fixture_numbers():
    for path, text in _all_text().items():
        if path.suffix == ".py" and path.parent.name == "tests":
            continue
        for number in PHONE.findall(text):
            assert number.strip() in ALLOWED_PHONES, f"{path}: {number}"


def test_us_number_is_in_555_01xx_fiction_range():
    digits = re.sub(r"\D", "", "+1 555 010 0199")
    assert digits[1:4] == "555" and digits[4:6] == "01"


def test_only_the_pii_transcript_contains_contact_data():
    for site, shift in data.all_transcripts():
        text = data.read_transcript(site, shift)
        has = bool(EMAIL.search(text) or PHONE.search(text))
        assert has == ((site, shift) == PII_TRANSCRIPT), (site, shift)


def test_pii_transcript_contains_each_fixture_item():
    text = data.read_transcript(*PII_TRANSCRIPT)
    for needle in ("Erika Mustermann", "erika.mustermann@example.com", *ALLOWED_PHONES):
        assert needle in text


def _detectors():
    return pytest.importorskip("preloop.services.model_content_detectors")


def test_current_detector_behaviour_is_as_documented():
    """README states these facts; fail loudly if the detector changes."""
    det = _detectors()
    assert det.PII_PHONE_RE.findall("+1 555 010 0199") == ["+1 555 010 0199"]
    # The US-shaped regex hits a substring of the German number, not the
    # whole international form. Detection fires; span-based redaction would
    # leave "+49 " behind.
    assert det.PII_PHONE_RE.findall("+49 7131 1234567") == ["7131 1234567"]
    assert det.detect_pii("erika.mustermann@example.com").types_found == ["email"]
    # Names are not detected by the regex detectors.
    assert det.detect_pii("Erika Mustermann").found is False
    found = det.detect_pii(data.read_transcript(*PII_TRANSCRIPT)).types_found
    assert set(found) == {"email", "phone"}


@pytest.mark.parametrize(
    "site,shift", [p for p in data.all_transcripts() if p != PII_TRANSCRIPT]
)
def test_other_transcripts_do_not_trip_the_detector(site, shift):
    det = _detectors()
    assert det.detect_pii(data.read_transcript(site, shift)).found is False
