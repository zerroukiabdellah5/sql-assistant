"""The upload dialog and active-source switching, guarded at the source.

The upload workflow is browser code, so no server test can exercise it.
These checks pin the wiring whose loss was the regression: clicking
Upload opens the in-app dialog without the OS file picker also opening,
a click inside the dialog does not dismiss it, the selected file name is
shown, a successful import offers an explicit Use action, and the active
database changes only when the user asks for it - with a way back to
``store.db``.

They are deliberate string checks against ``index.html``: the point is
to catch the wiring being dropped or rewired again, not to run a browser
(the opt-in browser smoke test lives outside the suite).
"""

import re
from pathlib import Path

import pytest

from app import config

INDEX = Path(__file__).resolve().parents[1] / "index.html"


@pytest.fixture(scope="module")
def html():
    return INDEX.read_text(encoding="utf-8")


def _function_body(source, name):
    """Return ``name``'s source up to the next top-level function."""

    match = re.search(r"function\s+" + re.escape(name) + r"\s*\(", source)

    assert match, f"{name} is not defined in index.html"

    start = match.start()
    following = re.search(r"\n\s*function\s+\w+\s*\(", source[start + 1:])

    end = start + 1 + following.start() if following else len(source)

    return source[start:end]


def test_upload_click_opens_the_dialog_without_opening_the_file_picker(html):
    """Upload must open the dialog first, never the OS picker."""

    body = _function_body(html, "openWorkspace")

    upload_branch = body[body.index('which === "upload"') :]

    assert "loadImports()" in upload_branch

    assert "openUploadPicker(" not in upload_branch


def test_a_click_inside_the_dialog_does_not_dismiss_it(html):
    """The dim backdrop closes the dialog; its contents never do."""

    for which in ("sessions", "upload", "approvals"):
        assert (
            f'closeWorkspaceOnBackdrop(event, \'{which}\')' in html
        ), f"the {which} backdrop is not guarded against inner clicks"

    guard = _function_body(html, "closeWorkspaceOnBackdrop")

    assert "event.target !== event.currentTarget" in guard


def test_the_selected_file_name_is_surfaced_before_uploading(html):
    assert 'id="uploadFileName"' in html

    assert 'onchange="onUploadFileSelected()"' in html

    assert "No file chosen" in html


def test_accepted_types_match_the_backend(html):
    accept = re.search(r'accept="([^"]+)"', html).group(1)

    allowed = (
        config.ALLOWED_DATABASE_EXTENSIONS
        + config.ALLOWED_SQL_EXTENSIONS
        + config.ALLOWED_EXCEL_EXTENSIONS
        + config.ALLOWED_CSV_EXTENSIONS
    )

    for extension in allowed:
        assert extension in accept, f"{extension} is accepted by the backend only"

    assert ".xls\"" not in accept and " .xls " not in accept


def test_a_successful_upload_does_not_replace_the_active_source(html):
    body = _function_body(html, "handleUpload")

    assert "setActiveSource(" not in body

    assert "loadImports()" in body

    assert "successfully" in body and "useUploadedImport(" in body


def test_use_switches_source_and_store_db_switches_back(html):
    use = _function_body(html, "useUploadedImport")
    assert "useImport(" in use

    default = _function_body(html, "useDefaultSource")
    assert 'setActiveSource("", null)' in default

    schema = _function_body(html, "sourceSchemaUrl")
    assert "/api/visitor/schema" in schema
    assert "/api/public/schema" in schema


def test_the_ask_request_carries_the_selected_import(html):
    assert "import_id: selectedImportId || undefined" in html
