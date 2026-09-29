"""Local-only correction suggestions, immutable snapshots, and audio alignment jobs."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from difflib import SequenceMatcher

import db
import local_tools


MODEL_PROFILES = {
    "8b": ("medical-qwen3-8b", "Qwen3-8B-4bit", "545dc4251c05440727734bcd94334791f6ab0192"),
    "30b": ("medical-qwen3-30b-a3b", "Qwen3-30B-A3B-Instruct-2507-4bit", "e9675aa3ca5f900ccef55267914466d55ab325fa"),
    "35b": ("medical-qwen36-35b-a3b", "Qwen3.6-35B-A3B-4bit", "38740b847e4cb78f352aba30aa41c76e08e6eb46"),
}
MODEL_PROFILE = os.environ.get("MEDICAL_REVIEW_MODEL", "").strip() or next((key for key in ("35b", "30b", "8b")
    if (db.APP_ROOT / "models" / MODEL_PROFILES[key][0] / "download-manifest.json").is_file()), "8b")
if MODEL_PROFILE not in MODEL_PROFILES:
    raise ValueError("MEDICAL_REVIEW_MODEL 僅支援 8b、30b 或 35b。")
MODEL_DIR = db.APP_ROOT / "models" / MODEL_PROFILES[MODEL_PROFILE][0]
MODEL_LABEL = MODEL_PROFILES[MODEL_PROFILE][1]
CHECKPOINT = Path.home() / ".cache" / "whisper" / "large-v3.pt"


def term_key(text):
    return re.sub(r"[\s\-_]+", "", text).casefold()


def term_edit_allowed(original, replacement, phonetic_restore=False):
    """Reject prose edits and cross-language translations, independently of the LLM."""
    latin = lambda value: bool(re.search(r"[A-Za-z]", value))
    han = lambda value: bool(re.search(r"[\u3400-\u9fff]", value))
    cross_script = latin(original) != latin(replacement) or han(original) != han(replacement)
    phonetic_restore = (phonetic_restore and han(original) and not latin(original)
                        and latin(replacement) and not han(replacement)
                        and len(original) <= 8 and len(replacement.split()) <= 4)
    if cross_script and not phonetic_restore:
        return False
    if any(c in original + replacement for c in "，,：:。！？；!?;\n\r"):
        return False
    if len(original) > 60 or len(replacement) > 80:
        return False
    # A medical term is not a clause. Keep surrounding spoken grammar untouched.
    grammar = ("這個", "那個", "就是", "所以", "然後", "變成", "成為", "跟", "與", "的", "把", "讓")
    if any(original.count(w) != replacement.count(w) for w in grammar):
        return False
    english_grammar = {"is", "are", "was", "were", "have", "has", "had", "and", "or", "because",
                       "there", "this", "that", "these", "those", "it", "they", "we", "you",
                       "need", "needed", "needs", "be", "been"}
    grammar_words = lambda text: [w for w in re.findall(r"[a-z]+", text.casefold()) if w in english_grammar]
    if grammar_words(original) != grammar_words(replacement):
        return False
    if not latin(original) and not phonetic_restore and (len(original) > 12 or len(replacement) > 12):
        return False
    # Do not expose capitalization/spacing-only changes as useful corrections.
    if term_key(original) == term_key(replacement):
        return False
    return True


def course_vocabulary(course_id, context):
    """Extract spelling references from uploaded handouts, never spoken sentences."""
    terms, seen = [], set()
    sources = [("使用者提供", context)]
    for row in db.list_handouts(course_id):
        document = db.get_handout(course_id, row["id"])
        if document is None:
            continue
        for page in json.loads(document["pages_json"]):
            sources.append((f'{row["filename"]} p.{page.get("page", "?")}', page.get("text", "")))
    for source, text in sources:
        for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9-]{2,}(?:[ \t]+[A-Za-z][A-Za-z0-9-]{2,}){0,4}\b", text):
            term = match.group().strip()
            if term_key(term) not in seen:
                terms.append({"term": term, "source": source})
                seen.add(term_key(term))
    return terms


def review_batches(segments, window_ms=60000, limit=24):
    """Bound target size while keeping time-based context independent of sentence length."""
    batch = []
    for segment in segments:
        if batch and (len(batch) >= limit or segment["start_ms"] - batch[0]["start_ms"] >= window_ms):
            yield batch
            batch = []
        batch.append(segment)
    if batch:
        yield batch


def select_vocabulary(terms, segments, limit=100):
    words = re.findall(r"[A-Za-z][A-Za-z-]{2,}", " ".join(s["text"] for s in segments))
    keys = {term_key(w) for w in words}
    def score(item):
        tokens = [term_key(w) for w in item["term"].split()]
        return max((SequenceMatcher(None, a, b).ratio() for a in tokens for b in keys), default=0)
    return sorted(terms, key=score, reverse=True)[:limit]


def inferred_vocabulary(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("terms"), list):
        raise ValueError("本地候選術語表格式無效。")
    output, seen = [], set()
    for term in payload["terms"]:
        if (not isinstance(term, str) or not 2 <= len(term.strip()) <= 80
                or len(term.split()) > 6 or any(c in term for c in "\n\r，,。！？；!?;{}[]")
                or term_key(term) in seen):
            continue
        output.append({"term": term.strip(), "source": "本地模型推定詞彙，非講義或原音證據"})
        seen.add(term_key(term))
        if len(output) == 80:
            break
    return output


def contains_term(text, replacement):
    pieces = re.split(r"[\s_-]+", replacement.strip())
    target = r"[\s_-]*".join(re.escape(p) for p in pieces)
    if replacement and replacement[0].isascii() and replacement[0].isalpha():
        target = r"(?<![A-Za-z])" + target
    if replacement and replacement[-1].isascii() and replacement[-1].isalpha():
        target += r"(?![A-Za-z])"
    return bool(replacement.strip() and re.search(target, text, re.IGNORECASE))


def evidence_for_term(evidence, replacement, segment):
    """Report only nearby exact textual agreement, never 'audio verified'."""
    nearby = " ".join(s["text"] for s in evidence.get("segments", [])
                      if s["end_ms"] > segment["start_ms"] - 3000
                      and s["start_ms"] < segment["end_ms"] + 3000)
    return contains_term(nearby, replacement)


def model_installed():
    try:
        manifest = json.loads((MODEL_DIR / "download-manifest.json").read_text())
        if manifest.get("sha") != MODEL_PROFILES[MODEL_PROFILE][2]:
            return False
        weights = [item for item in manifest["siblings"] if item["rfilename"].endswith(".safetensors")]
        return bool(weights) and (MODEL_DIR / "config.json").is_file() and (MODEL_DIR / "tokenizer.json").is_file() and all(
            Path(item["rfilename"]).name == item["rfilename"]
            and (MODEL_DIR / item["rfilename"]).stat().st_size == item["size"] for item in weights)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def readiness():
    return {
        "suggest_ready": model_installed() and importlib.util.find_spec("mlx_lm") is not None,
        "align_ready": CHECKPOINT.is_file() and importlib.util.find_spec("stable_whisper") is not None,
        "model": MODEL_LABEL + "（本地雙輪校訂）", "cloud": False,
    }


def snapshot(segment):
    return {key: segment[key] for key in ("text", "start_ms", "end_ms")}


def add_revision(conn, course_id, segment_id, kind, before, after, reason, model="", status="pending"):
    revision_id = db.new_id()
    conn.execute("""INSERT INTO transcript_revisions
        (id,course_id,segment_id,kind,status,before_json,after_json,reason,model,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", (revision_id, course_id, segment_id, kind, status,
        json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), reason, model, db.now_iso()))
    return revision_id


