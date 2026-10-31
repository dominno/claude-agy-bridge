import pytest

from agy_bridge.denylist import PATH_RULES, is_denied, matching_rules


@pytest.mark.parametrize("rule", PATH_RULES, ids=lambda r: r.pattern)
def test_r1_every_rule_denies_its_positive_example(rule):
    assert is_denied(rule.positive) is not None


@pytest.mark.parametrize("rule", PATH_RULES, ids=lambda r: r.pattern)
def test_r1_every_rule_leaves_its_negative_example_alone(rule):
    assert is_denied(rule.negative) is None


@pytest.mark.parametrize("rule", PATH_RULES, ids=lambda r: r.pattern)
def test_r1_the_rule_itself_fires_for_its_positive_example(rule):
    # Not just "something denied it": dropping this one rule must change the result.
    assert rule.pattern in matching_rules(rule.positive)


def test_r1_dot_env_rule_is_what_catches_a_plain_dot_env():
    assert ".env" in matching_rules(".env") and ".env" in matching_rules("a/.env")


def test_r1_new_credential_names_are_denied():
    for name in ("home/id_ed25519", "home/id_ecdsa", ".git-credentials", ".pgpass", ".kube/config",
                 ".docker/config.json", ".config/gcloud/x.json", "k.jks", "k.keystore", ".htpasswd",
                 "aws_credentials", "gcp-credentials.json"):
        assert is_denied(name) is not None, name


def test_r1_nested_denylisted_name_is_caught_at_any_depth():
    assert is_denied("a/b/c/d/.env") is not None
    assert is_denied("a/b/.git/hooks/pre-commit") is not None


def test_r1_case_and_unicode_tricks_do_not_hide_a_name():
    assert is_denied("CONFIG/.ENV") is not None
    assert is_denied("a/Secrets.YAML") is not None
    assert is_denied("a/.ｅnv") is not None  # fullwidth e folds to ASCII under NFKC
    assert is_denied("a/.e​nv") is not None  # zero-width space is dropped
    assert is_denied("a/.env ") is not None  # trailing space


def test_r1_ordinary_source_is_allowed():
    for ok in ("src/main.py", "README.md", "docs/guide.txt", "a/b/c.ts"):
        assert is_denied(ok) is None


def test_r1_repo_local_rules_can_add():
    assert is_denied("private/notes.txt") is None
    assert is_denied("private/notes.txt", extra=["private"]) == "private"
    assert is_denied("a/private/x/notes.txt", extra=["a/private"]) == "a/private"
    assert is_denied("a/other/x.txt", extra=["a/private"]) is None


def test_r1_repo_local_rules_cannot_remove_a_builtin():
    # extra is additive by construction: there is no parameter that subtracts.
    assert is_denied("x/.env", extra=["unrelated"]) is not None


