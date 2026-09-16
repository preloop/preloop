"""Validation rules for the saved session search contract.

These run without a database: they are about what the contract refuses, which
is the part that decides whether an unrunnable search can be stored at all.
"""

import pytest
from pydantic import ValidationError

from preloop.schemas.session_saved_search import (
    SessionSavedSearchCreate,
    SessionSavedSearchRunRequest,
    SessionSavedSearchUpdate,
)


def test_the_name_is_stripped():
    """A name is a label in a list, so its surrounding whitespace is noise."""
    saved = SessionSavedSearchCreate(name="  billing  ", query="billing")

    assert saved.name == "billing"


def test_a_whitespace_only_name_is_refused():
    """An unnamed saved search cannot be picked out of a list."""
    with pytest.raises(ValidationError):
        SessionSavedSearchCreate(name="   ", query="billing")


def test_the_query_is_normalised_the_way_the_search_endpoint_normalises_it():
    """The saved text has to be the text the search would have parsed."""
    saved = SessionSavedSearchCreate(name="billing", query="  billing   migration  ")

    assert saved.query == "billing migration"


def test_an_unknown_key_is_refused_rather_than_ignored():
    """A silently dropped key saves a different search than the caller meant."""
    with pytest.raises(ValidationError):
        SessionSavedSearchCreate(name="billing", query="billing", notafield=True)


def test_an_unknown_filter_key_is_refused():
    """Filters are validated at save time so a stored search can always run."""
    with pytest.raises(ValidationError):
        SessionSavedSearchCreate(
            name="billing", query="billing", filters={"notafilter": "x"}
        )


def test_an_unknown_mode_is_refused():
    """Only the three modes the search endpoint ranks with can be saved."""
    with pytest.raises(ValidationError):
        SessionSavedSearchCreate(name="billing", query="billing", mode="magic")


def test_visibility_defaults_to_private():
    """Sharing is a deliberate step, never a default."""
    saved = SessionSavedSearchCreate(name="billing", query="billing")

    assert saved.visibility == "private"


def test_an_empty_update_is_refused():
    """ "Nothing changed" is not an edit."""
    with pytest.raises(ValidationError):
        SessionSavedSearchUpdate()


def test_an_update_may_carry_one_field():
    """A rename is a complete edit on its own."""
    update = SessionSavedSearchUpdate(name="renamed")

    assert update.model_fields_set == {"name"}
    assert update.query is None


def test_a_run_body_takes_paging_and_nothing_else():
    """The saved search owns the question; the run body owns the page."""
    with pytest.raises(ValidationError):
        SessionSavedSearchRunRequest(query="something else")


def test_a_run_page_size_is_bounded():
    """The same bound the search endpoint applies, applied here too."""
    with pytest.raises(ValidationError):
        SessionSavedSearchRunRequest(limit=10_000)