def history(course_id):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM transcript_revisions WHERE course_id=? ORDER BY rowid DESC",
                            (course_id,)).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["before"] = json.loads(item.pop("before_json"))
        item["after"] = json.loads(item.pop("after_json"))
        output.append(item)
    return output


def annotations(course_id):
    """An inferred correction remains inferred after alignment or manual edits."""
    output = {}
    for row in history(course_id):
        if row["kind"] == "suggest" and row["status"] == "applied":
            output.setdefault(row["segment_id"], {})["inferred"] = True
    return output


def decide(course_id, revision_id, action):
    if not isinstance(action, str) or action not in {"apply", "reject"}:
        raise ValueError("請選擇採用或略過建議。")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM transcript_revisions WHERE id=? AND course_id=?",
                           (revision_id, course_id)).fetchone()
        if not row or row["kind"] != "suggest" or row["status"] != "pending":
            raise ValueError("建議不存在或已經處理。")
        if action == "apply":
            current = conn.execute("SELECT * FROM segments WHERE id=? AND course_id=?",
                                   (row["segment_id"], course_id)).fetchone()
            before, after = json.loads(row["before_json"]), json.loads(row["after_json"])
            if current is None or snapshot(current) != before:
                raise ValueError("原段落已變動，請重新產生建議，避免覆蓋新版逐字稿。")
            conn.execute("UPDATE segments SET text=?,source='manual',original_text=COALESCE(original_text,text) WHERE id=?",
                         (after["text"], row["segment_id"]))
        conn.execute("UPDATE transcript_revisions SET status=?,decided_at=? WHERE id=?",
                     ("applied" if action == "apply" else "rejected", db.now_iso(), revision_id))


