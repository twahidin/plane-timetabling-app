"""Turn an uploaded PDF, CSV or XLSX into plain text the model can read."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import PurePosixPath


class UnsupportedFile(Exception):
    pass


@dataclass
class Extracted:
    kind: str
    name: str
    text: str
    pages: int = 0
    warning: str | None = None


def extract(name: str, data: bytes) -> Extracted:
    ext = PurePosixPath(name).suffix.lower()
    if ext == ".xls":
        raise UnsupportedFile("The old .xls format is not supported. Save the file as .xlsx and upload again.")
    if ext == ".csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
            warning = "This CSV was not UTF-8; it was read as Windows-1252, check accented names."
            return Extracted("csv", name, text, warning=warning)
        return Extracted("csv", name, text)
    if ext == ".xlsx":
        if not data.startswith(b"PK"):
            raise UnsupportedFile(f"{name} is not an .xlsx workbook.")
        import openpyxl
        wb = None
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n")
                for row in ws.iter_rows(values_only=True):
                    if any(c is not None and str(c).strip() for c in row):
                        w.writerow(["" if c is None else c for c in row])
                parts.append(f"## Sheet: {ws.title}\n{buf.getvalue()}")
            return Extracted("xlsx", name, "\n".join(parts), pages=len(wb.worksheets))
        except Exception as e:
            raise UnsupportedFile(f"{name} could not be read as an .xlsx workbook: {e}")
        finally:
            if wb is not None:
                wb.close()
    if ext == ".pdf":
        if not data.startswith(b"%PDF"):
            raise UnsupportedFile(f"{name} is not a PDF.")
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            parts, chars = [], 0
            for i, page in enumerate(reader.pages, 1):
                t = (page.extract_text() or "").strip()
                chars += len(t)
                parts.append(f"## Page {i}\n{t}")
            n = max(1, len(reader.pages))
            warning = None
            if chars / n < 20:
                warning = "This PDF has no text layer; it looks like a scanned document. Export the original as PDF or paste the table into the chat."
            return Extracted("pdf", name, "\n".join(parts), pages=len(reader.pages), warning=warning)
        except Exception as e:
            raise UnsupportedFile(f"{name} could not be read as a PDF: {e}")
    raise UnsupportedFile(f"Unsupported file type {ext or '(none)'}. Upload .pdf, .csv or .xlsx.")
