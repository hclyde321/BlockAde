"""A missing answer must not prevent evidence-backed lecture positioning."""

import unittest
from unittest.mock import patch

import jobs
import match


SEGMENTS = [{
    "id": "seg-1", "start_ms": 1000, "end_ms": 2400,
    "text": "大面积烧伤会导致全身性发炎反应。",
}]


def question(*, answer_key=None, needs_review=True):
    return {
        "stem": "大面積燒傷會導致全身性發炎反應",
        "options": [{"key": "A", "text": "是"}, {"key": "B", "text": "非"}],
        "answer_key": answer_key,
        "needs_review": needs_review,
    }


class MatchUnreviewedQuestionsTests(unittest.TestCase):
    def test_local_match_preserves_evidence_time_but_requires_answer_review(self):
        unreviewed = match.suggest_question(question(), SEGMENTS)
        self.assertEqual(unreviewed.status, "pending_confirmation")
        self.assertEqual(unreviewed.question_time_ms, 2900)
        self.assertEqual(unreviewed.segment_id, "seg-1")
        self.assertEqual(unreviewed.provider, "local_text")
        self.assertIn("答案", unreviewed.reason)

        reviewed = match.suggest_question(question(answer_key="A", needs_review=False), SEGMENTS)
        self.assertEqual(reviewed.status, "confirmed")
        self.assertEqual(reviewed.question_time_ms, 2900)

    def test_missing_question_content_is_not_matched(self):
        incomplete = question()
        incomplete["options"] = []
        self.assertFalse(match.question_is_matchable(incomplete))
        suggestion = match.suggest_question(incomplete, SEGMENTS)
        self.assertEqual(suggestion.status, "unmatched")
        self.assertIsNone(suggestion.question_time_ms)

    def test_complete_english_statement_matches_but_isolated_terms_do_not(self):
        english = {
            "stem": "Acute inflammation causes vascular dilation and increased permeability.",
            "options": [{"key": "A", "text": "Yes"}, {"key": "B", "text": "No"}],
            "answer_key": "A", "needs_review": False,
        }
        complete = [{"id": "seg-1", "start_ms": 1000, "end_ms": 2400,
                     "text": english["stem"]}]
        isolated = [{"id": "seg-1", "start_ms": 1000, "end_ms": 2400,
                     "text": "Acute inflammation affects blood vessels."}]

        self.assertEqual(match.suggest_question(english, complete).status, "confirmed")
        self.assertEqual(match.suggest_question(english, isolated).status, "unmatched")

    def test_openai_job_analyzes_unreviewed_question_and_reports_real_results(self):
        class FakeProvider:
            name = "openai"
            auto_confirm_high_confidence = True

            def candidate_windows(self, question, segments):
                return [{"segments": segments}]

            def suggest(self, question, segments):
                return match.MatchSuggestion(
                    verdict="match", confidence_label="high",
                    evidence_segment_ids=["seg-1"], last_evidence_segment_id="seg-1",
                    reason="課堂有明確說明。",
                )

        unreviewed = {**question(), "id": "unreviewed"}
        reviewed = {**question(answer_key="A", needs_review=False), "id": "reviewed"}
        incomplete = {**question(), "id": "incomplete", "options": []}
        saved = {}
        updates = []
        with (patch.object(jobs.db, "list_questions", return_value=[unreviewed, reviewed, incomplete]),
              patch.object(jobs.db, "list_segments", return_value=SEGMENTS),
              patch.object(jobs.db, "get_match", return_value=None),
              patch.object(jobs.db, "get_course", return_value={"duration_seconds": 4}),
              patch.object(jobs.db, "save_match", side_effect=lambda qid, row: saved.setdefault(qid, row)),
              patch.object(jobs.db, "update_job", side_effect=lambda *args: updates.append(args))):
            jobs._run_match_analysis("job-1", "course-1", FakeProvider(), 10)

        self.assertEqual(set(saved), {"unreviewed", "reviewed"})
        self.assertEqual(saved["unreviewed"]["status"], "pending_confirmation")
        self.assertEqual(saved["unreviewed"]["question_time_ms"], 2900)
        self.assertEqual(saved["reviewed"]["status"], "confirmed")
        summary = updates[-1]
        self.assertEqual(summary[1:3], ("completed", "done"))
        self.assertIn("已送出 2 題", summary[4])
        self.assertIn("新增 1 題已確認出題時間", summary[4])
        self.assertIn("1 題有待確認的建議時間", summary[4])
        self.assertIn("1 題的題幹或選項文字不足", summary[4])


if __name__ == "__main__":
    unittest.main()
