"""Coordinate-aware import helpers for the Block 1 question and answer PDFs.

The question PDF uses a font with missing glyphs for its question/choice labels.
Those glyphs still occupy text spans in PyMuPDF's dict output, so their x/y
coordinates are used as structural markers.  The answer PDF is laid out as a
three-column table; number and answer cells are paired by their baselines.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import fitz  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - exercised only without dependency
    raise RuntimeError("PDF import requires PyMuPDF (install package 'PyMuPDF').") from exc


_QUESTION_DOC_MARKER = "臨床醫學考古題目"
_ANSWER_DOC_MARKER = "臨床醫學考古詳解"
_EXAM_RE = re.compile(r"\b(B\s*\d{2})\s*第\s*(?:一|1)\s*次區段考", re.IGNORECASE)
_EXPLICIT_NUMBER_RE = re.compile(r"^\s*(\d{1,3})\s*[.．、)]\s*(.*)$", re.DOTALL)
_VISIBLE_OPTION_RE = re.compile(r"^\s*\(?([A-E])\)\s*(.*)$", re.IGNORECASE | re.DOTALL)
_SINGLE_INTEGER_RE = re.compile(r"^\s*(\d{1,3})\s*$")

_SECTION_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("病史詢問及身體診察（不同老師）", ("病史詢問及身體診察", "病史詢問與身體診察")),
    ("面談溝通技巧", ("面談溝通技巧",)),
    ("病歷書寫（不同老師）", ("病歷書寫（不同老師）", "病歷書寫(不同老師)")),
    ("病歷書寫", ("病歷書寫",)),
    ("頭頸部的身體診察", ("頭頸部的身體診察",)),
    ("手術前之評估及準備", ("手術前之評估及準備", "手術前之評估與準備")),
    ("微創及侵入性治療之反應", ("微創及侵入性治療之反應",)),
    ("外科傷口之癒合（不同老師）", ("外科傷口之癒合（不同老師）", "外科傷口之癒合(不同老師)")),
    ("燒傷", ("燒傷",)),
)


def _clean_text(value: str) -> str:
    """Normalize PDF line whitespace while preserving readable word breaks."""
    value = value.replace("\u00a0", " ").replace("\u3000", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    return value.strip()


def _flat(value: str) -> str:
    return re.sub(r"[\s　()（）\[\]【】:：、，,。．.·]", "", value).casefold()


def _canonical_section(value: str | None) -> str:
    if not value:
        return ""
    text = _clean_text(value)
    exam = _EXAM_RE.search(text)
    if exam:
        return f"{exam.group(1).upper().replace(' ', '')} 第一次區段考"
    norm = _flat(text)
    # Test-section labels have a variant conjunction between the two PDFs.
    for canonical, aliases in _SECTION_ALIASES:
        if any(_flat(alias) in norm for alias in aliases):
            return canonical
    # A compact fallback preserves page headings that are not in the first
    # revision's alias list, while removing only extraction whitespace.
    return text


def _section_from_page(lines: list[dict[str, Any]], current: str = "") -> str:
    """Find a running chapter/exam header before the first question on a page."""
    candidates = sorted((line for line in lines if line["y"] < 125 and line["text"]), key=lambda item: (item["y"], item["x"]))
    # Exam identifiers take precedence over topic labels such as [燒傷].
    for line in candidates:
        match = _EXAM_RE.search(line["text"])
        if match:
            return _canonical_section(match.group(0))
    # Pathology booklets print a teacher/topic heading after §. In the
    # question booklet § and the title are separate text lines; in the answer
    # booklet they are one line. Only accept headings with 老師 to avoid
    # treating question-type labels such as § 單選題 as chapter boundaries.
    for line in candidates:
        title = line["text"].removeprefix("§").strip()
        if line["text"] == "§":
            neighbor = next((other for other in candidates if abs(other["y"] - line["y"]) <= 3 and 80 <= other["x"] <= 150 and other["text"]), None)
            title = neighbor["text"] if neighbor else ""
        if "老師" in title and (line["text"].startswith("§") or line["text"] == "§"):
            return _clean_text(title)
    for line in candidates:
        text = line["text"]
        if _QUESTION_DOC_MARKER in text or _ANSWER_DOC_MARKER in text or "BLOCK 1" in text.upper():
            continue
        if _SINGLE_INTEGER_RE.fullmatch(text):
            continue
        canonical = _canonical_section(text)
        if canonical != text or any(_flat(alias) in _flat(text) for _, aliases in _SECTION_ALIASES for alias in aliases):
            return canonical
    return current


def _page_items(page: Any) -> tuple[list[dict[str, Any]], list[tuple[float, float, float, float]]]:
    """Return positioned text lines and image rectangles in MuPDF block order."""
    data = page.get_text("dict")
    lines: list[dict[str, Any]] = []
    image_boxes: list[tuple[float, float, float, float]] = []
    for block in data.get("blocks", []):
        if block.get("type") == 1:
            bbox = block.get("bbox")
            if bbox:
                image_bbox = tuple(float(n) for n in bbox)
                # These PDFs encode the missing question/choice glyphs as
                # tiny raster marks too. They are structural labels, not
                # omitted clinical figures.
                if image_bbox[2] - image_bbox[0] >= 18 or image_bbox[3] - image_bbox[1] >= 18:
                    image_boxes.append(image_bbox)
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = _clean_text("".join(str(span.get("text", "")) for span in spans))
            x0 = min(float(span["bbox"][0]) for span in spans)
            y0 = min(float(span["bbox"][1]) for span in spans)
            x1 = max(float(span["bbox"][2]) for span in spans)
            y1 = max(float(span["bbox"][3]) for span in spans)
            q_marker = False
            option_marker = False
            for span in spans:
                font = str(span.get("font", ""))
                sx = float(span["bbox"][0])
                size = float(span.get("size", 0))
                if font.startswith("ArialMT") and not str(span.get("text", "")).strip() and 10.5 <= size <= 14.5:
                    if 65 <= sx < 89:
                        q_marker = True
                    elif 89 <= sx < 112:
                        option_marker = True
            lines.append({
                "text": text,
                "x": x0,
                "y": y0,
                "x1": x1,
                "y1": y1,
                "q_marker": q_marker,
                "option_marker": option_marker,
            })
    return lines, image_boxes


def _question_type_from_text(text: str) -> str | None:
    if "問答題" in text or "簡答題" in text:
        return "問答題"
    if "是非題" in text:
        return "是非題"
    if "多選題" in text:
        return "多選題"
    if "單選題" in text:
        return "單選題"
    if "選擇題" in text:
        return "選擇題"
    return None


def _effective_section(base: str, question_type: str | None) -> str:
    # These labels explicitly restart numbering in the supplied PDFs.
    if base == "面談溝通技巧" and question_type in {"是非題", "單選題"}:
        return f"{base} / {question_type}"
    if question_type == "問答題":
        return f"{base} / 問答題" if base else "問答題"
    if re.match(r"^B\d{2} 第一次區段考$", base) and question_type:
        # Exam answer pages restart local numbering between true/false and
        # choice blocks. The two PDFs use both 單選題 and 選擇題 for the same
        # choice section, so use a shared suffix for matching.
        suffix = "選擇題" if question_type in {"單選題", "選擇題"} else question_type
        return f"{base} / {suffix}"
    if base == "病史詢問及身體診察（不同老師）" and question_type in {"是非題", "單選題"}:
        return f"{base} / {'選擇題' if question_type == '單選題' else question_type}"
    if base == "燒傷" and question_type in {"單選題", "選擇題", "多選題"}:
        suffix = "選擇題" if question_type in {"單選題", "選擇題"} else "多選題"
        return f"{base} / {suffix}"
    return base


def _append_text(parts: list[str], text: str) -> None:
    text = _clean_text(text)
    if text:
        parts.append(text)


def _is_page_chrome(text: str) -> bool:
    return (
        not text
        or _QUESTION_DOC_MARKER in text
        or _ANSWER_DOC_MARKER in text
        or text.upper().startswith("BLOCK 1")
        or bool(_EXAM_RE.search(text))
        or text in {"§", "§ 單選題", "§ 是非題", "§ 問答題"}
        or bool(_SINGLE_INTEGER_RE.fullmatch(text))
    )


def import_questions(path: str | Path) -> list[dict[str, Any]]:
    """Parse question stems/options from the supplied question PDF.

    `number` and `local_number` are strings because some source PDFs may use
    non-numeric identifiers. For the current PDFs, hidden labels are
    reconstructed in page order; the B07 printed numbers are retained as-is.
    """
    doc = fitz.open(str(path))
    questions: list[dict[str, Any]] = []
    current_section = ""
    current_type: str | None = None
    counters: defaultdict[str, int] = defaultdict(int)
    active: dict[str, Any] | None = None
    current_option: dict[str, str] | None = None
    page_image_boxes: dict[int, list[tuple[float, float, float, float]]] = {}

    def finish_active() -> None:
        nonlocal active, current_option
        if active is None:
            current_option = None
            return
        if current_option is not None:
            active["options"].append(current_option)
            current_option = None
        stem = " ".join(active.pop("_stem_parts", []))
        stem = _clean_text(stem)
        option_pairs = active["options"]
        for option in option_pairs:
            option["text"] = _clean_text(option["text"])
        section = active["section"]
        number = active["number"]
        source_text = stem
        if option_pairs:
            option_text = "\n".join(f"({opt['key']}) {opt['text']}".rstrip() for opt in option_pairs)
            source_text = f"{stem}\n{option_text}".strip()
        review = bool(active.get("_needs_review"))
        if not section or not number or not stem:
            review = True
        binary_question = active.get("_question_type") == "是非題" or bool(re.search(r"(?:是非|true\s*/\s*false)", stem, re.IGNORECASE))
        essay_question = active.get("_question_type") == "問答題" or bool(re.match(r"^(請簡述|請寫出|寫出|說明|描述|試述)", stem))
        if binary_question and not option_pairs:
            option_pairs.extend([{"key": "O", "text": "是"}, {"key": "X", "text": "非"}])
            source_text = f"{stem}\n(O) 是\n(X) 非".strip()
        elif not essay_question and not binary_question and len(option_pairs) < 4:
            review = True
        if essay_question:
            # Text marked (A), (B), etc. inside an essay prompt are subparts,
            # not answer choices. Keep the prompt intact and out of practice.
            if option_pairs:
                stem = _clean_text(" ".join([stem, *(f"({item['key']}) {item['text']}" for item in option_pairs)]))
                option_pairs = []
                source_text = stem
            review = True
        if len(option_pairs) > 5:
            review = True
        if any(not option["text"] for option in option_pairs):
            review = True
        if any(re.search(r"表格遺失|圖片|如下圖|如圖所示", text) for text in [stem, *(o["text"] for o in option_pairs)]):
            review = True
        # Images that intersect the question's text span mean the extracted
        # text may omit a stem/choice; retain the record but require review.
        for page_no, (start_y, end_y) in active.get("_page_spans", {}).items():
            for x0, y0, x1, y1 in page_image_boxes.get(page_no, []):
                if x1 > 65 and x0 < 555 and y1 >= start_y - 5 and y0 <= end_y + 5:
                    review = True
                    break
        if essay_question:
            question_type = "essay"
        elif binary_question:
            question_type = "true_false"
        elif active.get("_question_type") == "多選題" or re.search(r"多選", stem):
            question_type = "multiple_select"
        else:
            question_type = "single_choice"
        questions.append({
            "number": str(number),
            "local_number": str(number),
            "section": section,
            "question_type": question_type,
            "stem": stem,
            "options": option_pairs,
            "source_page": active["source_page"],
            "source_text": source_text,
            "needs_review": review,
        })
        active = None
        current_option = None

    def start_question(page_no: int, y: float, text: str = "", explicit_number: str | None = None) -> None:
        nonlocal active, current_option
        finish_active()
        section = _effective_section(current_section, current_type)
        if explicit_number is None:
            counters[section] += 1
            number = str(counters[section])
        else:
            number = str(int(explicit_number))
            counters[section] = max(counters[section], int(explicit_number))
        active = {
            "number": number,
            "section": section,
            "source_page": page_no,
            "options": [],
            "_stem_parts": [],
            "_question_type": current_type,
            "_needs_review": False,
            "_page_spans": {page_no: [y, y]},
        }
        current_option = None
        _append_text(active["_stem_parts"], text)
        _promote_essay(active)

    def _promote_essay(question: dict[str, Any]) -> None:
        joined_stem = _clean_text(" ".join(question["_stem_parts"]))
        if question["_question_type"] == "問答題" or not re.match(r"^(請簡述|請寫出|寫出|說明|描述|試述)", joined_stem):
            return
        old_section = question["section"]
        if counters[old_section] == int(question["number"]):
            counters[old_section] -= 1
        question["section"] = _effective_section(current_section, "問答題")
        counters[question["section"]] += 1
        question["number"] = str(counters[question["section"]])
        question["_question_type"] = "問答題"

    for page_index, page in enumerate(doc):
        page_no = page_index + 1
        lines, image_boxes = _page_items(page)
        page_image_boxes[page_no] = image_boxes
        detected_section = _section_from_page(lines, current_section)
        if detected_section and detected_section != current_section:
            finish_active()
            current_section = detected_section
            current_type = None

        # Some questions are open-ended and therefore have neither a missing
        # glyph marker nor a visible local number. Their explicit section
        # heading is enough to recognize the prompts in this source PDF.
        for line in lines:
            text = line["text"]
            line_type = _question_type_from_text(text)
            if line_type:
                if line_type != current_type:
                    finish_active()
                    current_type = line_type
                continue
            # Empty text is expected for the missing-font label spans. Start
            # the question/choice before filtering blank visible text.
            if line["q_marker"]:
                start_question(page_no, line["y"], text)
                continue
            if not text and not line["option_marker"]:
                continue
            if (_is_page_chrome(text) and not line["option_marker"]) or text.startswith("§"):
                continue

            explicit = _EXPLICIT_NUMBER_RE.match(text) if line["x"] < 88 else None
            visible_option = _VISIBLE_OPTION_RE.match(text) if line["x"] >= 88 else None
            if explicit:
                explicit_number = explicit.group(1) if explicit else None
                body = explicit.group(2) if explicit else text
                start_question(page_no, line["y"], body, explicit_number)
                continue

            if active is None and current_type == "問答題" and re.match(r"^(請|寫出|說明|描述|比較|列出|試述)", text):
                start_question(page_no, line["y"], text)
                continue

            if active is None:
                continue
            active["_page_spans"].setdefault(page_no, [line["y"], line["y"]])
            active["_page_spans"][page_no][0] = min(active["_page_spans"][page_no][0], line["y"])
            active["_page_spans"][page_no][1] = max(active["_page_spans"][page_no][1], line["y1"])
            marker_option = line["option_marker"]
            if (marker_option or visible_option) and active.get("_question_type") != "問答題":
                if current_option is not None:
                    active["options"].append(current_option)
                if visible_option:
                    key = visible_option.group(1).upper()
                    option_body = visible_option.group(2)
                else:
                    key = chr(ord("A") + len(active["options"]))
                    option_body = text
                expected = chr(ord("A") + len(active["options"]))
                if key != expected and not marker_option:
                    active["_needs_review"] = True
                current_option = {"key": key, "text": _clean_text(option_body)}
            elif current_option is not None:
                current_option["text"] = _clean_text(f"{current_option['text']} {text}")
            else:
                _append_text(active["_stem_parts"], text)
                _promote_essay(active)

    finish_active()
    doc.close()

    duplicates: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    for index, question in enumerate(questions):
        duplicates[(question["section"], question["number"])].append(index)
    for indexes in duplicates.values():
        if len(indexes) > 1:
            for index in indexes:
                questions[index]["needs_review"] = True
    return questions


def _answer_key_prefix(text: str) -> tuple[str, str] | None:
    """Extract an answer cell even when its explanation shares the same line."""
    value = _clean_text(text)
    match = re.match(
        r"^\s*(無解|無|全|O|X|[A-I]{1,9}(?:/[A-I]{1,9})?|[A-I]\([A-I]{1,9}\))(?=\s|$)(.*)$",
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(1)).upper(), _clean_text(match.group(2))


def _answer_rows(lines: list[dict[str, Any]], *, pathology_layout: bool = False) -> dict[int, tuple[str, str, bool, str]]:
    """Find number/key rows by their adjacent table columns and baseline."""
    numbers: list[tuple[int, int, float, float, str]] = []
    answers: list[tuple[int, float, float, str, str]] = []
    # Some answer booklets first print a compact horizontal answer grid,
    # followed by the numbered explanation table. The grid has different
    # semantics and must not be mistaken for explanation rows.
    table_headers = [line["y"] for line in lines if line["text"] == "題號" and 65 <= line["x"] <= 110] if pathology_layout else []
    table_start = max(table_headers) if table_headers else 0.0
    for index, line in enumerate(lines):
        text, x, y = line["text"], line["x"], line["y"]
        if y <= table_start:
            continue
        single = re.fullmatch(r"\s*(\d{1,3})\s*\.?\s*", text) if pathology_layout else _SINGLE_INTEGER_RE.fullmatch(text)
        if single and 68 <= x <= 125 and 30 <= y <= 800:
            numbers.append((index, int(single.group(1)), x, y, text))
        if 105 <= x <= 175 and 30 <= y <= 800:
            key_prefix = _answer_key_prefix(text)
            if key_prefix:
                answers.append((index, x, y, key_prefix[0], key_prefix[1]))
        same_line = re.fullmatch(r"\s*(\d{1,3})\s+([A-E]{1,5}|O|X|無)\s*", text, re.IGNORECASE)
        if same_line and 68 <= x <= 160 and 30 <= y <= 800:
            numbers.append((index, int(same_line.group(1)), x, y, same_line.group(1)))
            answers.append((index, x + 30, y, same_line.group(2), ""))
    rows: dict[int, tuple[str, str, bool, str]] = {}
    for index, number, x, y, _ in numbers:
        aligned = [candidate for candidate in answers if abs(candidate[2] - y) <= 2.2 and candidate[1] > x + 15]
        if aligned:
            candidate = min(aligned, key=lambda entry: (abs(entry[2] - y), entry[1]))
            rows[index] = (str(number), candidate[3], False, candidate[4])
            continue

        # A multi-line answer cell can contain alternatives (for example
        # "B or BD") while the question number is vertically centered beside
        # it. Preserve the ambiguity instead of choosing one candidate.
        nearby = [candidate for candidate in answers if abs(candidate[2] - y) <= 24 and x + 15 < candidate[1] <= 175]
        distinct = []
        for candidate in sorted(nearby, key=lambda entry: entry[2]):
            if candidate[3] not in distinct:
                distinct.append(candidate[3])
        if len(distinct) > 1:
            raw = " or ".join(distinct)
            tail = " ".join(candidate[4] for candidate in nearby if candidate[4])
            rows[index] = (str(number), "", True, _clean_text(f"候選答案：{raw} {tail}"))
        elif len(nearby) == 1:
            candidate = nearby[0]
            rows[index] = (str(number), candidate[3], False, candidate[4])
    return rows


def import_answers(
    path: str | Path,
    question_rows: list[dict[str, Any]] | None = None,
    question_pdf_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Parse answer rows, using question types when a paired source is available.

    The original one-argument call remains supported. Callers with separately
    named uploads should pass parsed ``question_rows`` or ``question_pdf_path``
    so answer-key validation does not depend on filenames.
    """
    question_types: dict[tuple[str, str], str] = {}
    paired_rows = question_rows
    if paired_rows is None and question_pdf_path is not None:
        paired_rows = import_questions(question_pdf_path)
    if paired_rows is None:
        answer_path = Path(path)
        if _ANSWER_DOC_MARKER in answer_path.name:
            sibling_question_path = answer_path.with_name(answer_path.name.replace(_ANSWER_DOC_MARKER, _QUESTION_DOC_MARKER))
            if sibling_question_path.exists():
                paired_rows = import_questions(sibling_question_path)
    if paired_rows is not None:
        question_types = {
            (question["section"], question["number"]): question["question_type"]
            for question in paired_rows
            if question.get("section") and question.get("number") and question.get("question_type")
        }
    doc = fitz.open(str(path))
    pathology_layout = any("病理學block" in doc[index].get_text() for index in range(min(5, len(doc))))
    answers: list[dict[str, Any]] = []
    current_section = ""
    current_type: str | None = None
    active: dict[str, Any] | None = None

    def finish_active() -> None:
        nonlocal active
        if active is None:
            return
        explanation = _clean_text(" ".join(active["_explanation_parts"]))
        answer_key = active["answer_key"]
        review = bool(active.get("_needs_review"))
        if not active["section"] or not active["number"] or not answer_key:
            review = True
        if not re.fullmatch(r"(?:[A-E]{1,5}(?:/[A-E]{1,5})?|[A-E]\([A-E]{1,5}\)|O|X|無|無解|全|台:[^ ]+|美:[^ ]+)", answer_key, re.IGNORECASE):
            review = True
        if answer_key in {"無解", "全"}:
            # Keep the printed key, but flag nonstandard/non-gradeable forms.
            review = True
        question_type = active.get("_question_type")
        if question_type in {"是非題", "true_false"}:
            if answer_key not in {"O", "X"}:
                review = True
        elif question_type in {"多選題", "multiple_select"}:
            # The parser keeps the full key, but the current practice UI
            # cannot grade multiple correct choices as one answer.
            review = True
        elif question_type in {"單選題", "選擇題", "single_choice"} and answer_key and not re.fullmatch(r"[A-E]", answer_key, re.IGNORECASE):
            # Single-choice sections only accept one A-E key as confirmed.
            # Slash-separated keys, concatenated letters, and O/X require review.
            review = True
        elif question_type not in {"問答題", "essay"} and answer_key and not re.fullmatch(r"[A-E]", answer_key, re.IGNORECASE):
            # When neither PDF supplies a usable type label, don't confirm
            # concatenated keys or O/X without knowing the question format.
            review = True
        source_parts = []
        for part in [active.get("_source_answer_text", answer_key), answer_key, explanation]:
            if part and part not in source_parts:
                source_parts.append(part)
        answers.append({
            "number": active["number"],
            "local_number": active["number"],
            "section": active["section"],
            "answer_key": answer_key,
            "explanation": explanation,
            "source_page": active["source_page"],
            "source_text": "\n".join([active["number"], *source_parts]),
            "needs_review": review,
        })
        active = None

    for page_index, page in enumerate(doc):
        page_no = page_index + 1
        lines, _ = _page_items(page)
        detected_section = _section_from_page(lines, current_section)
        if detected_section and detected_section != current_section:
            finish_active()
            current_section = detected_section
            current_type = None
        row_by_line = _answer_rows(lines, pathology_layout=pathology_layout)
        for index, line in enumerate(lines):
            text = line["text"]
            if not text:
                continue
            line_type = _question_type_from_text(text)
            if line_type:
                if line_type != current_type:
                    finish_active()
                    current_type = line_type
                if "簡答題詳解" in text:
                    active = {
                        "number": "1",
                        "answer_key": "",
                        "section": _effective_section(current_section, "問答題"),
                        "source_page": page_no,
                        "_explanation_parts": [],
                        "_needs_review": True,
                        "_essay": True,
                    }
                continue
            row = row_by_line.get(index)
            if row:
                finish_active()
                number, answer_key, ambiguous, answer_cell_tail = row
                section = _effective_section(current_section, current_type)
                question_type = question_types.get((section, number), current_type)
                active = {
                    "number": number,
                    "answer_key": answer_key,
                    "section": section,
                    "source_page": page_no,
                    "_explanation_parts": [answer_cell_tail] if answer_cell_tail and not ambiguous else [],
                    "_needs_review": ambiguous,
                    "_source_answer_text": answer_cell_tail if ambiguous else answer_key,
                    "_question_type": question_type,
                }
                continue
            if current_type == "問答題" and _SINGLE_INTEGER_RE.fullmatch(text) and 68 <= line["x"] <= 125:
                finish_active()
                active = {
                    "number": text,
                    "answer_key": "",
                    "section": _effective_section(current_section, current_type),
                    "source_page": page_no,
                    "_explanation_parts": [],
                    "_needs_review": True,
                    "_essay": True,
                }
                continue
            if _is_page_chrome(text) or text in {"題號", "答案", "詳解"} or text.startswith("【出處】"):
                if active is not None and text.startswith("【出處】") and line["x"] >= 150:
                    _append_text(active["_explanation_parts"], text)
                continue
            if active is not None and (line["x"] >= 160 or active.get("_essay") and line["x"] >= 60):
                _append_text(active["_explanation_parts"], text)

    finish_active()
    doc.close()

    duplicates: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    for index, answer in enumerate(answers):
        duplicates[(answer["section"], answer["number"])].append(index)
    deduplicated: list[dict[str, Any]] = []
    for indexes in duplicates.values():
        records = [answers[index] for index in indexes]
        if len(records) == 1:
            deduplicated.append(records[0])
            continue
        keys = {record["answer_key"] for record in records}
        explanations = []
        for record in records:
            if record["explanation"] and record["explanation"] not in explanations:
                explanations.append(record["explanation"])
        merged = dict(records[0])
        merged["explanation"] = " ".join(explanations)
        merged["source_text"] = "\n".join(part for part in [merged["number"], merged["answer_key"], merged["explanation"]] if part)
        if len(keys) > 1:
            merged["answer_key"] = ""
            merged["needs_review"] = True
            merged["source_text"] = "\n".join(record["source_text"] for record in records)
        else:
            # Duplicate rows may be an extraction/layout artifact, but keep
            # the source discrepancy visible for a person to verify.
            merged["needs_review"] = True
        deduplicated.append(merged)
    return deduplicated


__all__ = ["import_questions", "import_answers"]
