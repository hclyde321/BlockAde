"""Loopback-only stdlib HTTP server for the medical lecture course app."""

from __future__ import annotations

import argparse
import json
import mimetypes
import math
import os
import re
import tempfile
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import db
import audio_merge
import handouts
import jobs
import openai_settings
import pbl_api
import transcription_quality
import medical_review
import match as match_module
from openai_match import DEFAULT_QUESTIONS_PER_RUN, MAX_QUESTIONS_PER_RUN, OpenAIMatchError


MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_AUDIO_BYTES = 750 * 1024 * 1024
MAX_PDF_BYTES = 50 * 1024 * 1024
ALLOWED_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".mp4", ".mov", ".webm"}
ALLOWED_MATCH_STATUSES = {"pending_confirmation", "confirmed", "disabled", "unmatched"}
OPTION_KEYS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _course_or_404(course_id: str) -> dict[str, Any]:
    course = db.get_course(course_id)
    if not course:
        raise ApiError(404, "找不到這堂課。")
    return course


def _normalize_options(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 12:
        raise ApiError(400, "options 必須是最多 12 個選項的陣列。")
    options: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            key = str(item.get("key", OPTION_KEYS[index])).strip().upper()
            text = str(item.get("text", "")).strip()
        else:
            key = OPTION_KEYS[index]
            text = str(item).strip()
        if not key or len(key) > 12 or len(text) > 8000:
            raise ApiError(400, "選項格式錯誤或文字過長。")
        options.append({"key": key, "text": text})
    if len({option["key"] for option in options}) != len(options):
        raise ApiError(400, "選項代碼不可重複。")
    return options


def _question_input(value: dict[str, Any]) -> dict[str, Any]:
    number = str(value.get("number", "")).strip()
    stem = str(value.get("stem", "")).strip()
    if not stem or len(stem) > 30000:
        raise ApiError(400, "題目 stem 不能空白，且最多 30000 個字元。")
    if len(number) > 100:
        raise ApiError(400, "題號最多 100 個字元。")
    correct_option = value.get("correct_option", value.get("answer_key"))
    if correct_option is not None:
        correct_option = str(correct_option).strip().upper()
        options = _normalize_options(value.get("options", []))
        if options and correct_option not in {item["key"] for item in options}:
            raise ApiError(400, "正解代碼必須符合其中一個選項。")
    else:
        options = _normalize_options(value.get("options", []))
    explanation = str(value.get("explanation", ""))
    if len(explanation) > 50000:
        raise ApiError(400, "解析最多 50000 個字元。")
    source_page = value.get("source_page")
    if source_page is not None:
        try:
            source_page = int(source_page)
            if source_page < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise ApiError(400, "source_page 必須是大於 0 的頁碼。")
    return {
        "section": str(value.get("section", ""))[:200],
        "number": number or "?",
        "stem": stem,
        "options": options,
        "source_page": source_page,
        "source_text": str(value.get("source_text", ""))[:50000],
        "needs_review": bool(value.get("needs_review", False)),
        "correct_option": correct_option,
        "explanation": explanation,
        "answer_source_page": value.get("answer_source_page"),
        "answer_source_text": str(value.get("answer_source_text", ""))[:50000],
    }


def _segment_input(value: dict[str, Any]) -> dict[str, Any]:
    text = str(value.get("text", "")).strip()
    if not text or len(text) > 20000:
        raise ApiError(400, "逐字稿 text 不能空白，且最多 20000 個字元。")
    try:
        start = float(value["start"])
        end = float(value["end"])
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, "逐字稿需要以秒為單位的 start 和 end。")
    if start < 0 or end < start or end > 24 * 60 * 60:
        raise ApiError(400, "逐字稿時間範圍無效。")
    return {"start_ms": round(start * 1000), "end_ms": round(end * 1000), "text": text}


def _safe_original_filename(value: str) -> str:
    normalized = value.replace("\\", "/").split("/")[-1].strip().replace("\x00", "")
    return normalized[:255] or "upload"


