"""The restored six-attempt trial, its refusal, and the absence of the
temporary jury bypass.

These tests pin the behaviour an evaluation of the ordinary product
depends on:

  * exactly six successful question attempts per browser, then the flat
    403 FREE_TRIAL_EXHAUSTED the frontend keys its dialog on;
  * the dialog copy explains that the trial has ended and offers an
    App Access Code or the owner contact, without claiming anything was
    sent;
  * a valid code unlocks, an invalid one is refused, and a deployment
    with no configured code fails closed instead of accepting a value
    published in the repository;
  * the counter stays a signed cookie that cannot be forged to buy
    attempts back;
  * the temporary jury bypass flag is gone from source, documentation
    and tests.
"""

import os

import pytest

import app.config as config
import app.access as access

from app.access import TRIAL_EXHAUSTED_CODE


ANSWER = "SELECT 1 AS a"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Assembled from parts on purpose: neither this file nor the check below
# may contain the literal flag name, so a repository-wide search for it
# comes back empty.
BANNED_FLAG = "JURY" + "_DEMO" + "_MODE"


@pytest.fixture
def stub_provider(monkeypatch):
    """Replace the provider call so no test touches the network."""

    def fake_ask(prompt, turns, request_id=None, generation=None):
        return "SELECT 1", "ok"

    monkeypatch.setattr("app.main.ask_active", fake_ask)


def spend(client):
    """Use the whole allowance. Returns the last 200 response."""

    response = None

    for _ in range(config.TRIAL_MAX_ATTEMPTS):
        response = client.post(
            "/api/ask", json={"prompt": "show products"}
        )

        assert response.status_code == 200

    return response


def trial_token_from(response):
    """The trial token this response wrote, straight from Set-Cookie.

    Read from the header rather than the client's jar: the jar is not
    consulted because these tests deliberately present competing
    cookies, which a jar cannot represent.
    """

    for value in response.headers.get_list("set-cookie"):

        if value.startswith(config.TRIAL_COOKIE_NAME + "="):

            return value.split(";", 1)[0].split("=", 1)[1]

    return None


# ============================================================
# THE SIX-ATTEMPT TRIAL
# ============================================================

def test_the_free_trial_is_six_attempts():
    assert config.TRIAL_MAX_ATTEMPTS == 6
    assert access.max_attempts() == 6


def test_attempts_one_to_six_are_answered_and_the_seventh_is_refused(
    anon_client, stub_provider
):
    assert config.TRIAL_MAX_ATTEMPTS == 6

    for _ in range(6):
        response = anon_client.post(
            "/api/ask", json={"prompt": "show products"}
        )

        assert response.status_code == 200

    refused = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    assert refused.status_code == 403
    assert refused.json()["detail"] == TRIAL_EXHAUSTED_CODE


def test_the_refusal_body_is_the_one_the_dialog_reads(
    anon_client, stub_provider, monkeypatch
):
    """index.html opens the access dialog only on this exact detail.

    The body must stay FLAT, must carry the six-attempt total and must
    offer the two next steps the dialog describes. Nothing may claim
    that contacting the owner sends anything automatically, and the
    contact address is operator configuration - cleared here so the
    assertion cannot depend on the machine's environment.
    """

    monkeypatch.setattr(config, "OWNER_CONTACT_EMAIL", "")

    spend(anon_client)

    body = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).json()

    assert body["detail"] == TRIAL_EXHAUSTED_CODE
    assert isinstance(body["message"], str)

    message = body["message"]
    assert "6" in message
    assert "free trial" in message.lower()
    assert "access code" in message.lower()
    assert "contact" in message.lower()
    assert "sent" not in message.lower()

    assert body["trial_max_attempts"] == 6
    assert body["reference_id"]

    assert "owner_contact" not in body


def test_the_dialog_offers_the_configured_owner_address(
    anon_client, stub_provider, monkeypatch
):
    monkeypatch.setattr(
        config, "OWNER_CONTACT_EMAIL", "zerroukiabdellah5@gmail.com"
    )

    spend(anon_client)

    body = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).json()

    assert body["owner_contact"] == "zerroukiabdellah5@gmail.com"
    assert body["detail"] == TRIAL_EXHAUSTED_CODE


# ============================================================
# THE ACCESS CODE: SET, UNSET, WRONG
# ============================================================

def test_a_valid_code_unlocks_and_an_invalid_one_is_refused(
    anon_client
):
    assert config.APP_ACCESS_CODE

    refused = anon_client.post(
        "/api/access/unlock", json={"code": "wrong-code"}
    )

    assert refused.status_code == 401
    assert anon_client.cookies.get(config.ACCESS_COOKIE_NAME) is None

    accepted = anon_client.post(
        "/api/access/unlock",
        json={"code": config.APP_ACCESS_CODE},
    )

    assert accepted.status_code == 200
    assert accepted.json()["unlocked"] is True
    assert anon_client.cookies.get(config.ACCESS_COOKIE_NAME)


