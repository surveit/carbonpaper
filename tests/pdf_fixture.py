"""Write a PDF whose text layer is known: one Helvetica line per page, an empty string a blank page."""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import ContentStream, DictionaryObject, NameObject

_HELVETICA_RESOURCES = DictionaryObject({
    NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }),
    }),
})


def write_text_pdf(path: Path, page_texts: list[str]) -> Path:
    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        if text:
            page[NameObject("/Resources")] = _HELVETICA_RESOURCES
            page.replace_contents(_draw_line(text))
    writer.write(path)
    return path


def _draw_line(text: str) -> ContentStream:
    content = ContentStream(None, None)
    content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
    return content