def _asset_extension(kind: str, filename: str, content_type: str) -> str:
    extension = Path(filename).suffix.lower()
    if kind in {"questions", "answers"}:
        if extension and extension != ".pdf":
            raise ApiError(415, "題目與答案解析檔只接受 PDF。")
        if content_type and content_type not in {"application/pdf", "application/octet-stream", "binary/octet-stream"}:
            raise ApiError(415, "題目與答案解析檔只接受 PDF。")
        return ".pdf"
    if extension:
        if extension not in ALLOWED_AUDIO_EXTS:
            raise ApiError(415, "錄音請使用 MP3、M4A、WAV、FLAC、AAC、OGG 或 MP4。")
        return extension
    mime_extensions = {
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/wav": ".wav",
        "audio/x-wav": ".wav", "audio/flac": ".flac", "audio/aac": ".aac", "audio/ogg": ".ogg",
        "video/mp4": ".mp4", "application/octet-stream": ".m4a", "binary/octet-stream": ".m4a",
    }
    extension = mime_extensions.get(content_type)
    if not extension:
        raise ApiError(415, "無法判斷錄音格式；請以 X-Filename 或 Content-Type 提供檔案格式。")
    return extension


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "MedicalCourseLocal/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep standard access logs useful without printing request bodies or secrets.
        super().log_message(format, *args)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Content-Length 無效。")
        if length <= 0:
            raise ApiError(400, "請提供 JSON request body。")
        if length > MAX_JSON_BYTES:
            raise ApiError(413, "JSON request body 超過 2 MB 限制。")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "request body 必須是 UTF-8 JSON。")
        if not isinstance(payload, dict):
            raise ApiError(400, "request body 必須是 JSON 物件。")
        return payload

    def _parts(self) -> list[str]:
        path = urllib.parse.urlsplit(self.path).path
        return [urllib.parse.unquote(item) for item in path.split("/") if item]

    def _dispatch(self) -> None:
        parts = self._parts()
        if parts[:1] == ["api"]:
            self._dispatch_api(parts[1:])
            return
        if self.command in {"GET", "HEAD"}:
            self._serve_static()
            return
        raise ApiError(404, "找不到這個路徑。")

    def _security_check(self) -> None:
        """Reject DNS-rebinding Host values and cross-origin browser writes."""
        hosts = self.headers.get_all("Host", [])
        expected_port = int(getattr(self.server, "server_port", 8000))
        if len(hosts) != 1:
            raise ApiError(400, "請使用本機服務網址連線。")
        try:
            authority = urllib.parse.urlsplit("//" + hosts[0].strip())
            host = (authority.hostname or "").lower()
            port = authority.port
        except ValueError:
            raise ApiError(400, "Host header 無效。")
        if (host not in {"127.0.0.1", "localhost"} or port != expected_port
                or authority.username or authority.password or authority.path or authority.query or authority.fragment):
            raise ApiError(403, "此服務只接受設定連接埠上的 localhost 或 127.0.0.1。")

        if self.command not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        fetch_site = self.headers.get("Sec-Fetch-Site", "").strip().lower()
        if fetch_site == "cross-site":
            raise ApiError(403, "已拒絕跨網站寫入要求。")
        origin = self.headers.get("Origin")
        if origin is None:
            return  # CLI clients such as curl commonly omit Origin.
        try:
            parsed = urllib.parse.urlsplit(origin.strip())
            origin_host = (parsed.hostname or "").lower()
            origin_port = parsed.port
        except ValueError:
            raise ApiError(403, "Origin 不符合本機服務來源。")
        if (parsed.scheme != "http" or origin_host != host
                or origin_port != expected_port or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ApiError(403, "已拒絕非本機同源寫入要求。")

    def _dispatch_api(self, parts: list[str]) -> None:
        if parts == ["pbl", "matches"]:
            if self.command == "GET":
                self._send_json(200, {"weeks": pbl_api.saved()})
                return
            if self.command == "POST":
                try:
                    result = pbl_api.analyze(self._json_body())
                except ValueError as exc:
                    raise ApiError(400, str(exc)) from None
                self._send_json(200, result)
                return
        if parts == ["settings", "openai"]:
            if self.command == "GET":
                self._send_json(200, openai_settings.status())
                return
            if self.command == "POST":
                try:
                    result = openai_settings.configure(self._json_body())
                except ValueError as exc:
                    raise ApiError(400, str(exc)) from None
                self._send_json(200, result)
                return
            if self.command == "DELETE":
                self._send_json(200, openai_settings.clear())
                return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if query.get("subject_id"):
            self._subject_id(query["subject_id"][0])
        if parts == ["subjects"] and self.command == "GET":
            self._send_json(200, {"subjects": db.list_subjects()})
            return
        if parts == ["wrong-questions"] and self.command == "GET":
            rows = db.wrong_questions(query.get("subject_id", [None])[0], query.get("course_id", [None])[0],
                                      query.get("history", ["0"])[0] == "1")
            for row in rows:
                for field in ("answer_key", "correct_option", "explanation", "answer_source_text"):
                    row.pop(field, None)
            self._send_json(200, {"questions": rows})
            return
        if parts == ["banks"]:
            if self.command == "GET":
                self._send_json(200, {"banks": [self._bank_payload(b["id"]) for b in db.list_banks(query.get("subject_id", [None])[0])]})
                return
            if self.command == "POST":
                body = self._json_body()
                title = str(body.get("title", "")).strip()
                if not title or len(title) > 200:
                    raise ApiError(400, "題庫名稱不能空白，且最多 200 字。")
                subject_id = self._subject_id(body.get("subject_id", "clinical"))
                bank = db.create_bank(title, subject_id)
                self._send_json(201, self._bank_payload(bank["id"]))
                return
        if len(parts) >= 2 and parts[0] == "banks":
            self._bank_route(parts[1], parts[2:])
            return
        if parts == ["health"] and self.command == "GET":
            self._send_json(200, {"ok": True})
            return
        if parts == ["courses"] and self.command == "GET":
            self._send_json(200, {"courses": db.list_courses()})
            return
        if parts == ["courses"] and self.command == "POST":
            body = self._json_body()
            title = str(body.get("title", "")).strip()
            if not title or len(title) > 200:
                raise ApiError(400, "課程 title 不能空白，且最多 200 個字元。")
            minutes = body.get("transcription_chunk_minutes", 5)
            if type(minutes) is not int or minutes not in (5, 10):
                raise ApiError(400, "轉錄分段僅支援 5 或 10 分鐘。")
            timetable_key = body.get("timetable_key")
            if timetable_key is not None and (not isinstance(timetable_key,str) or not timetable_key or len(timetable_key)>200):
                raise ApiError(400, "課表連結無效。")
            course = db.create_course(title, self._subject_id(body.get("subject_id", "clinical")), minutes, timetable_key)
            self._send_json(201, course)
            return
        if len(parts) >= 2 and parts[0] == "courses":
            self._course_route(parts[1], parts[2:])
            return
        if len(parts) == 2 and parts[0] == "segments" and self.command == "PATCH":
            self._patch_segment(parts[1])
            return
        if len(parts) == 2 and parts[0] == "questions":
            if self.command == "PATCH":
                self._patch_question(parts[1])
                return
            raise ApiError(405, "此題目路徑不支援這個 HTTP method。")
        if len(parts) == 3 and parts[0] == "questions" and parts[2] == "match" and self.command == "PATCH":
            self._patch_match(parts[1])
            return
        if len(parts) == 3 and parts[0] == "questions" and parts[2] == "attempt" and self.command == "POST":
            self._attempt(parts[1])
            return
        raise ApiError(404, "找不到 API 路徑。")

    def _course_route(self, course_id: str, tail: list[str]) -> None:
        course = _course_or_404(course_id)
        if not tail and self.command == "DELETE":
            with jobs._lock:
                if course_id in jobs._active_by_course:
                    raise ApiError(409, "課程正在處理中，請完成後再刪除。")
                with db.connect() as conn:
                    conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
            self._send_json(200, {"ok": True})
            return
        if tail == ["medical-review"]:
            if self.command == "GET":
                self._send_json(200, {**medical_review.readiness(), "revisions": medical_review.history(course_id)})
                return
            if self.command == "POST":
                body = self._json_body()
                try:
                    job_id = medical_review.start(course_id, body.get("mode"), body.get("start_seconds", 0), body.get("context", ""))
                except (ValueError, jobs.JobBusyError) as exc:
                    raise ApiError(409, str(exc)) from None
                self._send_json(202, {"job": db.latest_job(course_id) or {"id": job_id}})
                return
        if len(tail) == 2 and tail[0] == "medical-review" and self.command == "PATCH":
            body = self._json_body()
            try:
                with jobs._lock:
                    if course_id in jobs._active_by_course:
                        raise ValueError("課程正在處理中，完成後即可採用建議。")
                    medical_review.decide(course_id, tail[1], body.get("action"))
            except ValueError as exc:
                raise ApiError(409, str(exc)) from None
            self._send_json(200, {"ok": True})
            return
        if tail == ["transcription-review"] and self.command == "GET":
            record = transcription_quality.latest_record(db.DATA_DIR / "transcription_raw", course_id)
            if record is None:
                self._send_json(200, {"available": False})
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            kind = query.get("format", [None])[0]
            if kind:
                if kind not in {"txt", "vtt", "srt", "json"}:
                    raise ApiError(400, "不支援的匯出格式。")
                text = (json.dumps({k: v for k, v in record.items() if k not in {"source", "course_id"}},
                                   ensure_ascii=False, indent=2) if kind == "json" else
                        transcription_quality.subtitle(transcription_quality.raw_segments(record), kind,
                                                       record.get("source_offset_ms", 0)))
                self._send_json(200, {"text": text, "filename": f"whisper-raw.{kind}"})
                return
            report = record.get("quality", {})
            self._send_json(200, {"available": True, "offset_ms": record.get("source_offset_ms", 0),
                "flags": report.get("flags", []), "reviews": [
                    {k: v for k, v in review.items() if k not in {"raw_outputs", "error"}}
                    for review in report.get("reviews", [])],
                "review_limit": report.get("review_limit", 0),
                "start_ms": 0,
                "end_ms": round(record["duration_seconds"] * 1000) if "duration_seconds" in record else
                    max((s["end_ms"] for s in record["segments"]), default=0)})
            return
        if tail == ["audio-parts"] and self.command == "POST":
            self._upload_audio_part(course_id)
            return
        if tail == ["audio-parts", "finalize"] and self.command == "POST":
            try:
                job_id = jobs.start_audio_finalize(course_id)
            except jobs.JobBusyError as exc:
                raise ApiError(409, str(exc)) from None
            except ValueError as exc:
                raise ApiError(409, str(exc)) from None
            self._send_json(202, {"job": db.latest_job(course_id) or {"id": job_id, "status": "queued"}})
            return
        if len(tail) == 4 and tail[0] == "handouts" and tail[2] == "pages" and self.command == "GET":
            item = db.get_handout(course_id, tail[1])
            if not item:
                raise ApiError(404, "找不到講義。")
            try:
                page_number = int(tail[3])
            except ValueError:
                raise ApiError(400, "頁碼無效。") from None
            if not 1 <= page_number <= item["page_count"]:
                raise ApiError(404, "找不到此頁。")
            path = Path(item["path"]).resolve()
            try:
                path.relative_to((db.DATA_DIR / course_id / "handouts").resolve())
            except ValueError:
                raise ApiError(403, "講義路徑無效。") from None
            if not path.is_file():
                raise ApiError(404, "找不到講義檔案。")
            import fitz
            with fitz.open(path) as document:
                page = document[page_number - 1]
                payload = page.get_pixmap(matrix=fitz.Matrix(1200 / page.rect.width, 1200 / page.rect.width), alpha=False).tobytes("png")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(payload)
            return
        if tail == ["handouts"]:
            if self.command == "GET":
                self._send_json(200, {"handouts": db.list_handouts(course_id)})
                return
            if self.command in {"PUT", "POST"}:
                self._upload_handout(course_id)
                return
        if len(tail) == 2 and tail[0] == "handouts" and self.command == "DELETE":
            self._delete_handout(course_id, tail[1])
            return
        if not tail and self.command == "PATCH":
            title = self._json_body().get("title")
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
                raise ApiError(400, "課程名稱不能空白，且最多 200 個字元。")
            with db.connect() as conn:
                conn.execute("UPDATE courses SET title=?,updated_at=? WHERE id=?",
                             (title.strip(), db.now_iso(), course_id))
            self._send_json(200, db.get_course(course_id))
            return
        if tail == ["retranscribe-range"] and self.command == "POST":
            body = self._json_body()
            start, end = body.get("start_seconds"), body.get("end_seconds")
            if (type(start) not in (int, float) or type(end) not in (int, float)
                    or not math.isfinite(start) or not math.isfinite(end)):
                raise ApiError(400, "請提供有效的開始與結束秒數。")
            try:
                job_id = jobs.start_range_job(course_id, start, end)
            except jobs.JobBusyError as exc:
                raise ApiError(409, str(exc)) from None
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
            self._send_json(202, {"job": db.latest_job(course_id) or {"id": job_id, "status": "queued"}})
            return
        if tail == ["transcription-settings"] and self.command == "PATCH":
            minutes = self._json_body().get("transcription_chunk_minutes")
            if type(minutes) is not int or minutes not in (5, 10):
                raise ApiError(400, "轉錄分段僅支援 5 或 10 分鐘。")
            with jobs._lock:
                job = db.latest_job(course_id)
                if job and job["status"] in {"queued", "running"}:
                    raise ApiError(409, "正在處理課程，請完成後再調整分段。")
                with db.connect() as conn:
                    conn.execute("UPDATE courses SET transcription_chunk_minutes=?,updated_at=? WHERE id=?",
                                 (minutes, db.now_iso(), course_id))
            self._send_json(200, db.get_course(course_id))
            return
        if tail == ["history"] and self.command == "GET":
            self._send_json(200, db.learning_history(course_id))
            return
        if tail == ["restart"] and self.command == "POST":
            self._send_json(200, {"learning": db.restart_learning(course_id)})
            return
        if tail == ["listening"] and self.command == "POST":
            body = self._json_body()
            try:
                start, end = float(body["start_seconds"]), float(body["end_seconds"])
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end-start > 120:
                    raise ValueError()
                learning = db.record_listening(course_id, start, end, body.get("round_id"))
            except (KeyError, TypeError, ValueError) as exc:
                message = "學習輪次已更新，請重新整理課程。" if "round" in str(exc).lower() else "聽課區間無效，請確認錄音已處理完成；單次回報最多 120 秒。"
                raise ApiError(400, message) from None
            self._send_json(200, {"learning": learning})
            return
        if not tail and self.command == "GET":
            payload = db.course_payload(course_id)
            self._send_json(200, payload)
            return
        if tail == ["process"] and self.command == "POST":
            try:
                job_id = jobs.start_job(course_id)
            except jobs.JobBusyError as exc:
                raise ApiError(409, str(exc)) from None
            job = db.latest_job(course_id)
            self._send_json(202, {"job": job or {"id": job_id, "status": "queued"}})
            return
        if tail == ["sync-bank"] and self.command == "POST":
            added = db.sync_subject_questions(course_id)
            self._send_json(200, {"added": added, "total": len(db.list_questions(course_id))})
            return
        if tail == ["rematch"] and self.command == "POST":
            if not db.list_segments(course_id):
                raise ApiError(409, "這堂課還沒有逐字稿，請先新增逐字稿或完成錄音轉錄。")
            active_job = jobs.get_job(course_id)
            if active_job and active_job.get("status") in {"queued", "running"}:
                raise ApiError(409, "課程仍在處理中，請完成後再重新對位。")
            db.sync_subject_questions(course_id)
            if not db.list_questions(course_id):
                raise ApiError(409, "這個科目還沒有可用題庫，請先到題庫管理匯入題目。")
            match_module.refresh_course_matches(course_id)
            matches = db.list_matches(course_id)
            self._send_json(200, {
                "matched": sum(item["status"] == "confirmed" for item in matches),
                "unmatched": sum(item["status"] == "unmatched" for item in matches),
            })
            return
        if tail == ["analyze-matches"] and self.command == "POST":
            try:
                body_length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                raise ApiError(400, "Content-Length 無效。")
            body = self._json_body() if body_length > 0 else {}
            try:
                max_questions = int(body.get("max_questions", DEFAULT_QUESTIONS_PER_RUN))
            except (TypeError, ValueError):
                raise ApiError(400, "max_questions 必須是整數。")
            if not 1 <= max_questions <= MAX_QUESTIONS_PER_RUN:
                raise ApiError(400, f"max_questions 必須介於 1 到 {MAX_QUESTIONS_PER_RUN}。")
            db.sync_subject_questions(course_id)
            if not db.list_questions(course_id):
                raise ApiError(409, "這堂課還沒有題目，請先輸入或匯入題庫。")
            if not db.list_segments(course_id) and not db.list_handouts(course_id):
                raise ApiError(409, "請先新增逐字稿或上傳講義。")
            try:
                job_id = jobs.start_match_analysis(course_id, max_questions)
            except OpenAIMatchError as exc:
                raise ApiError(503, str(exc)) from None
            except jobs.JobBusyError as exc:
                raise ApiError(409, str(exc)) from None
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
            job = db.latest_job(course_id)
            self._send_json(202, {"job": job or {"id": job_id, "kind": "match_analysis", "status": "queued"}})
            return
        if tail == ["job"] and self.command == "GET":
            job = jobs.get_job(course_id)
            if not job:
                raise ApiError(404, "這堂課尚未開始處理工作。")
            self._send_json(200, {"job": job})
            return
        if tail == ["audio"] and self.command in {"GET", "HEAD"}:
            self._audio(course_id)
            return
        if tail == ["playback"] and self.command == "PATCH":
            body = self._json_body()
            try:
                position = float(body["position_seconds"])
                rate = float(body["playback_rate"]) if "playback_rate" in body else None
            except (KeyError, TypeError, ValueError):
                raise ApiError(400, "playback 需要 position_seconds 數值。")
            if position < 0 or position > 24 * 60 * 60 or (rate is not None and not 0.25 <= rate <= 4):
                raise ApiError(400, "播放位置或倍速超出允許範圍。")
            is_playing = body.get("is_playing")
            if is_playing is not None and not isinstance(is_playing, bool):
                raise ApiError(400, "is_playing 必須是布林值。")
            db.update_playback(course_id, position, rate, is_playing)
            self._send_json(200, {"playback": db.course_payload(course_id)["playback"]})
            return
        if tail == ["segments"] and self.command == "POST":
            body = self._json_body()
            raw_segments = body.get("segments") if isinstance(body.get("segments"), list) else [body]
            if not raw_segments or len(raw_segments) > 5000:
                raise ApiError(400, "segments 必須包含 1 到 5000 段。")
            normalized = [_segment_input(item) for item in raw_segments if isinstance(item, dict)]
            if len(normalized) != len(raw_segments):
                raise ApiError(400, "每個 segment 都必須是 JSON 物件。")
            ids = db.add_segments(course_id, normalized, source="manual")
            match_module.refresh_course_matches(course_id)
            db.set_course_status(course_id, "ready")
            output = []
            for segment_id in ids:
                item = db.get_segment(segment_id)
                output.append({"id": item["id"], "start": item["start_ms"] / 1000,
                               "end": item["end_ms"] / 1000, "text": item["text"]})
            self._send_json(201, {"segments": output})
            return
        if tail == ["questions"] and self.command == "POST":
            body = self._json_body()
            raw_questions = body.get("questions") if isinstance(body.get("questions"), list) else [body]
            if not raw_questions or len(raw_questions) > 1000:
                raise ApiError(400, "questions 必須包含 1 到 1000 題。")
            ids = []
            for item in raw_questions:
                if not isinstance(item, dict):
                    raise ApiError(400, "每個 question 都必須是 JSON 物件。")
                question = _question_input(item)
                ids.append(db.add_question(course_id, question))
            match_module.refresh_course_matches(course_id)
            db.set_course_status(course_id, "ready")
            self._send_json(201, {"questions": [next(q for q in db.course_payload(course_id)["questions"] if q["id"] == qid) for qid in ids]})
            return
        if len(tail) == 2 and tail[0] == "assets" and self.command == "PUT":
            self._upload_asset(course, tail[1])
            return
        raise ApiError(404, "找不到這堂課的 API 路徑。")

    def _handout_job_guard(self, course_id: str) -> None:
        latest = db.latest_job(course_id)
        if (course_id in jobs._active_by_course
                or latest and latest["status"] in {"queued", "running"}):
            raise ApiError(409, "這堂課正在處理，請等候完成後再變更講義。")

    def _upload_handout(self, course_id: str) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Content-Length 無效。")
        if length <= 0:
            raise ApiError(400, "上傳檔案不可為空。")
        if length > MAX_PDF_BYTES:
            raise ApiError(413, "講義 PDF 超過 50 MB 限制。")
        filename = _safe_original_filename(urllib.parse.unquote(self.headers.get("X-Filename", "講義.pdf")))
        content_type = self.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip().lower()
        if Path(filename).suffix.lower() != ".pdf" or content_type not in {
            "application/pdf", "application/octet-stream", "binary/octet-stream"
        }:
            raise ApiError(415, "老師講義只接受 PDF 檔案。")
        try:
            uuid.UUID(course_id)
        except ValueError:
            raise ApiError(500, "課程 ID 格式無效，無法安全儲存檔案。")
        with jobs._lock:
            self._handout_job_guard(course_id)
        folder = (db.DATA_DIR / course_id / "handouts").resolve()
        try:
            folder.relative_to(db.DATA_DIR)
        except ValueError:
            raise ApiError(400, "講義檔案目錄不安全。")
        folder.mkdir(parents=True, exist_ok=True)
        handout_id = db.new_id()
        destination = folder / f"handout-{handout_id}.pdf"
        temp_path: Path | None = None
        installed = False
        try:
            with tempfile.NamedTemporaryFile(prefix=".handout-", suffix=".tmp", dir=folder, delete=False) as temp:
                temp_path = Path(temp.name)
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ApiError(400, "上傳連線在講義傳完前中斷。")
                    temp.write(chunk)
                    remaining -= len(chunk)
                temp.flush()
                os.fsync(temp.fileno())
            with temp_path.open("rb") as uploaded:
                if uploaded.read(5) != b"%PDF-":
                    raise ApiError(415, "檔案內容不是有效的 PDF。")
            try:
                pages = handouts.extract_pdf(temp_path)
            except handouts.HandoutError as exc:
                raise ApiError(422, str(exc)) from None
            with jobs._lock:
                self._handout_job_guard(course_id)
                os.replace(temp_path, destination)
                installed = True
                item = db.add_handout(course_id, destination, filename, content_type, length, pages, handout_id)
        except Exception:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            if installed:
                destination.unlink(missing_ok=True)
            raise
        self._send_json(201, {"handout": item, "handouts": db.list_handouts(course_id)})

    def _delete_handout(self, course_id: str, handout_id: str) -> None:
        with jobs._lock:
            self._handout_job_guard(course_id)
            item = db.get_handout(course_id, handout_id)
            if item is None:
                raise ApiError(404, "找不到這份講義。")
            path = Path(item["path"]).resolve()
            try:
                path.relative_to((db.DATA_DIR / course_id / "handouts").resolve())
            except ValueError:
                raise ApiError(500, "講義路徑無效，已停止刪除。")
            db.delete_handout(course_id, handout_id)
            path.unlink(missing_ok=True)
        self._send_json(200, {"handouts": db.list_handouts(course_id)})

    def _subject_id(self, value: Any) -> str:
        if not isinstance(value, str) or value not in {s["id"] for s in db.list_subjects()}:
            raise ApiError(400, "請選擇有效科目。")
        return str(value)

    def _bank_payload(self, bank_id: str) -> dict[str, Any]:
        bank = db.get_bank(bank_id)
        if not bank:
            raise ApiError(404, "找不到題庫。")
        bank["assets"] = {
            kind: None if not (asset := db.get_bank_asset(bank_id, kind)) else
            {k: asset[k] for k in ("filename", "content_type", "size_bytes")}
            for kind in ("questions", "answers")
        }
        bank["question_count"] = len(db.list_bank_questions(bank_id))
        bank["status"] = "ready" if bank["question_count"] else "draft"
        return bank

    def _bank_route(self, bank_id: str, tail: list[str]) -> None:
        bank = self._bank_payload(bank_id)
        if not tail and self.command == "GET":
            bank["questions"] = db.list_bank_questions(bank_id)
            self._send_json(200, bank)
            return
        if len(tail) == 2 and tail[0] == "assets" and self.command == "PUT":
            if tail[1] not in {"questions", "answers"}:
                raise ApiError(400, "題庫僅接受題目與答案解析 PDF。")
            with jobs.bank_asset_lock:
                self._upload_asset(bank, tail[1], bank=True)
            return
        if tail == ["import"] and self.command == "POST":
            try:
                jobs.import_bank(bank_id)
            except (RuntimeError, ValueError) as exc:
                raise ApiError(400, str(exc)) from None
            self._send_json(200, self._bank_payload(bank_id))
            return
        raise ApiError(404, "找不到題庫 API。")

    def _upload_asset(self, course: dict[str, Any], kind: str, bank: bool = False) -> None:
        if kind not in {"audio", "questions", "answers"}:
            raise ApiError(404, "asset 類型必須是 audio、questions 或 answers。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Content-Length 無效。")
        limit = MAX_AUDIO_BYTES if kind == "audio" else MAX_PDF_BYTES
        if length <= 0:
            raise ApiError(400, "上傳檔案不可為空。")
        if length > limit:
            max_mb = limit // (1024 * 1024)
            raise ApiError(413, f"檔案超過 {max_mb} MB 限制。")
        filename = _safe_original_filename(urllib.parse.unquote(self.headers.get("X-Filename", "upload")))
        content_type = self.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip().lower()
        extension = _asset_extension(kind, filename, content_type)

        course_id = course["id"]
        if bank and db.list_bank_questions(course_id):
            raise ApiError(409, "題庫已匯入；請新增另一份題庫，以保留課程與作答紀錄。")
        if not bank and kind in {"questions", "answers"} and db.list_questions(course_id):
            raise ApiError(409, "這堂課已有題目，無法更換題庫 PDF；請建立新課程匯入新題庫。")
        if not bank and kind == "audio":
            active = jobs.get_job(course_id)
            if active and active.get("status") in {"queued", "running"}:
                raise ApiError(409, "這堂課正在處理，請等候完成後再上傳。")
            if db.list_segments(course_id):
                raise ApiError(409, "這堂課已有逐字稿，新的課堂錄音請建立新課程上傳。")
            if any(part["status"] == "pending" for part in db.list_audio_parts(course_id)):
                raise ApiError(409, "這堂課有待合併錄音，請先完成合併。")
        try:
            uuid.UUID(course_id)
        except ValueError:
            raise ApiError(500, "課程 ID 格式無效，無法安全儲存檔案。")
        folder = db.DATA_DIR / "banks" / course_id if bank else db.DATA_DIR / course_id
        folder.mkdir(parents=True, exist_ok=True)
        folder_real = folder.resolve()
        try:
            folder_real.relative_to(db.DATA_DIR)
        except ValueError:
            raise ApiError(400, "課程檔案目錄不安全。")
        # A unique destination keeps the previous asset intact until this upload
        # is fully validated and its database row has been replaced.
        destination = folder_real / f"{kind}-{uuid.uuid4().hex}{extension}"
        temp_path: Path | None = None
        installed = False
        written = 0
        old_asset = db.get_bank_asset(course_id, kind) if bank else db.get_asset(course_id, kind)
        try:
            with tempfile.NamedTemporaryFile(prefix=".upload-", suffix=".tmp", dir=folder_real, delete=False) as temp:
                temp_path = Path(temp.name)
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ApiError(400, "上傳連線在檔案傳完前中斷。")
                    temp.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
                temp.flush()
                os.fsync(temp.fileno())
            if kind in {"questions", "answers"}:
                with temp_path.open("rb") as uploaded:
                    if uploaded.read(5) != b"%PDF-":
                        raise ApiError(415, "檔案內容不是有效的 PDF。")
            os.replace(temp_path, destination)
            installed = True
            setter = db.set_bank_asset if bank else db.set_asset
            if not bank and kind == "audio":
                with jobs._lock:
                    if course_id in jobs._active_by_course or db.list_segments(course_id) or any(
                        part["status"] == "pending" for part in db.list_audio_parts(course_id)
                    ):
                        raise ApiError(409, "這堂課正在處理或已有逐字稿，請改用新增錄音功能。")
                    db.set_single_audio_asset(course_id, destination, filename, content_type, written)
            else:
                setter(course_id, kind, destination, filename, content_type, written)
        except Exception:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            if installed:
                destination.unlink(missing_ok=True)
            raise
        if old_asset:
            old_path = Path(old_asset["path"]).resolve()
            if old_path != destination:
                try:
                    old_path.relative_to(db.DATA_DIR)
                    with db.connect() as conn:
                        referenced = conn.execute(
                            "SELECT 1 FROM assets WHERE path=? UNION ALL SELECT 1 FROM bank_assets WHERE path=? LIMIT 1",
                            (str(old_path), str(old_path)),
                        ).fetchone()
                    if not referenced:
                        old_path.unlink(missing_ok=True)
                except ValueError:
                    pass
        payload = {"asset": {"kind": kind, "filename": filename,
                              "size_bytes": written, "content_type": content_type}}
        if not bank and kind == "audio":
            try:
                jobs.start_job(course_id)
                payload["job"] = jobs.get_job(course_id)
            except Exception:
                payload["processing_error"] = "錄音已儲存，但無法自動開始；請按重新處理。"
        self._send_json(201, payload)

    def _upload_audio_part(self, course_id: str) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Content-Length 無效。")
        if length <= 0:
            raise ApiError(400, "上傳檔案不可為空。")
        if length > MAX_AUDIO_BYTES:
            raise ApiError(413, "單段錄音超過 750 MB 限制。")
        filename = _safe_original_filename(urllib.parse.unquote(self.headers.get("X-Filename", "upload")))
        content_type = self.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip().lower()
        extension = _asset_extension("audio", filename, content_type)
        try:
            uuid.UUID(course_id)
        except ValueError:
            raise ApiError(500, "課程 ID 格式無效，無法安全儲存檔案。")
        folder = (db.DATA_DIR / course_id / "audio-parts").resolve()
        if not folder.is_relative_to(db.DATA_DIR):
            raise ApiError(400, "課程檔案目錄不安全。")
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"part-{uuid.uuid4().hex}{extension}"
        temp_path: Path | None = None
        installed = False
        try:
            with tempfile.NamedTemporaryFile(prefix=".upload-", suffix=extension, dir=folder, delete=False) as temp:
                temp_path = Path(temp.name)
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ApiError(400, "上傳連線在檔案傳完前中斷。")
                    temp.write(chunk)
                    remaining -= len(chunk)
                temp.flush()
                os.fsync(temp.fileno())
            try:
                duration = audio_merge.probe_audio(temp_path)
            except (RuntimeError, ValueError) as exc:
                raise ApiError(415, str(exc)) from None
            with jobs._lock:
                if course_id in jobs._active_by_course:
                    raise ApiError(409, "這堂課正在處理，請等候完成後再上傳。")
                if len(db.list_audio_parts(course_id)) >= 32:
                    raise ApiError(409, "每堂課最多 32 段錄音。")
                os.replace(temp_path, destination)
                installed = True
                db.add_audio_part(course_id, destination, filename, content_type, length, duration)
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if installed:
                destination.unlink(missing_ok=True)
            raise
        self._send_json(201, {"audio_parts": db.audio_part_metadata(course_id)})

    def _safe_asset_file(self, course_id: str, kind: str) -> tuple[dict[str, Any], Path]:
        asset = db.get_asset(course_id, kind)
        if not asset:
            raise ApiError(404, "這堂課還沒有上傳錄音。")
        path = Path(asset["path"]).resolve()
        try:
            path.relative_to(db.DATA_DIR)
        except ValueError:
            raise ApiError(500, "錄音路徑無效，已停止讀取。")
        if not path.is_file():
            raise ApiError(404, "找不到錄音檔，請重新上傳。")
        return asset, path

    def _audio(self, course_id: str) -> None:
        asset, path = self._safe_asset_file(course_id, "audio")
        size = path.stat().st_size
        range_header = self.headers.get("Range")
        start, end, status = 0, max(0, size - 1), 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            left, right = match.groups()
            if not left and not right:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not left:
                suffix = int(right)
                if suffix <= 0:
                    start = size
                else:
                    start = max(0, size - suffix)
                end = size - 1
            else:
                start = int(left)
                end = min(int(right), size - 1) if right else size - 1
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        content_length = max(0, end - start + 1)
        content_type = asset.get("content_type") or mimetypes.guess_type(asset["filename"])[0] or "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(asset['filename'])}")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as audio_file:
            audio_file.seek(start)
            remaining = content_length
            while remaining:
                chunk = audio_file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _patch_segment(self, segment_id: str) -> None:
        body = self._json_body()
        segment = db.get_segment(segment_id)
        if not segment:
            raise ApiError(404, "找不到這段逐字稿。")
        clean: dict[str, Any] = {}
        if "text" in body:
            text = str(body["text"]).strip()
            if not text or len(text) > 20000:
                raise ApiError(400, "逐字稿 text 不能空白，且最多 20000 個字元。")
            clean["text"] = text
        if "start" in body:
            clean["start"] = body["start"]
        if "end" in body:
            clean["end"] = body["end"]
        try:
            new_start = float(clean.get("start", segment["start_ms"] / 1000))
            new_end = float(clean.get("end", segment["end_ms"] / 1000))
        except (TypeError, ValueError):
            raise ApiError(400, "逐字稿時間必須是數值秒數。")
        if new_start < 0 or new_end < new_start or new_end > 24 * 60 * 60:
            raise ApiError(400, "逐字稿時間範圍無效。")
        clean["start"], clean["end"] = new_start, new_end
        with jobs._lock:
            if segment["course_id"] in jobs._active_by_course:
                raise ApiError(409, "課程正在處理中，完成後即可修改逐字稿。")
            db.patch_segment(segment_id, clean)
        self._send_json(200, {"segment": db.get_segment(segment_id)})

    def _patch_question(self, question_id: str) -> None:
        body = self._json_body()
        question = db.get_question(question_id)
        if not question:
            raise ApiError(404, "找不到這道題目。")
        clean: dict[str, Any] = {}
        for key in ("section", "number", "stem", "source_text", "source_page", "needs_review",
                    "correct_option", "answer_key", "explanation"):
            if key in body:
                clean[key] = body[key]
        if "section" in clean:
            clean["section"] = str(clean["section"])[:200]
        if "number" in clean:
            clean["number"] = str(clean["number"])[:100]
        if "needs_review" in clean and not isinstance(clean["needs_review"], bool):
            raise ApiError(400, "needs_review 必須是布林值。")
        if "stem" in clean:
            clean["stem"] = str(clean["stem"]).strip()
            if not clean["stem"] or len(clean["stem"]) > 30000:
                raise ApiError(400, "題目 stem 不能空白，且最多 30000 個字元。")
        if "options" in body:
            clean["options"] = _normalize_options(body["options"])
        key = clean.get("correct_option", clean.get("answer_key"))
        options = clean.get("options", question["options"])
        if key is None and "options" in clean:
            key = question.get("answer_key")
        if key is not None and options and str(key).strip().upper() not in {item["key"] for item in options}:
            raise ApiError(400, "正解代碼必須符合其中一個選項。")
        if "source_page" in clean and clean["source_page"] is not None:
            try:
                clean["source_page"] = int(clean["source_page"])
                if clean["source_page"] < 1:
                    raise ValueError
            except (TypeError, ValueError):
                raise ApiError(400, "source_page 必須是大於 0 的頁碼。")
        if "explanation" in clean and len(str(clean["explanation"])) > 50000:
            raise ApiError(400, "解析最多 50000 個字元。")
        if not clean:
            raise ApiError(400, "沒有可修改的題目欄位。")
        db.patch_question(question_id, clean)
        result = next(q for q in db.course_payload(question["course_id"])["questions"] if q["id"] == question_id)
        self._send_json(200, {"question": result})

    def _patch_match(self, question_id: str) -> None:
        body = self._json_body()
        question = db.get_question(question_id)
        if not question:
            raise ApiError(404, "找不到這道題目。")
        existing = db.get_match(question_id) or {}
        status = str(body.get("status", existing.get("status", "pending_confirmation")))
        if status not in ALLOWED_MATCH_STATUSES:
            raise ApiError(400, "match status 必須是 pending_confirmation、confirmed、disabled 或 unmatched。")
        question_time_ms = existing.get("question_time_ms")
        if "question_time" in body:
            try:
                question_time = float(body["question_time"])
            except (TypeError, ValueError):
                raise ApiError(400, "question_time 必須是秒數。")
            if question_time < 0 or question_time > 24 * 60 * 60:
                raise ApiError(400, "question_time 超出允許範圍。")
            question_time_ms = round(question_time * 1000)
        if status == "confirmed" and question_time_ms is None:
            raise ApiError(400, "確認題目位置前需要設定 question_time。")
        if status == "confirmed":
            answer_key = str(question.get("answer_key") or "").strip().upper()
            option_keys = {str(option.get("key", "")).strip().upper() for option in question.get("options", [])
                           if isinstance(option, dict)}
            if not answer_key or not option_keys or answer_key not in option_keys:
                raise ApiError(409, "確認出題位置前，請先校對題目選項與有效正解。")
        manual_time_only = "question_time" in body and not any(
            key in body for key in ("segment_id", "start", "end", "evidence")
        )
        segment_id = None if manual_time_only else body.get("segment_id", existing.get("segment_id"))
        start_ms = None if manual_time_only else existing.get("start_ms")
        end_ms = None if manual_time_only else existing.get("end_ms")
        if "start" in body:
            start_ms = round(float(body["start"]) * 1000)
        if "end" in body:
            end_ms = round(float(body["end"]) * 1000)
        if segment_id:
            segment = db.get_segment(str(segment_id))
            if not segment or segment["course_id"] != question["course_id"]:
                raise ApiError(400, "segment_id 必須指向同一堂課的逐字稿段落。")
            if start_ms is None:
                start_ms = segment["start_ms"]
            if end_ms is None:
                end_ms = segment["end_ms"]
        if status == "confirmed" and segment_id is None and "question_time" in body:
            question_time_ms = round(float(body["question_time"]) * 1000)
            start_ms = start_ms if start_ms is not None else question_time_ms
            end_ms = end_ms if end_ms is not None else question_time_ms
        reason = str(body.get(
            "reason", "人工設定出題時間；未附逐字稿證據。" if manual_time_only
            else existing.get("reason", "人工設定並確認。")
        ))[:2000]
        evidence = "" if manual_time_only else str(body.get("evidence", existing.get("evidence", "")))[:8000]
        match_id = db.save_match(question_id, {
            "segment_id": segment_id, "start_ms": start_ms, "end_ms": end_ms,
            "question_time_ms": question_time_ms, "status": status,
            "confidence": None if manual_time_only else existing.get("confidence"),
            "candidate_score": None if manual_time_only else existing.get("candidate_score"),
            "reason": reason, "evidence": evidence,
            "provider": "manual" if status == "confirmed" or manual_time_only else existing.get("provider", "local_lexical"),
        })
        match = next(item for item in db.list_matches(question["course_id"]) if item["id"] == match_id)
        self._send_json(200, {"match": {
            "id": match["id"], "question_id": question_id,
            "start": None if match["start_ms"] is None else match["start_ms"] / 1000,
            "end": None if match["end_ms"] is None else match["end_ms"] / 1000,
            "question_time": None if match["question_time_ms"] is None else match["question_time_ms"] / 1000,
            "status": match["status"], "confidence": match["confidence"],
            "candidate_score": match["candidate_score"], "reason": match["reason"],
            "evidence": match["evidence"], "segment_id": match["segment_id"], "provider": match["provider"],
        }})

    def _attempt(self, question_id: str) -> None:
        body = self._json_body()
        question = db.get_question(question_id)
        if not question:
            raise ApiError(404, "找不到這道題目。")
        answer_key = question.get("answer_key")
        if not answer_key:
            raise ApiError(409, "這道題目尚無可用正解，請先校對答案解析 PDF 或人工補上答案。")
        selected = str(body.get("selected_option", "")).strip().upper()
        if not selected:
            raise ApiError(400, "請提供 selected_option。")
        options = question.get("options", [])
        if options and selected not in {item["key"] for item in options}:
            raise ApiError(400, "selected_option 不在這道題目的選項中。")
        correct_option = str(answer_key).strip().upper()
        attempt = db.add_attempt(question_id, selected, selected == correct_option)
        self._send_json(201, {
            "correct": attempt["correct"], "correct_option": correct_option,
            "explanation": question.get("explanation", ""), "attempt": attempt,
        })

    def _serve_static(self) -> None:
        static_root = (db.APP_ROOT / "web").resolve()
        if not static_root.is_dir():
            raise ApiError(404, "前端 web/ 目錄尚未建立。")
        request_path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        relative = request_path.lstrip("/") or "index.html"
        candidate = (static_root / relative).resolve()
        try:
            candidate.relative_to(static_root)
        except ValueError:
            raise ApiError(404, "找不到前端檔案。")
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            raise ApiError(404, "找不到前端檔案。")
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        size = candidate.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"} else ""))
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            with candidate.open("rb") as static_file:
                while chunk := static_file.read(1024 * 1024):
                    self.wfile.write(chunk)

    def _handle(self) -> None:
        try:
            self._security_check()
            self._dispatch()
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            # Do not return stack traces, local paths, or process environment values to clients.
            self._send_json(500, {"error": f"本機服務處理失敗：{type(exc).__name__}。請查看終端機錯誤紀錄。"})
            print(f"request error {self.command} {self.path}: {type(exc).__name__}: {exc}")

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_HEAD = _handle

    def do_OPTIONS(self) -> None:
        try:
            self._security_check()
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()


def make_server(port: int = 8000) -> ThreadingHTTPServer:
    db.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local medical lecture course app server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    server = make_server(args.port)
    print(f"醫學上課錄音考古整合器已啟動：http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止本機服務…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
