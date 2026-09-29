"""In-process background job runner for audio/PDF course imports."""

from __future__ import annotations

import threading
import math
import time
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import db
import audio_merge
import match as match_module
import transcribe
from openai_match import (
    DEFAULT_QUESTIONS_PER_RUN,
    MAX_QUESTIONS_PER_RUN,
    OpenAIMatchError,
    OpenAIMatchProvider,
    OpenAIRateLimitError,
)


_lock = threading.Lock()
_transcription_lock = threading.Lock()
_active_by_course: dict[str, str] = {}
_analysis_active = False
bank_asset_lock = threading.Lock()

MATCH_REQUEST_MIN_INTERVAL_SECONDS = 3.0
MATCH_RATE_LIMIT_MAX_RETRIES = 3
MATCH_RATE_LIMIT_MAX_WAIT_SECONDS = 120.0


class JobBusyError(RuntimeError):
    """A job for this course or the single local analysis slot is already active."""


def _asset_path(asset: dict[str, Any]) -> Path:
    path = Path(asset["path"]).resolve()
    try:
        path.relative_to(db.DATA_DIR)
    except ValueError as exc:
        raise RuntimeError("課程檔案路徑不在本機資料目錄，已停止讀取。") from exc
    if not path.is_file():
        raise RuntimeError(f"找不到已上傳檔案：{asset.get('filename', 'unknown')}。請重新上傳後再處理。")
    return path


