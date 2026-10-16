"""The frontend keeps generation and execution as two visible steps.

The server split is only useful if the page uses it: the SQL must be
rendered in an editable box, generating must not run it, and a separate
control must run what is in the box. These are string checks against
``index.html`` (the same approach as test_frontend_upload_flow), because
the wiring is browser code no server test can exercise.
"""

import re
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "index.html"


@pytest.fixture(scope="module")
def html():
    return INDEX.read_text(encoding="utf-8")


def _function_body(source, name):
    match = re.search(r"function\s+" + re.escape(name) + r"\s*\(", source)

    assert match, f"{name} is not defined in index.html"

    start = match.start()
    following = re.search(
        r"\n\s*(?:async\s+)?function\s+\w+\s*\(", source[start + 1:]
    )

    end = start + 1 + following.start() if following else len(source)

    return source[start:end]


def test_the_sql_is_shown_in_an_editable_box(html):
    """The user can review AND edit before running."""

    match = re.search(r'<code id="sqlOutput"[^>]*>', html)

    assert match, "sqlOutput is not defined"

    assert "contenteditable" in match.group(0)

    assert 'oninput="onSqlInput()"' in match.group(0)


def test_there_are_two_separate_controls(html):
    """Generate and Run are distinct buttons, wired to distinct calls."""

    generate = re.search(r'<button id="generateButton"[^>]*>', html)

    run = re.search(r'<button id="runButton"[^>]*>', html)

    assert generate, "generateButton is missing"
    assert run, "runButton is missing"

    assert 'onclick="sendQuery()"' in generate.group(0)
    assert 'onclick="runQuery()"' in run.group(0)


def test_the_combined_label_is_gone(html):
    """The old one-click generate-and-run wording must not survive."""

    assert "Generate SQL &amp; Run" not in html
    assert "Generate SQL & Run" not in html


def test_send_query_only_calls_ask(html):
    body = _function_body(html, "sendQuery")

    assert '"/api/ask"' in body
    assert '"/api/run"' not in body

    # Generation sends the question and its context, not a statement.
    payload = body.split("JSON.stringify", 1)[1]

    assert "prompt," in payload
    assert "history:" in payload

    # The import selection is still carried on generation.
    assert "import_id: selectedImportId || undefined" in body


def test_run_query_posts_the_editor_contents_to_run(html):
    body = _function_body(html, "runQuery")

    assert '"/api/run"' in body
    assert '"/api/ask"' not in body

    # It runs what is in the editor, not a cached string.
    assert "readSqlFromEditor()" in body


def test_read_sql_from_editor_is_the_source_of_truth(html):
    body = _function_body(html, "readSqlFromEditor")

    assert '"sqlOutput"' in body


def test_run_requires_generation_is_handled(html):
    """A visitor with nothing generated is nudged, not sent to the modal."""

    assert "RUN_REQUIRES_GENERATION" in html

    body = _function_body(html, "runQuery")

    assert "RUN_REQUIRES_GENERATION" in body


# ============================================================
# CLEARING THE EDITOR DISCARDS THE RUN, NOT JUST THE TEXT
# ============================================================

def test_clear_sql_disables_run_and_clears_state(html):
    """A cleared editor must not leave a runnable placeholder behind."""

    body = _function_body(html, "clearSql")

    assert "setRunEnabled(false)" in body

    # The stale execution is discarded with the statement.
    assert "lastQueryTime = null" in body
    assert "lastResultData = []" in body
    assert "lastCount = 0" in body
    assert "lastGenerationTicket = null" in body

    assert "clearProvenance()" in body
    assert "hideChart()" in body
    assert "updateExportStates()" in body


def test_source_change_discards_the_ticket(html):
    """A statement written for one source is not 'generated' for another."""

    body = _function_body(html, "resetResultsForSourceChange")

    assert "lastGenerationTicket = null" in body


# ============================================================
# THE TICKET RIDES ALONG TO /api/run
# ============================================================

def test_generation_stores_the_ticket(html):
    body = _function_body(html, "sendQuery")

    assert "result.generation_ticket" in body
    assert "lastGenerationTicket" in body


def test_run_sends_the_ticket(html):
    body = _function_body(html, "runQuery")

    assert "generation_ticket:" in body
    assert "lastGenerationTicket" in body


# ============================================================
# THE PDF IS AN EXECUTION REPORT, SO IT WAITS FOR A RUN
# ============================================================

def test_pdf_button_waits_for_an_execution(html):
    body = _function_body(html, "updateExportStates")

    assert "lastQueryTime" in body


def test_export_pdf_requires_an_execution(html):
    body = _function_body(html, "exportPdf")

    assert "lastQueryTime" in body


def test_server_pdf_requires_an_execution(html):
    body = _function_body(html, "serverPdf")

    assert "lastQueryTime" in body
