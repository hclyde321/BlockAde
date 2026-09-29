from collections import Counter
from pathlib import Path
import unittest

from pdf_import import import_answers, import_questions


ROOT = Path(__file__).resolve().parents[1]
BANK_DIR = ROOT / "data" / "banks" / "f2b8974b-167f-41d7-99a1-e9e7341bbbd7"
QUESTION_PDF = BANK_DIR / "questions-1aef518fd57240fcb2c72c869d487f69.pdf"
ANSWER_PDF = BANK_DIR / "answers-4a57df4d740049478df6e41ae65f3c3d.pdf"


@unittest.skipUnless(QUESTION_PDF.is_file() and ANSWER_PDF.is_file(), "需要本機病理學 PDF 樣本")
class PathologyPdfImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.questions = import_questions(QUESTION_PDF)
        cls.answers = import_answers(ANSWER_PDF, question_rows=cls.questions)

    def test_topic_and_exam_headings_preserve_local_numbers(self):
        self.assertEqual(len(self.questions), 323)
        self.assertEqual(len(self.answers), 317)
        self.assertEqual(self.questions[0]["section"], "鄭永銘老師—緒論")
        self.assertEqual(self.questions[0]["number"], "1")
        self.assertEqual(self.questions[3]["section"], "吳木榮老師—病理解剖")
        self.assertEqual(self.questions[3]["number"], "1")
        self.assertEqual(self.questions[164]["section"], "B05 第一次區段考")
        self.assertEqual(self.questions[164]["number"], "1")
        self.assertFalse(any(not row["section"] for row in self.questions + self.answers))
        self.assertFalse([item for item, count in Counter((row["section"], row["number"]) for row in self.questions).items() if count > 1])

    def test_answers_come_from_the_matching_explanation_rows(self):
        answers = {(row["section"], row["number"]): row for row in self.answers}
        self.assertEqual(answers[("鄭永銘老師—緒論", "1")]["answer_key"], "E")
        self.assertEqual(answers[("鄭永銘老師—緒論", "2")]["answer_key"], "B")
        self.assertEqual(answers[("B08 第一次區段考", "1")]["answer_key"], "C")
        self.assertEqual(answers[("B09 第一次區段考", "26")]["answer_key"], "D")
        self.assertEqual(answers[("B10 第一次區段考", "2")]["answer_key"], "D")
        self.assertTrue(answers[("連晃駿老師—血行動力學病理（一）（二）（三）", "1")]["needs_review"])
        self.assertEqual(answers[("連晃駿老師—血行動力學病理（一）（二）（三）", "1")]["answer_key"], "CH")
        self.assertTrue(answers[("B06 第一次區段考", "26")]["needs_review"])
        self.assertEqual(answers[("B06 第一次區段考", "26")]["answer_key"], "F")
        self.assertNotIn(("B10 第一次區段考", "1"), answers)


if __name__ == "__main__":
    unittest.main()
