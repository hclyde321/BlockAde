from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import transcribe


class TranscriptionProgressParsingTests(unittest.TestCase):
    def test_reads_whisper_percentage_and_timestamp_progress(self) -> None:
        self.assertEqual(
            transcribe._whisper_progress_fraction(
                "whisper_print_progress_callback: progress = 42%", None
            ),
            0.42,
        )
        self.assertAlmostEqual(
            transcribe._whisper_progress_fraction(
                "[00:00:05.500 --> 00:00:09.000] sample", 30.0
            ),
            0.3,
        )
        self.assertAlmostEqual(
            transcribe._ffmpeg_progress_fraction("out_time=00:00:05.000000", 10.0),
            0.5,
        )

    def test_streams_lines_and_keeps_only_bounded_diagnostic_tails(self) -> None:
        observed: list[tuple[str, str]] = []
        command = [
            sys.executable,
            "-c",
            "import sys; print('x' * 20000); print('progress = 42%', file=sys.stderr)",
        ]

        result = transcribe._run_streaming(command, timeout=5, line_callback=lambda stream, line: observed.append((stream, line)))

        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), transcribe._DIAGNOSTIC_TAIL_BYTES)
        self.assertLessEqual(len(result.stderr.encode("utf-8")), transcribe._DIAGNOSTIC_TAIL_BYTES)
        self.assertIn(("stderr", "progress = 42%"), observed)

    def test_streaming_process_timeout_preserves_recent_output(self) -> None:
        command = [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(10)"]

        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            transcribe._run_streaming(command, timeout=0.2)

        self.assertIn("started", str(raised.exception.output))


class TranscriptionCallbackTests(unittest.TestCase):
    def _run_with_payload(self, payload: dict[str, object]) -> tuple[list[tuple[float, str]], RuntimeError | None]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lecture.m4a"
            model = root / "model.bin"
            work = root / "work"
            source.write_bytes(b"audio")
            model.write_bytes(b"model")
            callbacks: list[tuple[float, str]] = []

            def fake_runner(command: list[str], *, timeout: float | None, line_callback: object) -> subprocess.CompletedProcess[str]:
                del timeout
                callback = line_callback
                if command[0] == "ffmpeg":
                    Path(command[-1]).write_bytes(b"wav")
                    callback("stdout", "out_time=00:00:05.000000")
                else:
                    output_base = Path(command[command.index("-of") + 1])
                    output_base.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
                    callback("stderr", "whisper_print_progress_callback: progress = 50%")
                return subprocess.CompletedProcess(command, 0, "", "")

            env = {"WHISPER_REVIEW_LIMIT": "0", "WHISPER_MODEL": str(model), "WHISPER_TIMEOUT_SECONDS": "600"}
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(transcribe.db, "DATA_DIR", root / "data"),
                mock.patch.object(transcribe, "_tool", side_effect=lambda name: name),
                mock.patch.object(transcribe, "_duration_seconds", return_value=10.0),
                mock.patch.object(transcribe, "_run_streaming", side_effect=fake_runner),
            ):
                error = None
                try:
                    transcribe.transcribe_audio(source, work, lambda fraction, message: callbacks.append((fraction, message)))
                except RuntimeError as exc:
                    error = exc
            return callbacks, error

    def test_success_reports_observed_progress_and_completes_after_parsing(self) -> None:
        callbacks, error = self._run_with_payload({"transcription": [
            {"offsets": {"from": 0, "to": 1000}, "text": "软件"},
        ]})

        self.assertIsNone(error)
        self.assertIn((0.55 * 0.85, "Whisper 轉錄進度 50%（預設模式）"), callbacks)
        self.assertEqual(callbacks[-1], (1.0, "轉錄完成"))
        self.assertTrue(all(fraction < 1.0 for fraction, _ in callbacks[:-1]))

    def test_invalid_transcript_never_reports_complete(self) -> None:
        callbacks, error = self._run_with_payload({"transcription": []})

        self.assertIsNotNone(error)
        self.assertFalse(any(fraction == 1.0 for fraction, _ in callbacks))


