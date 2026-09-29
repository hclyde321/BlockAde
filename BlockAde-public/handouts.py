"""Local text extraction and page context for course lecture handouts.

Handouts are separate from question-bank assets. No OCR or network request is
performed here; pages without selectable text remain empty and cannot be used
as evidence for a match.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import db

try:
    import fitz  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - installation failure
    raise RuntimeError("講義 PDF 需要 PyMuPDF；請安裝 requirements.txt 中的依賴。") from exc


MAX_PAGES = 500
MAX_EXTRACTED_CHARS = 1_000_000
MAX_CONTEXT_CHARS = 120_000
MAX_CONTEXT_PAGE_CHARS = 6_000
_EXPLICIT_HEADING = re.compile(
    r"^(?:第\s*[一二三四五六七八九十百0-9]+\s*[章節單元]|"
    r"(?:chapter|unit|section)\s+\d+\b|\d+(?:\.\d+)+\s+\S)", re.IGNORECASE
)


class HandoutError(ValueError):
    """A PDF cannot be used as a searchable lecture handout."""


def _section(lines: list[str], page_number: int) -> str:
    """Keep a page heading only when it resembles a short title."""
    for line in lines[:12]:
        clean = re.sub(r"\s+", " ", line).strip()
        if len(clean) <= 100 and _EXPLICIT_HEADING.match(clean):
            return clean
    for line in lines[:8]:
        clean = re.sub(r"\s+", " ", line).strip()
        if not clean:
            continue
        if len(clean) <= 100 and not re.fullmatch(r"[\d\s/\-]+", clean):
            return clean
        break
    return f"第 {page_number} 頁"


def extract_pdf(path: Path) -> list[dict[str, Any]]:
    """Validate and extract every page; reject invalid or wholly scanned PDFs."""
    try:
        with fitz.open(str(path)) as doc:
            if doc.needs_pass:
                raise HandoutError("這份講義 PDF 有密碼保護，請先匯出未加密的版本。")
            if not 1 <= len(doc) <= MAX_PAGES:
                raise HandoutError(f"講義 PDF 頁數必須介於 1 到 {MAX_PAGES} 頁。")
            pages: list[dict[str, Any]] = []
            total_chars = 0
            for index, page in enumerate(doc, start=1):
                raw = page.get_text("text", sort=True)
                lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
                text = "\n".join(line for line in lines if line).strip()
                total_chars += len(text)
                if total_chars > MAX_EXTRACTED_CHARS:
                    raise HandoutError("講義文字超過 100 萬字元限制，請拆成數份 PDF 上傳。")
                pages.append({"page": index, "section": _section(lines, index), "text": text})
    except HandoutError:
        raise
    except (RuntimeError, ValueError, OSError) as exc:
        raise HandoutError("無法讀取講義 PDF，請確認檔案完整且未損壞。") from exc
    if not any(page["text"] for page in pages):
        raise HandoutError("這份講義 PDF 沒有可選取的文字；請先使用 OCR 轉成可搜尋的 PDF，再重新上傳。")
    return pages


def course_context(course_id: str, *, max_chars: int = MAX_CONTEXT_CHARS) -> list[dict[str, Any]]:
    """Return bounded, citeable page excerpts for matching prompts.

    The cap is spread over all pages so later chapters are not dropped merely
    because earlier pages consumed the prompt budget.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id,filename,pages_json FROM course_handouts WHERE course_id=? ORDER BY rowid",
            (course_id,),
        ).fetchall()
    entries = [(row, page) for row in rows for page in json.loads(row["pages_json"]) if page.get("text")]
    if not entries or max_chars <= 0:
        return []
    per_page = min(MAX_CONTEXT_PAGE_CHARS, max_chars // len(entries))
    result = []
    for row, page in entries:
        page_number = int(page["page"])
        result.append({
            "id": f"handout-{row['id']}-p{page_number}",
            "handout_id": row["id"], "filename": row["filename"],
            "page": page_number, "section": page["section"],
            "text": page["text"][:per_page],
        })
    return result