def validate_suggestions(payload, segments, terminology=None, audio_evidence=None):
    """Only exact, bounded term replacements; never trust model IDs or rewritten prose."""
    if not isinstance(payload, dict) or not isinstance(payload.get("suggestions"), list):
        raise ValueError("本地模型未回傳有效的建議格式，原稿未修改。")
    known = {s["id"]: s for s in segments}
    grouped = {}
    for item in payload["suggestions"]:
        if not isinstance(item, dict):
            continue
        sid, original, replacement, reason = (item.get(k) for k in ("segment_id", "original", "replacement", "reason"))
        if not all(isinstance(v, str) for v in (sid, original, replacement, reason)):
            continue
        segment = known.get(sid)
        if (segment is None or not original.strip() or not replacement.strip()
                or original == replacement or len(original) > 80 or len(replacement) > max(80, len(original) * 3)
                or segment["text"].count(original) != 1 or not reason.strip()):
            continue
        if not term_edit_allowed(original, replacement,
                                 phonetic_restore=evidence_for_term(audio_evidence or {}, replacement, segment)):
            continue
        if terminology is not None:
            prior = terminology.get(term_key(original))
            if prior and term_key(prior) != term_key(replacement):
                continue
        before = snapshot(segment)
        after = {**before, "text": before["text"].replace(original, replacement, 1)}
        if (any(c in original + replacement for c in "\n\r。！？；!?;")
                or re.findall(r"\d+(?:\.\d+)?", before["text"]) != re.findall(r"\d+(?:\.\d+)?", after["text"])
                or any(before["text"].count(term) != after["text"].count(term)
                       for term in ("不是", "沒有", "不會", "不能", "不可", "無", "未"))):
            continue
        start = before["text"].index(original)
        end = start + len(original)
        edits = grouped.setdefault(sid, [])
        if len(edits) >= 3 or any(a < end and b > start for a, b, *_ in edits):
            continue
        edits.append((start, end, original, replacement, reason[:400]))
        if terminology is not None:
            terminology[term_key(original)] = replacement
    output = []
    for sid, edits in grouped.items():
        before = snapshot(known[sid])
        text = before["text"]
        for start, end, original, replacement, reason in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        after = {**before, "text": text, "term_edits": [
            {"original": e[2], "replacement": e[3]} for e in edits]}
        output.append((sid, before, after, "；".join(e[4] for e in edits)[:1200]))
    return output