class TranscriptionChunkingTests(unittest.TestCase):
    def test_chunk_callback_runs_before_next_chunk_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lecture.m4a"
            source.write_bytes(b"audio")
            events: list[tuple[object, ...]] = []

            def extract(command: list[str], *, timeout: float | None) -> subprocess.CompletedProcess[str]:
                del timeout
                Path(command[-1]).write_bytes(b"wav")
                return subprocess.CompletedProcess(command, 0, "", "")

            def transcribe_chunk(*args: object, **kwargs: object) -> dict[str, object]:
                del args, kwargs
                events.append(("recognize",))
                index = sum(event == ("recognize",) for event in events)
                local_start = 1000 if index == 1 else (3000 if index == 2 else 2000)
                return {"segments": [{"start_ms": local_start, "end_ms": local_start + 500, "text": "文字"}],
                        "duration_seconds": 0, "model": "mock"}

            with (mock.patch.object(transcribe, "_duration_seconds", return_value=601.0),
                  mock.patch.object(transcribe, "_tool", return_value="ffmpeg"),
                  mock.patch.object(transcribe, "_run_streaming", side_effect=extract),
                  mock.patch.object(transcribe, "_transcribe_audio_single", side_effect=transcribe_chunk)):
                transcribe.transcribe_audio(
                    source, Path(directory) / "work", chunk_minutes=5,
                    chunk_callback=lambda rows, current, total: events.append(
                        ("callback", current, total, rows[0]["start_ms"])),
                )

            self.assertEqual(events, [
                ("recognize",), ("callback", 1, 3, 1000),
                ("recognize",), ("callback", 2, 3, 301000),
                ("recognize",), ("callback", 3, 3, 600000),
            ])

    def _run_chunked(
        self,
        chunk_minutes: int,
        *,
        duration_seconds: float = 750.0,
        fail_on_chunk: int | None = None,
    ) -> tuple[dict[str, object] | None, list[tuple[float, str]], list[list[str]], RuntimeError | None]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lecture.m4a"
            source.write_bytes(b"source audio")
            work = root / "work"
            callbacks: list[tuple[float, str]] = []
            extraction_commands: list[list[str]] = []
            helper_calls = 0

            def fake_runner(command: list[str], *, timeout: float | None, line_callback: object = None) -> subprocess.CompletedProcess[str]:
                del timeout, line_callback
                extraction_commands.append(command)
                Path(command[-1]).write_bytes(b"wav")
                return subprocess.CompletedProcess(command, 0, "", "")

            def fake_single(
                audio_path: str | Path,
                work_dir: str | Path | None,
                progress_callback: object,
                *,
                allow_empty: bool,
                input_is_normalized_wav: bool,
            ) -> dict[str, object]:
                nonlocal helper_calls
                self.assertTrue(allow_empty)
                self.assertTrue(input_is_normalized_wav)
                del work_dir
                chunk_index = helper_calls
                helper_calls += 1
                callback = progress_callback
                callback(0.5, "Whisper 轉錄進度 50%")
                callback(1.0, "轉錄完成")
                if fail_on_chunk == helper_calls:
                    raise RuntimeError("mock chunk failure")
                local_start = (chunk_minutes * 60 - 2) if chunk_index == 0 else 2
                segments = [{
                    "start_ms": local_start * 1000,
                    "end_ms": (local_start + 1) * 1000,
                    "text": f"段落{chunk_index + 1}",
                }]
                return {"segments": segments, "duration_seconds": 0, "model": "model.bin"}

            with (
                mock.patch.dict(os.environ, {"WHISPER_MODEL": "/tmp/model.bin"}, clear=True),
                mock.patch.object(transcribe, "_duration_seconds", return_value=duration_seconds),
                mock.patch.object(transcribe, "_tool", side_effect=lambda name: name),
                mock.patch.object(transcribe, "_run_streaming", side_effect=fake_runner),
                mock.patch.object(transcribe, "_transcribe_audio_single", side_effect=fake_single),
            ):
                result = None
                error = None
                try:
                    result = transcribe.transcribe_audio(
                        source, work, lambda fraction, message: callbacks.append((fraction, message)),
                        chunk_minutes=chunk_minutes,
                    )
                except RuntimeError as exc:
                    error = exc
            self.assertEqual(list(work.iterdir()) if work.exists() else [], [])
            self.assertTrue(source.is_file())
            return result, callbacks, extraction_commands, error

    def test_five_and_ten_minute_chunks_receive_absolute_timestamps(self) -> None:
        for chunk_minutes, expected_starts, expected_segment_starts in (
            (5, ["0.000", "298.000", "598.000"], [298000, 300000, 600000]),
            (10, ["0.000", "598.000"], [598000, 600000]),
        ):
            with self.subTest(chunk_minutes=chunk_minutes):
                result, callbacks, commands, error = self._run_chunked(chunk_minutes)

                self.assertIsNone(error)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual([segment["start_ms"] for segment in result["segments"]], expected_segment_starts)
                self.assertEqual([command[command.index("-ss") + 1] for command in commands], expected_starts)
                self.assertEqual(callbacks[-1], (1.0, "轉錄完成"))
                self.assertTrue(all(fraction < 1.0 for fraction, _ in callbacks[:-1]))

    def test_chunk_failure_never_reports_completion_or_returns_partial_rows(self) -> None:
        result, callbacks, _, error = self._run_chunked(5, fail_on_chunk=2)

        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertFalse(any(fraction == 1.0 for fraction, _ in callbacks))

    def test_fractional_tail_gets_its_own_final_chunk(self) -> None:
        result, _, commands, error = self._run_chunked(5, duration_seconds=300.1)

        self.assertIsNone(error)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual([command[command.index("-ss") + 1] for command in commands], ["0.000", "298.000"])
        self.assertEqual([segment["start_ms"] for segment in result["segments"]], [298000, 300000])

    def test_empty_whisper_chunk_is_allowed_only_when_explicitly_requested(self) -> None:
        self.assertEqual(transcribe.parse_whisper_json({"transcription": []}, allow_empty=True), [])
        with self.assertRaises(RuntimeError):
            transcribe.parse_whisper_json({"transcription": []})


if __name__ == "__main__":
    unittest.main()
