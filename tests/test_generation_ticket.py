# ============================================================
# GENERATION TICKETS
# ============================================================
# /api/ask signs what it generated; /api/run verifies that signature
# before it will call a statement "generated". These tests pin the
# properties the integrity of that claim depends on:
#
#   * a ticket is unforgeable without the server secret,
#   * a tampered ticket is rejected outright,
#   * verification is a closed answer of three words, never free text,
#   * issuing is deterministic, so a byte-identical generation stays
#     byte-identical end to end.
# ============================================================

import base64
import json

import pytest

from app import config, generation_ticket


@pytest.fixture(autouse=True)
def ticket_secret(monkeypatch):
    """A throwaway signing secret, so no test depends on the operator's."""

    monkeypatch.setattr(
        config, "ACCESS_SIGNING_SECRET", "test-only-ticket-secret"
    )
    return "test-only-ticket-secret"


SQL = "SELECT id, name FROM products"


# ============================================================
# 1. ISSUE AND VERIFY
# ============================================================

def test_a_ticket_round_trips_its_claims():
    token = generation_ticket.issue("nvidia", "model-x", SQL)

    claims = generation_ticket.verify(token)

    assert claims is not None
    assert claims["provider"] == "nvidia"
    assert claims["model"] == "model-x"
    assert claims["sql_digest"] == generation_ticket._sql_digest(SQL)


def test_issuing_is_deterministic():
    """No nonce and no clock: the same generation yields the same bytes."""

    first = generation_ticket.issue("nvidia", "model-x", SQL)
    second = generation_ticket.issue("nvidia", "model-x", SQL)

    assert first == second


def test_a_ticket_has_three_parts_and_no_padding():
    token = generation_ticket.issue("nvidia", "model-x", SQL)

    parts = token.split(".")

    assert len(parts) == 3
    assert parts[0] == generation_ticket._TICKET_VERSION
    assert "=" not in parts[1]
    assert "=" not in parts[2]


def test_the_body_is_urlsafe_base64_and_the_signature_is_hex():
    token = generation_ticket.issue("nvidia", "model-x", SQL)

    body, signature = token.split(".")[1], token.split(".")[2]

    base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))

    assert all(char in "0123456789abcdef" for char in signature)


# ============================================================
# 2. WHAT IS REJECTED
# ============================================================

def test_no_ticket_is_not_a_ticket():
    assert generation_ticket.verify(None) is None
    assert generation_ticket.verify("") is None
    assert generation_ticket.verify(12345) is None


@pytest.mark.parametrize(
    "token",
    [
        "not-a-ticket",
        "gt1.only-two-parts",
        "gt1..sig",
        "gt1.body.",
        "gt9.QQ.AA",
        "gt1.!!!not-base64!!!.AA",
    ],
)
def test_a_malformed_ticket_is_rejected(token):
    assert generation_ticket.verify(token) is None


def test_a_tampered_body_is_rejected():
    token = generation_ticket.issue("nvidia", "model-x", SQL)

    version, body, signature = token.split(".")

    forged = base64.urlsafe_b64encode(
        json.dumps(
            {"p": "evil", "m": "evil", "h": "0" * 64},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).decode("ascii").rstrip("=")

    assert generation_ticket.verify(
        f"{version}.{forged}.{signature}"
    ) is None


def test_a_tampered_signature_is_rejected():
    token = generation_ticket.issue("nvidia", "model-x", SQL)

    version, body, signature = token.split(".")

    flipped = "f" + signature[1:] if signature[0] != "f" else "0" + signature[1:]

    assert generation_ticket.verify(
        f"{version}.{body}.{flipped}"
    ) is None


def test_a_ticket_from_another_secret_is_rejected(monkeypatch):
    token = generation_ticket.issue("nvidia", "model-x", SQL)

    monkeypatch.setattr(
        config, "ACCESS_SIGNING_SECRET", "a-different-secret"
    )

    assert generation_ticket.verify(token) is None


def test_the_signing_secret_never_appears_in_the_ticket():
    token = generation_ticket.issue("nvidia", "model-x", SQL)

    assert "test-only-ticket-secret" not in token


# ============================================================
# 3. CLASSIFY: THE THREE TRUE ORIGINS
# ============================================================

def test_a_matching_ticket_is_generated():
    token = generation_ticket.issue("nvidia", "model-x", SQL)

    origin, provider, model = generation_ticket.classify(token, SQL)

    assert origin == generation_ticket.GENERATED
    assert provider == "nvidia"
    assert model == "model-x"


def test_a_different_statement_is_edited():
    token = generation_ticket.issue("nvidia", "model-x", SQL)

    origin, provider, model = generation_ticket.classify(
        token, "SELECT id FROM products"
    )

    assert origin == generation_ticket.EDITED
    assert provider == "nvidia"
    assert model == "model-x"


def test_no_ticket_is_manual():
    origin, provider, model = generation_ticket.classify(None, SQL)

    assert origin == generation_ticket.MANUAL
    assert provider is None
    assert model is None


def test_a_forged_ticket_is_manual_not_generated():
    origin, provider, model = generation_ticket.classify(
        "gt1.forged.forged", SQL
    )

    assert origin == generation_ticket.MANUAL
    assert provider is None
    assert model is None


def test_leading_and_trailing_whitespace_does_not_break_matching():
    """The digest covers the stripped statement, as /api/run trims it."""

    token = generation_ticket.issue("nvidia", "model-x", SQL)

    origin, _, _ = generation_ticket.classify(token, f"  {SQL}\n")

    assert origin == generation_ticket.GENERATED


def test_issue_and_verify_survive_a_missing_secret(monkeypatch):
    """No secret means no ticket, never an exception."""

    monkeypatch.setattr(config, "ACCESS_SIGNING_SECRET", "")
    monkeypatch.setattr(config, "APP_API_KEY", "")

    assert generation_ticket.issue("nvidia", "model-x", SQL) is None

    assert generation_ticket.verify("gt1.a.b") is None


def test_classify_returns_only_the_three_known_words():
    tokens = [
        generation_ticket.issue("nvidia", "model-x", SQL),
        generation_ticket.issue("nvidia", "model-x", SQL),
        None,
    ]

    statements = [SQL, "SELECT 1", SQL]

    for token, statement in zip(tokens, statements):
        origin, _, _ = generation_ticket.classify(token, statement)

        assert origin in {
            generation_ticket.GENERATED,
            generation_ticket.EDITED,
            generation_ticket.MANUAL,
        }