def _normalize_section(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _normalize_number(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().rstrip(".．、:： ").split())


def _merge_answers(question_rows: list[dict[str, Any]], answer_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair by section and number; only use sectionless numbers when unique."""
    question_counts: dict[str, int] = {}
    answer_counts: dict[str, int] = {}
    for row in question_rows:
        number = _normalize_number(row.get("number"))
        if number:
            question_counts[number] = question_counts.get(number, 0) + 1
    for row in answer_rows:
        number = _normalize_number(row.get("number"))
        if number:
            answer_counts[number] = answer_counts.get(number, 0) + 1

    answers_by_exact: dict[tuple[str, str], list[dict[str, Any]]] = {}
    answers_by_number: dict[str, list[dict[str, Any]]] = {}
    for answer in answer_rows:
        number = _normalize_number(answer.get("number"))
        section = _normalize_section(answer.get("section"))
        if number:
            answers_by_exact.setdefault((section, number), []).append(answer)
            answers_by_number.setdefault(number, []).append(answer)

    merged = []
    for original in question_rows:
        question = dict(original)
        if not isinstance(question.get("options"), list) or not question.get("options"):
            question["needs_review"] = True
        number = _normalize_number(question.get("number"))
        section = _normalize_section(question.get("section"))
        if section:
            choices = answers_by_exact.get((section, number), [])
        elif question_counts.get(number) == 1 and answer_counts.get(number) == 1:
            choices = answers_by_number.get(number, [])
        else:
            choices = []
        if len(choices) == 1:
            answer = choices[0]
            if not answer.get("needs_review") and answer.get("answer_key"):
                question["answer_key"] = answer.get("answer_key")
                question["explanation"] = answer.get("explanation", "")
                question["answer_source_page"] = answer.get("source_page")
                question["answer_source_text"] = answer.get("source_text", "")
            else:
                question["needs_review"] = True
        elif len(choices) > 1 or answers_by_number.get(number):
            question["needs_review"] = True
        else:
            question["needs_review"] = True
        merged.append(question)
    return merged


def _import_pdfs(course_id: str) -> list[str]:
    question_asset = db.get_asset(course_id, "questions")
    answer_asset = db.get_asset(course_id, "answers")
    if not question_asset and not answer_asset:
        return []
    existing = db.list_questions(course_id)
    snapshot = db.pdf_import_snapshot(course_id)
    if existing:
        current_question_asset_id = question_asset["id"] if question_asset else None
        current_answer_asset_id = answer_asset["id"] if answer_asset else None
        if snapshot and (snapshot["questions_asset_id"], snapshot["answers_asset_id"]) == (
            current_question_asset_id, current_answer_asset_id
        ):
            return ["PDF 題庫已匯入；本次略過以避免重複建立題目。"]
        raise RuntimeError("目前不支援替換已匯入或已建立的題庫，請建立新課程。")
    if not question_asset:
        return ["已上傳答案解析 PDF，但尚未上傳題目 PDF；答案尚未匯入。"]
    try:
        from pdf_import import import_answers, import_questions
    except ImportError as exc:
        raise RuntimeError("PDF 匯入模組尚未就緒，請稍後重新處理。") from exc

    question_rows: list[dict[str, Any]] = []
    answer_rows: list[dict[str, Any]] = []
    if question_asset:
        question_rows = import_questions(_asset_path(question_asset))
    if answer_asset:
        answer_rows = import_answers(_asset_path(answer_asset), question_rows=question_rows)
    merged = _merge_answers(question_rows, answer_rows)
    warnings: list[str] = []
    for row in merged:
        if not row.get("number"):
            row["number"] = f"page-{row.get('source_page') or 'unknown'}-{len(warnings) + 1}"
            row["needs_review"] = True
        if not row.get("stem"):
            row["stem"] = row.get("source_text") or "（此題文字需人工校對）"
            row["needs_review"] = True
        if row.get("needs_review"):
            warnings.append(str(row["number"]))
        db.add_question(course_id, row)
    db.set_pdf_import_snapshot(
        course_id,
        question_asset["id"] if question_asset else None,
        answer_asset["id"] if answer_asset else None,
    )
    if warnings:
        return [f"{len(warnings)} 題的 PDF 題號、版面或答案配對需要人工校對。"]
    return []


def import_bank(bank_id: str) -> None:
    """Import a reusable bank atomically; existing course copies are untouched."""
    with bank_asset_lock:
        if db.list_bank_questions(bank_id):
            return
        question_asset = db.get_bank_asset(bank_id, "questions")
        answer_asset = db.get_bank_asset(bank_id, "answers")
        if not question_asset or not answer_asset:
            raise RuntimeError("請先上傳此題庫的題目 PDF 與答案解析 PDF。")
        from pdf_import import import_answers, import_questions
        questions = import_questions(_asset_path(question_asset))
        answers = import_answers(_asset_path(answer_asset), question_rows=questions)
        rows = _merge_answers(questions, answers)
        if not rows:
            raise RuntimeError("PDF 中沒有找到題目，請確認 PDF 可選取文字且包含題號與選項。")
        for i, row in enumerate(rows):
            if not row.get("number"):
                row["number"] = str(i + 1)
                row["needs_review"] = True
            if not row.get("stem"):
                row["stem"] = row.get("source_text") or "（此題文字需人工校對）"
                row["needs_review"] = True
        db.replace_bank_questions(bank_id, rows)


def _run_job(job_id: str, course_id: str) -> None:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        # A resumed process job starts a fresh transcription attempt.
        db.clear_transcription_preview(job_id)
        db.update_job(job_id, "running", "transcription", 0.08, "檢查錄音與本機轉錄模型")
        audio = db.get_asset(course_id, "audio")
        if audio:
            if any(segment["source"] == "manual" for segment in db.list_segments(course_id)):
                warnings.append("課程含人工輸入或修正的逐字稿；為保護人工內容，略過重新轉錄。")
            else:
                db.update_job(job_id, "running", "transcription_wait", 0.08, "等待本機轉錄槽位（同時只轉錄一堂課）")
                try:
                    with _transcription_lock:
                        db.update_job(job_id, "running", "transcription", 0.1, "使用本機 Whisper 模型轉錄錄音")
                        def report_progress(fraction: float, message: str) -> None:
                            db.update_job(job_id, "running", "transcription", min(0.85, fraction * 0.85),
                                          f"轉錄 {round(fraction * 100)}% · {message}")
                        def report_chunk(segments: list[dict[str, Any]], chunk_number: int,
                                         total_chunks: int) -> None:
                            db.append_transcription_preview_chunk(job_id, segments, chunk_number, total_chunks)
                        result = transcribe.transcribe_audio(_asset_path(audio), Path(audio["path"]).parent,
                                                            progress_callback=report_progress,
                                                            chunk_minutes=db.get_course(course_id)["transcription_chunk_minutes"],
                                                            chunk_callback=report_chunk)
                    db.add_segments(course_id, result["segments"], source="whisper", replace=True,
                                    reject_manual=True)
                    db.set_course_status(course_id, "processing", float(result["duration_seconds"]))
                except Exception as exc:
                    errors.append(str(exc))
        else:
            db.update_job(job_id, "running", "transcription", 0.2, "未上傳錄音；保留人工輸入逐字稿")

        db.update_job(job_id, "running", "pdf_import", 0.88, "載入科目共用題庫")
        try:
            # Migrated PDFs now belong to a reusable bank; do not re-import the
            # legacy course attachment and accidentally duplicate its questions.
            migrated_bank = any(bank.get("legacy_course_id") == course_id for bank in db.list_banks())
            if not migrated_bank:
                warnings.extend(_import_pdfs(course_id))
            db.sync_subject_questions(course_id)
        except Exception as exc:
            errors.append(str(exc))

        db.update_job(job_id, "running", "matching", 0.94, "比對逐字稿與科目題庫")
        try:
            match_module.refresh_course_matches(course_id, preserve_analysis=False)
        except Exception as exc:
            errors.append(f"題目候選位置建立失敗：{exc}")

        if errors:
            message = "；".join(warnings + errors)
            db.set_course_status(course_id, "error")
            db.update_job(job_id, "error", "needs_attention", 1, message[:4000], "\n".join(errors)[:4000])
        else:
            db.set_course_status(course_id, "ready")
            matches = db.list_matches(course_id)
            confirmed = sum(match["status"] == "confirmed" and match["question_time_ms"] is not None
                            for match in matches)
            suggested = sum(match["status"] == "pending_confirmation" and match["question_time_ms"] is not None
                            for match in matches)
            message = "；".join(warnings + [
                f"轉錄與題目比對完成：{confirmed} 題已確認出題時間，{suggested} 題有待確認的建議時間。"
            ])
            if not db.list_questions(course_id):
                message += " 此科尚無題庫，可稍後加入題庫並重新比對。"
            db.update_job(job_id, "completed", "done", 1, message[:4000])
    except Exception as exc:
        db.set_course_status(course_id, "error")
        db.update_job(job_id, "error", "failed", 1, "背景處理失敗", str(exc)[:4000])
    finally:
        with _lock:
            _active_by_course.pop(course_id, None)


def start_job(course_id: str) -> str:
    with _lock:
        active = _active_by_course.get(course_id)
        if active:
            latest_active = db.latest_job(course_id)
            if latest_active and latest_active["id"] == active and latest_active.get("kind", "process") == "process":
                return active
            raise JobBusyError("這堂課已有其他背景工作進行中，請等候完成後重試。")
        latest = db.latest_job(course_id)
        if latest and latest["status"] in {"queued", "running"}:
            if latest.get("kind", "process") == "process":
                _active_by_course[course_id] = latest["id"]
                thread = threading.Thread(target=_run_job, args=(latest["id"], course_id),
                                          name=f"course-job-{course_id[:8]}", daemon=True)
                thread.start()
                return latest["id"]
            # Explicit analysis and selected-range transcription are never replayed.
            db.update_job(latest["id"], "error", "interrupted", 1,
                          "本機服務重新啟動，背景工作已中斷；請再次明確啟動。",
                          "服務重新啟動時背景工作中斷。")
            if latest.get("kind") == "range_transcription":
                db.set_course_status(course_id, "ready")
        job_id = db.create_job(course_id)
        _active_by_course[course_id] = job_id
        thread = threading.Thread(target=_run_job, args=(job_id, course_id), name=f"course-job-{course_id[:8]}", daemon=True)
        thread.start()
        return job_id


def _run_audio_finalize(job_id: str, course_id: str, pending_ids: list[str],
                        previous_status: str) -> None:
    merged_path: Path | None = None
    temporary_path: Path | None = None
    prefix_path: Path | None = None
    published = False
    try:
        db.clear_transcription_preview(job_id)
        db.update_job(job_id, "running", "audio_merge", 0.03, "依上傳順序合併錄音")
        parts = db.list_audio_parts(course_id)
        pending = [part for part in parts if part["status"] == "pending"]
        if [part["id"] for part in pending] != pending_ids:
            raise RuntimeError("錄音清單在合併期間已變更，請重試。")
        previous_asset = db.get_asset(course_id, "audio")
        folder = (db.DATA_DIR / course_id).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        inputs: list[Path] = []
        old_duration = 0.0
        if previous_asset:
            previous_path = _asset_path(previous_asset)
            if previous_path.name.startswith("audio-merged-") and previous_path.suffix == ".wav":
                old_duration = audio_merge.probe_audio(previous_path)
                inputs.append(previous_path)
            else:
                # Legacy MP3/M4A container duration may differ from the number
                # of decoded samples. Measure the normalized prefix itself.
                with tempfile.NamedTemporaryFile(prefix=".audio-prefix-", suffix=".wav",
                                                 dir=folder, delete=False) as temp:
                    prefix_path = Path(temp.name)
                old_duration = audio_merge.merge_audio([previous_path], prefix_path)
                inputs.append(prefix_path)
        if old_duration + sum(float(part["duration_seconds"] or 0) for part in pending) > 24 * 3600:
            raise ValueError("合併錄音超過 24 小時，請分成不同課程。")
        inputs.extend(_asset_path(part) for part in pending)
        with tempfile.NamedTemporaryFile(prefix=".merged-", suffix=".wav", dir=folder,
                                         delete=False) as temp:
            temporary_path = Path(temp.name)
        total_duration = audio_merge.merge_audio(inputs, temporary_path)
        if total_duration <= old_duration:
            raise RuntimeError("新錄音未使合併音檔增加長度，原錄音已保留。")
        course = db.get_course(course_id)
        if not course:
            raise RuntimeError("找不到指定課程。")
        existing_segments = db.list_segments(course_id)
        if not previous_asset and existing_segments:
            raise RuntimeError("課程已有人工逐字稿，無法從開頭合併錄音。")
        db.update_job(job_id, "running", "transcription_wait", 0.12, "等待本機轉錄槽位")
        with _transcription_lock:
            def report(fraction: float, message: str) -> None:
                db.update_job(job_id, "running", "transcription", min(0.9, 0.15 + fraction * 0.75),
                              f"轉錄新增錄音 {round(fraction * 100)}% · {message}")

            def report_chunk(segments: list[dict[str, Any]], chunk_number: int,
                             total_chunks: int) -> None:
                db.append_transcription_preview_chunk(job_id, segments, chunk_number, total_chunks)

            if previous_asset and existing_segments:
                # A normalized WAV published by the prior finalize is the first
                # ffmpeg input, so its sample timeline is exactly the old prefix.
                result = transcribe.transcribe_audio_range(
                    temporary_path, old_duration, total_duration, folder, report,
                    chunk_minutes=course["transcription_chunk_minutes"],
                    chunk_callback=report_chunk,
                )
            else:
                result = transcribe.transcribe_audio(
                    temporary_path, folder, report,
                    chunk_minutes=course["transcription_chunk_minutes"],
                    chunk_callback=report_chunk,
                )
            segments = result["segments"]
            if not segments:
                raise RuntimeError("新增錄音沒有可用逐字稿，原錄音已保留。")
        db.update_job(job_id, "running", "saving", 0.93, "儲存合併錄音與新逐字稿")
        merged_path = folder / f"audio-merged-{uuid.uuid4().hex}.wav"
        os.replace(temporary_path, merged_path)
        temporary_path = None
        db.publish_audio_parts(course_id, pending_ids, merged_path, total_duration, segments,
                               old_duration_seconds=old_duration)
        published = True
        db.update_job(job_id, "running", "matching", 0.97, "比對新增的逐字稿")
        warning = ""
        try:
            db.sync_subject_questions(course_id)
            _refresh_local_matches(course_id)
        except Exception as exc:
            warning = f"；錄音與逐字稿已儲存，但題目比對未完成：{exc}"
        db.update_job(job_id, "completed", "done", 1,
                      f"已依序合併 {len(pending_ids)} 段錄音，原逐字稿與作答紀錄已保留{warning}"[:4000])
    except Exception as exc:
        if not published:
            db.set_course_status(course_id, previous_status)
        db.update_job(job_id, "error", "needs_attention", 1,
                      f"錄音合併或轉錄失敗，原音檔與逐字稿已保留：{exc}"[:4000], str(exc)[:4000])
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)
        if prefix_path:
            prefix_path.unlink(missing_ok=True)
        if merged_path and not published:
            merged_path.unlink(missing_ok=True)
        with _lock:
            if _active_by_course.get(course_id) == job_id:
                _active_by_course.pop(course_id, None)


def start_audio_finalize(course_id: str) -> str:
    """Finalize all staged parts once, with repeatable retry after failure."""
    with _lock:
        if course_id in _active_by_course:
            raise JobBusyError("這堂課已有背景工作進行中，請等候完成後重試。")
        parts = db.list_audio_parts(course_id)
        pending_ids = [part["id"] for part in parts if part["status"] == "pending"]
        if not pending_ids:
            raise ValueError("沒有待合併的錄音。")
        course = db.get_course(course_id)
        if not course:
            raise ValueError("找不到指定課程。")
        if not db.get_asset(course_id, "audio") and db.list_segments(course_id):
            raise ValueError("課程已有人工逐字稿，無法從開頭合併錄音。")
        previous_status = course["processing_status"]
        latest = db.latest_job(course_id)
        if latest and latest["status"] in {"queued", "running"}:
            db.update_job(latest["id"], "error", "interrupted", 1,
                          "本機服務重新啟動，背景工作已中斷；請再次明確啟動。",
                          "服務重新啟動時背景工作中斷。")
            if latest.get("kind") == "audio_finalize":
                previous_status = "ready" if db.get_asset(course_id, "audio") else "draft"
        job_id = db.create_job(course_id, kind="audio_finalize")
        _active_by_course[course_id] = job_id
        db.set_course_status(course_id, "processing")
        thread = threading.Thread(
            target=_run_audio_finalize,
            args=(job_id, course_id, pending_ids, previous_status),
            name=f"audio-finalize-{course_id[:8]}", daemon=True,
        )
        try:
            thread.start()
        except Exception:
            _active_by_course.pop(course_id, None)
            db.set_course_status(course_id, previous_status)
            db.update_job(job_id, "error", "failed", 1, "無法啟動錄音合併工作")
            raise
        return job_id


def _effective_range(course_id: str, start_ms: int, end_ms: int,
                     duration_ms: int) -> tuple[int, int, set[str]]:
    """Expand to whole intersecting rows so existing text is never cut mid-segment."""
    segments = db.list_segments(course_id)
    changed = True
    while changed:
        changed = False
        for segment in segments:
            if segment["start_ms"] >= end_ms or segment["end_ms"] <= start_ms:
                continue
            if segment["source"] != "whisper":
                raise ValueError("指定範圍與人工輸入或修正的逐字稿重疊，請改選不含人工內容的範圍。")
            if segment["start_ms"] < 0 or segment["end_ms"] > duration_ms:
                raise ValueError("現有逐字稿超出錄音長度，請先修正時間範圍。")
            new_start = max(0, min(start_ms, segment["start_ms"]))
            new_end = min(duration_ms, max(end_ms, segment["end_ms"]))
            if (new_start, new_end) != (start_ms, end_ms):
                start_ms, end_ms = new_start, new_end
                changed = True
    expected = {
        segment["id"] for segment in segments
        if segment["start_ms"] < end_ms and segment["end_ms"] > start_ms
    }
    return start_ms, end_ms, expected


def _refresh_local_matches(course_id: str) -> None:
    """Update local matches while retaining manually assigned and cloud results."""
    segments = db.list_segments(course_id)
    provider = match_module.LocalCandidateProvider(segments)
    course = db.get_course(course_id) or {}
    duration_ms = round(float(course.get("duration_seconds") or 0) * 1000) or None
    for question in db.list_questions(course_id):
        previous = db.get_match(question["id"])
        if previous and (previous["provider"] in {"manual", "openai", "codex"} or previous["status"] == "disabled"):
            continue
        suggestion = match_module.suggest_question(question, segments, provider, duration_ms)
        db.save_match(question["id"], {
            "segment_id": suggestion.segment_id, "start_ms": suggestion.start_ms,
            "end_ms": suggestion.end_ms, "question_time_ms": suggestion.question_time_ms,
            "status": suggestion.status, "confidence": suggestion.confidence,
            "candidate_score": suggestion.candidate_score, "reason": suggestion.reason,
            "evidence": suggestion.evidence, "provider": suggestion.provider,
        })


def _range_time(ms: int) -> str:
    whole_seconds, milliseconds = divmod(ms, 1000)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    clock = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
    return clock + (f".{milliseconds:03d}".rstrip("0") if milliseconds else "")


def _run_range_job(job_id: str, course_id: str, start_ms: int, end_ms: int,
                   expected_ids: set[str], prior_status: str) -> None:
    label = (f"實際重新轉錄範圍 {_range_time(start_ms)}–{_range_time(end_ms)}"
             f"（{start_ms / 1000:g}–{end_ms / 1000:g} 秒）")
    replaced = False
    try:
        db.update_job(job_id, "running", "transcription_wait", 0.02,
                      f"{label}；等待本機轉錄槽位")
        with _transcription_lock:
            audio = db.get_asset(course_id, "audio")
            if not audio:
                raise ValueError("尚未上傳錄音，無法重新轉錄指定範圍。")
            course = db.get_course(course_id)
            if not course:
                raise ValueError("找不到指定課程。")

            def report(fraction: float, message: str) -> None:
                db.update_job(job_id, "running", "transcription", min(0.9, 0.05 + fraction * 0.85),
                              f"{label}；轉錄 {round(fraction * 100)}% · {message}")

            def report_chunk(segments: list[dict[str, Any]], chunk_number: int,
                             total_chunks: int) -> None:
                db.append_transcription_preview_chunk(job_id, segments, chunk_number, total_chunks)

            result = transcribe.transcribe_audio_range(
                _asset_path(audio), start_ms / 1000, end_ms / 1000,
                Path(audio["path"]).parent, report,
                chunk_minutes=course["transcription_chunk_minutes"],
                chunk_callback=report_chunk,
            )
            db.update_job(job_id, "running", "saving", 0.92, f"{label}；正在更新逐字稿")
            db.replace_range_segments(course_id, start_ms, end_ms, expected_ids, result["segments"])
            replaced = True
        db.update_job(job_id, "running", "matching", 0.96, f"{label}；正在重新比對題目")
        _refresh_local_matches(course_id)
        db.set_course_status(course_id, "ready")
        db.update_job(job_id, "completed", "done", 1,
                      f"{label}；重新轉錄完成，範圍外逐字稿與作答紀錄已保留。")
    except Exception as exc:
        db.set_course_status(course_id, prior_status)
        detail = "逐字稿已更新，但題目比對未完成" if replaced else "原逐字稿已保留"
        db.update_job(job_id, "error", "needs_attention", 1,
                      f"{label}；重新轉錄失敗，{detail}：{exc}"[:4000], str(exc)[:4000])
    finally:
        with _lock:
            if _active_by_course.get(course_id) == job_id:
                _active_by_course.pop(course_id, None)


def start_range_job(course_id: str, start_seconds: float, end_seconds: float) -> str:
    """Queue an explicit bounded retranscription of one course recording."""
    try:
        if isinstance(start_seconds, bool) or isinstance(end_seconds, bool):
            raise TypeError
        start, end = float(start_seconds), float(end_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("請提供有效的重新轉錄開始與結束秒數。") from exc
    course = db.get_course(course_id)
    if not course:
        raise ValueError("找不到指定課程。")
    duration = course.get("duration_seconds")
    if duration is None or not math.isfinite(float(duration)) or not (
        math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= float(duration)
    ):
        raise ValueError("重新轉錄範圍須介於 0 秒與錄音長度之間，且結束時間必須晚於開始時間。")
    start_ms, end_ms, duration_ms = round(start * 1000), round(end * 1000), round(float(duration) * 1000)
    if end_ms <= start_ms:
        raise ValueError("重新轉錄範圍必須至少長於一毫秒。")
    audio = db.get_asset(course_id, "audio")
    if not audio or not Path(audio["path"]).is_file():
        raise ValueError("尚未上傳可用的錄音，無法重新轉錄指定範圍。")
    with _lock:
        if course_id in _active_by_course:
            raise JobBusyError("這堂課已有背景工作進行中，請等候完成後重試。")
        latest = db.latest_job(course_id)
        prior_status = course["processing_status"]
        if latest and latest["status"] in {"queued", "running"}:
            if latest.get("kind") == "process":
                raise JobBusyError("這堂課已有背景工作進行中，請等候完成後重試。")
            db.update_job(latest["id"], "error", "interrupted", 1,
                          "本機服務重新啟動，背景工作已中斷；請再次明確啟動。",
                          "服務重新啟動時背景工作中斷。")
            if latest.get("kind") == "range_transcription":
                prior_status = "ready"
                db.set_course_status(course_id, prior_status)
        effective_start, effective_end, expected = _effective_range(
            course_id, start_ms, end_ms, duration_ms,
        )
        job_id = db.create_job(course_id, kind="range_transcription")
        _active_by_course[course_id] = job_id
        db.set_course_status(course_id, "processing")
        thread = threading.Thread(
            target=_run_range_job,
            args=(job_id, course_id, effective_start, effective_end, expected,
                  prior_status),
            name=f"range-transcription-{course_id[:8]}", daemon=True,
        )
        try:
            thread.start()
        except Exception:
            _active_by_course.pop(course_id, None)
            db.set_course_status(course_id, prior_status)
            db.update_job(job_id, "error", "failed", 1, "無法啟動指定範圍重新轉錄。")
            raise
        return job_id


def _wait_for_match_rate_limit(job_id: str, delay_seconds: float, progress: float,
                               completed: int, retry: int) -> None:
    """Show a countdown while honoring the server's bounded retry delay."""
    remaining = delay_seconds
    while remaining > 0:
        db.update_job(
            job_id, "running", "rate_limit_wait", progress,
            f"API 暫時限流；已保留完成的 {completed} 題。"
            f"等待約 {math.ceil(remaining)} 秒後重試（第 {retry} / {MATCH_RATE_LIMIT_MAX_RETRIES} 次）。",
        )
        chunk = min(1.0, remaining)
        time.sleep(chunk)
        remaining -= chunk


def _run_match_analysis(job_id: str, course_id: str, provider: OpenAIMatchProvider,
                        max_questions: int) -> None:
    global _analysis_active
    try:
        questions = db.list_questions(course_id)
        segments = db.list_segments(course_id)
        if not questions:
            raise OpenAIMatchError("這堂課還沒有題目，請先輸入或匯入題庫。")
        if not segments and not getattr(provider, "handout_context", None):
            raise OpenAIMatchError("這堂課還沒有逐字稿，請先新增逐字稿或完成錄音轉錄。")

        bounded_questions = questions[:1000]
        eligible_count = sum(1 for question in bounded_questions if match_module.question_is_matchable(question))
        call_limit = min(max_questions, MAX_QUESTIONS_PER_RUN)
        analyzed = 0
        skipped_invalid = len(bounded_questions) - eligible_count
        skipped_candidates = 0
        skipped_existing = 0
        skipped_prior_analysis = 0
        handout_without_time = 0
        no_match = 0
        confirmed = 0
        suggested_time = 0
        pending_without_time = 0
        last_request_started_at: float | None = None
        db.update_job(job_id, "running", "candidate_search", 0.05,
                      f"本機先篩選逐字稿候選；本次最多分析 {call_limit} 題。")

        # Work through in source order. Questions already confirmed by a person or
        # prior trusted analysis are left intact; lexical suggestions are replaceable.
        for question in bounded_questions:
            if analyzed >= call_limit:
                break
            if not match_module.question_is_matchable(question):
                continue
            previous = db.get_match(question["id"])
            if previous and previous["status"] in {"confirmed", "disabled"}:
                skipped_existing += 1
                continue
            if previous and previous.get("provider") in {"openai", "codex"}:
                # Do not silently spend again on prior uncertain/no-match results.
                # A future explicit retry control can opt selected questions back in.
                skipped_prior_analysis += 1
                continue
            windows = provider.candidate_windows(question, segments)
            if not windows:
                relevant_handouts = getattr(provider, "relevant_handout_excerpts", None)
                if callable(relevant_handouts) and relevant_handouts(question):
                    # A handout can show course scope, but cannot supply an
                    # audio location. Keep an explicit review item without an
                    # API call or a fabricated timestamp.
                    suggestion = provider.suggest(question, segments)
                    db.save_match(question["id"], {
                        "segment_id": None, "start_ms": None, "end_ms": None,
                        "question_time_ms": None,
                        "status": "pending_confirmation", "confidence": None,
                        "candidate_score": None, "reason": suggestion.reason,
                        "evidence": "", "provider": "handout_local",
                    })
                    handout_without_time += 1
                    continue
                skipped_candidates += 1
                continue

            db.update_job(job_id, "running", "analyzing_matches",
                          min(0.95, 0.1 + 0.8 * analyzed / max(1, call_limit)),
                          f"OpenAI 題目對位：已送出 {analyzed} / {call_limit} 題。")
            retries = 0
            while True:
                if last_request_started_at is not None:
                    remaining_interval = MATCH_REQUEST_MIN_INTERVAL_SECONDS - (
                        time.monotonic() - last_request_started_at
                    )
                    if remaining_interval > 0:
                        time.sleep(remaining_interval)
                last_request_started_at = time.monotonic()
                try:
                    suggestion = provider.suggest(question, segments)
                    break
                except OpenAIRateLimitError as exc:
                    if retries >= MATCH_RATE_LIMIT_MAX_RETRIES:
                        raise OpenAIMatchError(
                            f"API 請求速率仍超過上限（{exc.code}）；已保留完成的 {analyzed} 題。"
                            "請稍後再執行分析，並檢查模型的請求與 token 速率限制。"
                        ) from exc
                    delay = exc.retry_after_seconds
                    if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay <= 0:
                        delay = 20.0
                    if delay > MATCH_RATE_LIMIT_MAX_WAIT_SECONDS:
                        raise OpenAIMatchError(
                            f"API 要求等待約 {math.ceil(delay)} 秒才可重試（{exc.code}）；"
                            f"已保留完成的 {analyzed} 題。請稍後重新執行分析。"
                        ) from exc
                    retries += 1
                    _wait_for_match_rate_limit(
                        job_id, max(1.0, float(delay)),
                        min(0.95, 0.1 + 0.8 * analyzed / max(1, call_limit)),
                        analyzed, retries,
                    )
                    db.update_job(job_id, "running", "analyzing_matches",
                                  min(0.95, 0.1 + 0.8 * analyzed / max(1, call_limit)),
                                  f"API 限流等待結束；重新分析目前題目（已完成 {analyzed} 題）。")
            course = db.get_course(course_id) or {}
            duration_ms = round(float(course.get("duration_seconds") or 0) * 1000) or None
            suggestion = match_module.validate_provider_suggestion(
                question, segments, suggestion, provider, duration_ms,
            )
            db.save_match(question["id"], {
                "segment_id": suggestion.segment_id,
                "start_ms": suggestion.start_ms,
                "end_ms": suggestion.end_ms,
                "question_time_ms": suggestion.question_time_ms,
                "status": suggestion.status,
                "confidence": suggestion.confidence,
                "candidate_score": suggestion.candidate_score,
                "reason": suggestion.reason,
                "evidence": suggestion.evidence,
                "provider": suggestion.provider,
            })
            analyzed += 1
            if suggestion.status == "unmatched":
                no_match += 1
            elif suggestion.question_time_ms is not None:
                if suggestion.status == "confirmed":
                    confirmed += 1
                else:
                    suggested_time += 1
            else:
                pending_without_time += 1
            db.update_job(job_id, "running", "analyzing_matches",
                          min(0.98, 0.1 + 0.8 * analyzed / max(1, call_limit)),
                          f"OpenAI 題目對位：已完成 {analyzed} 題。")

        unscanned = max(0, eligible_count - skipped_existing - skipped_prior_analysis
                        - analyzed - skipped_candidates - handout_without_time)
        summary = (
            f"本輪分析結束：已送出 {analyzed} 題；新增 {confirmed} 題已確認出題時間、"
            f"{suggested_time} 題有待確認的建議時間；{no_match} 題未找到講課證據；"
            f"{pending_without_time} 題證據仍需確認且尚無建議時間。"
            f" {skipped_candidates} 題沒有達門檻的本機候選而略過；"
            f"{handout_without_time} 題與講義相關但逐字稿未找到時間證據，已列待確認；"
            f"{skipped_invalid} 題的題幹或選項文字不足；{skipped_existing} 題已確認/停用而保留；"
            f"{skipped_prior_analysis} 題已有 OpenAI 結果而略過。"
        )
        if analyzed == 0:
            summary += " 本輪沒有可送出的候選題，不代表全部題目分析完成。"
        if unscanned:
            summary += f" 尚有 {unscanned} 題本輪未掃描；可再次執行分析。"
        if len(questions) > len(bounded_questions):
            summary += " 題庫超過本次安全掃描上限；請分批處理或建立較小課程。"
        db.update_job(job_id, "completed", "done", 1, summary[:4000])
    except OpenAIMatchError as exc:
        message = str(exc)[:1000]
        db.update_job(job_id, "error", "needs_attention", 1, message, message)
    except Exception as exc:
        # Do not persist exception details, payloads, request headers, or secrets.
        message = f"OpenAI 題目分析失敗（{type(exc).__name__}）；請稍後重試。"
        db.update_job(job_id, "error", "needs_attention", 1, message, message)
    finally:
        with _lock:
            if _active_by_course.get(course_id) == job_id:
                _active_by_course.pop(course_id, None)
            _analysis_active = False


def start_match_analysis(course_id: str, max_questions: int = DEFAULT_QUESTIONS_PER_RUN) -> str:
    global _analysis_active
    if not 1 <= int(max_questions) <= MAX_QUESTIONS_PER_RUN:
        raise ValueError(f"max_questions 必須介於 1 到 {MAX_QUESTIONS_PER_RUN}。")
    # Constructing checks key/model configuration but makes no network request.
    provider = OpenAIMatchProvider()
    # Loading local page text is read-only. Uploading handouts never triggers
    # cloud analysis; only this explicit action passes scope context onward.
    import handouts
    provider.handout_context = handouts.course_context(course_id)
    with _lock:
        if course_id in _active_by_course:
            raise JobBusyError("這堂課已有背景工作進行中，請等候完成後重試。")
        if _analysis_active:
            raise JobBusyError("目前已有另一堂課正在進行 OpenAI 分析，完成後即可重試。")
        latest = db.latest_job(course_id)
        if latest and latest["status"] in {"queued", "running"}:
            if latest.get("kind", "process") == "process":
                raise JobBusyError("這堂課的轉錄/PDF 工作尚未完成，完成後再分析題目。")
            db.update_job(latest["id"], "error", "interrupted", 1,
                          "本機服務重新啟動，題目分析已中斷；請再次明確按下分析。",
                          "服務重新啟動時分析工作中斷。")
        job_id = db.create_job(course_id, kind="match_analysis")
        _active_by_course[course_id] = job_id
        _analysis_active = True
        thread = threading.Thread(
            target=_run_match_analysis, args=(job_id, course_id, provider, int(max_questions)),
            name=f"match-analysis-{course_id[:8]}", daemon=True,
        )
        try:
            thread.start()
        except Exception:
            _active_by_course.pop(course_id, None)
            _analysis_active = False
            db.update_job(job_id, "error", "failed", 1, "無法啟動題目分析工作。", "無法啟動背景執行緒。")
            raise
        return job_id


def get_job(course_id: str) -> dict[str, Any] | None:
    with _lock:
        latest = db.latest_job(course_id)
        if (latest and latest.get("kind") == "audio_finalize"
                and latest["status"] in {"queued", "running"}
                and _active_by_course.get(course_id) != latest["id"]):
            pending = any(part["status"] == "pending" for part in db.list_audio_parts(course_id))
            if pending:
                db.update_job(latest["id"], "error", "interrupted", 1,
                              "錄音合併因服務重新啟動而中斷；待合併錄音已保留，可再次按合併。",
                              "服務重新啟動時工作中斷。")
                db.set_course_status(course_id, "ready" if db.get_asset(course_id, "audio") else "draft")
            else:
                db.update_job(latest["id"], "completed", "done", 1,
                              "合併錄音與逐字稿已儲存。")
            latest = db.latest_job(course_id)
        if (latest and latest.get("kind") in {"range_transcription", "medical_suggest", "medical_align"}
                and latest["status"] in {"queued", "running"}
                and _active_by_course.get(course_id) != latest["id"]):
            db.update_job(latest["id"], "error", "interrupted", 1,
                          "本機服務重新啟動，逐字稿處理已中斷；請檢查紀錄後重新啟動需要的工作。",
                          "服務重新啟動時工作中斷；請檢查逐字稿並重新啟動需要的工作。")
            db.set_course_status(course_id, "ready")
            latest = db.latest_job(course_id)
        if latest:
            latest["preview_revision"] = db.preview_revision(latest["id"])
        return latest
