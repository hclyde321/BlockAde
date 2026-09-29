from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import jobs
import transcribe


class TranscriptionPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir, self.old_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = (Path(self.temp.name) / "data").resolve()
        db.DB_PATH = db.DATA_DIR / "app.sqlite3"
        db.init_db()
        self.course_id = db.create_course("preview test")["id"]

    def tearDown(self) -> None:
        db.DATA_DIR, db.DB_PATH = self.old_data_dir, self.old_db_path
        self.temp.cleanup()

    def test_latest_job_only_and_stable_segment_ids(self) -> None:
        first = db.create_job(self.course_id)
        db.append_transcription_preview_chunk(first, [
            {"start_ms": 1000, "end_ms": 2000, "text": "first"},
        ], 1, 2)
        preview = db.course_payload(self.course_id)["transcription_preview"]
        self.assertEqual(preview["revision"], 1)
        self.assertEqual(preview["completed_chunks"], 1)
        self.assertEqual(preview["total_chunks"], 2)
        self.assertEqual(preview["segments"][0]["start"], 1)
        stable_id = preview["segments"][0]["id"]
        db.append_transcription_preview_chunk(first, [
            {"start_ms": 300000, "end_ms": 301000, "text": "second"},
        ], 2, 2)
        self.assertEqual(db.course_payload(self.course_id)["transcription_preview"]["segments"][0]["id"], stable_id)
        self.assertEqual(jobs.get_job(self.course_id)["preview_revision"], 2)

        db.update_job(first, "error", "failed", 1, "failed")
        self.assertEqual(db.course_payload(self.course_id)["transcription_preview"]["status"], "partial")
        second = db.create_job(self.course_id)
        self.assertNotEqual(first, second)
        self.assertIsNone(db.course_payload(self.course_id)["transcription_preview"])
        self.assertEqual(jobs.get_job(self.course_id)["preview_revision"], 0)

    def test_range_failure_preserves_official_rows_and_partial_preview(self) -> None:
        audio = db.DATA_DIR / "lecture.wav"
        audio.write_bytes(b"audio")
        db.set_asset(self.course_id, "audio", audio, "lecture.wav", "audio/wav", 5)
        db.set_course_status(self.course_id, "ready", 900)
        db.add_segments(self.course_id, [
            {"start_ms": 600000, "end_ms": 601000, "text": "original"},
        ], source="whisper")
        original = db.list_segments(self.course_id)
        job_id = db.create_job(self.course_id, kind="range_transcription")

        def fail_after_one_chunk(*args: object, **kwargs: object) -> dict:
            callback = kwargs["chunk_callback"]
            callback([{"start_ms": 600100, "end_ms": 600900, "text": "draft"}], 1, 2)
            self.assertEqual(db.list_segments(self.course_id), original)
            preview = db.course_payload(self.course_id)["transcription_preview"]
            self.assertEqual(preview["segments"][0]["start"], 600.1)
            raise RuntimeError("second chunk failed")

        with mock.patch.object(transcribe, "transcribe_audio_range", side_effect=fail_after_one_chunk):
            jobs._run_range_job(job_id, self.course_id, 600000, 601000,
                                {original[0]["id"]}, "ready")

        self.assertEqual(db.list_segments(self.course_id), original)
        self.assertEqual(jobs.get_job(self.course_id)["status"], "error")
        self.assertEqual(jobs.get_job(self.course_id)["preview_revision"], 1)
        preview = db.course_payload(self.course_id)["transcription_preview"]
        self.assertEqual(preview["status"], "partial")
        self.assertEqual([row["text"] for row in preview["segments"]], ["draft"])

    def test_full_retranscription_failure_keeps_original_rows(self) -> None:
        audio = db.DATA_DIR / "lecture.wav"
        audio.write_bytes(b"audio")
        db.set_asset(self.course_id, "audio", audio, "lecture.wav", "audio/wav", 5)
        db.add_segments(self.course_id, [
            {"start_ms": 1000, "end_ms": 2000, "text": "original"},
        ], source="whisper")
        original = db.list_segments(self.course_id)
        job_id = db.create_job(self.course_id)

        def fail_after_one_chunk(*args: object, **kwargs: object) -> dict:
            del args
            kwargs["chunk_callback"](
                [{"start_ms": 1000, "end_ms": 2000, "text": "draft"}], 1, 2,
            )
            self.assertEqual(db.list_segments(self.course_id), original)
            raise RuntimeError("second chunk failed")

        with (mock.patch.object(transcribe, "transcribe_audio", side_effect=fail_after_one_chunk),
              mock.patch.object(jobs, "_import_pdfs", return_value=[]),
              mock.patch.object(jobs.match_module, "refresh_course_matches")):
            jobs._run_job(job_id, self.course_id)

        self.assertEqual(db.list_segments(self.course_id), original)
        self.assertEqual(jobs.get_job(self.course_id)["status"], "error")
        self.assertEqual(jobs.get_job(self.course_id)["preview_revision"], 1)
        self.assertEqual(db.course_payload(self.course_id)["transcription_preview"]["status"], "partial")

    def test_completed_job_hides_preview(self) -> None:
        job_id = db.create_job(self.course_id)
        db.append_transcription_preview_chunk(job_id, [], 1, 1)
        db.update_job(job_id, "completed", "done", 1, "done")
        self.assertIsNone(db.course_payload(self.course_id)["transcription_preview"])

    def test_full_publish_rejects_manual_edit_atomically(self) -> None:
        db.add_segments(self.course_id, [
            {"start_ms": 1000, "end_ms": 2000, "text": "original"},
        ], source="whisper")
        db.add_segments(self.course_id, [
            {"start_ms": 3000, "end_ms": 4000, "text": "edited"},
        ], source="manual")
        original = db.list_segments(self.course_id)

        with self.assertRaisesRegex(RuntimeError, "人工修改"):
            db.add_segments(self.course_id, [
                {"start_ms": 1000, "end_ms": 4000, "text": "new"},
            ], source="whisper", replace=True, reject_manual=True)

        self.assertEqual(db.list_segments(self.course_id), original)


if __name__ == "__main__":
    unittest.main()
