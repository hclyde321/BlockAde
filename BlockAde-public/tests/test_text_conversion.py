from __future__ import annotations

import unittest

import transcribe
from text_conversion import to_traditional_chinese


class TranscriptTraditionalChineseTests(unittest.TestCase):
    def test_converts_simplified_chinese_and_preserves_english(self) -> None:
        converted = to_traditional_chinese("软件使用 English ECG ABC")

        self.assertEqual(converted, "軟體使用 English ECG ABC")

    def test_whisper_normalization_preserves_timestamps_and_converts_text(self) -> None:
        segments = transcribe.parse_whisper_json({"transcription": [
            {
                "offsets": {"from": 1250, "to": 2750},
                "timestamps": {"from": "00:00:01,250", "to": "00:00:02,750"},
                "text": "软件可用于 ECG analysis",
            },
        ]})

        self.assertEqual(segments, [{
            "start_ms": 1250,
            "end_ms": 2750,
            "text": "軟件可用於 ECG analysis",
        }])

    def test_verbatim_preserves_fillers_repetitions_and_corrections(self) -> None:
        text = "嗯，呃，我、我说软件，不是，硬件。ECG，ECG。"
        segments = transcribe.parse_whisper_json({"segments": [
            {"start": 0, "end": 5, "text": text},
            {"start": 5, "end": 6, "text": "ECG，ECG。"},
        ]})
        self.assertEqual(segments[0]["text"], "嗯，呃，我、我說軟件，不是，硬件。ECG，ECG。")
        self.assertEqual(segments[1]["text"], "ECG，ECG。")
