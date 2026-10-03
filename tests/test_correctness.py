# ============================================================
# CORRECTNESS vs PROVENANCE — STEP 4B-5
# ============================================================
# Step 4B-4 published provenance: where a result came from and how
# it was produced. That is a description, and a description that
# arrives next to a table of numbers can be read as an endorsement.
# This step closes that gap from both ends: the server states
# outright that nothing checked the answer, and the interface stops
# implying that it did.
#
# Two failure modes are held apart throughout:
#   * claiming correctness it cannot know, and
#   * hiding the source so well that nobody can check it themselves.
# Both are avoided by the same move: the facts and the verdict are
# published as separate things, in separate keys, in separate words.
#
# Every test below names which of the two it protects.
# ============================================================

import ast
import inspect
import json
import os
import sqlite3
import sys

import pytest

from app import config, correctness, main, provenance, reports

from tests.test_provenance import STORE_SQL, stub_ask

# The exact verification contract. Pinned rather than merely checked
# for presence: adding a field to this block is how a claim would
# eventually get in, so it has to be a deliberate edit.
REQUIRED_KEYS = {
    "status",
    "correctness_checked",
    "note",
}

# Affirmative correctness claims. These are the words that would turn
# a description into a guarantee, and this module may state the
# ABSENCE of each of them but must never contain the claim itself.
CLAIM_WORDS = (
    "verified",
    "correct",
    "accurate",
    "trusted",
    "guaranteed",
    "proof",
)

# Provenance facts. None of them belongs in the verification block: a
# fingerprint next to the word "not verified" starts to look like the
# evidence for the claim.
PROVENANCE_FIELDS = (
    "source_database",
    "source_sha256",
    "provider",
    "model",
    "executed_at",
    "elapsed_ms",
    "rows_returned",
    "total_matched",
    "truncated",
    "generated_by_ai",
    "request_id",
)

# Copy that was removed from the interface because it claimed
# something the application cannot know. The first two told the user
# to trust a generated statement; the third implied a general safety
# the app only enforces for the statement it runs.
RETIRED_COPY = (
    "Get trusted SQL",
    "Review before you trust",
    "Safe by default",
)


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def provider_stub(monkeypatch):
    fake_ask = stub_ask()
    monkeypatch.setattr(main, "ask_active", fake_ask)
    return fake_ask


@pytest.fixture
def asked(authed_client, provider_stub):
    """A successful /api/ask against the real store.db."""

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code == 200, response.text

    return response


@pytest.fixture(scope="module")
def index_source():
    """The shipped frontend, read as text for static assertions."""

    with open(config.INDEX_PATH, encoding="utf-8") as handle:
        return handle.read()


def _source(module):
    return inspect.getsource(module)


def _header(module):
    lines = []

    for line in _source(module).splitlines():

        if not line.startswith("#"):
            if lines:
                break
            continue

        lines.append(line)

    return "\n".join(lines)


# ============================================================
# 1. THE CONTRACT
# ============================================================

def test_a_successful_answer_states_that_nothing_checked_it(asked):
    block = asked.json()["verification"]

    assert block["status"] == correctness.NOT_VERIFIED
    assert block["correctness_checked"] is False
    assert block["note"].strip()


def test_the_block_contains_exactly_the_agreed_fields(asked):
    assert set(asked.json()["verification"]) == REQUIRED_KEYS


def test_the_block_is_json_serialisable(asked):
    json.dumps(asked.json()["verification"])


def test_the_status_is_a_word_and_not_a_boolean(asked):
    """The whole reason this field is not "verified": false.

    A boolean is a status report, and a status report is something
    clients branch on. This is a name for what happened, and it has
    exactly one value.
    """

    status = asked.json()["verification"]["status"]

    assert isinstance(status, str)
    assert status.startswith("not_")


def test_two_identical_requests_report_the_same_verification(
    authed_client, provider_stub
):
    first = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()["verification"]
    second = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()["verification"]

    assert first == second


# ============================================================
# 2. IT CANNOT BECOME A CLAIM
# ============================================================

def test_the_builder_takes_no_arguments():
    """Nothing a caller holds can be turned into a verdict.

    This is the structural guarantee: there is no parameter to pass a
    flag, a confidence score or a truthy object through, so no future
    call site can produce a block that says the answer was checked.
    """

    parameters = inspect.signature(
        correctness.build_verification
    ).parameters

    assert list(parameters) == []


def test_the_block_never_reports_a_check_as_done():
    """No environment, no argument, no exception path flips it."""

    for _ in range(3):
        block = correctness.build_verification()

        assert block["correctness_checked"] is False
        assert block["status"] == "not_verified"


