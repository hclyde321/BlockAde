"""Lecture handouts guide scope; only transcript rows can support a time."""

import json
import unittest
from unittest.mock import patch

import jobs
from openai_match import OpenAIMatchProvider


QUESTION = {
    "id": "q-1", "stem": "Which molecule mediates platelet tethering to injured endothelium?",
    "options": [{"key": "A", "text": "Albumin"}, {"key": "B", "text": "Fibrinogen"}],
    "answer_key": "B", "needs_review": False,
}
HANDOUT = [{
    "id": "handout-h1-p3", "handout_id": "h1", "filename": "凝血講義.pdf",
    "page": 3, "section": "Primary hemostasis",
    "text": "Platelet tethering to injured endothelium is mediated by von Willebrand factor.",
}]


class FakeResponse:
    def __init__(self, answer):
        self.answer = answer

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        payload = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(self.answer)}
        ]}]}
        return json.dumps(payload).encode()[:limit]


class HandoutMatchContextTests(unittest.TestCase):
    def test_handout_bridges_to_real_transcript_window_and_is_sent_as_scope(self):
        provider = OpenAIMatchProvider(api_key="test-key", handout_context=HANDOUT)
        transcript = [{
            "id": "real-segment", "start_ms": 12000, "end_ms": 18000,
            "text": "von Willebrand factor binds the platelet receptor and starts primary hemostasis.",
        }]
        windows = provider.candidate_windows(QUESTION, transcript)
        self.assertEqual([part["id"] for part in windows[0]["segments"]], ["real-segment"])
        answer = {
            "verdict": "match", "confidence": "high",
            "evidence_segment_ids": ["real-segment"],
            "last_evidence_segment_id": "real-segment", "reason": "逐字稿有說明。",
        }
        with patch("openai_match.urlopen", return_value=FakeResponse(answer)) as remote:
            suggestion = provider.suggest(QUESTION, transcript)
        payload = json.loads(json.loads(remote.call_args.args[0].data)["input"])
        self.assertEqual(payload["lecture_handout_excerpts"][0]["id"], "handout-h1-p3")
        self.assertEqual(payload["candidate_windows"][0]["segments"][0]["id"], "real-segment")
        self.assertEqual(suggestion.evidence_segment_ids, ["real-segment"])

    def test_handout_scope_without_transcript_stays_pending_and_never_calls_api(self):
        provider = OpenAIMatchProvider(api_key="test-key", handout_context=HANDOUT)
        transcript = [{
            "id": "unrelated", "start_ms": 1000, "end_ms": 2000,
            "text": "The kidney filters sodium and water.",
        }]
        self.assertEqual(provider.candidate_windows(QUESTION, transcript), [])
        with patch("openai_match.urlopen") as remote:
            suggestion = provider.suggest(QUESTION, transcript)
        remote.assert_not_called()
        self.assertEqual(suggestion.status, "pending_confirmation")
        self.assertIsNone(suggestion.question_time_ms)
        self.assertIn("凝血講義.pdf 第 3 頁", suggestion.reason)

    def test_no_match_response_does_not_override_relevant_handout_scope(self):
        provider = OpenAIMatchProvider(api_key="test-key", handout_context=HANDOUT)
        transcript = [{
            "id": "real-segment", "start_ms": 12000, "end_ms": 18000,
            "text": "von Willebrand factor binds the platelet receptor.",
        }]
        answer = {
            "verdict": "no_match", "confidence": "low", "evidence_segment_ids": [],
            "last_evidence_segment_id": None, "reason": "沒有找到完整說明。",
        }
        with patch("openai_match.urlopen", return_value=FakeResponse(answer)):
            suggestion = provider.suggest(QUESTION, transcript)
        self.assertEqual(suggestion.status, "pending_confirmation")
        self.assertEqual(suggestion.verdict, "uncertain")
        self.assertIsNone(suggestion.question_time_ms)

    def test_zero_sent_summary_explicitly_says_analysis_is_incomplete(self):
        provider = OpenAIMatchProvider(api_key="test-key", handout_context=HANDOUT)
        saved = []
        updates = []
        with (patch.object(jobs.db, "list_questions", return_value=[QUESTION]),
              patch.object(jobs.db, "list_segments", return_value=[{
                  "id": "unrelated", "start_ms": 1000, "end_ms": 2000,
                  "text": "The kidney filters sodium and water.",
              }]),
              patch.object(jobs.db, "get_match", return_value=None),
              patch.object(jobs.db, "save_match", side_effect=lambda qid, row: saved.append(row)),
              patch.object(jobs.db, "update_job", side_effect=lambda *args: updates.append(args)),
              patch("openai_match.urlopen") as remote):
            jobs._run_match_analysis("job", "course", provider, 10)
        remote.assert_not_called()
        self.assertEqual(saved[0]["provider"], "handout_local")
        self.assertIsNone(saved[0]["question_time_ms"])
        self.assertIn("本輪沒有可送出的候選題，不代表全部題目分析完成", updates[-1][4])

    def test_handout_can_shortlist_without_a_transcript_but_has_no_time(self):
        provider = OpenAIMatchProvider(api_key="test-key", handout_context=HANDOUT)
        saved = []
        updates = []
        with (patch.object(jobs.db, "list_questions", return_value=[QUESTION]),
              patch.object(jobs.db, "list_segments", return_value=[]),
              patch.object(jobs.db, "get_match", return_value=None),
              patch.object(jobs.db, "save_match", side_effect=lambda qid, row: saved.append(row)),
              patch.object(jobs.db, "update_job", side_effect=lambda *args: updates.append(args)),
              patch("openai_match.urlopen") as remote):
            jobs._run_match_analysis("job", "course", provider, 10)
        remote.assert_not_called()
        self.assertEqual(saved[0]["status"], "pending_confirmation")
        self.assertIsNone(saved[0]["question_time_ms"])
        self.assertEqual(updates[-1][1:3], ("completed", "done"))


if __name__ == "__main__":
    unittest.main()
