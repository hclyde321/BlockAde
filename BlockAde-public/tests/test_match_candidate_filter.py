"""Lecture candidate retrieval should not expand generic question wording."""

import unittest

from match import top_candidate_windows


class CandidateFilterTests(unittest.TestCase):
    def test_generic_multiple_choice_words_do_not_create_candidates(self):
        question = {
            "stem": "Which of the following is not part of an autopsy?",
            "options": [{"text": "Consent"}, {"text": "Pathologist"}, {"text": "Dissection"}],
        }
        segments = [
            {"id": "s1", "start_ms": 0, "end_ms": 2000,
             "text": "Which one is not a factor in the following disease process?"},
            {"id": "s2", "start_ms": 2000, "end_ms": 4000,
             "text": "Negative charge of phospholipid"},
        ]
        self.assertEqual(top_candidate_windows(question, segments), [])

    def test_exact_medical_terms_retain_course_candidates(self):
        question = {
            "stem": "Which factor has anti-thrombotic activity?",
            "options": [{"text": "Thrombomodulin"},
                        {"text": "Tissue factor pathway inhibitor"}],
        }
        segments = [
            {"id": "s1", "start_ms": 1000, "end_ms": 3000,
             "text": "Thrombomodulin and tissue factor pathway inhibitor regulate coagulation."},
        ]
        windows = top_candidate_windows(question, segments)
        self.assertEqual(windows[0]["segment_ids"], ["s1"])

    def test_chinese_question_boilerplate_does_not_count_as_topic(self):
        question = {
            "stem": "下列何者正確？",
            "options": [{"text": "屍體解剖"}, {"text": "法醫鑑定"}],
        }
        segments = [
            {"id": "s1", "start_ms": 0, "end_ms": 1000,
             "text": "下列我們介紹凝血系統。"},
        ]
        self.assertEqual(top_candidate_windows(question, segments), [])


if __name__ == "__main__":
    unittest.main()
