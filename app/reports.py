# ============================================================
# SERVER-SIDE PDF REPORTS
# ============================================================
# PDF built entirely with reportlab (already a dependency).
# Bar charts are drawn natively with reportlab.graphics shapes,
# so no extra charting library is required.
# ============================================================

import html
import os

from datetime import datetime

import app.config as config

from app import correctness  # noqa: E402

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.graphics.shapes import (  # noqa: E402
    Drawing,
    Line,
    Rect,
    String,
)
from reportlab.platypus import (  # noqa: E402
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_AXIS_COLOR = colors.HexColor("#94A3B8")
_GRID_COLOR = colors.HexColor("#E2E8F0")
_BAR_COLOR = colors.HexColor("#2563EB")
_TEXT_COLOR = colors.HexColor("#334155")


def _escape(text):
    return html.escape(str(text or ""), quote=True)


def _clean_for_paragraph(text, max_chars=4000):
    cleaned = _escape(text).replace("\r", " ").replace("\n", " ")
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "…"
    return cleaned


def _report_dir(output_dir=None):
    if output_dir:
        directory = os.path.abspath(output_dir)
    else:
        directory = os.path.join(
            os.path.abspath(config.UPLOAD_DIR), "reports"
        )
    os.makedirs(directory, exist_ok=True)
    return directory


def _numeric_columns(data):
    if not data:
        return []
    first = data[0]
    return [
        key
        for key, value in first.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]


def _bar_chart_drawing(data, label_column, numeric_column, limit=14):
    """Native reportlab bar chart returned as a Flowable Drawing."""

    rows = data[:limit]

    labels = []
    values = []

    for index, row in enumerate(rows, start=1):
        values.append(float(row[numeric_column]))
        if label_column:
            labels.append(str(row[label_column])[:16])
        else:
            labels.append(f"#{index}")

    width, height = 460, 210
    left, right, top, bottom = 46, 10, 18, 34
    plot_width = width - left - right
    plot_height = height - top - bottom

    maximum = max(values) if values else 1
    if maximum == 0:
        maximum = 1

    count = len(values)
    slot = plot_width / max(count, 1)
    bar_width = min(slot * 0.6, 34)

    drawing = Drawing(width, height)

    drawing.add(
        Line(left, bottom, left, height - top, strokeColor=_AXIS_COLOR)
    )
    drawing.add(
        Line(left, bottom, width - right, bottom, strokeColor=_AXIS_COLOR)
    )

    for step in range(5):

        fraction = step / 4.0
        y = bottom + fraction * plot_height

        drawing.add(
            Line(
                left, y, width - right, y,
                strokeColor=_GRID_COLOR, strokeWidth=0.5,
            )
        )

        drawing.add(
            String(
                left - 6, y - 2.5,
                str(round(maximum * fraction, 1)),
                fontSize=7.5, textAnchor="end",
                fillColor=_TEXT_COLOR,
            )
        )

    for index, (label, value) in enumerate(zip(labels, values)):

        x0 = left + index * slot + (slot - bar_width) / 2
        bar_height = max((value / maximum) * plot_height, 0.6)

        drawing.add(
            Rect(
                x0, bottom, bar_width, bar_height,
                fillColor=_BAR_COLOR, strokeColor=None,
            )
        )

        if bar_height > 10:
            drawing.add(
                String(
                    x0 + bar_width / 2, bottom + bar_height + 2,
                    str(value), fontSize=6.5, textAnchor="middle",
                    fillColor=_TEXT_COLOR,
                )
            )

        drawing.add(
            String(
                x0 + bar_width / 2, bottom - 12,
                label, fontSize=6.5, textAnchor="middle",
                fillColor=_TEXT_COLOR,
            )
        )

    return drawing


def _chart_flowable(data):
    """Return a bar chart Drawing when the data supports one."""

    if not data:
        return None

    numeric = _numeric_columns(data)

    if not numeric:
        return None

    column = numeric[0]

    label_column = next(
        (
            key
            for key in data[0]
            if key != column
            and isinstance(data[0][key], (str, int, float))
            and not isinstance(data[0][key], bool)
        ),
        None,
    )

    return _bar_chart_drawing(data, label_column, column)


def _results_table(data, cap_rows=100):

    if not data:
        return None

    rows = data[:cap_rows]

    headers = list(rows[0].keys())

    if len(headers) > 6:
        headers = headers[:6]

    table_data = [
        [_escape(column) for column in headers]
    ]

    for row in rows:
        table_data.append(
            [
                _escape(row.get(column, ""))[:120]
                for column in headers
            ]
        )

    table = Table(table_data, repeatRows=1)

    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
                    colors.white,
                    colors.HexColor("#F1F5F9"),
                ]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    return table


def _source_lines(source_database, source_sha256, model):
    """The provenance facts that can actually be printed, as lines.

    Each value is used only if it still has the shape it had in the
    /api/ask response: a basename, a 64-character hex digest, a short
    label. A report is generated from a client request, so this is the
    last point at which a value that was not produced here can be kept
    out of the document. A missing fact is left out rather than filled
    in with a guess.
    """

    lines = []

    if source_database:
        lines.append(f"Database: {source_database}")

    if source_sha256:
        lines.append(f"SHA-256: {source_sha256}")

    if model:
        lines.append(f"Written by: {model}")

    return lines


