"""Diagnostic PDF report (M-07, FR-07.1 and FR-07.2), drawn with ReportLab.

The report is built in memory each time it is downloaded and never written to disk, so the
patient's name and MR number stay only in the patients table (NFR-3). Everything shown comes
from the stored case and the same `results.result_for` the result page uses, so the report
and the screen always agree.
"""

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app import __version__
from app.core.clock import as_utc, utcnow
from app.models import Case, Patient
from app.schemas.cases import ResultOut
from app.services import storage

TITLE = "CaviNet"
SUBTITLE = "Decision-support report: pulmonary TB vs NTM lung disease on chest CT"
BAND_LEGEND = (
    "Confidence bands: High 80% or more; Moderate 65% to 79.9%; Low below 65%, reported as "
    "Inconclusive."
)
DEMO_NOTE = (
    "This result comes from a demo model trained on synthetic data. It says nothing about "
    "this patient."
)
NOT_MEASURED = "Not yet measured on real patients."
SLICE_CAPTIONS = ("Upper lung zone", "Middle lung zone", "Lower lung zone")

# Bitstream Vera ships with ReportLab and is embedded in the PDF, so names with accents
# (é, ü, ł, ...) print the same on every computer.
_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
FONT, FONT_BOLD = "CaviNetSans", "CaviNetSans-Bold"
_fonts_registered = False

BRAND = colors.HexColor("#1d4ed8")
DEMO_RED = colors.HexColor("#b91c1c")
MUTED = colors.HexColor("#475569")
RULE = colors.HexColor("#cbd5e1")
PANEL = colors.HexColor("#f1f5f9")

_TZ_NAME = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")


def _register_fonts() -> None:
    global _fonts_registered
    if not _fonts_registered:
        pdfmetrics.registerFont(TTFont(FONT, str(_FONT_DIR / "Vera.ttf")))
        pdfmetrics.registerFont(TTFont(FONT_BOLD, str(_FONT_DIR / "VeraBd.ttf")))
        _fonts_registered = True


def timezone_or_utc(name: str | None) -> ZoneInfo:
    """The doctor's time zone (an IANA name from the browser) for the report date, or UTC."""
    if name and _TZ_NAME.match(name):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return ZoneInfo("UTC")


def age_on(born: date, on: date) -> int:
    """Whole years from `born` to `on`."""
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def _date(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "Not recorded"


def _datetime(value: datetime | None, zone: ZoneInfo) -> str:
    if value is None:
        return "Not recorded"
    return as_utc(value).astimezone(zone).strftime("%d %b %Y, %H:%M")


def _number(value: float | None, unit: str, digits: int = 2) -> str:
    return "Not recorded" if value is None else f"{value:.{digits}f} {unit}"


def _percent(value: float | None) -> str:
    return "Not measured" if value is None else f"{value * 100:.1f}%"


def safe_filename_part(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "-", text).strip("-")[:40] or "patient"


def report_filename(patient: Patient, generated_at: datetime) -> str:
    return f"CaviNet-report-{safe_filename_part(patient.mr_number)}-{generated_at:%Y-%m-%d}.pdf"


@dataclass(frozen=True)
class ReportInput:
    case: Case
    patient: Patient
    result: ResultOut
    generated_by: str
    generated_at: datetime  # timezone-aware, in the doctor's time zone


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName=FONT, fontSize=9.5, leading=13)
    return {
        "base": base,
        "title": ParagraphStyle(
            "title", base, fontName=FONT_BOLD, fontSize=24, leading=28, textColor=BRAND
        ),
        "subtitle": ParagraphStyle("subtitle", base, fontSize=10, textColor=MUTED),
        "h2": ParagraphStyle(
            "h2", base, fontName=FONT_BOLD, fontSize=11.5, leading=14, spaceBefore=5, spaceAfter=2
        ),
        "label": ParagraphStyle("label", base, fontSize=7.5, leading=9, textColor=MUTED),
        "value": ParagraphStyle("value", base, fontName=FONT_BOLD, fontSize=9, leading=11.5),
        "big": ParagraphStyle("big", base, fontName=FONT_BOLD, fontSize=17, leading=21),
        "medium": ParagraphStyle("medium", base, fontName=FONT_BOLD, fontSize=12, leading=15),
        "small": ParagraphStyle("small", base, fontSize=8, leading=10.5, textColor=MUTED),
        "group": ParagraphStyle(
            "group", base, fontName=FONT_BOLD, fontSize=8.5, leading=11, alignment=TA_CENTER
        ),
        "caption": ParagraphStyle(
            "caption", base, fontSize=8, leading=10, textColor=MUTED, alignment=TA_CENTER
        ),
        "banner": ParagraphStyle(
            "banner", base, fontName=FONT_BOLD, fontSize=13, leading=16, textColor=colors.white
        ),
        "banner_note": ParagraphStyle("banner_note", base, fontSize=9, textColor=colors.white),
        "disclaimer": ParagraphStyle("disclaimer", base, fontName=FONT_BOLD, fontSize=10.5),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text), style)


