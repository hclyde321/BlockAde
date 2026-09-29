"""Rate-limit recovery for an explicitly requested lecture match job."""

import unittest
from unittest.mock import patch

import jobs
import match
from openai_match import OpenAIMatchError, OpenAIRateLimitError


SEGMENTS = [{"id": "seg-1", "start_ms": 1000, "end_ms": 2400,
             "text": "大面積燒傷會導致全身性發炎反應。"}]


def _question(qid):
    return {
        "id": qid, "stem": "大面積燒傷會導致全身性發炎反應",
        "options": [{"key": "A", "text": "是"}, {"key": "B", "text": "非"}],
        "answer_key": "A", "needs_review": False,
    }


def _suggestion():
    return match.MatchSuggestion(
        verdict="match", confidence_label="high",
        evidence_segment_ids=["seg-1"], last_evidence_segment_id="seg-1",
        reason="課堂有明確說明。",
    )


class FakeProvider:
    name = "openai"
    auto_confirm_high_confidence = True

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def candidate_windows(self, question, segments):
        return [{"segments": segments}]

    def suggest(self, question, segments):
        self.calls.append(question["id"])
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class MatchRateRetryTests(unittest.TestCase):
    def _run(self, provider, questions):
        saved = []
        updates = []
        sleeps = []
        with (patch.object(jobs.db, "list_questions", return_value=questions),
              patch.object(jobs.db, "list_segments", return_value=SEGMENTS),
              patch.object(jobs.db, "get_match", return_value=None),
              patch.object(jobs.db, "get_course", return_value={"duration_seconds": 4}),
              patch.object(jobs.db, "save_match", side_effect=lambda qid, row: saved.append((qid, row))),
              patch.object(jobs.db, "update_job", side_effect=lambda *args: updates.append(args)),
              patch.object(jobs.time, "sleep", side_effect=lambda seconds: sleeps.append(seconds))):
            jobs._run_match_analysis("job-1", "course-1", provider, len(questions))
        return saved, updates, sleeps

    def test_transient_limit_retries_same_question_and_keeps_prior_match(self):
        provider = FakeProvider([
            _suggestion(), OpenAIRateLimitError("rate limited", 2), _suggestion(),
        ])
        saved, updates, sleeps = self._run(provider, [_question("q1"), _question("q2")])

        self.assertEqual(provider.calls, ["q1", "q2", "q2"])
        self.assertEqual([qid for qid, _ in saved], ["q1", "q2"])
        self.assertTrue(any(row[2] == "rate_limit_wait" for row in updates))
        self.assertEqual(updates[-1][1:3], ("completed", "done"))
        self.assertGreaterEqual(sum(sleeps), 2)
        self.assertTrue(all(seconds <= 3 for seconds in sleeps))

    def test_repeated_limit_stops_after_three_retries(self):
        provider = FakeProvider([OpenAIRateLimitError("rate limited", 1)] * 4)
        saved, updates, _ = self._run(provider, [_question("q1")])

        self.assertEqual(provider.calls, ["q1"] * 4)
        self.assertEqual(saved, [])
        self.assertEqual(updates[-1][1:3], ("error", "needs_attention"))
        self.assertIn("已保留完成的 0 題", updates[-1][4])

    def test_long_retry_after_is_not_retried_early(self):
        provider = FakeProvider([OpenAIRateLimitError("rate limited", 180)])
        _, updates, sleeps = self._run(provider, [_question("q1")])

        self.assertEqual(provider.calls, ["q1"])
        self.assertEqual(sleeps, [])
        self.assertIn("180 秒", updates[-1][4])
        self.assertEqual(updates[-1][1:3], ("error", "needs_attention"))

    def test_quota_error_is_not_retried(self):
        provider = FakeProvider([OpenAIMatchError("API 可用額度不足（insufficient_quota）")])
        _, updates, sleeps = self._run(provider, [_question("q1")])

        self.assertEqual(provider.calls, ["q1"])
        self.assertEqual(sleeps, [])
        self.assertEqual(updates[-1][1:3], ("error", "needs_attention"))
        self.assertIn("insufficient_quota", updates[-1][4])


if __name__ == "__main__":
    unittest.main()
