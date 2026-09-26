"""Tests for repository review policy parsing."""

import pytest

from preloop.services.review_policy import (
    POLICY_PATH,
    ReviewPolicyError,
    is_safe_version_linter,
    linter_argv,
    matching_rules,
    parse_review_policy,
    version_is_newer,
)

PERL_POLICY = """\
# Repository review policy

Rules in this file are blocking.

```yaml
compatibility:
  - language: perl
    minimum_version: "5.10"
    paths:
      - "daemons/**"
    version_linter: "perlver --blame"
    allowed:
      - "say"
      - "state"
      - "defined-or (//)"
    forbidden:
      - "postfix dereference"
```

Perl under daemons/ must stay on the declared minimum.
"""


def test_policy_path_is_the_documented_file() -> None:
    assert POLICY_PATH == ".preloop/review-policy.md"


def test_quoted_perl_version_is_preserved() -> None:
    policy = parse_review_policy(PERL_POLICY)
    rule = policy.rules[0]
    assert rule.language == "perl"
    assert rule.minimum_version == "5.10"
    assert rule.extensions == (".pl", ".pm", ".t")
    assert rule.allowed == ("say", "state", "defined-or (//)")
    assert "postfix dereference" in rule.forbidden
    assert "blocking" in policy.prose
    assert "compatibility:" not in policy.prose


def test_unquoted_yaml_version_is_rejected() -> None:
    text = """\
```yaml
compatibility:
  - language: perl
    minimum_version: 5.10
```
"""
    with pytest.raises(ReviewPolicyError, match="quoted string"):
        parse_review_policy(text)


def test_perl_files_under_the_declared_tree_match() -> None:
    policy = parse_review_policy(PERL_POLICY)
    pairs = matching_rules(
        policy,
        ["daemons/poll.pl", "daemons/lib/Transform.pm", "lib/Other.pm", "README.md"],
    )
    matched = [path for path, _rule in pairs]
    assert matched == ["daemons/poll.pl", "daemons/lib/Transform.pm"]


def test_extension_match_is_case_insensitive() -> None:
    policy = parse_review_policy(PERL_POLICY)
    pairs = matching_rules(policy, ["daemons/Poll.PL"])
    assert [path for path, _rule in pairs] == ["daemons/Poll.PL"]


def test_default_perl_linter_when_the_command_is_omitted() -> None:
    text = """\
```yaml
compatibility:
  - language: perl
    minimum_version: "5.10"
    paths: ["daemons/**"]
```
"""
    rule = parse_review_policy(text).rules[0]
    assert linter_argv(rule, "daemons/poll.pl") == [
        "perlver",
        "--blame",
        "daemons/poll.pl",
    ]


def test_custom_linter_appends_the_path_once() -> None:
    rule = parse_review_policy(PERL_POLICY).rules[0]
    assert linter_argv(rule, "daemons/my file.pl") == [
        "perlver",
        "--blame",
        "daemons/my file.pl",
    ]


def test_unsafe_linter_is_not_executed() -> None:
    text = """\
```yaml
compatibility:
  - language: perl
    minimum_version: "5.10"
    version_linter: "perlver --blame; rm -rf /"
```
"""
    rule = parse_review_policy(text).rules[0]
    assert is_safe_version_linter("perlver --blame")
    assert not is_safe_version_linter(rule.version_linter or "")
    assert linter_argv(rule, "daemons/poll.pl") is None


def test_other_languages_have_no_default_linter() -> None:
    text = """\
```yaml
compatibility:
  - language: python
    minimum_version: "3.8"
    paths: ["services/**"]
    extensions: [".py"]
```
"""
    rule = parse_review_policy(text).rules[0]
    assert rule.extensions == (".py",)
    assert linter_argv(rule, "services/app.py") is None


def test_prose_only_policy_has_no_rules() -> None:
    policy = parse_review_policy("All daemons stay on the declared runtime.\n")
    assert policy.rules == ()
    assert "declared runtime" in policy.prose


def test_version_comparison_keeps_trailing_zeros() -> None:
    assert version_is_newer("5.24", "5.10")
    assert version_is_newer("5.11", "5.10")
    assert not version_is_newer("5.10", "5.10")
    assert not version_is_newer("5.10.0", "5.10")
    assert not version_is_newer("5.9", "5.10")


def test_unknown_rule_key_is_rejected() -> None:
    text = """\
```yaml
compatibility:
  - language: perl
    minimum_version: "5.10"
    minimun_version: "5.10"
```
"""
    with pytest.raises(ReviewPolicyError, match="unknown keys"):
        parse_review_policy(text)