def worker(mode, payload, timeout=1800):
    """Heavy models live in separate processes, never in the web server."""
    with tempfile.TemporaryDirectory(prefix="medical-review-") as folder:
        source, result = Path(folder) / "input.json", Path(folder) / "output.json"
        source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
        ffmpeg = local_tools.find_executable("ffmpeg")
        if ffmpeg:
            env["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + env.get("PATH", "")
        completed = subprocess.run([sys.executable, str(db.APP_ROOT / "local_review_worker.py"), mode,
                                    str(source), str(result)], env=env, capture_output=True, text=True, timeout=timeout)
        if completed.returncode or not result.is_file():
            raise RuntimeError("本地校訂／對齊工具執行失敗；原稿與既有時間已保留。" + completed.stderr[-600:])
        return json.loads(result.read_text(encoding="utf-8"))


def save_alignment(course_id, segment, aligned):
    before = snapshot(segment)
    words = aligned.get("words", [])
    if not words:
        raise ValueError("沒有可用的對齊結果。")
    last = -1
    for word in words:
        start, end = word.get("start_ms"), word.get("end_ms")
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or start < last or end <= start
                or start < aligned["clip_start_ms"] or end > aligned["clip_end_ms"]):
            raise ValueError("對齊結果不完整或時間無效，原時間已保留。")
        last = end
    normalize = lambda text: "".join(str(text).split())
    if normalize("".join(w["word"] for w in words)) != normalize(segment["text"]):
        raise ValueError("對齊文字不完整，原時間已保留。")
    after = {"text": before["text"], "start_ms": round(words[0]["start_ms"]),
             "end_ms": round(words[-1]["end_ms"]), "words": words}
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM segments WHERE id=? AND course_id=?", (segment["id"], course_id)).fetchone()
        if current is None or snapshot(current) != before:
            raise ValueError("對齊期間段落已變動，未套用過期結果。")
        # Do not let an alignment move into an adjacent segment.
        neighbors = conn.execute("SELECT start_ms,end_ms FROM segments WHERE course_id=? AND id!=?",
                                 (course_id, segment["id"])).fetchall()
        if any(n["start_ms"] < after["end_ms"] and n["end_ms"] > after["start_ms"] for n in neighbors):
            raise ValueError("對齊結果與相鄰段落重疊，原時間已保留。")
        conn.execute("UPDATE segments SET start_ms=?,end_ms=? WHERE id=?", (after["start_ms"], after["end_ms"], segment["id"]))
        add_revision(conn, course_id, segment["id"], "align", before, after,
                     "音訊強制對齊的估計時間，不代表推定文字已被證實。", "stable-ts / Large v3", "applied")


