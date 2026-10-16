import base64
import os
import re
import zlib

import app.reports as reports


SAMPLE_DATA = [
    {"product": "Widget", "price": 9.99, "stock": 40},
    {"product": "Gadget", "price": 19.99, "stock": 5},
    {"product": "Gizmo", "price": 4.50, "stock": 120},
]


def _page_content(path):
    """Decode the page content stream of a report.

    reportlab writes it ASCII85-wrapped and Flate-compressed. No PDF
    parser is installed here, so the few operators this test needs are
    read directly from the decoded stream.
    """

    with open(path, "rb") as handle:
        raw = handle.read()

    start = raw.index(b"stream") + len(b"stream")
    end = raw.index(b"endstream", start)
    chunk = raw[start:end].strip()
    if chunk.endswith(b"~>"):
        chunk = chunk[:-2]

    return zlib.decompress(base64.a85decode(chunk)).decode("latin-1")


SECTION_HEADINGS = ("Generated SQL", "Database Schema")


def _heading_box_tops(content):
    """Map each section heading to (baseline_y, following_box_top_y).

    A Paragraph places its text with a `cm` translation plus an inner
    `Tm` offset; a backColor Paragraph then paints its padded box with
    an `re` rectangle in that same translated space. Walking the stream
    in order lets the heading be paired with the box drawn just below
    it, so their vertical extents can be compared.
    """

    boxes = {}
    cur_y = None
    pending = None

    for line in content.splitlines():
        cm = re.match(r"1 0 0 1 [\d.]+ (-?[\d.]+) cm", line)
        if cm:
            cur_y = float(cm.group(1))
            continue

        text = re.search(
            r"1 0 0 1 0 (-?[\d.]+) Tm /F\d [\d.]+ Tf [\d.]+ TL \(([^)]*)\)",
            line,
        )
        if text and cur_y is not None:
            if text.group(2) in SECTION_HEADINGS:
                pending = (text.group(2), cur_y + float(text.group(1)))
            continue

        rect = re.search(r"n (-?[\d.]+) (-?[\d.]+) ([\d.]+) ([\d.]+) re f\*", line)
        if rect and pending and cur_y is not None:
            label, baseline = pending
            pending = None
            boxes.setdefault(
                label,
                (baseline, cur_y + float(rect.group(2)) + float(rect.group(4))),
            )

    return boxes


def test_section_boxes_do_not_clip_their_headings(tmp_path):
    """A backgrounded code/schema box must not overdraw the heading above.

    A backColor Paragraph paints its padded box taller than the height
    wrap() reports, so without matching spacing it rises into the
    preceding "Generated SQL" / "Database Schema" heading and clips it.
    """

    path = reports.build_report_pdf(
        title="Clipping Check",
        question="How many orders last month?",
        sql="SELECT COUNT(*) FROM orders",
        explanation="Counts orders.",
        schema_text="orders(id INTEGER, placed_at TIMESTAMP)",
        data=[{"orders": 42}],
        count=1,
        output_dir=str(tmp_path),
    )

    boxes = _heading_box_tops(_page_content(path))
    for label in SECTION_HEADINGS:
        assert label in boxes, f"{label!r} heading not found in the report"

    for label, (baseline, box_top) in boxes.items():
        assert box_top <= baseline, (
            f"{label!r}: background box top {box_top:.2f} rises above the "
            f"heading baseline {baseline:.2f} and clips it"
        )


def test_build_report_pdf_creates_file(tmp_path):
    path = reports.build_report_pdf(
        title="Inventory Report",
        question="Show all products",
        sql="SELECT product, price, stock FROM products",
        explanation="Lists products with price and stock.",
        data=SAMPLE_DATA,
        count=len(SAMPLE_DATA),
        truncated=False,
        schema_text="TABLE products (id INTEGER, name TEXT)",
        output_dir=str(tmp_path),
    )
    assert os.path.exists(path)
    with open(path, "rb") as handle:
        head = handle.read(5)
    assert head == b"%PDF-"


def test_build_report_with_empty_data(tmp_path):
    path = reports.build_report_pdf(
        title="Empty Report",
        question="count empty",
        sql="SELECT COUNT(*) FROM t",
        data=[],
        count=0,
        output_dir=str(tmp_path),
    )
    with open(path, "rb") as handle:
        assert handle.read(5) == b"%PDF-"


def test_build_report_without_numeric_chart(tmp_path):
    path = reports.build_report_pdf(
        title="Text Only",
        question="list names",
        sql="SELECT name FROM t",
        data=[{"name": "A"}, {"name": "B"}],
        count=2,
        output_dir=str(tmp_path),
    )
    with open(path, "rb") as handle:
        assert handle.read(5) == b"%PDF-"


def test_build_report_long_inputs(tmp_path):
    path = reports.build_report_pdf(
        title="Long",
        question="<script>x</script> " * 5,
        sql="SELECT '" + "y" * 9000 + "'",
        data=[{"col": "z" * 500} for _ in range(120)],
        count=120,
        truncated=True,
        schema_text="TABLE t (" + ", ".join(f"c{i} TEXT" for i in range(20)) + ")",
        output_dir=str(tmp_path),
    )
    with open(path, "rb") as handle:
        assert handle.read(5) == b"%PDF-"
