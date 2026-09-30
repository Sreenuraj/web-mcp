"""PDF text extraction with a page cap."""
from __future__ import annotations

import io
import re

from ..textutil import iso_date
from .html import Extracted


def extract_pdf(body: bytes, max_pages: int) -> Extracted:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(body), strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    return Extracted(text="", kind="pdf")
            except Exception:
                return Extracted(text="", kind="pdf")
        pages = []
        total = len(reader.pages)
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                pages.append(f"[… {total - max_pages} more pages not read]")
                break
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        meta = reader.metadata
        title = (getattr(meta, "title", None) or "") if meta else ""
        published = None
        if meta and getattr(meta, "creation_date", None):
            published = iso_date(meta.creation_date.isoformat())
    except (PyPdfError, ValueError, OSError, KeyError, TypeError):
        return Extracted(text="", kind="pdf")
    text = "\n\n".join(pages)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Extracted(title=title.strip(), text=text, published=published, kind="pdf")
