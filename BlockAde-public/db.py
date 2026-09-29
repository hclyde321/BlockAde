"""SQLite persistence and local data paths for the course app."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", APP_ROOT / "data")).resolve()
DB_PATH = Path(os.environ.get("APP_DB_PATH", DATA_DIR / "app.sqlite3")).resolve()

SUBJECTS = (
    ("pathology", "病理學"),
    ("pharmacology", "藥理學"),
    ("clinical", "臨床醫學"),
    ("laboratory", "檢驗醫學"),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back on context exit, then release the connection."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS courses (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                processing_status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                playback_position_seconds REAL NOT NULL DEFAULT 0,
                playback_rate REAL NOT NULL DEFAULT 1,
                is_playing INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL
            );
            CREATE TABLE IF NOT EXISTS timetable_courses (
                lesson_key TEXT PRIMARY KEY,
                course_id TEXT NOT NULL UNIQUE REFERENCES courses(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('audio','questions','answers')),
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(course_id, kind)
            );
            CREATE TABLE IF NOT EXISTS audio_parts (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                duration_seconds REAL,
                status TEXT NOT NULL CHECK(status IN ('pending','ready')),
                created_at TEXT NOT NULL,
                UNIQUE(course_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS idx_audio_parts_course ON audio_parts(course_id, ordinal);
            CREATE TABLE IF NOT EXISTS segments (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                text TEXT NOT NULL,
                original_text TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                UNIQUE(course_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS transcript_revisions (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                segment_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                decided_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_revisions_course ON transcript_revisions(course_id, created_at);
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                section TEXT NOT NULL DEFAULT '',
                number TEXT NOT NULL,
                stem TEXT NOT NULL,
                options_json TEXT NOT NULL DEFAULT '[]',
                source_page INTEGER,
                source_text TEXT NOT NULL DEFAULT '',
                needs_review INTEGER NOT NULL DEFAULT 0,
                answer_key TEXT,
                explanation TEXT NOT NULL DEFAULT '',
                answer_source_page INTEGER,
                answer_source_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(course_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS matches (
                id TEXT PRIMARY KEY,
                question_id TEXT NOT NULL UNIQUE REFERENCES questions(id) ON DELETE CASCADE,
                segment_id TEXT REFERENCES segments(id) ON DELETE SET NULL,
                start_ms INTEGER,
                end_ms INTEGER,
                question_time_ms INTEGER,
                status TEXT NOT NULL DEFAULT 'pending_confirmation',
                confidence REAL,
                candidate_score REAL,
                reason TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'local_lexical',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY,
                question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                selected_option TEXT NOT NULL,
                correct INTEGER NOT NULL,
                attempted_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_segments_course_start ON segments(course_id, start_ms);
            CREATE INDEX IF NOT EXISTS idx_questions_course_ordinal ON questions(course_id, ordinal);
            CREATE INDEX IF NOT EXISTS idx_attempts_question_time ON attempts(question_id, attempted_at);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                kind TEXT NOT NULL DEFAULT 'process',
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_course_created ON jobs(course_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS transcription_previews (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL DEFAULT 0,
                completed_chunks INTEGER NOT NULL DEFAULT 0,
                total_chunks INTEGER NOT NULL DEFAULT 0,
                segments_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS pdf_imports (
                course_id TEXT PRIMARY KEY REFERENCES courses(id) ON DELETE CASCADE,
                questions_asset_id TEXT,
                answers_asset_id TEXT,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS course_handouts (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                page_count INTEGER NOT NULL,
                section_count INTEGER NOT NULL,
                pages_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_course_handouts_course ON course_handouts(course_id, created_at);
            """
        )
        # Small in-place migration for databases created during an MVP rollout.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(questions)").fetchall()}
        if "section" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN section TEXT NOT NULL DEFAULT ''")
        if "needs_review" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0")
        job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "kind" not in job_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'process'")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subjects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS banks (
                id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL REFERENCES subjects(id),
                title TEXT NOT NULL,
                legacy_course_id TEXT UNIQUE REFERENCES courses(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bank_assets (
                id TEXT PRIMARY KEY,
                bank_id TEXT NOT NULL REFERENCES banks(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('questions','answers')),
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(bank_id, kind)
            );
            CREATE TABLE IF NOT EXISTS bank_questions (
                id TEXT PRIMARY KEY,
                bank_id TEXT NOT NULL REFERENCES banks(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                section TEXT NOT NULL DEFAULT '',
                number TEXT NOT NULL,
                stem TEXT NOT NULL,
                options_json TEXT NOT NULL DEFAULT '[]',
                source_page INTEGER,
                source_text TEXT NOT NULL DEFAULT '',
                needs_review INTEGER NOT NULL DEFAULT 0,
                answer_key TEXT,
                explanation TEXT NOT NULL DEFAULT '',
                answer_source_page INTEGER,
                answer_source_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(bank_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS learning_rounds (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                UNIQUE(course_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS listening_intervals (
                id TEXT PRIMARY KEY,
                round_id TEXT NOT NULL REFERENCES learning_rounds(id) ON DELETE CASCADE,
                start_seconds REAL NOT NULL,
                end_seconds REAL NOT NULL,
                CHECK(start_seconds >= 0 AND end_seconds > start_seconds)
            );
            CREATE INDEX IF NOT EXISTS idx_banks_subject ON banks(subject_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_bank_questions_bank ON bank_questions(bank_id, ordinal);
            CREATE INDEX IF NOT EXISTS idx_rounds_course ON learning_rounds(course_id, ordinal);
            CREATE INDEX IF NOT EXISTS idx_listening_round ON listening_intervals(round_id, start_seconds);
            CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY);
            """
        )
        for subject_id, name in SUBJECTS:
            conn.execute("INSERT INTO subjects(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                         (subject_id, name))
        course_columns = {row[1] for row in conn.execute("PRAGMA table_info(courses)")}
        if "subject_id" not in course_columns:
            conn.execute("ALTER TABLE courses ADD COLUMN subject_id TEXT NOT NULL DEFAULT 'clinical'")
        if "current_round_id" not in course_columns:
            conn.execute("ALTER TABLE courses ADD COLUMN current_round_id TEXT REFERENCES learning_rounds(id)")
        if "transcription_chunk_minutes" not in course_columns:
            conn.execute("ALTER TABLE courses ADD COLUMN transcription_chunk_minutes INTEGER NOT NULL DEFAULT 5")
        conn.execute(
            """INSERT INTO audio_parts(id,course_id,ordinal,path,filename,content_type,size_bytes,
                                        duration_seconds,status,created_at)
               SELECT a.id,a.course_id,0,a.path,a.filename,a.content_type,a.size_bytes,
                      c.duration_seconds,'ready',a.created_at
               FROM assets a JOIN courses c ON c.id=a.course_id
               WHERE a.kind='audio' AND NOT EXISTS
                     (SELECT 1 FROM audio_parts p WHERE p.course_id=a.course_id)"""
        )
        question_columns = {row[1] for row in conn.execute("PRAGMA table_info(questions)")}
        if "bank_question_id" not in question_columns:
            conn.execute("ALTER TABLE questions ADD COLUMN bank_question_id TEXT REFERENCES bank_questions(id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_course_bank ON questions(course_id,bank_question_id)")
        attempt_columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
        if "round_id" not in attempt_columns:
            conn.execute("ALTER TABLE attempts ADD COLUMN round_id TEXT REFERENCES learning_rounds(id)")
        for course in conn.execute("SELECT id,created_at FROM courses WHERE current_round_id IS NULL").fetchall():
            round_id = new_id()
            conn.execute("INSERT INTO learning_rounds(id,course_id,ordinal,started_at) VALUES(?,?,1,?)",
                         (round_id, course["id"], course["created_at"]))
            conn.execute("UPDATE courses SET current_round_id=? WHERE id=?", (round_id, course["id"]))
        conn.execute("""UPDATE attempts SET round_id=(SELECT c.current_round_id FROM questions q
                      JOIN courses c ON c.id=q.course_id WHERE q.id=attempts.question_id)
                      WHERE round_id IS NULL""")
        _migrate_legacy_banks(conn)


def _migrate_legacy_banks(conn: sqlite3.Connection) -> None:
    """Move pre-bank PDF imports into named banks without replacing course rows."""
    if conn.execute("SELECT 1 FROM schema_migrations WHERE name='legacy_banks_v1'").fetchone():
        return
    rows = conn.execute(
        """SELECT c.id,c.title,c.subject_id FROM courses c
           WHERE EXISTS(SELECT 1 FROM assets a WHERE a.course_id=c.id AND a.kind IN ('questions','answers'))
              OR EXISTS(SELECT 1 FROM pdf_imports p WHERE p.course_id=c.id)"""
    ).fetchall()
    for course in rows:
        bank_id, now = new_id(), now_iso()
        conn.execute("""INSERT INTO banks(id,subject_id,title,legacy_course_id,created_at,updated_at)
                        VALUES(?,?,?,?,?,?)""",
                     (bank_id, course["subject_id"], f"{course['title']} 題庫", course["id"], now, now))
        for asset in conn.execute("SELECT * FROM assets WHERE course_id=? AND kind IN ('questions','answers')",
                                  (course["id"],)).fetchall():
            conn.execute("""INSERT INTO bank_assets(id,bank_id,kind,path,filename,content_type,size_bytes,created_at)
                            VALUES(?,?,?,?,?,?,?,?)""",
                         (new_id(), bank_id, asset["kind"], asset["path"], asset["filename"],
                          asset["content_type"], asset["size_bytes"], asset["created_at"]))
        for question in conn.execute("SELECT * FROM questions WHERE course_id=? ORDER BY ordinal",
                                     (course["id"],)).fetchall():
            bank_question_id = new_id()
            conn.execute("""INSERT INTO bank_questions
                            (id,bank_id,ordinal,section,number,stem,options_json,source_page,source_text,
                             needs_review,answer_key,explanation,answer_source_page,answer_source_text,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (bank_question_id, bank_id, question["ordinal"], question["section"], question["number"],
                          question["stem"], question["options_json"], question["source_page"], question["source_text"],
                          question["needs_review"], question["answer_key"], question["explanation"],
                          question["answer_source_page"], question["answer_source_text"], question["created_at"]))
            conn.execute("UPDATE questions SET bank_question_id=? WHERE id=?", (bank_question_id, question["id"]))
    conn.execute("INSERT INTO schema_migrations(name) VALUES('legacy_banks_v1')")


def new_id() -> str:
    return str(uuid.uuid4())


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def list_subjects() -> list[dict[str, str]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute("SELECT id,name FROM subjects ORDER BY CASE id WHEN 'pathology' THEN 1 WHEN 'pharmacology' THEN 2 WHEN 'clinical' THEN 3 ELSE 4 END")]


def _require_subject(conn: sqlite3.Connection, subject_id: str) -> None:
    if not conn.execute("SELECT 1 FROM subjects WHERE id=?", (subject_id,)).fetchone():
        raise ValueError("Unknown subject")


def get_course(course_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        course = row_to_dict(conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone())
    if course:
        course["learning"] = learning_summary(course_id)
    return course


def create_course(title: str, subject_id: str = "clinical", transcription_chunk_minutes: int = 5, timetable_key: str | None = None) -> dict[str, Any]:
    if type(transcription_chunk_minutes) is not int or transcription_chunk_minutes not in (5, 10):
        raise ValueError("轉錄分段僅支援 5 或 10 分鐘。")
    if timetable_key is not None and (not isinstance(timetable_key, str) or not timetable_key or len(timetable_key)>200):
        raise ValueError("課表連結無效。")
    course_id = new_id()
    round_id = new_id()
    now = now_iso()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if timetable_key:
            existing=conn.execute("SELECT course_id FROM timetable_courses WHERE lesson_key=?", (timetable_key,)).fetchone()
            if existing:
                return get_course(existing["course_id"]) or {}
        _require_subject(conn, subject_id)
        conn.execute(
            "INSERT INTO courses(id,title,subject_id,created_at,updated_at,transcription_chunk_minutes) VALUES(?,?,?,?,?,?)",
            (course_id, title, subject_id, now, now, transcription_chunk_minutes),
        )
        conn.execute("INSERT INTO learning_rounds(id,course_id,ordinal,started_at) VALUES(?,?,1,?)",
                     (round_id, course_id, now))
        conn.execute("UPDATE courses SET current_round_id=? WHERE id=?", (round_id, course_id))
        if timetable_key:
            conn.execute("INSERT INTO timetable_courses(lesson_key,course_id) VALUES(?,?)", (timetable_key,course_id))
    return get_course(course_id) or {}


def list_courses() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.id,c.title,c.subject_id,c.processing_status,c.created_at,
                      (SELECT lesson_key FROM timetable_courses t WHERE t.course_id=c.id) AS timetable_key,
                      (SELECT COUNT(*) FROM questions q WHERE q.course_id=c.id) AS question_count,
                      (SELECT COUNT(*) FROM segments s WHERE s.course_id=c.id) AS segment_count
               FROM courses c ORDER BY c.created_at DESC"""
        ).fetchall()
    return [{**dict(row), "learning": learning_summary(row["id"])} for row in rows]


def create_bank(title: str, subject_id: str) -> dict[str, Any]:
    bank_id, now = new_id(), now_iso()
    if not title.strip():
        raise ValueError("Bank title is required")
    with connect() as conn:
        _require_subject(conn, subject_id)
        conn.execute("INSERT INTO banks(id,subject_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (bank_id, subject_id, title.strip(), now, now))
    return get_bank(bank_id) or {}


def _bank_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    bank = dict(row)
    assets = {kind: None for kind in ("questions", "answers")}
    for asset in conn.execute("SELECT * FROM bank_assets WHERE bank_id=?", (bank["id"],)):
        assets[asset["kind"]] = {"id": asset["id"], "filename": asset["filename"],
                                  "content_type": asset["content_type"], "size_bytes": asset["size_bytes"]}
    bank["assets"] = assets
    bank["question_count"] = conn.execute("SELECT COUNT(*) FROM bank_questions WHERE bank_id=?", (bank["id"],)).fetchone()[0]
    bank["status"] = "ready" if bank["question_count"] else "draft"
    return bank


def get_bank(bank_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM banks WHERE id=?", (bank_id,)).fetchone()
        return _bank_from_row(conn, row) if row else None


def list_banks(subject_id: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if subject_id is not None:
            _require_subject(conn, subject_id)
        rows = conn.execute("SELECT * FROM banks WHERE (? IS NULL OR subject_id=?) ORDER BY created_at DESC,rowid DESC",
                            (subject_id, subject_id)).fetchall()
        return [_bank_from_row(conn, row) for row in rows]


def get_bank_asset(bank_id: str, kind: str) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM bank_assets WHERE bank_id=? AND kind=?",
                                        (bank_id, kind)).fetchone())


def set_bank_asset(bank_id: str, kind: str, path: Path, filename: str,
                   content_type: str, size_bytes: int) -> None:
    if kind not in {"questions", "answers"}:
        raise ValueError("Bank asset kind must be questions or answers")
    now = now_iso()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM banks WHERE id=?", (bank_id,)).fetchone():
            raise ValueError("Unknown bank")
        if conn.execute("SELECT 1 FROM bank_questions WHERE bank_id=? LIMIT 1", (bank_id,)).fetchone():
            raise ValueError("Imported bank assets are immutable")
        conn.execute("""INSERT INTO bank_assets(id,bank_id,kind,path,filename,content_type,size_bytes,created_at)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(bank_id,kind) DO UPDATE SET
                        id=excluded.id,path=excluded.path,filename=excluded.filename,
                        content_type=excluded.content_type,size_bytes=excluded.size_bytes,created_at=excluded.created_at""",
                     (new_id(), bank_id, kind, str(path.resolve()), filename, content_type, size_bytes, now))
        conn.execute("UPDATE banks SET updated_at=? WHERE id=?", (now, bank_id))


def _decode_question(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["options"] = json.loads(item.pop("options_json"))
    return item


def list_bank_questions(bank_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return [_decode_question(row) for row in conn.execute(
            "SELECT * FROM bank_questions WHERE bank_id=? ORDER BY ordinal", (bank_id,))]


def replace_bank_questions(bank_id: str, questions: list[dict[str, Any]]) -> list[str]:
    """Perform the initial import; subsequent imports require a new named bank."""
    ids: list[str] = []
    now = now_iso()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM banks WHERE id=?", (bank_id,)).fetchone():
            raise ValueError("Unknown bank")
        if conn.execute("SELECT 1 FROM bank_questions WHERE bank_id=? LIMIT 1", (bank_id,)).fetchone():
            raise ValueError("Imported bank questions are immutable; create a new bank")
        for ordinal, question in enumerate(questions):
            question_id = new_id()
            conn.execute("""INSERT INTO bank_questions
                            (id,bank_id,ordinal,section,number,stem,options_json,source_page,source_text,
                             needs_review,answer_key,explanation,answer_source_page,answer_source_text,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (question_id, bank_id, ordinal, str(question.get("section", "")),
                          str(question.get("number", ordinal + 1)), str(question.get("stem", "")).strip(),
                          json.dumps(question.get("options", []), ensure_ascii=False), question.get("source_page"),
                          question.get("source_text", ""), int(bool(question.get("needs_review", False))),
                          question.get("answer_key", question.get("correct_option")), question.get("explanation", ""),
                          question.get("answer_source_page"), question.get("answer_source_text", ""), now))
            ids.append(question_id)
        conn.execute("UPDATE banks SET updated_at=? WHERE id=?", (now, bank_id))
    return ids


def sync_subject_questions(course_id: str) -> int:
    """Copy missing canonical questions into one course, retaining existing copies."""
    inserted = 0
    now = now_iso()
    with connect() as conn:
        course = conn.execute("SELECT subject_id FROM courses WHERE id=?", (course_id,)).fetchone()
        if not course:
            raise ValueError("Unknown course")
        rows = conn.execute("""SELECT bq.* FROM bank_questions bq JOIN banks b ON b.id=bq.bank_id
                               WHERE b.subject_id=? ORDER BY b.created_at,b.rowid,bq.ordinal""",
                            (course["subject_id"],)).fetchall()
        ordinal = conn.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM questions WHERE course_id=?",
                               (course_id,)).fetchone()[0]
        for question in rows:
            if conn.execute("SELECT 1 FROM questions WHERE course_id=? AND bank_question_id=?",
                            (course_id, question["id"])).fetchone():
                continue
            conn.execute("""INSERT INTO questions
                            (id,course_id,bank_question_id,ordinal,section,number,stem,options_json,source_page,
                             source_text,needs_review,answer_key,explanation,answer_source_page,answer_source_text,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (new_id(), course_id, question["id"], ordinal, question["section"], question["number"],
                          question["stem"], question["options_json"], question["source_page"], question["source_text"],
                          question["needs_review"], question["answer_key"], question["explanation"],
                          question["answer_source_page"], question["answer_source_text"], now))
            ordinal += 1
            inserted += 1
        if inserted:
            conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (now, course_id))
    return inserted


def get_asset(course_id: str, kind: str) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM assets WHERE course_id=? AND kind=?", (course_id, kind)).fetchone()
        )


def set_asset(course_id: str, kind: str, path: Path, filename: str, content_type: str, size_bytes: int) -> None:
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO assets(id,course_id,kind,path,filename,content_type,size_bytes,created_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(course_id,kind) DO UPDATE SET
                 id=excluded.id,path=excluded.path,filename=excluded.filename,
                 content_type=excluded.content_type,size_bytes=excluded.size_bytes,created_at=excluded.created_at""",
            (new_id(), course_id, kind, str(path.resolve()), filename, content_type, size_bytes, now),
        )
        conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (now, course_id))


def list_audio_parts(course_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audio_parts WHERE course_id=? ORDER BY ordinal", (course_id,)).fetchall()
    return [dict(row) for row in rows]


def audio_part_metadata(course_id: str) -> list[dict[str, Any]]:
    return [{key: part[key] for key in ("id", "ordinal", "filename", "content_type",
                                       "size_bytes", "duration_seconds", "status")}
            for part in list_audio_parts(course_id)]


def add_audio_part(course_id: str, path: Path, filename: str, content_type: str,
                   size_bytes: int, duration_seconds: float) -> dict[str, Any]:
    part_id, now = new_id(), now_iso()
    with connect() as conn:
        ordinal = conn.execute(
            "SELECT COALESCE(MAX(ordinal),-1)+1 FROM audio_parts WHERE course_id=?", (course_id,)
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO audio_parts(id,course_id,ordinal,path,filename,content_type,size_bytes,
                                       duration_seconds,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (part_id, course_id, ordinal, str(path.resolve()), filename, content_type,
             size_bytes, duration_seconds, "pending", now),
        )
        conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (now, course_id))
    return {"id": part_id, "ordinal": ordinal, "filename": filename,
            "content_type": content_type, "size_bytes": size_bytes,
            "duration_seconds": duration_seconds, "status": "pending"}


def replace_single_audio_part(course_id: str, asset: dict[str, Any]) -> None:
    """Keep the legacy single-file upload compatible with the part list."""
    with connect() as conn:
        conn.execute("DELETE FROM audio_parts WHERE course_id=?", (course_id,))
        conn.execute(
            """INSERT INTO audio_parts(id,course_id,ordinal,path,filename,content_type,size_bytes,
                                       duration_seconds,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (asset["id"], course_id, 0, asset["path"], asset["filename"], asset["content_type"],
             asset["size_bytes"], None, "ready", asset["created_at"]),
        )


def set_single_audio_asset(course_id: str, path: Path, filename: str,
                           content_type: str, size_bytes: int) -> None:
    """Replace the legacy audio asset and its one-part manifest atomically."""
    asset_id, now = new_id(), now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO assets(id,course_id,kind,path,filename,content_type,size_bytes,created_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(course_id,kind) DO UPDATE SET
                 id=excluded.id,path=excluded.path,filename=excluded.filename,
                 content_type=excluded.content_type,size_bytes=excluded.size_bytes,created_at=excluded.created_at""",
            (asset_id, course_id, "audio", str(path.resolve()), filename, content_type, size_bytes, now),
        )
        conn.execute("DELETE FROM audio_parts WHERE course_id=?", (course_id,))
        conn.execute(
            """INSERT INTO audio_parts(id,course_id,ordinal,path,filename,content_type,size_bytes,
                                       duration_seconds,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (asset_id, course_id, 0, str(path.resolve()), filename, content_type,
             size_bytes, None, "ready", now),
        )
        conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (now, course_id))


def publish_audio_parts(course_id: str, expected_pending_ids: list[str], path: Path,
                        duration_seconds: float, segments: list[dict[str, Any]],
                        old_duration_seconds: float = 0.0) -> None:
    """Publish a merged playable recording and its new transcript together."""
    if not expected_pending_ids:
        raise ValueError("沒有待合併的錄音。")
    now = now_iso()
    with connect() as conn:
        pending = conn.execute(
            "SELECT id FROM audio_parts WHERE course_id=? AND status='pending' ORDER BY ordinal", (course_id,)
        ).fetchall()
        if [row["id"] for row in pending] != expected_pending_ids:
            raise RuntimeError("錄音清單在合併期間已變更，請重新合併。")
        if conn.execute(
            "SELECT 1 FROM segments WHERE course_id=? AND end_ms>? LIMIT 1",
            (course_id, round(old_duration_seconds * 1000) + 100),
        ).fetchone():
            raise RuntimeError("合併期間逐字稿已延伸到新錄音範圍，請先檢查重疊段落。")
        asset_id = new_id()
        conn.execute(
            """INSERT INTO assets(id,course_id,kind,path,filename,content_type,size_bytes,created_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(course_id,kind) DO UPDATE SET
                 id=excluded.id,path=excluded.path,filename=excluded.filename,
                 content_type=excluded.content_type,size_bytes=excluded.size_bytes,created_at=excluded.created_at""",
            (asset_id, course_id, "audio", str(path.resolve()), "合併錄音.wav", "audio/wav",
             path.stat().st_size, now),
        )
        ordinal = conn.execute(
            "SELECT COALESCE(MAX(ordinal),-1)+1 FROM segments WHERE course_id=?", (course_id,)
        ).fetchone()[0]
        for offset, item in enumerate(segments):
            start_ms, end_ms = int(item["start_ms"]), int(item["end_ms"])
            text = str(item["text"]).strip()
            if not text or start_ms < 0 or end_ms < start_ms or end_ms > round(duration_seconds * 1000) + 100:
                raise ValueError("新逐字稿的時間或文字無效，原錄音已保留。")
            conn.execute(
                """INSERT INTO segments(id,course_id,ordinal,start_ms,end_ms,text,original_text,source)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (new_id(), course_id, ordinal + offset, start_ms, end_ms, text,
                 str(item.get("original_text", text)), "whisper"),
            )
        conn.executemany("UPDATE audio_parts SET status='ready' WHERE id=?", [(item,) for item in expected_pending_ids])
        conn.execute(
            "UPDATE courses SET duration_seconds=?,processing_status='ready',updated_at=? WHERE id=?",
            (duration_seconds, now, course_id),
        )
        _invalidate_uncertain_handout_matches(conn, course_id)


def set_course_status(course_id: str, status: str, duration_seconds: float | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE courses SET processing_status=?,duration_seconds=COALESCE(?,duration_seconds),updated_at=? WHERE id=?",
            (status, duration_seconds, now_iso(), course_id),
        )


def add_segments(course_id: str, segments: list[dict[str, Any]], source: str = "manual", replace: bool = False,
                 reject_manual: bool = False) -> list[str]:
    ids: list[str] = []
    with connect() as conn:
        if reject_manual and conn.execute(
            "SELECT 1 FROM segments WHERE course_id=? AND source='manual' LIMIT 1", (course_id,)
        ).fetchone():
            raise RuntimeError("轉錄期間逐字稿已有人工修改；新轉錄未覆蓋人工內容。")
        if replace:
            # A retry may replace generated transcript rows, but never user-authored
            # or user-corrected rows.
            conn.execute("DELETE FROM segments WHERE course_id=? AND source=?", (course_id, source))
        start_ordinal = conn.execute(
            "SELECT COALESCE(MAX(ordinal),-1)+1 FROM segments WHERE course_id=?", (course_id,)
        ).fetchone()[0]
        for offset, segment in enumerate(segments):
            segment_id = new_id()
            start_ms = int(segment["start_ms"])
            end_ms = int(segment["end_ms"])
            text = str(segment["text"]).strip()
            conn.execute(
                """INSERT INTO segments(id,course_id,ordinal,start_ms,end_ms,text,original_text,source)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (segment_id, course_id, start_ordinal + offset, start_ms, end_ms, text,
                 segment.get("original_text", text), source),
            )
            ids.append(segment_id)
        if replace:
            rows = conn.execute(
                "SELECT id FROM segments WHERE course_id=? ORDER BY start_ms,end_ms,ordinal", (course_id,)
            ).fetchall()
            conn.execute("UPDATE segments SET ordinal=ordinal+1000000 WHERE course_id=?", (course_id,))
            for ordinal, row in enumerate(rows):
                conn.execute("UPDATE segments SET ordinal=? WHERE id=?", (ordinal, row["id"]))
        conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (now_iso(), course_id))
    return ids


def replace_range_segments(course_id: str, start_ms: int, end_ms: int,
                           expected_ids: set[str], segments: list[dict[str, Any]]) -> list[str]:
    """Replace generated rows in a time interval in one transaction.

    The expected row set protects against edits made while Whisper was running.
    """
    if start_ms < 0 or end_ms <= start_ms or not segments:
        raise ValueError("指定範圍沒有有效的轉錄內容，原逐字稿已保留。")
    prepared = []
    for item in segments:
        item_start, item_end = int(item["start_ms"]), int(item["end_ms"])
        item_text = str(item["text"]).strip()
        if item_start < start_ms or item_end > end_ms or item_end < item_start or not item_text:
            raise ValueError("新逐字稿的時間或文字無效，原逐字稿已保留。")
        prepared.append((item_start, item_end, item_text, str(item.get("original_text", item_text))))
    ids: list[str] = []
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,source FROM segments WHERE course_id=? AND start_ms<? AND end_ms>?",
            (course_id, end_ms, start_ms),
        ).fetchall()
        if any(row["source"] != "whisper" for row in rows):
            raise ValueError("指定範圍含人工修正的逐字稿，已保留原內容。")
        if {row["id"] for row in rows} != expected_ids:
            raise ValueError("轉錄期間逐字稿已變更，請重新選取範圍再試。")
        conn.executemany("DELETE FROM segments WHERE id=?", [(row["id"],) for row in rows])
        ordinal = conn.execute(
            "SELECT COALESCE(MAX(ordinal),-1)+1 FROM segments WHERE course_id=?", (course_id,)
        ).fetchone()[0]
        for offset, (item_start, item_end, item_text, original_text) in enumerate(prepared):
            segment_id = new_id()
            conn.execute(
                "INSERT INTO segments(id,course_id,ordinal,start_ms,end_ms,text,original_text,source) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (segment_id, course_id, ordinal + offset, item_start, item_end,
                 item_text, original_text, "whisper"),
            )
            ids.append(segment_id)
        ordered = conn.execute(
            "SELECT id FROM segments WHERE course_id=? ORDER BY start_ms,end_ms,ordinal", (course_id,)
        ).fetchall()
        conn.execute("UPDATE segments SET ordinal=ordinal+1000000 WHERE course_id=?", (course_id,))
        for offset, row in enumerate(ordered):
            conn.execute("UPDATE segments SET ordinal=? WHERE id=?", (offset, row["id"]))
        conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (now_iso(), course_id))
    return ids


def list_segments(course_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM segments WHERE course_id=? ORDER BY ordinal", (course_id,)).fetchall()
    return [dict(row) for row in rows]


def add_question(course_id: str, question: dict[str, Any]) -> str:
    question_id = new_id()
    now = now_iso()
    with connect() as conn:
        ordinal = conn.execute(
            "SELECT COALESCE(MAX(ordinal),-1)+1 FROM questions WHERE course_id=?", (course_id,)
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO questions(id,course_id,ordinal,section,number,stem,options_json,source_page,source_text,
                                      needs_review,answer_key,explanation,answer_source_page,answer_source_text,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (question_id, course_id, ordinal, str(question.get("section", "")), str(question.get("number", ordinal + 1)),
             str(question.get("stem", "")).strip(), json.dumps(question.get("options", []), ensure_ascii=False),
             question.get("source_page"), question.get("source_text", ""), int(bool(question.get("needs_review", False))),
             question.get("answer_key", question.get("correct_option")), question.get("explanation", ""),
             question.get("answer_source_page"), question.get("answer_source_text", ""), now),
        )
        conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (now, course_id))
    return question_id


def list_questions(course_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("""SELECT q.*,b.id AS bank_id,b.title AS bank_title FROM questions q
                               LEFT JOIN bank_questions bq ON bq.id=q.bank_question_id
                               LEFT JOIN banks b ON b.id=bq.bank_id
                               WHERE q.course_id=? ORDER BY q.ordinal""", (course_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["options"] = json.loads(item.pop("options_json"))
        result.append(item)
    return result


def save_match(question_id: str, match: dict[str, Any]) -> str:
    now = now_iso()
    with connect() as conn:
        previous = conn.execute("SELECT id FROM matches WHERE question_id=?", (question_id,)).fetchone()
        match_id = previous["id"] if previous else new_id()
        conn.execute(
            """INSERT INTO matches(id,question_id,segment_id,start_ms,end_ms,question_time_ms,status,
                                    confidence,candidate_score,reason,evidence,provider,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(question_id) DO UPDATE SET
                 segment_id=excluded.segment_id,start_ms=excluded.start_ms,end_ms=excluded.end_ms,
                 question_time_ms=excluded.question_time_ms,status=excluded.status,confidence=excluded.confidence,
                 candidate_score=excluded.candidate_score,reason=excluded.reason,evidence=excluded.evidence,
                 provider=excluded.provider,updated_at=excluded.updated_at""",
            (match_id, question_id, match.get("segment_id"), match.get("start_ms"), match.get("end_ms"),
             match.get("question_time_ms"), match.get("status", "pending_confirmation"), match.get("confidence"),
             match.get("candidate_score"), match.get("reason", ""), match.get("evidence", ""),
             match.get("provider", "local_lexical"), now),
        )
    return match_id


def list_matches(course_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT m.*,q.course_id FROM matches m JOIN questions q ON q.id=m.question_id
               WHERE q.course_id=? ORDER BY q.ordinal""", (course_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def add_attempt(question_id: str, selected_option: str, correct: bool) -> dict[str, Any]:
    attempted_at = now_iso()
    attempt_id = new_id()
    with connect() as conn:
        course = conn.execute("""SELECT c.current_round_id FROM questions q
                                 JOIN courses c ON c.id=q.course_id WHERE q.id=?""", (question_id,)).fetchone()
        if not course:
            raise ValueError("Unknown question")
        round_id = course["current_round_id"]
        conn.execute(
            "INSERT INTO attempts(id,question_id,selected_option,correct,attempted_at,round_id) VALUES(?,?,?,?,?,?)",
            (attempt_id, question_id, selected_option, int(correct), attempted_at, round_id),
        )
    return {"id": attempt_id, "question_id": question_id, "selected_option": selected_option,
            "correct": bool(correct), "attempted_at": attempted_at, "round_id": round_id}


def list_attempts(course_id: str, history: bool = False) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT a.* FROM attempts a JOIN questions q ON q.id=a.question_id
               JOIN courses c ON c.id=q.course_id
               WHERE q.course_id=? AND (? OR a.round_id=c.current_round_id)
               ORDER BY a.attempted_at,a.rowid""", (course_id, int(history))
        ).fetchall()
    return [dict(row) for row in rows]


def latest_attempt(question_id: str, history: bool = False) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute(
            """SELECT a.* FROM attempts a JOIN questions q ON q.id=a.question_id
               JOIN courses c ON c.id=q.course_id
               WHERE a.question_id=? AND (? OR a.round_id=c.current_round_id)
               ORDER BY a.attempted_at DESC,a.rowid DESC LIMIT 1""",
            (question_id, int(history)),
        ).fetchone())


def _union_seconds(intervals: list[tuple[float, float]]) -> float:
    total, end = 0.0, 0.0
    for start, stop in sorted(intervals):
        if stop <= end:
            continue
        total += stop - max(start, end)
        end = stop
    return total


def _round_summary(conn: sqlite3.Connection, course: sqlite3.Row, round_row: sqlite3.Row) -> dict[str, Any]:
    round_id = round_row["id"]
    intervals = [(row[0], row[1]) for row in conn.execute(
        "SELECT start_seconds,end_seconds FROM listening_intervals WHERE round_id=?", (round_id,))]
    listened = _union_seconds(intervals)
    duration = course["duration_seconds"]
    progress = min(1.0, listened / duration) if duration and duration > 0 else 0.0
    question_count = conn.execute("SELECT COUNT(*) FROM questions WHERE course_id=?", (course["id"],)).fetchone()[0]
    latest = conn.execute("""SELECT a.question_id,a.correct FROM attempts a
                             JOIN questions q ON q.id=a.question_id
                             WHERE q.course_id=? AND a.round_id=? AND a.rowid IN
                             (SELECT MAX(a2.rowid) FROM attempts a2 WHERE a2.round_id=? GROUP BY a2.question_id)""",
                          (course["id"], round_id, round_id)).fetchall()
    answered_count = len(latest)
    wrong_count = sum(not bool(row["correct"]) for row in latest)
    listening_complete = bool(duration and duration > 0 and progress >= 0.9 - 1e-9)
    quiz_complete = question_count > 0 and answered_count == question_count
    return {"round_id": round_id, "round_number": round_row["ordinal"], "started_at": round_row["started_at"],
            "listened_seconds": listened, "duration_seconds": duration, "progress": progress,
            "listening_complete": listening_complete, "question_count": question_count,
            "answered_count": answered_count, "wrong_count": wrong_count, "quiz_complete": quiz_complete,
            "completed": listening_complete}


def learning_summary(course_id: str) -> dict[str, Any]:
    with connect() as conn:
        course = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        if not course:
            raise ValueError("Unknown course")
        rounds = conn.execute("SELECT * FROM learning_rounds WHERE course_id=? ORDER BY ordinal", (course_id,)).fetchall()
        current = next((row for row in rounds if row["id"] == course["current_round_id"]), None)
        if current is None:
            raise RuntimeError("Course has no current learning round")
        result = _round_summary(conn, course, current)
        result["completed_rounds"] = sum(_round_summary(conn, course, row)["completed"] for row in rounds)
        return result


def record_listening(course_id: str, start_seconds: float, end_seconds: float,
                     round_id: str | None = None) -> dict[str, Any]:
    start, end = float(start_seconds), float(end_seconds)
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
        raise ValueError("Invalid listening interval")
    with connect() as conn:
        course = conn.execute("SELECT current_round_id,duration_seconds FROM courses WHERE id=?", (course_id,)).fetchone()
        if not course:
            raise ValueError("Unknown course")
        if round_id is not None and round_id != course["current_round_id"]:
            raise ValueError("Listening round is no longer current")
        duration = course["duration_seconds"]
        if duration is None or duration <= 0 or end > duration + 0.001:
            raise ValueError("Listening interval exceeds known audio duration")
        conn.execute("INSERT INTO listening_intervals(id,round_id,start_seconds,end_seconds) VALUES(?,?,?,?)",
                     (new_id(), course["current_round_id"], start, min(end, duration)))
    return learning_summary(course_id)


def restart_learning(course_id: str) -> dict[str, Any]:
    now, round_id = now_iso(), new_id()
    with connect() as conn:
        course = conn.execute("SELECT id FROM courses WHERE id=?", (course_id,)).fetchone()
        if not course:
            raise ValueError("Unknown course")
        ordinal = conn.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM learning_rounds WHERE course_id=?",
                               (course_id,)).fetchone()[0]
        conn.execute("INSERT INTO learning_rounds(id,course_id,ordinal,started_at) VALUES(?,?,?,?)",
                     (round_id, course_id, ordinal, now))
        conn.execute("""UPDATE courses SET current_round_id=?,playback_position_seconds=0,
                        is_playing=0,updated_at=? WHERE id=?""", (round_id, now, course_id))
    return learning_summary(course_id)


def learning_history(course_id: str) -> dict[str, Any]:
    with connect() as conn:
        course = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        if not course:
            raise ValueError("Unknown course")
        rounds = [_round_summary(conn, course, row) for row in conn.execute(
            "SELECT * FROM learning_rounds WHERE course_id=? ORDER BY ordinal", (course_id,))]
    return {"rounds": rounds, "attempts": list_attempts(course_id, history=True)}


def wrong_questions(subject_id: str | None = None, course_id: str | None = None,
                    history: bool = False) -> list[dict[str, Any]]:
    with connect() as conn:
        if subject_id is not None:
            _require_subject(conn, subject_id)
        rows = conn.execute("""SELECT q.*,c.title AS course_title,c.subject_id,
                                      b.id AS bank_id,b.title AS bank_title FROM questions q
                               JOIN courses c ON c.id=q.course_id
                               LEFT JOIN bank_questions bq ON bq.id=q.bank_question_id
                               LEFT JOIN banks b ON b.id=bq.bank_id
                               WHERE (? IS NULL OR c.subject_id=?) AND (? IS NULL OR c.id=?)
                               ORDER BY c.created_at DESC,q.ordinal""",
                            (subject_id, subject_id, course_id, course_id)).fetchall()
        result = []
        for row in rows:
            attempts = conn.execute("""SELECT a.correct,a.round_id FROM attempts a WHERE a.question_id=?
                                       ORDER BY a.attempted_at DESC,a.rowid DESC""", (row["id"],)).fetchall()
            if not attempts:
                continue
            if history:
                include = any(not bool(attempt["correct"]) for attempt in attempts)
            else:
                include = not bool(attempts[0]["correct"])
            if include:
                item = _decode_question(row)
                item["latest_correct"] = bool(attempts[0]["correct"])
                result.append(item)
    return result


def create_job(course_id: str, kind: str = "process") -> str:
    job_id = new_id()
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO jobs(id,course_id,kind,status,stage,progress,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, course_id, kind, "queued", "queued", 0, "等待處理", now, now),
        )
        if kind == "process":
            conn.execute("UPDATE courses SET processing_status='processing',updated_at=? WHERE id=?", (now, course_id))
    return job_id


def set_pdf_import_snapshot(course_id: str, questions_asset_id: str | None,
                            answers_asset_id: str | None) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO pdf_imports(course_id,questions_asset_id,answers_asset_id,imported_at)
               VALUES(?,?,?,?) ON CONFLICT(course_id) DO UPDATE SET
                 questions_asset_id=excluded.questions_asset_id,
                 answers_asset_id=excluded.answers_asset_id,imported_at=excluded.imported_at""",
            (course_id, questions_asset_id, answers_asset_id, now_iso()),
        )


def pdf_import_snapshot(course_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM pdf_imports WHERE course_id=?", (course_id,)).fetchone())


def update_job(job_id: str, status: str, stage: str, progress: float, message: str, error: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=?,stage=?,progress=?,message=?,error=?,updated_at=? WHERE id=?",
            (status, stage, max(0.0, min(1.0, progress)), message, error, now_iso(), job_id),
        )


def latest_job(course_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute(
            "SELECT * FROM jobs WHERE course_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (course_id,)
        ).fetchone())


def append_transcription_preview_chunk(job_id: str, segments: list[dict[str, Any]],
                                       chunk_number: int, total_chunks: int) -> None:
    """Persist one completed core chunk, separate from the official transcript."""
    if not (1 <= chunk_number <= total_chunks):
        raise ValueError("Invalid preview chunk number")
    with connect() as conn:
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None or job["status"] not in {"queued", "running"}:
            raise RuntimeError("Transcription job is no longer active")
        row = conn.execute(
            "SELECT * FROM transcription_previews WHERE job_id=?", (job_id,)
        ).fetchone()
        previous = 0 if row is None else row["completed_chunks"]
        if chunk_number != previous + 1 or (row is not None and total_chunks != row["total_chunks"]):
            raise ValueError("Preview chunks must arrive once and in order")
        prior_segments = [] if row is None else json.loads(row["segments_json"])
        incoming = [{
            "id": f"preview-{job_id}-{chunk_number}-{index}",
            "start": int(segment["start_ms"]) / 1000,
            "end": int(segment["end_ms"]) / 1000,
            "text": str(segment["text"]),
        } for index, segment in enumerate(segments)]
        conn.execute(
            """INSERT INTO transcription_previews(job_id,revision,completed_chunks,total_chunks,segments_json)
               VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
               revision=excluded.revision,completed_chunks=excluded.completed_chunks,
               total_chunks=excluded.total_chunks,segments_json=excluded.segments_json""",
            (job_id, chunk_number, chunk_number, total_chunks,
             json.dumps(prior_segments + incoming, ensure_ascii=False)),
        )


def clear_transcription_preview(job_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM transcription_previews WHERE job_id=?", (job_id,))


def preview_revision(job_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT revision FROM transcription_previews WHERE job_id=?", (job_id,)
        ).fetchone()
    return 0 if row is None else int(row["revision"])


def transcription_preview(course_id: str) -> dict[str, Any] | None:
    """Expose only the latest unfinished or failed transcription job's staged rows."""
    with connect() as conn:
        row = conn.execute(
            """SELECT j.id AS job_id,j.status,p.revision,p.completed_chunks,
                      p.total_chunks,p.segments_json
               FROM jobs j JOIN transcription_previews p ON p.job_id=j.id
               WHERE j.id=(SELECT id FROM jobs WHERE course_id=?
                           ORDER BY created_at DESC,rowid DESC LIMIT 1)""",
            (course_id,),
        ).fetchone()
    if row is None or row["status"] == "completed" or row["completed_chunks"] == 0:
        return None
    return {
        "job_id": row["job_id"], "revision": row["revision"],
        "completed_chunks": row["completed_chunks"], "total_chunks": row["total_chunks"],
        "segments": json.loads(row["segments_json"]),
        "status": "partial" if row["status"] == "error" else "running",
    }


def update_playback(course_id: str, position_seconds: float, playback_rate: float | None = None,
                    is_playing: bool | None = None) -> None:
    assignments = ["playback_position_seconds=?", "updated_at=?"]
    values: list[Any] = [max(0.0, position_seconds), now_iso()]
    if playback_rate is not None:
        assignments.append("playback_rate=?")
        values.append(playback_rate)
    if is_playing is not None:
        assignments.append("is_playing=?")
        values.append(int(is_playing))
    values.append(course_id)
    with connect() as conn:
        conn.execute(f"UPDATE courses SET {','.join(assignments)} WHERE id=?", values)


def patch_segment(segment_id: str, fields: dict[str, Any]) -> bool:
    mapping: dict[str, Any] = {}
    if "text" in fields:
        mapping["text"] = str(fields["text"]).strip()
    if "start" in fields:
        mapping["start_ms"] = round(float(fields["start"]) * 1000)
    if "end" in fields:
        mapping["end_ms"] = round(float(fields["end"]) * 1000)
    if not mapping:
        return False
    assignments = ",".join(f"{column}=?" for column in mapping)
    with connect() as conn:
        import medical_review
        before = conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
        cur = conn.execute(f"UPDATE segments SET {assignments},source='manual' WHERE id=?", [*mapping.values(), segment_id])
        if before and cur.rowcount:
            after = conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
            if medical_review.snapshot(before) != medical_review.snapshot(after):
                medical_review.add_revision(conn, before["course_id"], segment_id, "manual",
                    medical_review.snapshot(before), medical_review.snapshot(after), "使用者手動校訂", status="applied")
        return cur.rowcount > 0


def patch_question(question_id: str, fields: dict[str, Any]) -> bool:
    mapping: dict[str, Any] = {}
    allowed = {"section": "section", "number": "number", "stem": "stem", "source_page": "source_page",
               "source_text": "source_text", "correct_option": "answer_key", "answer_key": "answer_key",
               "explanation": "explanation", "needs_review": "needs_review"}
    for external, internal in allowed.items():
        if external in fields:
            mapping[internal] = int(bool(fields[external])) if internal == "needs_review" else fields[external]
    if "options" in fields:
        mapping["options_json"] = json.dumps(fields["options"], ensure_ascii=False)
    if not mapping:
        return False
    assignments = ",".join(f"{column}=?" for column in mapping)
    with connect() as conn:
        cur = conn.execute(f"UPDATE questions SET {assignments} WHERE id=?", [*mapping.values(), question_id])
        return cur.rowcount > 0


def get_question(question_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["options"] = json.loads(item.pop("options_json"))
    return item


def get_segment(segment_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone())


def get_match(question_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM matches WHERE question_id=?", (question_id,)).fetchone())


def list_handouts(course_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id,filename,page_count,section_count,created_at FROM course_handouts
               WHERE course_id=? ORDER BY rowid""", (course_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_handout(course_id: str, handout_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM course_handouts WHERE course_id=? AND id=?",
                           (course_id, handout_id)).fetchone()
    return dict(row) if row else None


def _invalidate_uncertain_handout_matches(conn: sqlite3.Connection, course_id: str) -> None:
    """Allow explicit reanalysis after course context changes, keeping decisions."""
    conn.execute(
        """DELETE FROM matches WHERE question_id IN
           (SELECT id FROM questions WHERE course_id=?) AND
           ((provider='openai' AND status IN ('unmatched','pending_confirmation'))
             OR (provider='handout_local' AND status='pending_confirmation'))""",
        (course_id,),
    )


def add_handout(course_id: str, path: Path, filename: str, content_type: str,
                size_bytes: int, pages: list[dict[str, Any]], handout_id: str | None = None) -> dict[str, Any]:
    handout_id = handout_id or new_id()
    now = now_iso()
    sections = {page["section"] for page in pages
                if page["section"] != f"第 {page['page']} 頁"}
    with connect() as conn:
        conn.execute(
            """INSERT INTO course_handouts
               (id,course_id,path,filename,content_type,size_bytes,page_count,section_count,pages_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (handout_id, course_id, str(path), filename, content_type, size_bytes,
             len(pages), len(sections), json.dumps(pages, ensure_ascii=False), now),
        )
        conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (now, course_id))
        _invalidate_uncertain_handout_matches(conn, course_id)
    return {"id": handout_id, "filename": filename, "page_count": len(pages),
            "section_count": len(sections), "created_at": now}


def delete_handout(course_id: str, handout_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM course_handouts WHERE course_id=? AND id=?",
                           (course_id, handout_id))
        if cur.rowcount:
            conn.execute("UPDATE courses SET updated_at=? WHERE id=?", (now_iso(), course_id))
            _invalidate_uncertain_handout_matches(conn, course_id)
    return bool(cur.rowcount)


def course_payload(course_id: str) -> dict[str, Any] | None:
    course = get_course(course_id)
    if not course:
        return None
    audio_asset = get_asset(course_id, "audio")
    question_asset = get_asset(course_id, "questions")
    answer_asset = get_asset(course_id, "answers")
    segments = list_segments(course_id)
    questions = list_questions(course_id)
    matches = list_matches(course_id)
    attempts = list_attempts(course_id)
    attempts_by_question: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        attempts_by_question.setdefault(attempt["question_id"], []).append(attempt)
    output_questions = []
    for question in questions:
        qid = question["id"]
        q_attempts = attempts_by_question.get(qid, [])
        item = {"id": qid, "bank_id": question["bank_id"], "bank_title": question["bank_title"],
                "section": question["section"], "number": question["number"], "stem": question["stem"],
                "options": question["options"], "source_page": question["source_page"],
                "needs_review": bool(question["needs_review"]), "answered": bool(q_attempts)}
        if q_attempts:
            item["correct_option"] = question["answer_key"]
            item["explanation"] = question["explanation"]
        output_questions.append(item)
    import medical_review
    marks = medical_review.annotations(course_id)
    output_segments = [{"id": s["id"], "start": s["start_ms"] / 1000,
                        "end": s["end_ms"] / 1000, "text": s["text"],
                        **marks.get(s["id"], {})} for s in segments]
    output_matches = []
    for match in matches:
        output_matches.append({
            "id": match["id"], "question_id": match["question_id"],
            "start": None if match["start_ms"] is None else match["start_ms"] / 1000,
            "end": None if match["end_ms"] is None else match["end_ms"] / 1000,
            "question_time": None if match["question_time_ms"] is None else match["question_time_ms"] / 1000,
            "status": match["status"], "confidence": match["confidence"],
            "candidate_score": match["candidate_score"], "reason": match["reason"],
            "evidence": match["evidence"], "segment_id": match["segment_id"],
            "provider": match["provider"],
        })
    output_attempts = [{"question_id": a["question_id"], "selected_option": a["selected_option"],
                        "correct": bool(a["correct"]), "attempted_at": a["attempted_at"]} for a in attempts]
    asset_metadata = {
        "audio": None if audio_asset is None else {"filename": audio_asset["filename"],
                                                    "size_bytes": audio_asset["size_bytes"],
                                                    "content_type": audio_asset["content_type"]},
        "questions": None if question_asset is None else {"filename": question_asset["filename"],
                                                            "size_bytes": question_asset["size_bytes"],
                                                            "content_type": question_asset["content_type"]},
        "answers": None if answer_asset is None else {"filename": answer_asset["filename"],
                                                        "size_bytes": answer_asset["size_bytes"],
                                                        "content_type": answer_asset["content_type"]},
    }
    return {
        "id": course["id"], "title": course["title"], "subject_id": course["subject_id"],
        "transcription_chunk_minutes": course["transcription_chunk_minutes"],
        "learning": course["learning"], "processing_status": course["processing_status"],
        "audio": None if audio_asset is None else {"filename": audio_asset["filename"],
                                                    "duration_seconds": course["duration_seconds"],
                                                    "size_bytes": audio_asset["size_bytes"]},
        "assets": asset_metadata,
        "audio_parts": audio_part_metadata(course_id),
        "questions_asset": asset_metadata["questions"],
        "answer_asset": asset_metadata["answers"],
        "segments": output_segments, "questions": output_questions, "matches": output_matches,
        "handouts": list_handouts(course_id),
        "transcription_preview": transcription_preview(course_id),
        "attempts": output_attempts,
        "playback": {"position_seconds": course["playback_position_seconds"],
                     "playback_rate": course["playback_rate"], "is_playing": bool(course["is_playing"])},
    }
