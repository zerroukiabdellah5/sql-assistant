import os

import app.reports as reports


SAMPLE_DATA = [
    {"product": "Widget", "price": 9.99, "stock": 40},
    {"product": "Gadget", "price": 19.99, "stock": 5},
    {"product": "Gizmo", "price": 4.50, "stock": 120},
]


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