def _facts(rows: list[list[tuple[str, str]]], styles, widths: list[float]) -> Table:
    """A grid of label/value pairs: each row is a list of (label, value) cells."""
    data = [
        [[_p(label, styles["label"]), _p(value, styles["value"])] for label, value in row]
        for row in rows
    ]
    table = Table(data, colWidths=widths, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _header(info: ReportInput, styles) -> Table:
    zone = info.generated_at.tzinfo
    left = [_p(TITLE, styles["title"]), _p(SUBTITLE, styles["subtitle"])]
    right = [
        _p("Report date", styles["label"]),
        _p(info.generated_at.strftime("%d %b %Y, %H:%M"), styles["value"]),
        _p(f"Time zone: {zone}", styles["small"]),
    ]
    table = Table([[left, right]], colWidths=[136 * mm, 38 * mm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("LINEBELOW", (0, 0), (-1, 0), 1.5, BRAND),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def _banner(styles, text: str) -> Table:
    table = Table(
        [[[_p(text, styles["banner"]), _p(DEMO_NOTE, styles["banner_note"])]]],
        colWidths=[174 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), DEMO_RED),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def _patient(info: ReportInput, styles) -> list:
    patient, case = info.patient, info.case
    scan_day = case.study_date or as_utc(case.created_at).date()
    age = age_on(patient.date_of_birth, scan_day)
    return [
        _p("Patient", styles["h2"]),
        _facts(
            [
                [
                    ("Name", patient.full_name),
                    ("MR number", patient.mr_number),
                    ("Age", f"{age} years"),
                    ("Sex", patient.sex.capitalize()),
                ]
            ],
            styles,
            [62 * mm, 42 * mm, 35 * mm, 35 * mm],
        ),
    ]


def _scan(info: ReportInput, styles) -> list:
    case, zone = info.case, info.generated_at.tzinfo
    spacing = (
        f"{case.pixel_spacing_row_mm:.2f} × {case.pixel_spacing_col_mm:.2f} mm"
        if case.pixel_spacing_row_mm is not None and case.pixel_spacing_col_mm is not None
        else "Not recorded"
    )
    scanner = " ".join(p for p in (case.manufacturer, case.manufacturer_model) if p)
    series = "Not recorded"
    if case.series_found:
        series = f"Largest of {case.series_found}" if case.series_found > 1 else "1 series"
    return [
        _p("Scan details", styles["h2"]),
        _facts(
            [
                [
                    ("Study date", _date(case.study_date)),
                    ("Uploaded", _datetime(case.created_at, zone)),
                    ("Slices", str(case.num_slices) if case.num_slices else "Not recorded"),
                    ("Slice thickness", _number(case.slice_thickness_mm, "mm")),
                ],
                [
                    ("Pixel spacing", spacing),
                    ("Scanner", scanner or "Not recorded"),
                    ("Kernel", case.convolution_kernel or "Not recorded"),
                    ("Image series", series),
                ],
            ],
            styles,
            [32 * mm, 70 * mm, 36 * mm, 36 * mm],
        ),
    ]


def _headline(result) -> str:
    if result.inconclusive:
        return f"Inconclusive (leans towards {result.predicted_class})"
    return result.predicted_class


def _summary(result, label: str, styles) -> Table:
    summary = Table(
        [
            [
                [_p(label, styles["label"]), _p(_headline(result), styles["big"])],
                [
                    _p("Probability of TB", styles["label"]),
                    _p(f"{result.probability_tb_pct:.1f}%", styles["big"]),
                ],
                [
                    _p("Confidence", styles["label"]),
                    _p(f"{result.confidence_pct:.1f}% ({result.band})", styles["big"]),
                ],
            ]
        ],
        colWidths=[70 * mm, 46 * mm, 58 * mm],
    )
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return summary


def _combined_inputs(combined) -> str:
    symptoms = ", ".join(combined.symptoms) if combined.symptoms else "none of the 12 listed"
    age = f"{combined.age} years" if combined.age is not None else "unknown"
    sex = combined.sex or "unknown"
    return (
        f"CT + symptoms: the CT result combined with age ({age}), sex ({sex}) and the symptoms "
        f"entered at upload: {symptoms}. Symptom-assisted model {combined.fusion_version}."
    )


def _comparison(result, combined, styles) -> Table:
    """M-12: CT-only and combined results in one compact table, so the report stays one page."""

    def row(label: str, r) -> list:
        return [
            _p(label, styles["value"]),
            _p(_headline(r), styles["medium"]),
            _p(f"{r.probability_tb_pct:.1f}%", styles["medium"]),
            _p(f"{r.confidence_pct:.1f}% ({r.band})", styles["medium"]),
        ]

    table = Table(
        [
            [
                _p("", styles["label"]),
                _p("Result", styles["label"]),
                _p("Probability of TB", styles["label"]),
                _p("Confidence", styles["label"]),
            ],
            row("CT only", result),
            row("CT + symptoms", combined),
        ],
        colWidths=[44 * mm, 52 * mm, 34 * mm, 44 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 1), (-1, 1), 0.5, RULE),
            ]
        )
    )
    return table