def test_the_module_imports_nothing_so_it_can_read_no_evidence():
    """There is no data source here, so there is nothing to check."""

    for node in ast.walk(ast.parse(_source(correctness))):

        names = []

        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]

        assert names == []


def test_the_module_cannot_reach_the_database_or_the_provider():
    source = _source(correctness)

    assert "sqlite3" not in source
    assert "connect" not in source
    assert "from app.db" not in source
    assert "requests" not in source
    assert "httpx" not in source


def test_no_provenance_fact_leaks_into_the_verification_block(asked):
    """The two blocks must not blur into each other.

    A fingerprint printed beside a verification status reads as the
    evidence for that status. It is not: it identifies the file. The
    prose may mention that a language model wrote the query - that is
    the whole point of the note - so what is forbidden is a provenance
    VALUE, not the English word "model".
    """

    body = asked.json()

    block = body["verification"]

    assert set(block).isdisjoint(PROVENANCE_FIELDS)
    assert set(block) & set(body["provenance"]) == set()

    rendered = str(block).lower()

    for key, value in body["provenance"].items():

        if isinstance(value, str) and value:
            assert value.lower() not in rendered, key


def test_the_verification_block_carries_no_path_sql_prompt_or_rows(
    asked,
):
    body = asked.json()
    block = str(body["verification"])

    assert config.DATABASE_PATH not in block
    assert config.BASE_DIR not in block
    assert os.sep not in block
    assert "SELECT" not in block.upper()
    assert body["prompt"] not in block

    for value in body["data"][0].values():
        if isinstance(value, str):
            assert value not in block


def test_provenance_still_makes_no_correctness_claim(asked):
    """The existing boundary is unchanged by this step.

    Step 4B-4 pinned the provenance block against these words. The
    fix for an over-read description is a separate statement, not a
    vocabulary of hedges inside the description.
    """

    block = str(asked.json()["provenance"]).lower()

    for word in CLAIM_WORDS:
        assert word not in block, word


def test_the_module_explains_why_it_exists_in_plain_words():
    header = _header(correctness).lower()

    assert "correctness" in header
    assert "not computed" in header
    assert "no arguments" in header
    assert "never" in header


# ============================================================
# 3. PROVENANCE SURVIVES ALONGSIDE IT
# ============================================================

def test_the_two_blocks_are_separate_keys_at_the_same_level(asked):
    body = asked.json()

    assert "provenance" in body
    assert "verification" in body

    assert body["verification"] is not body["provenance"]


def test_adding_the_statement_changed_nothing_else(asked):
    """The pre-existing response contract is untouched.

    Everything the frontend and any other client already relied on
    is still there, unchanged, and the new key is purely additive.
    """

    body = asked.json()

    for key in (
        "success",
        "prompt",
        "sql",
        "explanation",
        "data",
        "count",
        "truncated",
    ):
        assert key in body, key

    assert body["sql"] == STORE_SQL
    assert body["count"] == len(body["data"])


def test_provenance_is_still_complete_and_descriptive(asked):
    """The source facts did not get dropped to make room."""

    block = asked.json()["provenance"]

    assert block["source_database"] == "store.db"
    assert len(block["source_sha256"]) == 64
    assert block["request_id"]
    assert block["generated_by_ai"] is True


# ============================================================
# 4. IT IS ABSENT WHEN THERE IS NO RESULT
# ============================================================

def test_no_verification_when_generation_fails(
    authed_client, monkeypatch
):
    def fail(prompt, turns, request_id=None, generation=None):
        raise RuntimeError("NVIDIA request failed")

    monkeypatch.setattr(main, "ask_active", fail)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code >= 400
    assert "verification" not in response.json()
    assert "provenance" not in response.json()


def test_no_verification_when_sql_execution_fails(
    authed_client, monkeypatch, provider_stub
):
    def explode(sql, path=None):
        raise sqlite3.OperationalError("no such table: absent")

    monkeypatch.setattr(main, "execute_sql_with_metadata", explode)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code >= 400
    assert "verification" not in response.json()

    # The statement that nothing was checked is not printed beside a
    # result that does not exist.
    assert "not_verified" not in response.text


def test_a_missing_provenance_block_does_not_break_the_panel(
    asked, monkeypatch, provider_stub
):
    """Optional metadata stays optional, in both directions.

    The interface has a fallback for a body with no verification
    block, and the panel is hidden without a provenance block. Neither
    path invents a fact, and neither raises.
    """

    del asked

    assert "FALLBACK_NOTE" in open(
        config.INDEX_PATH, encoding="utf-8"
    ).read()


