from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

import audio_merge
import db
import jobs
import transcribe


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class AudioPartsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir, self.old_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = (Path(self.temp.name) / "data").resolve()
        db.DB_PATH = db.DATA_DIR / "app.sqlite3"
        db.init_db()
        self.course_id = db.create_course("two recordings")["id"]
        self.folder = db.DATA_DIR / self.course_id
        self.folder.mkdir(parents=True)

    def tearDown(self) -> None:
        db.DATA_DIR, db.DB_PATH = self.old_data_dir, self.old_db_path
        self.temp.cleanup()

    def _wav(self, name: str, sample: int, seconds: int = 1) -> Path:
        path = self.folder / name
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(sample.to_bytes(2, "little", signed=True) * (16000 * seconds))
        return path

    def _wait(self) -> dict:
        for _ in range(500):
            job = db.latest_job(self.course_id)
            if job and job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail("audio finalize did not finish")

    def test_ordered_merge_append_preserves_old_rows_and_attempts(self) -> None:
        first = self._wav("first.wav", 1000)
        second = self._wav("second.wav", 2000)
        for path in (second, first):
            db.add_audio_part(self.course_id, path, path.name, "audio/wav", path.stat().st_size, 1)
        with mock.patch.object(transcribe, "transcribe_audio", return_value={
            "segments": [{"start_ms": 100, "end_ms": 500, "text": "old"}],
            "duration_seconds": 2, "model": "mock",
        }):
            jobs.start_audio_finalize(self.course_id)
            self.assertEqual(self._wait()["status"], "completed")
        merged = Path(db.get_asset(self.course_id, "audio")["path"])
        self.assertAlmostEqual(audio_merge.probe_audio(merged), 2, places=2)
        with wave.open(str(merged), "rb") as audio:
            samples = audio.readframes(32000)
        self.assertEqual(samples[:2], (2000).to_bytes(2, "little", signed=True))
        self.assertEqual(samples[32000:32002], (1000).to_bytes(2, "little", signed=True))
        old_segment = db.list_segments(self.course_id)[0]
        question_id = db.add_question(self.course_id, {"number": "1", "stem": "old"})
        db.add_attempt(question_id, "A", False)
        db.save_match(question_id, {"segment_id": old_segment["id"], "status": "confirmed",
                                    "provider": "manual", "question_time_ms": 500})
        third = self._wav("third.wav", 3000)
        db.add_audio_part(self.course_id, third, third.name, "audio/wav", third.stat().st_size, 1)
        with mock.patch.object(transcribe, "transcribe_audio_range", return_value={
            "segments": [{"start_ms": 2100, "end_ms": 2500, "text": "new"}],
            "duration_seconds": 1, "model": "mock",
        }) as recognize:
            jobs.start_audio_finalize(self.course_id)
            self.assertEqual(self._wait()["status"], "completed")
        self.assertAlmostEqual(recognize.call_args.args[1], 2, places=2)
        self.assertAlmostEqual(recognize.call_args.args[2], 3, places=2)
        segments = db.list_segments(self.course_id)
        self.assertEqual([(item["id"], item["text"]) for item in segments],
                         [(old_segment["id"], "old"), (segments[1]["id"], "new")])
        self.assertEqual(db.get_match(question_id)["provider"], "manual")
        self.assertEqual(len(db.list_attempts(self.course_id)), 1)
        self.assertTrue(all(part["status"] == "ready" for part in db.list_audio_parts(self.course_id)))

    def test_failed_transcription_keeps_old_asset_then_retry_once(self) -> None:
        old = self._wav("old.wav", 1000)
        db.set_asset(self.course_id, "audio", old, old.name, "audio/wav", old.stat().st_size)
        db.replace_single_audio_part(self.course_id, db.get_asset(self.course_id, "audio"))
        db.set_course_status(self.course_id, "ready", 1)
        db.add_segments(self.course_id, [{"start_ms": 100, "end_ms": 400, "text": "old"}], source="whisper")
        pending = self._wav("new.wav", 2000)
        db.add_audio_part(self.course_id, pending, pending.name, "audio/wav", pending.stat().st_size, 1)
        with mock.patch.object(transcribe, "transcribe_audio_range", side_effect=RuntimeError("model failed")):
            jobs.start_audio_finalize(self.course_id)
            self.assertEqual(self._wait()["status"], "error")
        self.assertEqual(db.get_asset(self.course_id, "audio")["path"], str(old.resolve()))
        self.assertEqual(len(db.list_segments(self.course_id)), 1)
        self.assertEqual(db.list_audio_parts(self.course_id)[1]["status"], "pending")
        with mock.patch.object(transcribe, "transcribe_audio_range", return_value={
            "segments": [{"start_ms": 1100, "end_ms": 1400, "text": "new"}],
            "duration_seconds": 1, "model": "mock",
        }):
            jobs.start_audio_finalize(self.course_id)
            self.assertEqual(self._wait()["status"], "completed")
        self.assertEqual(len(db.list_segments(self.course_id)), 2)
        self.assertAlmostEqual(db.get_course(self.course_id)["duration_seconds"], 2, places=2)
        with self.assertRaisesRegex(ValueError, "沒有待合併"):
            jobs.start_audio_finalize(self.course_id)

    def test_legacy_m4a_append_uses_decoded_sample_boundary(self) -> None:
        old_wav = self._wav("legacy.wav", 900)
        old_m4a = self.folder / "legacy.m4a"
        subprocess.run([shutil.which("ffmpeg"), "-loglevel", "error", "-y", "-i", str(old_wav),
                        "-c:a", "aac", str(old_m4a)], check=True)
        expected_wav = self.folder / "expected-prefix.wav"
        expected_start = audio_merge.merge_audio([old_m4a], expected_wav)
        db.set_asset(self.course_id, "audio", old_m4a, old_m4a.name, "audio/mp4", old_m4a.stat().st_size)
        db.replace_single_audio_part(self.course_id, db.get_asset(self.course_id, "audio"))
        db.set_course_status(self.course_id, "ready", audio_merge.probe_audio(old_m4a))
        db.add_segments(self.course_id, [{"start_ms": 100, "end_ms": 400, "text": "old"}], source="whisper")
        new = self._wav("appended.wav", 1800)
        db.add_audio_part(self.course_id, new, new.name, "audio/wav", new.stat().st_size, 1)

        def recognize(_path: Path, start: float, end: float, *_args: object, **_kwargs: object) -> dict:
            return {"segments": [{"start_ms": round((start + 0.1) * 1000),
                                    "end_ms": round((end - 0.1) * 1000), "text": "new"}],
                    "duration_seconds": end - start, "model": "mock"}

        with mock.patch.object(transcribe, "transcribe_audio_range", side_effect=recognize) as patched:
            jobs.start_audio_finalize(self.course_id)
            self.assertEqual(self._wait()["status"], "completed")
        self.assertAlmostEqual(patched.call_args.args[1], expected_start, places=4)
        self.assertEqual([segment["text"] for segment in db.list_segments(self.course_id)], ["old", "new"])

    def test_interrupted_finalize_keeps_pending_parts_for_retry(self) -> None:
        source = self._wav("pending.wav", 900)
        db.add_audio_part(self.course_id, source, source.name, "audio/wav", source.stat().st_size, 1)
        db.create_job(self.course_id, kind="audio_finalize")
        db.set_course_status(self.course_id, "processing")
        job = jobs.get_job(self.course_id)
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["stage"], "interrupted")
        self.assertEqual(db.get_course(self.course_id)["processing_status"], "draft")
        self.assertEqual(db.list_audio_parts(self.course_id)[0]["status"], "pending")