def start(course_id, mode, start_seconds, context=""):
    import jobs
    if not isinstance(mode, str) or mode not in {"suggest", "align"}:
        raise ValueError("不支援的校訂工作。")
    if type(start_seconds) not in (int, float) or not math.isfinite(start_seconds) or start_seconds < 0:
        raise ValueError("開始秒數必須是有效的非負數。")
    if not isinstance(context, str) or len(context) > 4000:
        raise ValueError("術語背景最多 4000 字。")
    if not readiness()[mode + "_ready"]:
        raise ValueError("本地模型或套件尚未安裝完成，請先完成本機模型設定。")
    start_ms = int(start_seconds // 300) * 300000
    segments = [s for s in db.list_segments(course_id) if start_ms <= s["start_ms"] < start_ms + 300000]
    if not segments:
        raise ValueError("這個五分鐘區間沒有逐字稿。")
    audio = db.get_asset(course_id, "audio")
    if mode == "align" and not audio:
        raise ValueError("請先上傳錄音，才能重新對齊。")
    audio_path = str(jobs._asset_path(audio)) if audio else None
    with jobs._lock:
        if course_id in jobs._active_by_course:
            raise jobs.JobBusyError("這堂課正在處理中，請稍後再試。")
        job_id = db.create_job(course_id, kind="medical_" + mode)
        jobs._active_by_course[course_id] = job_id
        thread = threading.Thread(target=_run, args=(job_id, course_id, mode, segments, context, audio_path), daemon=True)
        try:
            thread.start()
        except Exception:
            jobs._active_by_course.pop(course_id, None)
            db.update_job(job_id, "error", "failed", 1, "無法啟動本地校訂。")
            raise
    return job_id


def _run(job_id, course_id, mode, segments, context, audio_path):
    import jobs
    done, failed = 0, 0
    try:
        db.update_job(job_id, "running", mode, .01, "等待本機模型運算空間…")
        with jobs._transcription_lock:
            if mode == "suggest":
                all_segments = db.list_segments(course_id)
                vocabulary = course_vocabulary(course_id, context)
                db.update_job(job_id, "running", mode, .01, "從本堂課語境建立本地候選術語表…")
                vocabulary_error = ""
                vocabulary_diagnostic = ""
                try:
                    # Focus on this interval plus context, and bound the model input.
                    topic_text = "\n".join(s["text"] for s in all_segments
                        if segments[0]["start_ms"] - 90000 <= s["start_ms"] <= segments[-1]["end_ms"] + 90000)[:18000]
                    vocabulary_result = worker("vocabulary", {"model": str(MODEL_DIR),
                        "title": db.get_course(course_id)["title"], "context": context,
                        "text": topic_text}, timeout=600)
                    vocabulary += inferred_vocabulary(vocabulary_result)
                    if vocabulary_result.get("recovered_complete_terms"):
                        vocabulary_error = "術語表輸出達長度上限，僅保留完整詞條。"
                except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                    vocabulary_error = "本地推定術語表未產生，僅使用既有文字與講義參考。"
                    vocabulary_diagnostic = str(exc)[-1200:]
                terminology = {}
                # Applied suggestions supply spelling consistency, not proof of audio.
                for record in reversed(history(course_id)):
                    if record["kind"] == "suggest" and record["status"] == "applied":
                        for edit in record["after"].get("term_edits", [record["after"].get("term_edit")]):
                            if not edit:
                                continue
                            terminology[term_key(edit["original"])] = edit["replacement"]
                offset = 0
                for batch in review_batches(segments, limit=16 if MODEL_PROFILE == "35b" else 24):
                    nearby = [s for s in all_segments
                              if batch[0]["start_ms"] - 90000 <= s["start_ms"] <= batch[-1]["end_ms"] + 90000]
                    batch_ids = {s["id"] for s in batch}
                    references = select_vocabulary(vocabulary, nearby)
                    evidence = {}
                    if audio_path:
                        db.update_job(job_id, "running", mode, offset / len(segments),
                                      f"本地重聽短段 {offset}/{len(segments)} 段；先重新辨識，再審核候選")
                        try:
                            evidence = worker("listen", {"audio": audio_path,
                                "start_ms": max(0, batch[0]["start_ms"] - 2000),
                                "end_ms": min(batch[-1]["end_ms"] + 2000,
                                              batch[0]["start_ms"] + 90000)}, timeout=720)
                        except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
                            evidence = {"error": "短段重聽失敗，僅有文字語境；需人工重聽。", "detail": str(exc)[-300:]}
                    db.update_job(job_id, "running", mode, offset / len(segments),
                                  f"本地雙輪校訂 {offset}/{len(segments)} 段；參考上下文與講義詞彙")
                    payload = worker(mode, {"model": str(MODEL_DIR), "context": context,
                        "title": db.get_course(course_id)["title"], "segments": batch,
                        "neighbors": [s for s in nearby if s["id"] not in batch_ids],
                        "vocabulary": references,
                        "terminology": terminology, "audio_evidence": evidence}, timeout=1200)
                    archive_dir = db.DATA_DIR / "medical_review_runs" / job_id
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    archive = archive_dir / f'{batch[0]["start_ms"]}.json'
                    temporary = archive.with_suffix(".partial")
                    temporary.write_text(json.dumps({"course_id": course_id, "model": MODEL_LABEL,
                        "start_ms": batch[0]["start_ms"], "end_ms": batch[-1]["end_ms"],
                        "vocabulary": references, "vocabulary_note": vocabulary_error,
                        "vocabulary_error_detail": vocabulary_diagnostic,
                        "audio_evidence": evidence, "result": payload}, ensure_ascii=False, indent=2))
                    temporary.replace(archive)
                    proposals = validate_suggestions(payload, batch, terminology, evidence)
                    with db.connect() as conn:
                        for sid, before, after, reason in proposals:
                            edits = after["term_edits"]
                            after = {**after, "context": context, "term_edit": edits[0],
                                "audio_evidence": evidence,
                                "second_asr_agrees": all(evidence_for_term(evidence, edit["replacement"], before) for edit in edits),
                                "spelling_references": [r for r in references
                                    if any(contains_term(r["term"], edit["replacement"]) for edit in edits)],
                                "vocabulary_note": vocabulary_error,
                                "review_method": "local-context-audio-glossary-v4", "run_archive": str(archive)}
                            # Repeated runs do not duplicate the same pending suggestion.
                            pending = conn.execute("SELECT after_json FROM transcript_revisions WHERE course_id=? AND segment_id=? AND kind='suggest' AND status='pending' AND before_json=?",
                                (course_id, sid, json.dumps(before, ensure_ascii=False))).fetchall()
                            duplicate = any(json.loads(row["after_json"])["text"] == after["text"] for row in pending)
                            if not duplicate:
                                add_revision(conn, course_id, sid, mode, before, after, reason,
                                             MODEL_LABEL)
                                done += 1
                    offset += len(batch)
            else:
                # Only edited segments need corrected-text alignment; avoid running unchanged lectures.
                edited = [s for s in segments if s["source"] == "manual"]
                if not edited:
                    raise ValueError("這個區間尚無採用建議或手動校訂的段落。")
                latest_alignment = {}
                for record in history(course_id):
                    if record["kind"] == "align":
                        latest_alignment.setdefault(record["segment_id"], record)
                edited = [s for s in edited if not (
                    (record := latest_alignment.get(s["id"])) and record["status"] == "applied"
                    and snapshot(record["after"]) == snapshot(s))]
                for index, segment in enumerate(edited):
                    db.update_job(job_id, "running", mode, index / len(edited),
                                  f"校訂文字重新對齊音訊 {index + 1}/{len(edited)}；CPU 運算可能較久")
                    try:
                        if segment["end_ms"] - segment["start_ms"] > 60000:
                            raise ValueError("段落超過 60 秒，請先拆成較短段落再對齊。")
                        current = db.list_segments(course_id)
                        before_end = max((s["end_ms"] for s in current if s["end_ms"] <= segment["start_ms"] and s["id"] != segment["id"]), default=0)
                        next_start = min((s["start_ms"] for s in current if s["start_ms"] >= segment["end_ms"] and s["id"] != segment["id"]),
                                         default=round((db.get_course(course_id).get("duration_seconds") or segment["end_ms"] / 1000) * 1000))
                        result = worker(mode, {"checkpoint": str(CHECKPOINT), "audio": audio_path, "segment": segment,
                            "clip_start_ms": max(before_end, segment["start_ms"] - 2000),
                            "clip_end_ms": min(next_start, segment["end_ms"] + 2000)})
                        save_alignment(course_id, segment, result)
                        done += 1
                    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        failed += 1
                        with db.connect() as conn:
                            add_revision(conn, course_id, segment["id"], "align", snapshot(segment), snapshot(segment),
                                         str(exc)[:1200], "stable-ts / Large v3", "failed")
        message = (f"產生 {done} 個校正建議；請開啟「醫學校訂」逐項重聽與採用。" if mode == "suggest"
                   else f"已對齊 {done} 段，{failed} 段未通過檢查並保留原時間；詳見校訂紀錄。")
        all_failed = mode == "align" and failed > 0 and done == 0
        db.update_job(job_id, "error" if all_failed else "completed", "failed" if all_failed else "done", 1,
                      message, message if all_failed else None)
    except Exception as exc:
        db.update_job(job_id, "error", "failed", 1, "本地校訂未完成；已產生的紀錄仍保留。", str(exc)[:1200])
    finally:
        with jobs._lock:
            jobs._active_by_course.pop(course_id, None)