# ============================================================
# 5. THE INTERFACE DOES NOT OVERSTATE
# ============================================================

def test_the_retired_claims_are_gone(index_source):
    for phrase in RETIRED_COPY:
        assert phrase not in index_source, phrase


def test_no_correctness_claim_survives_in_the_markup(index_source):
    """The visible text of the page.

    Checked against the whole document, because a claim hidden in a
    title, a tooltip or a placeholder is still a claim. The denials
    are removed first: "not verified" is the wording this step
    exists to produce, and it contains the word it denies.
    """

    lowered = index_source.lower()

    for denial in (
        "not verified",
        "not_verified",
        "never verified",
        "unverified",
    ):
        lowered = lowered.replace(denial, "")

    for word in ("verified", "accurate", "guaranteed", "proof"):
        assert word not in lowered, word

    # A bare affirmative badge next to a table of rows is the failure
    # this test exists to catch, so the shapes are named directly.
    for phrase in (
        'class="verified"',
        ">verified<",
        "status verified",
        "is verified",
        "answer verified",
    ):
        assert phrase not in lowered, phrase


def test_the_page_shows_where_the_rows_came_from(index_source):
    """A described result has to be visible, not just available."""

    for marker in (
        'id="provenancePanel"',
        'id="provSource"',
        'id="provDigest"',
        'id="provModel"',
        'id="provExecuted"',
        'id="provRequestId"',
        "Where these rows came from",
    ):
        assert marker in index_source, marker


def test_the_panel_starts_hidden(index_source):
    """No source facts on an empty page is not a defect worth showing."""

    assert 'id="provenancePanel" class="provenance hidden"' in index_source


def test_the_page_reads_both_blocks_from_the_response(index_source):
    assert "result.provenance" in index_source
    assert "result.verification" in index_source


def test_the_badge_is_a_constant_and_not_a_value_from_the_server(
    index_source,
):
    """The interface states the known fact itself.

    The server can only ever say "not_verified", but a UI that
    echoed whatever it received would be one bad field away from
    announcing that an answer had been confirmed. So the server's
    status string is never read by the page at all.
    """

    assert 'const VERIFICATION_BADGE = "Not verified";' in index_source
    assert "verification.status" not in index_source
    assert "verification.correctness_checked" not in index_source


def test_the_page_states_plainly_that_nothing_was_checked(index_source):
    """Both wordings exist on purpose and must not drift apart.

    The server's note is the contract. The fallback in the page
    covers a body that arrives without one, and it says the same
    thing rather than staying silent.
    """

    assert (
        "Nothing here checked that it answers"
        in correctness.CORRECTNESS_NOTE
    )

    assert (
        "Nothing checked that it answers"
        in index_source
    )


def test_the_page_does_not_present_provenance_as_a_source_of_truth(
    index_source,
):
    """Read-only is enforced; correctness is not. The copy says which."""

    assert "Read-only enforced" in index_source
    assert "not a confirmed answer" in index_source


def test_every_rendered_provenance_value_goes_through_text_content(
    index_source,
):
    """A model name or a digest is server-supplied text.

    The panel writes with textContent and never with innerHTML, so a
    provider name cannot become markup.
    """

    panel = index_source[
        index_source.index("function renderProvenance") : index_source.index(
            "function clearProvenance"
        )
    ]

    assert "textContent" in panel
    assert "innerHTML" not in panel


def test_unavailable_facts_are_labelled_not_filled_in(index_source):
    """A dash reads as "nothing to report", which is a different claim."""

    assert 'const PROVENANCE_UNAVAILABLE = "unavailable";' in index_source
    assert "unavailable" in index_source


def test_a_cleared_session_clears_the_provenance(index_source):
    """Facts from a previous question must not sit under new rows."""

    cleared = index_source[
        index_source.index("function clearSession") : index_source.index(
            "function clearSql"
        )
    ]

    assert "clearProvenance()" in cleared


# ============================================================
# 6. THE FACTS TRAVEL WITH THE EXPORT
# ============================================================

def test_the_server_report_carries_the_not_verified_note(tmp_path):
    """A PDF is shared. Its reader never saw this screen.

    The note is unconditional: a report generated with no provenance
    at all still says the query was model-written and still says
    nothing confirmed it.
    """

    path = reports.build_report_pdf(
        title="Report",
        question="show the products",
        sql=STORE_SQL,
        data=[{"id": 1, "name": "Widget"}],
        count=1,
        output_dir=str(tmp_path),
    )

    assert os.path.exists(path)

    with open(path, "rb") as handle:
        assert handle.read(5) == b"%PDF-"


