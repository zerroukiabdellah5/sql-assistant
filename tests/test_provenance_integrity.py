# ============================================================
# PROVENANCE INTEGRITY ACROSS THE SPLIT
# ============================================================
# /api/ask generates SQL; /api/run executes it later. The two arrive in
# different requests, so provenance must not take the client's word for
# who wrote the statement. These tests pin the fix: provider and model
# in the provenance block come only from a server-signed generation
# ticket, and a hand-written statement is reported as manual even when
# the client claims otherwise.
# ============================================================

from app import generation_ticket, main

from tests.test_provenance import STORE_SQL, stub_ask

HANDWRITTEN = "SELECT id, name FROM products ORDER BY id"


def _run(client, **fields):
    return client.post("/api/run", json=fields)


# ============================================================
# 1. THE TICKET TRAVELS WITH THE GENERATION
# ============================================================

def test_ask_returns_a_generation_ticket(authed_client, monkeypatch):
    monkeypatch.setattr(main, "ask_active", stub_ask())

    body = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()

    assert body["generation_ticket"]

    claims = generation_ticket.verify(body["generation_ticket"])

    assert claims["provider"] == "nvidia"
    assert claims["model"] == "stub-model-1"


# ============================================================
# 2. A VALID TICKET NAMES THE MODEL, TRUTHFULLY
# ============================================================

def test_run_with_the_ticket_reports_generated(
    authed_client, monkeypatch
):
    monkeypatch.setattr(main, "ask_active", stub_ask())

    generated = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()

    block = _run(
        authed_client,
        sql=generated["sql"],
        generation_ticket=generated["generation_ticket"],
    ).json()["provenance"]

    assert block["sql_origin"] == "generated"
    assert block["generated_by_ai"] is True
    assert block["provider"] == "nvidia"
    assert block["model"] == "stub-model-1"


def test_edited_sql_is_reported_as_edited(authed_client, monkeypatch):
    monkeypatch.setattr(main, "ask_active", stub_ask())

    generated = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()

    block = _run(
        authed_client,
        sql=HANDWRITTEN,
        generation_ticket=generated["generation_ticket"],
    ).json()["provenance"]

    assert block["sql_origin"] == "edited"
    assert block["generated_by_ai"] is True
    assert block["provider"] == "nvidia"
    assert block["model"] == "stub-model-1"


# ============================================================
# 3. NO TICKET MEANS MANUAL, AND MANUAL NAMES NO MODEL
# ============================================================

def test_handwritten_sql_is_manual_and_names_no_model(authed_client):
    block = _run(authed_client, sql=STORE_SQL).json()["provenance"]

    assert block["sql_origin"] == "manual"
    assert block["generated_by_ai"] is False
    assert block["provider"] is None
    assert block["model"] is None


def test_a_client_cannot_spoof_provider_or_model(authed_client):
    """Provider and model are not accepted fields; a claim is ignored."""

    block = _run(
        authed_client,
        sql=STORE_SQL,
        provider="evil",
        model="evil-model",
    ).json()["provenance"]

    assert block["sql_origin"] == "manual"
    assert block["generated_by_ai"] is False
    assert block["provider"] is None
    assert block["model"] is None


def test_a_client_cannot_override_the_ticket(authed_client, monkeypatch):
    """A real ticket wins over a conflicting provider/model in the body."""

    monkeypatch.setattr(main, "ask_active", stub_ask())

    generated = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()

    block = _run(
        authed_client,
        sql=generated["sql"],
        generation_ticket=generated["generation_ticket"],
        provider="evil",
        model="evil-model",
    ).json()["provenance"]

    assert block["provider"] == "nvidia"
    assert block["model"] == "stub-model-1"


def test_a_forged_ticket_is_treated_as_manual(authed_client):
    block = _run(
        authed_client,
        sql=STORE_SQL,
        generation_ticket="gt1.forged.forged",
    ).json()["provenance"]

    assert block["sql_origin"] == "manual"
    assert block["generated_by_ai"] is False


# ============================================================
# 4. THE ORIGIN IS A CLOSED VOCABULARY
# ============================================================

def test_sql_origin_is_one_of_three_words(authed_client, monkeypatch):
    monkeypatch.setattr(main, "ask_active", stub_ask())

    generated = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()

    for sql, token in (
        (generated["sql"], generated["generation_ticket"]),
        (HANDWRITTEN, generated["generation_ticket"]),
        (STORE_SQL, None),
    ):
        origin = _run(
            authed_client, sql=sql, generation_ticket=token
        ).json()["provenance"]["sql_origin"]

        assert origin in {"generated", "edited", "manual"}


def test_the_provenance_block_gains_sql_origin(authed_client, monkeypatch):
    monkeypatch.setattr(main, "ask_active", stub_ask())

    generated = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()

    block = _run(
        authed_client,
        sql=generated["sql"],
        generation_ticket=generated["generation_ticket"],
    ).json()["provenance"]

    assert "sql_origin" in block
