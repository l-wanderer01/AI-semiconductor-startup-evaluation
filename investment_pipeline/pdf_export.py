from __future__ import annotations

from evaluation.recording import traced

from html import escape
from pathlib import Path

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer


def _register_korean_font() -> str:
    candidates = [
        ("AppleGothic", "/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
        ("ArialUnicode", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ]
    for font_name, font_path in candidates:
        try:
            pdfmetrics.registerFont(TTFont(font_name, font_path))
            return font_name
        except Exception:
            continue
    return "Helvetica"


@traced('export_markdown_to_pdf')
def export_markdown_to_pdf(markdown_path: Path, pdf_path: Path) -> None:
    font_name = _register_korean_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleKo",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=18,
        leading=24,
        alignment=TA_LEFT,
    )
    heading_style = ParagraphStyle(
        "HeadingKo",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=13,
        leading=18,
        alignment=TA_LEFT,
    )
    body_style = ParagraphStyle(
        "BodyKo",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10,
        leading=15,
        alignment=TA_LEFT,
    )
    mono_style = ParagraphStyle(
        "MonoKo",
        parent=styles["Code"],
        fontName=font_name,
        fontSize=8,
        leading=11,
        alignment=TA_LEFT,
    )

    story = []
    content = markdown_path.read_text(encoding="utf-8").splitlines()
    for line in content:
        stripped = line.rstrip()
        if not stripped:
            story.append(Spacer(1, 4))
            continue
        if stripped.startswith("# "):
            story.append(Paragraph(escape(stripped[2:]), title_style))
            story.append(Spacer(1, 6))
            continue
        if stripped.startswith("## ") or stripped.startswith("### ") or stripped.startswith("#### "):
            text = stripped.lstrip("#").strip()
            story.append(Paragraph(escape(text), heading_style))
            story.append(Spacer(1, 4))
            continue
        if stripped.startswith("|") or stripped.startswith("- "):
            story.append(Preformatted(stripped, mono_style))
            continue
        story.append(Paragraph(escape(stripped), body_style))

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    doc.build(story)