def _result(info: ReportInput, styles) -> list:
    """FR-06.1 result; with symptoms entered (M-12, FR-12.3), CT-only and combined results."""
    result = info.result
    combined = result.combined
    if combined:
        parts = [
            _p("AI result", styles["h2"]),
            _comparison(result, combined, styles),
            Spacer(1, 4),
            _p(f"CT only: confidence band {result.band}. {result.explanation}", styles["base"]),
            _p(_combined_inputs(combined), styles["small"]),
        ]
    else:
        parts = [
            _p("AI result", styles["h2"]),
            _summary(result, "Result", styles),
            Spacer(1, 5),
            _p(f"Confidence band: {result.band}. {result.explanation}", styles["base"]),
        ]
        if result.combined_note:
            parts += [Spacer(1, 2), _p(result.combined_note, styles["small"])]
    parts += [Spacer(1, 2), _p(BAND_LEGEND, styles["small"])]
    if result.warnings:
        parts.append(Spacer(1, 4))
        parts.append(_p("Warnings: " + " ".join(result.warnings), styles["small"]))
    return parts


def _slice_image(path: Path, width: float) -> Image | None:
    if not path.is_file():
        return None
    image = Image(str(path))
    scale = width / image.imageWidth
    height = min(image.imageHeight * scale, 38 * mm)
    image.drawWidth, image.drawHeight = image.imageWidth * (height / image.imageHeight), height
    return image


SLICE_NOTE = (
    "Lung window, a quarter, half and three quarters of the way down the lungs; "
    "anterior up, the patient's right on the left."
)


def _image_cell(path: Path | None, width: float, caption: str, styles) -> list:
    image = _slice_image(path, width) if path is not None else None
    return [image or _p("Image not available", styles["caption"]), _p(caption, styles["caption"])]


def _slices(info: ReportInput, styles) -> list:
    """FR-05.3 representative slices and, when the case has one, the FR-11.2 heatmap: the 3
    slices with the largest heatmap share, side by side so the report stays one page."""
    details = info.case.result_details or {}
    names = (details.get("previews") or {}).get("representative") or []
    heatmap = details.get("heatmap") or {}
    hot, top = heatmap.get("top_files") or [], heatmap.get("top") or []
    folder = storage.case_dir(info.case.id) / "previews"
    width = 26 * mm if hot else 38 * mm
    cells = [
        _image_cell(folder / names[n] if n < len(names) else None, width, caption, styles)
        for n, caption in enumerate(SLICE_CAPTIONS)
    ]
    if not hot:
        table = Table([cells], colWidths=[58 * mm] * 3)
        heading, notes = _p("Representative slices", styles["h2"]), [SLICE_NOTE]
    else:
        count = len(heatmap.get("files") or [])
        cells += [
            _image_cell(folder / name, width, f"Slice {top[n] + 1} of {count}", styles)
            for n, name in enumerate(hot)
        ]
        titles = [
            _p("Representative slices", styles["group"]),
            "",
            "",
            _p(f"Heatmap: what the model looked at (result {heatmap['target']})", styles["group"]),
            "",
            "",
        ][: len(cells)]
        table = Table([titles, cells], colWidths=[29.5 * mm] * len(cells))
        heading = _p("Slices", styles["h2"])
        notes = [f"Left: {SLICE_NOTE[0].lower()}{SLICE_NOTE[1:]}", f"Right: {heatmap['label']}"]
    style = [
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1),
    ]
    if hot:
        style += [("SPAN", (0, 0), (2, 0)), ("SPAN", (3, 0), (len(cells) - 1, 0))]
    table.setStyle(TableStyle(style))
    return [KeepTogether([heading, table, *(_p(n, styles["small"]) for n in notes)])]


