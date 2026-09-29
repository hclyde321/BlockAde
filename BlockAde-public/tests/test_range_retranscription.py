from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import db
import jobs
import transcribe


class RangeJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir, self.old_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = (Path(self.temp.name) / "data").resolve()
        db.DB_PATH = db.DATA_DIR / "app.sqlite3"
        db.init_db()
        self.course_id = db.create_course("range test")["id"]
        audio = db.DATA_DIR / "recording.wav"
        audio.write_bytes(b"audio")
        db.set_asset(self.course_id, "audio", audio, "recording.wav", "audio/wav", 5)
        db.set_course_status(self.course_id, "ready", 60)

    def tearDown(self) -> None:
        db.DATA_DIR, db.DB_PATH = self.old_data_dir, self.old_db_path
        self.temp.cleanup()

    def _wait(self) -> dict:
        for _ in range(200):
            job = jobs.get_job(self.course_id)
            if job and job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail("range job did not finish")

    def test_replaces_expanded_generated_rows_and_preserves_outside_ids(self) -> None:
        db.add_segments(self.course_id, [
            {"start_ms": 1000, "end_ms": 3000, "text": "before"},
            {"start_ms": 5000, "end_ms": 8000, "text": "old selected"},
            {"start_ms": 9000, "end_ms": 11000, "text": "after"},
        ], source="whisper")
        before = db.list_segments(self.course_id)
        with mock.patch.object(transcribe, "transcribe_audio_range", return_value={
            "segments": [{"start_ms": 5000, "end_ms": 8000, "text": "replacement"}],
            "duration_seconds": 3, "model": "mock",
        }) as recognition:
            job_id = jobs.start_range_job(self.course_id, 6, 7)
            job = self._wait()
        self.assertEqual(job["id"], job_id)
        self.assertEqual(job["status"], "completed")
        self.assertIn("5–8 秒", job["message"])
        self.assertEqual(recognition.call_args.args[1:3], (5.0, 8.0))
        after = db.list_segments(self.course_id)
        self.assertEqual([(row["id"], row["text"]) for row in after if row["text"] != "replacement"],
                         [(before[0]["id"], "before"), (before[2]["id"], "after")])
        self.assertEqual([row["ordinal"] for row in after], [0, 1, 2])

    def test_failure_and_empty_output_keep_original_rows(self) -> None:
        db.add_segments(self.course_id, [{"start_ms": 5000, "end_ms": 8000, "text": "original"}], source="whisper")
        original = db.list_segments(self.course_id)
        for result in (RuntimeError("mock failure"), {
            "segments": [], "duration_seconds": 3, "model": "mock",
        }):
            with self.subTest(result=result), mock.patch.object(transcribe, "transcribe_audio_range", side_effect=[result] if isinstance(result, Exception) else None,
                                                   return_value=result if isinstance(result, dict) else None):
                jobs.start_range_job(self.course_id, 5, 8)
                job = self._wait()
                self.assertEqual(job["status"], "error")
                self.assertEqual(db.list_segments(self.course_id), original)
                self.assertEqual(db.get_course(self.course_id)["processing_status"], "ready")

    def test_validation_and_manual_overlap_after_expansion(self) -> None:
        db.add_segments(self.course_id, [
            {"start_ms": 5000, "end_ms": 8000, "text": "generated"},
        ], source="whisper")
        db.add_segments(self.course_id, [
            {"start_ms": 7500, "end_ms": 9000, "text": "edited"},
        ], source="manual")
        for start, end in ((-1, 2), (3, 3), (0, 61), (float("nan"), 2), (True, 2)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                jobs.start_range_job(self.course_id, start, end)
        with self.assertRaisesRegex(ValueError, "人工"):
            jobs.start_range_job(self.course_id, 5.2, 6)
        self.assertIsNone(db.latest_job(self.course_id))

    def test_replacement_transaction_rechecks_manual_edits(self) -> None:
        db.add_segments(self.course_id, [{"start_ms": 5000, "end_ms": 8000, "text": "generated"}], source="whisper")
        existing = db.list_segments(self.course_id)[0]
        db.patch_segment(existing["id"], {"text": "edited"})
        with self.assertRaisesRegex(ValueError, "人工"):
            db.replace_range_segments(self.course_id, 5000, 8000, {existing["id"]}, [
                {"start_ms": 5000, "end_ms": 8000, "text": "new"},
            ])
        self.assertEqual(db.list_segments(self.course_id)[0]["text"], "edited")

    def test_stale_range_job_is_interrupted_without_replay(self) -> None:
        old_id = db.create_job(self.course_id, kind="range_transcription")
        db.set_course_status(self.course_id, "processing")

        interrupted = jobs.get_job(self.course_id)

        self.assertEqual(interrupted["id"], old_id)
        self.assertEqual(interrupted["status"], "error")
        self.assertEqual(interrupted["stage"], "interrupted")
        self.assertEqual(db.get_course(self.course_id)["processing_status"], "ready")

    def test_direct_retry_after_stale_job_restores_ready_on_failure(self) -> None:
        db.create_job(self.course_id, kind="range_transcription")
        db.set_course_status(self.course_id, "processing")
        with mock.patch.object(transcribe, "transcribe_audio_range", side_effect=RuntimeError("failed")):
            jobs.start_range_job(self.course_id, 5, 8)
            job = self._wait()
        self.assertEqual(job["status"], "error")
        self.assertEqual(db.get_course(self.course_id)["processing_status"], "ready")


class RangeTranscriptionTests(unittest.TestCase):
    def test_chunk_preview_uses_absolute_course_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.wav"
            source.write_bytes(b"audio")
            observed: list[tuple[list[dict], int, int]] = []

            def extract(command: list[str], *, timeout: float | None) -> subprocess.CompletedProcess[str]:
                del timeout
                Path(command[-1]).write_bytes(b"clip")
                return subprocess.CompletedProcess(command, 0, "", "")

            def recognize(*args: object, **kwargs: object) -> dict:
                del args
                kwargs["chunk_callback"](
                    [{"start_ms": 250, "end_ms": 1250, "text": "selected"}], 1, 2,
                )
                return {"segments": [{"start_ms": 250, "end_ms": 1250, "text": "selected"}],
                        "duration_seconds": 3, "model": "mock"}

            with mock.patch.object(transcribe, "_tool", return_value="ffmpeg"), \
                 mock.patch.object(transcribe, "_run_streaming", side_effect=extract), \
                 mock.patch.object(transcribe, "transcribe_audio", side_effect=recognize):
                transcribe.transcribe_audio_range(
                    source, 12, 15, root,
                    chunk_callback=lambda rows, number, total: observed.append((rows, number, total)),
                )
            self.assertEqual(observed, [([{
                "start_ms": 12250, "end_ms": 13250, "text": "selected",
            }], 1, 2)])

    def test_extracts_selected_audio_and_maps_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.wav"
            source.write_bytes(b"audio")
            commands = []

            def extract(command: list[str], *, timeout: float | None) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                Path(command[-1]).write_bytes(b"clip")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(transcribe, "_tool", return_value="ffmpeg"), \
                 mock.patch.object(transcribe, "_run_streaming", side_effect=extract), \
                 mock.patch.object(transcribe, "transcribe_audio", return_value={
                     "segments": [{"start_ms": 250, "end_ms": 1250, "text": "selected"}],
                     "duration_seconds": 3, "model": "mock",
                 }) as recognition:
                result = transcribe.transcribe_audio_range(source, 12, 15, root, chunk_minutes=10)
            self.assertEqual(commands[0][commands[0].index("-ss") + 1], "12.000")
            self.assertEqual(commands[0][commands[0].index("-t") + 1], "3.000")
            self.assertEqual(result["segments"], [{
                "start_ms": 12250, "end_ms": 13250, "text": "selected",
            }])
            self.assertEqual(recognition.call_args.kwargs["chunk_minutes"], 10)
            self.assertEqual([path.name for path in root.iterdir()], ["recording.wav"])


if __name__ == "__main__":
    unittest.main()
