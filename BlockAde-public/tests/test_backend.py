from __future__ import annotations

import io
import json
import sqlite3
import shutil
import tempfile
import time
import unittest
import wave
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError
from unittest.mock import patch

import db
import jobs
import match
import openai_match
import server
import transcribe


class BackendApiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir, self.old_db_path = db.DATA_DIR, db.DB_PATH
        root = Path(self.temp.name)
        db.DATA_DIR = (root / "data").resolve()
        db.DB_PATH = (db.DATA_DIR / "app.sqlite3").resolve()
        db.init_db()

    def tearDown(self) -> None:
        db.DATA_DIR, db.DB_PATH = self.old_data_dir, self.old_db_path
        self.temp.cleanup()

    def request(self, method: str, path: str, body: bytes | dict | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, bytes, Any]:
        request_headers = Message()
        request_headers["Host"] = "127.0.0.1:8000"
        for key, value in (headers or {}).items():
            if request_headers.get_all(key):
                request_headers.replace_header(key, value)
            else:
                request_headers[key] = value
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
            if request_headers.get("Content-Type") is None:
                request_headers["Content-Type"] = "application/json"
        if body is not None:
            if request_headers.get("Content-Length") is None:
                request_headers["Content-Length"] = str(len(body))

        class Harness(server.RequestHandler):
            def __init__(self) -> None:
                self.command = method
                self.path = path
                self.headers = request_headers
                self.rfile = io.BytesIO(body if isinstance(body, bytes) else b"")
                self.wfile = io.BytesIO()
                self.response_status = 200
                self.response_headers: dict[str, str] = {}
                self.response_payload: Any = None
                self.server = SimpleNamespace(server_port=8000)

            def send_response(self, code: int, message: str | None = None) -> None:
                self.response_status = code

            def send_header(self, keyword: str, value: str) -> None:
                self.response_headers[keyword] = value

            def end_headers(self) -> None:
                return

            def _send_json(self, status: int, payload: Any) -> None:
                self.response_status = status
                self.response_payload = payload
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        handler = Harness()
        handler._handle()
        return handler.response_status, handler.wfile.getvalue(), handler.response_payload, handler.response_headers

    def create_course(self, title: str = "smoke") -> str:
        status, _, payload, _ = self.request("POST", "/api/courses", {"title": title})
        self.assertEqual(status, 201)
        return payload["id"]

    def test_manual_transcript_question_answer_privacy_and_match(self) -> None:
        course_id = self.create_course()
        status, _, segments, _ = self.request("POST", f"/api/courses/{course_id}/segments", {
            "start": 8, "end": 10, "text": "β blocker lowers heart rate and blood pressure."
        })
        self.assertEqual(status, 201)
        self.assertEqual(segments["segments"][0]["start"], 8)
        status, _, questions, _ = self.request("POST", f"/api/courses/{course_id}/questions", {
            "number": "1", "stem": "Which beta blocker lowers heart rate?",
            "options": [{"key": "A", "text": "Drug A"}, {"key": "B", "text": "Drug B"}],
            "correct_option": "B", "explanation": "Beta blockade lowers heart rate."
        })
        self.assertEqual(status, 201)
        question_id = questions["questions"][0]["id"]

        status, raw, detail, _ = self.request("GET", f"/api/courses/{course_id}")
        self.assertEqual(status, 200)
        self.assertNotIn(b"correct_option", raw)
        self.assertNotIn(b"Beta blockade lowers", raw)
        self.assertFalse(detail["questions"][0]["answered"])
        self.assertEqual(detail["matches"][0]["status"], "confirmed")
        self.assertEqual(detail["matches"][0]["provider"], "local_text")
        self.assertIsNone(detail["matches"][0]["confidence"])

        status, _, result, _ = self.request("PATCH", f"/api/questions/{question_id}/match", {
            "question_time": 10.5, "status": "confirmed"
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["match"]["status"], "confirmed")
        self.assertEqual(result["match"]["provider"], "manual")
        self.assertIsNone(result["match"]["segment_id"])
        self.assertEqual(result["match"]["start"], 10.5)
        self.assertEqual(result["match"]["end"], 10.5)
        self.assertEqual(result["match"]["evidence"], "")
        self.assertIsNone(result["match"]["candidate_score"])
        status, _, result, _ = self.request("PATCH", f"/api/courses/{course_id}/playback", {
            "position_seconds": 9.5, "playback_rate": 1.25, "is_playing": False
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["playback"]["position_seconds"], 9.5)

        status, _, result, _ = self.request("POST", f"/api/questions/{question_id}/attempt", {"selected_option": "B"})
        self.assertEqual(status, 201)
        self.assertTrue(result["correct"])
        self.assertEqual(result["correct_option"], "B")
        status, raw, detail, _ = self.request("GET", f"/api/courses/{course_id}")
        self.assertEqual(status, 200)
        self.assertIn(b"Beta blockade lowers", raw)
        self.assertEqual(detail["questions"][0]["correct_option"], "B")
        self.assertEqual(detail["attempts"][0]["selected_option"], "B")

    def test_local_text_match_confirms_distinctive_traditional_simplified_phrase(self) -> None:
        question = {
            "stem": "大面積燒傷會導致全身性發炎反應",
            "options": [{"key": "A", "text": "是"}, {"key": "B", "text": "非"}],
            "answer_key": "A",
            "needs_review": False,
        }
        segments = [{
            "id": "seg-1", "start_ms": 1000, "end_ms": 2400,
            "text": "大面积烧伤会导致全身性发炎反应。",
        }]

        suggestion = match.suggest_question(question, segments)

        self.assertEqual(suggestion.status, "confirmed")
        self.assertEqual(suggestion.provider, "local_text")
        self.assertEqual(suggestion.question_time_ms, 2900)
        self.assertIsNone(suggestion.confidence)
        self.assertEqual(suggestion.evidence_segment_ids, ["seg-1"])

    def test_local_text_match_leaves_weak_stroke_burn_overlap_unmatched(self) -> None:
        question = {
            "stem": "下列預估與大面積燒傷的死亡率有關，何者除外？",
            "options": [{"key": "A", "text": "休克"}, {"key": "B", "text": "中風"}],
            "answer_key": "A",
            "needs_review": False,
        }
        segments = [{
            "id": "seg-1", "start_ms": 360000, "end_ms": 363000,
            "text": "中风会提高患者的死亡率。",
        }]

        suggestion = match.suggest_question(question, segments)

        self.assertEqual(suggestion.status, "unmatched")
        self.assertIsNone(suggestion.segment_id)
        self.assertIsNone(suggestion.question_time_ms)
        self.assertIsNone(suggestion.candidate_score)
        self.assertEqual(suggestion.evidence, "")

    def test_local_text_match_requires_more_than_one_subtopic_for_complex_stems(self) -> None:
        cases = [
            (
                "以病人為中心的會談，是以封閉式問句從頭到尾貫穿整個會談",
                "就是在講以病人為中心的會談。",
            ),
            (
                "人際溝通的訊息傳遞絕大部分是藉由語言溝通來完成，非語言溝通的部分影響很小",
                "以及非語言溝通的部分。",
            ),
        ]
        for stem, text in cases:
            with self.subTest(stem=stem):
                question = {
                    "stem": stem,
                    "options": [{"key": "A", "text": "是"}, {"key": "B", "text": "非"}],
                    "answer_key": "A",
                    "needs_review": False,
                }
                segments = [{"id": "seg-1", "start_ms": 61000, "end_ms": 63000, "text": text}]
                suggestion = match.suggest_question(question, segments)
                self.assertEqual(suggestion.status, "unmatched")
                self.assertIsNone(suggestion.question_time_ms)

    def test_local_text_match_allows_a_complete_first_person_role_introduction(self) -> None:
        segments = [{
            "id": "seg-1", "start_ms": 395000, "end_ms": 399000,
            "text": "我是某某某實習醫學生。",
        }]
        introduction = {
            "stem": "為避免病人不讓我做身體診察，我在病人面前要自稱為實習醫師",
            "options": [{"key": "A", "text": "是"}, {"key": "B", "text": "非"}],
            "answer_key": "A",
        }
        catheter = {
            "stem": "如果在沒有監督的情況下為病人插導尿管，則違反哪一項核心能力？",
            "options": [{"key": "A", "text": "病人照護"}, {"key": "B", "text": "專業素養"}],
            "answer_key": "A",
        }

        introduction_match = match.suggest_question(introduction, segments)
        catheter_match = match.suggest_question(catheter, segments)

        self.assertEqual(introduction_match.status, "confirmed")
        self.assertEqual(introduction_match.question_time_ms, 399500)
        self.assertEqual(catheter_match.status, "unmatched")
        self.assertIsNone(catheter_match.question_time_ms)

    def test_refresh_rechecks_local_matches_and_preserves_manual_and_openai_results(self) -> None:
        course_id = self.create_course()
        db.add_segments(course_id, [{
            "start_ms": 1000, "end_ms": 2400,
            "text": "大面积烧伤会导致全身性发炎反应。",
        }])
        question = {
            "stem": "大面積燒傷會導致全身性發炎反應",
            "options": [{"key": "A", "text": "是"}, {"key": "B", "text": "非"}],
            "answer_key": "A",
        }
        local_id = db.add_question(course_id, question)
        manual_id = db.add_question(course_id, question)
        disabled_id = db.add_question(course_id, question)
        openai_confirmed_id = db.add_question(course_id, question)
        openai_pending_id = db.add_question(course_id, question)
        db.save_match(local_id, {
            "status": "pending_confirmation", "provider": "local_lexical",
            "question_time_ms": 2100, "reason": "old broad candidate",
        })
        db.save_match(manual_id, {
            "status": "confirmed", "provider": "manual", "question_time_ms": 9000,
        })
        db.save_match(disabled_id, {
            "status": "disabled", "provider": "manual", "question_time_ms": 8000,
        })
        db.save_match(openai_confirmed_id, {
            "status": "confirmed", "provider": "openai", "question_time_ms": 7000,
        })
        db.save_match(openai_pending_id, {
            "status": "pending_confirmation", "provider": "openai", "question_time_ms": 6000,
        })
        db.add_attempt(local_id, "A", True)

        match.refresh_course_matches(course_id)

        matches = {item["question_id"]: item for item in db.list_matches(course_id)}
        self.assertEqual(matches[local_id]["status"], "confirmed")
        self.assertEqual(matches[local_id]["provider"], "local_text")
        self.assertIsNone(matches[local_id]["confidence"])
        self.assertEqual(matches[manual_id]["question_time_ms"], 9000)
        self.assertEqual(matches[disabled_id]["status"], "disabled")
        self.assertEqual(matches[openai_confirmed_id]["question_time_ms"], 7000)
        self.assertEqual(matches[openai_pending_id]["provider"], "openai")
        self.assertEqual(matches[openai_pending_id]["status"], "pending_confirmation")
        self.assertEqual(matches[openai_pending_id]["question_time_ms"], 6000)
        self.assertEqual(len(db.list_attempts(course_id)), 1)

    def test_all_assets_audio_range_and_failed_pdf_preserves_previous(self) -> None:
        course_id = self.create_course()
        # This case checks asset storage, not the automatic transcription worker.
        with patch.object(jobs, "start_job"):
            for kind, filename, content_type, content in (
                ("audio", "lecture.wav", "audio/wav", b"0123456789"),
                ("questions", "questions.pdf", "application/pdf", b"%PDF-1.4\nquestion"),
                ("answers", "answers.pdf", "application/pdf", b"%PDF-1.4\nanswer"),
            ):
                status, _, payload, _ = self.request(
                    "PUT", f"/api/courses/{course_id}/assets/{kind}", content,
                    {"Content-Type": content_type, "X-Filename": filename},
                )
                self.assertEqual(status, 201, payload)
                self.assertEqual(payload["asset"]["kind"], kind)

        detail = self.request("GET", f"/api/courses/{course_id}")[2]
        self.assertEqual(detail["assets"]["questions"]["filename"], "questions.pdf")
        self.assertEqual(detail["questions_asset"]["size_bytes"], len(b"%PDF-1.4\nquestion"))
        self.assertEqual(detail["answer_asset"]["filename"], "answers.pdf")

        status, _, payload, _ = self.request(
            "PUT", f"/api/courses/{course_id}/assets/questions", b"not a pdf",
            {"Content-Type": "application/pdf", "X-Filename": "bad.pdf"},
        )
        self.assertEqual(status, 415)
        questions_asset = db.get_asset(course_id, "questions")
        self.assertEqual(questions_asset["filename"], "questions.pdf")
        self.assertEqual(Path(questions_asset["path"]).read_bytes(), b"%PDF-1.4\nquestion")

        status, audio_data, _, audio_headers = self.request(
            "GET", f"/api/courses/{course_id}/audio", headers={"Range": "bytes=2-5"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(audio_headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(audio_data, b"2345")

        # Reject the declared size before reading the upload body.
        status, _, payload, _ = self.request(
            "PUT", f"/api/courses/{course_id}/assets/audio", b"",
            {"Content-Length": str(server.MAX_AUDIO_BYTES + 1), "Content-Type": "audio/wav", "X-Filename": "too-large.wav"},
        )
        self.assertEqual(status, 413)
        self.assertIn("750 MB", payload["error"])

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe required")
    def test_audio_parts_upload_api_stages_in_order_and_exposes_safe_metadata(self) -> None:
        course_id = self.create_course()
        samples = io.BytesIO()
        with wave.open(samples, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 16000)
        for name in ("part-two.wav", "part-one.wav"):
            status, _, payload, _ = self.request(
                "POST", f"/api/courses/{course_id}/audio-parts", samples.getvalue(),
                {"Content-Type": "audio/wav", "X-Filename": name},
            )
            self.assertEqual(status, 201, payload)
        detail = self.request("GET", f"/api/courses/{course_id}")[2]
        self.assertEqual([part["filename"] for part in detail["audio_parts"]],
                         ["part-two.wav", "part-one.wav"])
        self.assertEqual([part["ordinal"] for part in detail["audio_parts"]], [0, 1])
        self.assertTrue(all(part["status"] == "pending" for part in detail["audio_parts"]))
        self.assertTrue(all("path" not in part for part in detail["audio_parts"]))
        self.assertIsNone(detail["audio"])
        with patch.object(jobs, "start_audio_finalize", return_value="queued-job"):
            status, _, payload, _ = self.request("POST", f"/api/courses/{course_id}/audio-parts/finalize", {})
        self.assertEqual(status, 202)
        self.assertEqual(payload["job"]["status"], "queued")

    def test_process_job_completes_and_can_be_retried_without_audio(self) -> None:
        course_id = self.create_course()
        self.request("POST", f"/api/courses/{course_id}/segments", {"start": 0, "end": 1, "text": "manual"})
        for _ in range(2):
            status, _, payload, _ = self.request("POST", f"/api/courses/{course_id}/process", {})
            self.assertEqual(status, 202)
            job_id = payload["job"]["id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status, _, job_payload, _ = self.request("GET", f"/api/courses/{course_id}/job")
                self.assertEqual(status, 200)
                if job_payload["job"]["id"] == job_id and job_payload["job"]["status"] == "completed":
                    break
                if job_payload["job"]["id"] == job_id and job_payload["job"]["status"] == "error":
                    self.fail(job_payload["job"]["error"] or job_payload["job"]["message"])
                time.sleep(0.025)
            else:
                self.fail("job did not complete in time")
        detail = self.request("GET", f"/api/courses/{course_id}")[2]
        self.assertEqual(detail["processing_status"], "ready")

    def test_explicit_openai_analysis_endpoint_runs_bounded_background_job(self) -> None:
        course_id = self.create_course()
        self.request("POST", f"/api/courses/{course_id}/segments", {
            "start": 0, "end": 1, "text": "The beta receptor response lowers heart rate."
        })
        _, _, created, _ = self.request("POST", f"/api/courses/{course_id}/questions", {
            "number": "1", "stem": "Question one?",
            "options": [{"key": "A", "text": "increase"}, {"key": "B", "text": "decrease"}],
            "correct_option": "B",
        })
        question_id = created["questions"][0]["id"]
        second, _, second_payload, _ = self.request("POST", f"/api/courses/{course_id}/questions", {
            "number": "2", "stem": "Question two?",
            "options": [{"key": "A", "text": "increase"}, {"key": "B", "text": "decrease"}],
            "correct_option": "B",
        })
        self.assertEqual(second, 201)
        second_question_id = second_payload["questions"][0]["id"]

        class FakeProvider:
            name = "openai"
            auto_confirm_high_confidence = True

            def __init__(self):
                self.calls = []

            def candidate_windows(self, question, segments):
                return [{"segments": [{"id": segments[0]["id"]}]}]

            def suggest(self, question, segments):
                self.calls.append(question["id"])
                confidence = "medium" if len(self.calls) == 1 else "high"
                return match.MatchSuggestion(
                    verdict="match", confidence_label=confidence, evidence_segment_ids=[segments[0]["id"]],
                    last_evidence_segment_id=segments[0]["id"], reason="提供者依證據確認。", provider=self.name,
                )

        fake_provider = FakeProvider()
        with patch.object(jobs, "OpenAIMatchProvider", return_value=fake_provider):
            status, _, payload, _ = self.request(
                "POST", f"/api/courses/{course_id}/analyze-matches", {"max_questions": 1}
            )
        self.assertEqual(status, 202, payload)
        self.assertEqual(payload["job"]["kind"], "match_analysis")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.request("GET", f"/api/courses/{course_id}/job")[2]["job"]
            if job["status"] == "completed":
                break
            if job["status"] == "error":
                self.fail(job["error"] or job["message"])
            time.sleep(0.01)
        else:
            self.fail("match analysis job did not complete in time")
        while time.monotonic() < deadline and course_id in jobs._active_by_course:
            time.sleep(0.005)
        self.assertEqual(db.get_match(question_id)["status"], "pending_confirmation")
        self.assertEqual(db.get_match(question_id)["question_time_ms"], 1500)
        self.assertEqual(fake_provider.calls, [question_id])

        with patch.object(jobs, "OpenAIMatchProvider", return_value=fake_provider):
            status, _, second_job, _ = self.request(
                "POST", f"/api/courses/{course_id}/analyze-matches", {"max_questions": 1}
            )
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.request("GET", f"/api/courses/{course_id}/job")[2]["job"]
            if job["id"] != second_job["job"]["id"]:
                time.sleep(0.01)
                continue
            if job["status"] == "completed":
                break
            if job["status"] == "error":
                self.fail(job["error"] or job["message"])
            time.sleep(0.01)
        else:
            self.fail("second match analysis job did not complete in time")
        self.assertEqual(fake_provider.calls, [question_id, second_question_id])
        self.assertEqual(db.get_match(question_id)["status"], "pending_confirmation")
        self.assertEqual(db.get_match(second_question_id)["status"], "confirmed")
        self.assertEqual(db.get_match(question_id)["provider"], "openai")

    def test_openai_analysis_without_key_is_actionable_and_does_not_create_job(self) -> None:
        course_id = self.create_course()
        self.request("POST", f"/api/courses/{course_id}/segments", {"start": 0, "end": 1, "text": "Transcript"})
        self.request("POST", f"/api/courses/{course_id}/questions", {
            "number": "1", "stem": "Question", "options": ["A", "B"], "correct_option": "A",
        })
        with patch.dict("os.environ", {}, clear=True):
            status, _, payload, _ = self.request("POST", f"/api/courses/{course_id}/analyze-matches", {})
        self.assertEqual(status, 503)
        self.assertIn("OPENAI_API_KEY", payload["error"])
        self.assertIsNone(db.latest_job(course_id))

    def test_local_host_and_same_origin_write_guards(self) -> None:
        status, _, payload, _ = self.request("GET", "/api/courses", headers={"Host": "example.com:8000"})
        self.assertEqual(status, 403)
        status, _, payload, _ = self.request("GET", "/api/courses", headers={"Host": "localhost:8123"})
        self.assertEqual(status, 403)

        status, _, payload, _ = self.request(
            "POST", "/api/courses", {"title": "cross origin"},
            {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(db.list_courses(), [])

        status, _, payload, _ = self.request(
            "POST", "/api/courses", {"title": "no-cors cross-site"},
            {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(db.list_courses(), [])

        status, _, created, _ = self.request(
            "POST", "/api/courses", {"title": "same origin"},
            {"Host": "localhost:8000", "Origin": "http://localhost:8000", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["title"], "same origin")
        status, _, _, _ = self.request(
            "POST", "/api/courses", {"title": "mixed origin"},
            {"Host": "127.0.0.1:8000", "Origin": "http://localhost:8000"},
        )
        self.assertEqual(status, 403)

    def test_pdf_asset_replacement_after_import_is_explicitly_rejected(self) -> None:
        course_id = self.create_course()
        question_path = db.DATA_DIR / "question.pdf"
        answer_path = db.DATA_DIR / "answer.pdf"
        question_path.parent.mkdir(parents=True, exist_ok=True)
        question_path.write_bytes(b"%PDF-1.4\nq")
        answer_path.write_bytes(b"%PDF-1.4\na")
        db.set_asset(course_id, "questions", question_path, "questions.pdf", "application/pdf", 9)
        db.set_asset(course_id, "answers", answer_path, "answers.pdf", "application/pdf", 9)
        question_asset = db.get_asset(course_id, "questions")
        answer_asset = db.get_asset(course_id, "answers")
        db.add_question(course_id, {"number": "1", "stem": "Imported", "options": []})
        db.set_pdf_import_snapshot(course_id, question_asset["id"], answer_asset["id"])
        self.assertIn("已匯入", jobs._import_pdfs(course_id)[0])

        status, _, payload, _ = self.request(
            "PUT", f"/api/courses/{course_id}/assets/questions", b"%PDF-1.4\nnew",
            {"Content-Type": "application/pdf", "X-Filename": "replacement.pdf"},
        )
        self.assertEqual(status, 409)
        self.assertIn("請建立新課程", payload["error"])
        self.assertEqual(db.get_asset(course_id, "questions")["id"], question_asset["id"])
        self.assertEqual(question_path.read_bytes(), b"%PDF-1.4\nq")

        replacement_path = db.DATA_DIR / "replacement.pdf"
        replacement_path.write_bytes(b"%PDF-1.4\nnew")
        db.set_asset(course_id, "questions", replacement_path, "replacement.pdf", "application/pdf", 12)
        with self.assertRaisesRegex(RuntimeError, "請建立新課程"):
            jobs._import_pdfs(course_id)


class BackendPureFunctionTests(unittest.TestCase):
    def test_db_connection_context_manager_commits_rolls_back_and_closes(self) -> None:
        old_data_dir, old_db_path = db.DATA_DIR, db.DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db.DATA_DIR = (root / "data").resolve()
            db.DB_PATH = (db.DATA_DIR / "app.sqlite3").resolve()
            try:
                with db.connect() as committed_connection:
                    committed_connection.execute("CREATE TABLE values_table (value TEXT)")
                    committed_connection.execute("INSERT INTO values_table VALUES (?)", ("committed",))
                with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed database"):
                    committed_connection.execute("SELECT 1")

                with self.assertRaisesRegex(ValueError, "rollback"):
                    with db.connect() as rolled_back_connection:
                        rolled_back_connection.execute("INSERT INTO values_table VALUES (?)", ("rolled back",))
                        raise ValueError("rollback")
                with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed database"):
                    rolled_back_connection.execute("SELECT 1")

                with db.connect() as verification_connection:
                    values = [row[0] for row in verification_connection.execute("SELECT value FROM values_table")]
                self.assertEqual(values, ["committed"])
            finally:
                db.DATA_DIR, db.DB_PATH = old_data_dir, old_db_path

    def test_whisper_offsets_are_milliseconds(self) -> None:
        segments = transcribe.parse_whisper_json({"transcription": [
            {"offsets": {"from": 400, "to": 1680}, "timestamps": {"from": "00:00:00,400", "to": "00:00:01,680"}, "text": "中文"},
            {"start": 3348.37, "end": 3349.12, "text": "seconds source"},
        ]})
        self.assertEqual(segments[0]["start_ms"], 400)
        self.assertEqual(segments[0]["end_ms"], 1680)
        self.assertEqual(segments[1]["start_ms"], 3_348_370)

    def test_answer_pairing_uses_section_and_keeps_ambiguous_numbers_unmatched(self) -> None:
        questions = [
            {"section": "Cardiology", "number": "1", "stem": "one"},
            {"section": "Pharmacology", "number": "1", "stem": "two"},
        ]
        answers = [
            {"section": "Cardiology", "number": "1", "answer_key": "A", "explanation": "a"},
            {"section": "Pharmacology", "number": "1", "answer_key": "C", "explanation": "c"},
        ]
        merged = jobs._merge_answers(questions, answers)
        self.assertEqual([row.get("answer_key") for row in merged], ["A", "C"])
        ambiguous = jobs._merge_answers(
            [{"number": "1", "stem": "one"}, {"number": "1", "stem": "two"}],
            [{"number": "1", "answer_key": "B"}],
        )
        self.assertTrue(all(row.get("answer_key") is None for row in ambiguous))
        self.assertTrue(all(row["needs_review"] for row in ambiguous))

    def test_high_provider_match_is_confirmed_only_with_local_evidence_and_answer(self) -> None:
        class Provider:
            name = "openai"
            auto_confirm_high_confidence = True

            def __init__(self, confidence_label: str = "high") -> None:
                self.confidence_label = confidence_label

            def suggest(self, question, segments):
                return match.MatchSuggestion(
                    verdict="match",
                    confidence_label=self.confidence_label,
                    evidence_segment_ids=["seg-1"],
                    last_evidence_segment_id="seg-1",
                    reason="model evidence",
                    provider=self.name,
                )

        question = {"answer_key": "A", "options": [{"key": "A", "text": "yes"}], "needs_review": False}
        segments = [{"id": "seg-1", "start_ms": 1200, "end_ms": 1800, "text": "evidence"}]
        confirmed = match.suggest_question(question, segments, Provider(), audio_duration_ms=3000)
        self.assertEqual(confirmed.status, "confirmed")
        self.assertEqual(confirmed.question_time_ms, 2300)

        low_confidence = match.suggest_question(question, segments, Provider("medium"), audio_duration_ms=3000)
        self.assertEqual(low_confidence.status, "pending_confirmation")
        self.assertEqual(low_confidence.segment_id, "seg-1")
        self.assertEqual(low_confidence.start_ms, 1200)
        self.assertEqual(low_confidence.question_time_ms, 2300)
        missing_evidence = Provider()
        missing_evidence.suggest = lambda question, segments: match.MatchSuggestion(
            verdict="match", confidence_label="high",
            evidence_segment_ids=["not-local"], last_evidence_segment_id="not-local"
        )
        invalid = match.suggest_question(question, segments, missing_evidence, audio_duration_ms=3000)
        self.assertEqual(invalid.status, "pending_confirmation")

    def test_retry_replaces_whisper_rows_but_preserves_manual_corrections(self) -> None:
        temp = tempfile.TemporaryDirectory()
        old_data_dir, old_db_path = db.DATA_DIR, db.DB_PATH
        try:
            db.DATA_DIR = (Path(temp.name) / "data").resolve()
            db.DB_PATH = (db.DATA_DIR / "app.sqlite3").resolve()
            db.init_db()
            course = db.create_course("preserve edits")
            first_ids = db.add_segments(course["id"], [{"start_ms": 0, "end_ms": 1000, "text": "machine"}], source="whisper")
            db.patch_segment(first_ids[0], {"text": "human correction"})
            db.add_segments(course["id"], [{"start_ms": 0, "end_ms": 1200, "text": "new machine"}], source="whisper", replace=True)
            rows = db.list_segments(course["id"])
            self.assertEqual({row["text"] for row in rows}, {"human correction", "new machine"})
            self.assertEqual({row["source"] for row in rows}, {"manual", "whisper"})
        finally:
            db.DATA_DIR, db.DB_PATH = old_data_dir, old_db_path
            temp.cleanup()


class OpenAIMatchProviderTests(unittest.TestCase):
    def test_suggest_uses_responses_strict_json_schema_and_bounds_transcript(self) -> None:
        question = {
            "section": "Cardiology", "number": "4", "stem": "Explain beta blocker receptor response.",
            "options": [{"key": "A", "text": "wrong"}, {"key": "B", "text": "correct"}],
            "answer_key": "B", "explanation": "Brief rationale.",
        }
        segments = [
            {"id": f"seg-{index}", "start_ms": index * 1000, "end_ms": index * 1000 + 800,
             "text": ("beta blocker receptor lowers heart rate " + ("lecture material " * 250))}
            for index in range(12)
        ]
        provider = openai_match.OpenAIMatchProvider(api_key="test-key", model="gpt-6-sol")
        first_candidate = provider.candidate_windows(question, segments)[0]
        result = {
            "verdict": "match", "confidence": "high",
            "evidence_segment_ids": [first_candidate["segments"][0]["id"]],
            "last_evidence_segment_id": first_candidate["segments"][0]["id"],
            "reason": "明確說明此概念。",
        }
        response_bytes = json.dumps({"status": "completed", "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(result, ensure_ascii=False)}]},
        ]}).encode("utf-8")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return response_bytes[:limit]

        with patch.object(openai_match, "urlopen", return_value=FakeResponse()) as mock_urlopen:
            suggestion = provider.suggest(question, segments)
        import ssl
        context = mock_urlopen.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        request = mock_urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        sent_input = json.loads(body["input"])
        sent_transcript_chars = sum(
            len(segment["text"])
            for window in sent_input["candidate_windows"]
            for segment in window["segments"]
        )
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertFalse(body["store"])
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertLessEqual(len(sent_input["candidate_windows"]), 3)
        self.assertLessEqual(sent_transcript_chars, 8000)
        self.assertEqual(suggestion.verdict, "match")
        self.assertEqual(suggestion.evidence_segment_ids, result["evidence_segment_ids"])

    def test_match_evidence_cannot_be_spliced_from_different_candidate_windows(self) -> None:
        provider = openai_match.OpenAIMatchProvider(api_key="test-key")
        question = {"stem": "beta receptor response heart rate", "options": []}
        segments = [
            {"id": f"seg-{index}", "start_ms": index * 2000, "end_ms": index * 2000 + 900,
             "text": "beta receptor response lowers heart rate"}
            for index in range(8)
        ]
        windows = provider.candidate_windows(question, segments)
        self.assertGreaterEqual(len(windows), 2)
        first_id = windows[0]["segments"][0]["id"]
        second_id = windows[1]["segments"][0]["id"]
        response = {"status": "completed", "output": [{"type": "message", "content": [{
            "type": "output_text", "text": json.dumps({
                "verdict": "match", "confidence": "high", "evidence_segment_ids": [first_id, second_id],
                "last_evidence_segment_id": second_id, "reason": "spliced evidence",
            }),
        }]}]}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return json.dumps(response).encode("utf-8")[:limit]

        with patch.object(openai_match, "urlopen", return_value=FakeResponse()):
            suggestion = provider.suggest(question, segments)
        self.assertEqual(suggestion.verdict, "uncertain")
        self.assertEqual(suggestion.status, "pending_confirmation")
        self.assertIsNone(suggestion.segment_id)

    def test_no_candidate_skips_network_and_remote_error_body_is_sanitized(self) -> None:
        provider = openai_match.OpenAIMatchProvider(api_key="never-log-me")
        question = {"stem": "Unrelated nephrotic mechanism", "options": []}
        segments = [{"id": "seg", "start_ms": 0, "end_ms": 500, "text": "completely different words"}]
        with patch.object(openai_match, "urlopen") as mock_urlopen:
            suggestion = provider.suggest(question, segments)
        self.assertEqual(suggestion.verdict, "no_match")
        mock_urlopen.assert_not_called()

        error_body = io.BytesIO(b"private payload")
        error = HTTPError("https://api.openai.com/v1/responses", 401, "unauthorized", {}, error_body)
        with patch.object(openai_match, "urlopen", side_effect=error):
            with self.assertRaises(openai_match.OpenAIMatchError) as caught:
                provider.suggest(
                    {"stem": "beta receptor response", "options": [{"key": "A", "text": "beta"}]},
                    [{"id": "s1", "start_ms": 0, "end_ms": 400, "text": "beta receptor lowers heart rate"}],
                )
        self.assertNotIn("private payload", str(caught.exception))
        self.assertNotIn("never-log-me", str(caught.exception))
        self.assertTrue(error_body.closed)


if __name__ == "__main__":
    unittest.main()