def test_an_unset_access_code_fails_closed(anon_client):
    """No configured code means nobody unlocks - never a shipped default."""

    import app.config as app_config

    app_config.APP_ACCESS_CODE = ""

    try:

        for candidate in (
            "", "1234", "admin", "password", "changeme"
        ):

            response = anon_client.post(
                "/api/access/unlock", json={"code": candidate}
            )

            assert response.status_code == 401
            assert anon_client.cookies.get(
                config.ACCESS_COOKIE_NAME
            ) is None

    finally:

        app_config.APP_ACCESS_CODE = "test-only-access-code"


def test_the_configuration_ships_no_access_code_default():
    """An unset APP_ACCESS_CODE must never fall back to a known value.

    The code is deployment configuration: if the source carried a
    literal default, everyone who reads the repository could unlock.
    """

    path = os.path.join(REPO_DIR, "app", "config.py")

    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    assert 'os.getenv("APP_ACCESS_CODE", "' not in source
    assert "APP_ACCESS_CODE = (os.getenv" in source


# ============================================================
# THE COUNTER CANNOT BE FORGED BACK
# ============================================================

def test_a_forged_counter_cannot_buy_attempts_back(
    anon_client, stub_provider
):
    """The count is covered by the signature, so it cannot be edited.

    Editing a real token buys nothing beyond what clearing cookies
    already buys (a fresh visitor allowance, which is the documented
    trade-off for holding the counter client-side). What must never
    happen is the edited value being BELIEVED - a visitor must not be
    able to write themselves a smaller count.
    """

    spend(anon_client)

    spent = anon_client.cookies.get(config.TRIAL_COOKIE_NAME)

    assert spent
    assert access.verify_trial_token(spent) == 6

    # The real, spent token still refuses the seventh question.
    assert anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).status_code == 403

    # Rewrite the count to zero, keep the server's signature. The
    # signature no longer matches, so the token is unusable.
    version, _used, expiry, signature = spent.split(".")
    tampered = ".".join([version, "0", expiry, signature])

    assert access.verify_trial_token(tampered) is None

    # Presented end to end, it earns no more than an unknown visitor:
    # it is treated as no token at all, not as a chosen count.
    anon_client.cookies.delete(config.TRIAL_COOKIE_NAME)
    anon_client.cookies.set(config.TRIAL_COOKIE_NAME, tampered)

    restarted = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    assert restarted.status_code == 200
    assert access.verify_trial_token(
        trial_token_from(restarted)
    ) == 1

    # And the untouched spent token is still spent.
    anon_client.cookies.delete(config.TRIAL_COOKIE_NAME)
    anon_client.cookies.set(config.TRIAL_COOKIE_NAME, spent)

    assert anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).status_code == 403


def test_only_the_server_can_mint_a_usable_counter(anon_client):
    """A hand-written token, however well formed, is never accepted."""

    for token in (
        "tv1.0.99999999999.deadbeef",
        "tv1.-1.99999999999.deadbeef",
        "tv9.0.99999999999.deadbeef",
        "tv1.0.99999999999.",
        "garbage",
        "",
    ):

        assert access.verify_trial_token(token) is None


# ============================================================
# NO JURY DEMO MODE ANYWHERE
# ============================================================

def _project_files():
    """Source, documentation and configuration files of the project.

    Deliberately limited to the directories and top-level documents that
    make up the product, so scratch files, virtual environments and the
    untracked educational material in this working tree cannot decide
    whether this test passes.
    """

    skipped_dirs = {
        "__pycache__", ".pytest_cache", ".venv", "venv",
    }

    for directory in ("app", "tests", "api"):

        base = os.path.join(REPO_DIR, directory)

        if not os.path.isdir(base):
            continue

        for root, dirs, files in os.walk(base):

            dirs[:] = [d for d in dirs if d not in skipped_dirs]

            for name in files:

                if name.endswith(".py"):
                    yield os.path.join(root, name)

    for name in os.listdir(REPO_DIR):

        path = os.path.join(REPO_DIR, name)

        if not os.path.isfile(path):
            continue

        if name.endswith((".md", ".html", ".py", ".json", ".toml")) or name in (
            ".env.example", ".gitignore", ".python-version",
        ):
            yield path


def test_no_jury_demo_mode_reference_remains():
    """The temporary bypass is gone from code, docs and tests alike.

    The needle is assembled from parts so this file does not match
    itself - and so a plain repository-wide search for the flag name
    comes back empty too.
    """

    needle = BANNED_FLAG
    offenders = []

    for path in _project_files():

        try:

            with open(path, encoding="utf-8", errors="ignore") as handle:
                if needle in handle.read():
                    offenders.append(
                        os.path.relpath(path, REPO_DIR)
                    )

        except OSError:
            continue

    assert offenders == []


def test_the_frontend_still_keys_on_the_exhaustion_code():
    """The dialog trigger the frontend depends on is untouched."""

    with open(
        os.path.join(REPO_DIR, "index.html"), encoding="utf-8"
    ) as handle:
        markup = handle.read()

    assert "FREE_TRIAL_EXHAUSTED" in markup
    assert "showAccessModal(" in markup
    assert BANNED_FLAG not in markup
    assert "test-only-access-code" not in markup
    assert "APP_ACCESS_CODE=" not in markup