def _model(info: ReportInput, styles) -> list:
    result = info.result
    model, performance = result.model, result.validated_performance
    trained = model.trained_at[:10] if model.trained_at else "Not recorded"
    ensemble = f"{model.folds} models" if model.folds else "Not recorded"
    if performance.auc is None:
        measured = NOT_MEASURED + (" This is the demo model." if result.is_demo else "")
    else:
        cases = f"{performance.cases} test cases" if performance.cases else "the test set"
        measured = f"Measured once on {cases} the model never saw during training."
    rows = [
        [
            ("Model", model.name),
            ("Version", model.version),
            ("Trained", trained),
            ("Ensemble", ensemble),
        ],
        [
            ("Test AUC", "Not measured" if performance.auc is None else f"{performance.auc:.2f}"),
            ("Sensitivity (TB)", _percent(performance.sensitivity)),
            ("Specificity (NTM)", _percent(performance.specificity)),
            ("Test cases", str(performance.cases) if performance.cases else "Not measured"),
        ],
    ]
    parts = [
        _p("Model and validated performance", styles["h2"]),
        _facts(rows, styles, [70 * mm, 40 * mm, 32 * mm, 32 * mm]),
        _p(" ".join(filter(None, [measured, performance.dataset])), styles["small"]),
    ]
    return [KeepTogether(parts)]


def _closing(info: ReportInput, styles) -> list:
    disclaimer = Table([[_p(info.result.disclaimer, styles["disclaimer"])]], colWidths=[174 * mm])
    disclaimer.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 1, colors.black),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    line = "_" * 34
    signature = Table(
        [
            [
                _p("Reviewing doctor (name)", styles["label"]),
                _p("Signature", styles["label"]),
                _p("Date", styles["label"]),
            ],
            [_p(line, styles["base"]), _p(line, styles["base"]), _p("_" * 18, styles["base"])],
        ],
        colWidths=[70 * mm, 70 * mm, 34 * mm],
        hAlign="LEFT",
    )
    signature.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 1), (-1, 1), 14),
            ]
        )
    )
    return [KeepTogether([Spacer(1, 6), disclaimer, Spacer(1, 6), signature])]


class _NumberedCanvas(canvas.Canvas):
    """Draws the footer with "Page n of N" once the page count is known."""

    footer_text = ""
    demo = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pages: list[dict] = []

    def showPage(self):  # noqa: N802 (ReportLab API)
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total: int) -> None:
        width, _ = A4
        self.setStrokeColor(RULE)
        self.line(18 * mm, 15 * mm, width - 18 * mm, 15 * mm)
        self.setFont(FONT, 7.5)
        self.setFillColor(MUTED)
        self.drawString(18 * mm, 11 * mm, self.footer_text)
        self.drawRightString(width - 18 * mm, 11 * mm, f"Page {self._pageNumber} of {total}")
        if self.demo:
            self.setFillColor(DEMO_RED)
            self.setFont(FONT_BOLD, 7.5)
            self.drawCentredString(width / 2, 7 * mm, "DEMO MODEL: NOT FOR CLINICAL USE")


def build_report(info: ReportInput) -> bytes:
    """The FR-07.2 report as PDF bytes."""
    _register_fonts()
    styles = _styles()
    story: list = [_header(info, styles), Spacer(1, 4)]
    if info.result.is_demo and info.result.demo_banner:
        story += [_banner(styles, info.result.demo_banner), Spacer(1, 4)]
    story += _patient(info, styles)
    story += _scan(info, styles)
    story += _result(info, styles)
    story += _slices(info, styles)
    story += _model(info, styles)
    story += _closing(info, styles)

    footer = (
        f"CaviNet {__version__} · Case {str(info.case.id)[:8]} · "
        f"Generated {info.generated_at:%d %b %Y %H:%M} by {info.generated_by}"
    )

    class Footer(_NumberedCanvas):
        footer_text = footer
        demo = info.result.is_demo

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=22 * mm,
        title="CaviNet diagnostic report",
        author="CaviNet",
        subject=SUBTITLE,
        creator=f"CaviNet {__version__}",
    )
    document.build(story, canvasmaker=Footer)
    return buffer.getvalue()


def report_for(
    case: Case, patient: Patient, result: ResultOut, doctor_name: str, zone: ZoneInfo
) -> tuple[bytes, str]:
    """The PDF and its download file name."""
    generated_at = utcnow().astimezone(zone)
    info = ReportInput(case, patient, result, doctor_name, generated_at)
    return build_report(info), report_filename(patient, generated_at)