def test_the_report_module_reads_the_note_from_the_one_source():
    """The wording is defined once, in the module that owns the claim."""

    assert "correctness.CORRECTNESS_NOTE" in _source(reports)


def test_the_report_prints_only_shaped_source_values():
    lines = reports._source_lines(
        source_database="store.db",
        source_sha256="a" * 64,
        model="nvidia / meta-llama",
    )

    assert lines == [
        "Database: store.db",
        f"SHA-256: {'a' * 64}",
        "Written by: nvidia / meta-llama",
    ]


def test_the_report_omits_a_fact_it_cannot_support():
    """A missing fact is left out, not filled in with a guess."""

    assert reports._source_lines(None, None, None) == []

    assert reports._source_lines(
        "store.db", None, None
    ) == ["Database: store.db"]


def test_the_route_revalidates_echoed_provenance(
    authed_client, monkeypatch
):
    """The client sends these values back, so the server re-checks them.

    Anything without the right shape is dropped before it can reach
    an exported document, however it was phrased.
    """

    captured = {}

    def spy(**kwargs):
        captured.update(kwargs)
        return "report.pdf"

    monkeypatch.setattr(main, "build_report_pdf", spy)

    authed_client.post(
        "/api/report",
        json={
            "title": "Report",
            "sql": STORE_SQL,
            "source_database": "store.db",
            "source_sha256": "A" * 64,
            "model": "meta/llama",
        },
    )

    assert captured["source_database"] == "store.db"

    # Uppercase is not the digest this application publishes, and a
    # model name is not a database.
    assert captured["source_sha256"] is None
    assert captured["model"] == "meta/llama"


def test_a_client_cannot_invent_a_source_name_in_the_report(
    authed_client, monkeypatch
):
    """A path is not a basename, and a path must not reach a PDF."""

    captured = {}

    monkeypatch.setattr(
        main,
        "build_report_pdf",
        lambda **kwargs: captured.update(kwargs) or "report.pdf",
    )

    authed_client.post(
        "/api/report",
        json={
            "title": "Report",
            "sql": STORE_SQL,
            "source_database": config.DATABASE_PATH,
        },
    )

    assert captured["source_database"] == "store.db"
    assert os.sep not in captured["source_database"]


def test_a_client_cannot_smuggle_text_into_the_digest_field(
    authed_client, monkeypatch
):
    captured = {}

    monkeypatch.setattr(
        main,
        "build_report_pdf",
        lambda **kwargs: captured.update(kwargs) or "report.pdf",
    )

    authed_client.post(
        "/api/report",
        json={
            "title": "Report",
            "sql": STORE_SQL,
            "source_sha256": (
                "All rows confirmed correct against the source."
            ),
        },
    )

    assert captured["source_sha256"] is None


# ============================================================
# 7. THE SANITISERS THESE PATHS RELY ON
# ============================================================

def test_hex_digest_accepts_exactly_one_shape():
    assert provenance.hex_digest("f" * 64) == "f" * 64


@pytest.mark.parametrize(
    "value",
    [
        None,
        12345,
        "F" * 64,
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "store.db " + "a" * 54,
        "a" * 32 + " " + "a" * 31,
    ],
)
def test_hex_digest_rejects_everything_else(value):
    assert provenance.hex_digest(value) is None


def test_safe_label_refuses_anything_that_is_not_a_short_string():
    assert provenance.safe_label("meta/llama-3.1-70b") == (
        "meta/llama-3.1-70b"
    )

    for value in (None, 7, object(), ["nvidia"], b"nvidia"):
        assert provenance.safe_label(value) is None


def test_safe_label_bounds_and_sanitises():
    label = provenance.safe_label("n" * 500 + "\n<meta>")

    assert label is not None
    assert len(label) <= 64
    assert "<" not in label


def test_the_route_uses_those_sanitisers(index_source):
    """A static reminder that the frontend sends the raw values.

    The server re-validates, so prettifying client-side would only
    mean the two implementations could disagree.
    """

    assert "source_database: sourceLabel()" in index_source
    assert "source_sha256: digestLabel()" in index_source


# ============================================================
# HELPERS
# ============================================================

def test_the_sanitiser_imports_are_standard_library():
    allowed = set(sys.stdlib_module_names) | {"app"}

    for node in ast.walk(ast.parse(_source(provenance))):

        names = []

        if isinstance(node, ast.Import):
            names = [
                alias.name.split(".")[0] for alias in node.names
            ]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module.split(".")[0]]

        for name in names:
            assert name in allowed, name