def _footer(canvas_object, document):

    canvas_object.saveState()
    canvas_object.setFont("Helvetica", 7)
    canvas_object.setFillColor(colors.grey)
    canvas_object.drawCentredString(
        document.pagesize[0] / 2,
        12 * mm,
        f"TIIX SQL Studio — Report — Page {document.page}",
    )
    canvas_object.restoreState()


def build_report_pdf(
    *,
    title,
    question,
    sql,
    explanation="",
    data=None,
    count=0,
    truncated=False,
    schema_text="",
    generated_at=None,
    output_dir=None,
    source_database=None,
    source_sha256=None,
    model=None,
):
    """Generate a professional PDF report and return its path.

    The three source_* / model arguments are the provenance the client
    already received from /api/ask. They are optional and each is
    printed only if it has the right shape, so a report generated
    without them is still a valid report: it just says less about
    where the rows came from.
    """

    data = data or []

    timestamp = generated_at or datetime.now()
    generated_label = timestamp.strftime("%Y-%m-%d %H:%M:%S")

    filename = "sql_report_" + timestamp.strftime("%Y%m%d_%H%M%S") + ".pdf"
    destination = os.path.join(_report_dir(output_dir), filename)

    document = SimpleDocTemplate(
        destination,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title="SQL Assistant Report",
        author="TIIX SQL Studio",
    )

    title_style = ParagraphStyle(
        name="Title",
        fontName="Helvetica-Bold",
        fontSize=17,
        spaceAfter=4,
        textColor=colors.HexColor("#0F172A"),
    )

    section_style = ParagraphStyle(
        name="Section",
        fontName="Helvetica-Bold",
        fontSize=11,
        spaceBefore=10,
        spaceAfter=3,
        textColor=colors.HexColor("#1E293B"),
    )

    body_style = ParagraphStyle(
        name="Body",
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
    )

    mono_style = ParagraphStyle(
        name="Mono",
        fontName="Courier",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#334155"),
        backColor=colors.HexColor("#F8FAFC"),
        borderPadding=6,
    )

    schema_style = ParagraphStyle(
        name="Schema",
        fontName="Courier",
        fontSize=7.5,
        leading=9,
        textColor=colors.HexColor("#475569"),
        backColor=colors.HexColor("#F8FAFC"),
        borderPadding=6,
    )

    flowables = []

    flowables.append(
        Paragraph(_escape(title) or "SQL Report", title_style)
    )
    flowables.append(
        Paragraph(f"Generated: {generated_label}", body_style)
    )

    flowables.append(Paragraph("Question", section_style))
    flowables.append(
        Paragraph(_clean_for_paragraph(question, 6000), body_style)
    )

    flowables.append(Paragraph("Generated SQL", section_style))
    flowables.append(
        Paragraph(_clean_for_paragraph(sql), mono_style)
    )

    if explanation:
        flowables.append(Paragraph("Explanation", section_style))
        flowables.append(
            Paragraph(_clean_for_paragraph(explanation, 6000), body_style)
        )

    if schema_text:
        flowables.append(Paragraph("Database Schema", section_style))
        flowables.append(
            Paragraph(
                _escape(schema_text).replace("\n", "<br/>"),
                schema_style,
            )
        )

    # Where the rows came from, printed before the rows themselves and
    # never mixed with a judgement about them. A report outlives the
    # screen it was made from, so the source facts travel with it.
    source_lines = _source_lines(
        source_database,
        source_sha256,
        model,
    )

    if source_lines:
        flowables.append(Paragraph("Source", section_style))
        flowables.append(
            Paragraph(
                "<br/>".join(_escape(line) for line in source_lines),
                body_style,
            )
        )

    # And the statement that none of this checked the answer. It is
    # unconditional: a report generated with no provenance at all
    # still says the query was written by a model and still says
    # nothing confirmed it.
    flowables.append(Spacer(1, 3 * mm))
    flowables.append(
        Paragraph(
            _escape(correctness.CORRECTNESS_NOTE),
            ParagraphStyle(
                name="Unverified",
                fontName="Helvetica-Oblique",
                fontSize=8.5,
                leading=11.5,
                textColor=colors.HexColor("#64748B"),
                backColor=colors.HexColor("#F8FAFC"),
                borderPadding=5,
            ),
        )
    )

    flowables.append(Paragraph("Results", section_style))
    flowables.append(
        Paragraph(
            f"{count} row{'s' if count != 1 else ''} returned"
            + (" <b>(truncated)</b>" if truncated else ""),
            body_style,
        )
    )

    table = _results_table(data)

    if table:
        flowables.append(Spacer(1, 4 * mm))
        flowables.append(table)

    chart = _chart_flowable(data)

    if chart:
        flowables.append(Paragraph("Chart", section_style))
        flowables.append(Spacer(1, 2 * mm))
        flowables.append(chart)

    flowables.append(Spacer(1, 8 * mm))
    flowables.append(
        Paragraph(
            "TIIX SQL Studio — automated report",
            ParagraphStyle(
                name="Footer",
                fontName="Helvetica-Oblique",
                fontSize=7.5,
                textColor=colors.grey,
            ),
        )
    )

    document.build(flowables, onFirstPage=_footer, onLaterPages=_footer)

    return destination