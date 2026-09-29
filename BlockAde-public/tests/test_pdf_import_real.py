from collections import Counter
from pathlib import Path
import shutil
import tempfile
import unittest

import db
import jobs
from pdf_import import import_answers, import_questions


ROOT = Path(__file__).resolve().parents[1]
QUESTION_PDF = ROOT / "B11 臨床醫學考古題目 Block 1.pdf"
ANSWER_PDF = ROOT / "B11 臨床醫學考古詳解 Block 1.pdf"


def _record(rows, section, number):
    return next(row for row in rows if row["section"] == section and row["number"] == number)


@unittest.skipUnless(QUESTION_PDF.is_file() and ANSWER_PDF.is_file(), "需要本機 B11 PDF 樣本")
class RealBlock1PdfImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.questions = import_questions(QUESTION_PDF)
        cls.answers = import_answers(ANSWER_PDF)

    def test_real_block1_pdf_counts_and_contract(self):
        self.assertEqual(len(self.questions), 275)
        self.assertEqual(len(self.answers), 275)
        self.assertTrue(all({"number", "stem", "options", "source_page", "source_text", "needs_review", "section"} <= row.keys() for row in self.questions))
        self.assertTrue(all({"number", "answer_key", "explanation", "source_page", "source_text", "needs_review", "section"} <= row.keys() for row in self.answers))
        self.assertFalse([key for key, count in Counter((q["section"], q["number"]) for q in self.questions).items() if count > 1])
        self.assertFalse([key for key, count in Counter((a["section"], a["number"]) for a in self.answers).items() if count > 1])
        self.assertEqual(Counter(q["section"] for q in self.questions), Counter(a["section"] for a in self.answers))
        self.assertEqual({(q["section"], q["number"]) for q in self.questions}, {(a["section"], a["number"]) for a in self.answers})
        self.assertTrue(all("第一次區段考" not in row["stem"] and "Block 1" not in row["stem"] for row in self.questions))
        self.assertEqual(sum(question["needs_review"] for question in self.questions), 7)
        self.assertEqual(sum(answer["needs_review"] for answer in self.answers), 17)

    def test_three_sections_keep_local_number_options_and_answer_pairing(self):
        expected = {
            "B03 第一次區段考 / 選擇題": "B",
            "B07 第一次區段考 / 選擇題": "C",
            "B09 第一次區段考 / 選擇題": "A",
        }

        for section, answer_key in expected.items():
            question = _record(self.questions, section, "1")
            answer = _record(self.answers, section, "1")
            self.assertEqual([option["key"] for option in question["options"]], ["A", "B", "C", "D"])
            self.assertEqual(answer["answer_key"], answer_key)
            self.assertIsNotNone(question["source_page"])
            self.assertIsNotNone(answer["source_page"])
            self.assertFalse(question["needs_review"])
            self.assertFalse(answer["needs_review"])

    def test_non_mcq_and_uncertain_answer_rows_are_not_confirmed(self):
        essay = _record(self.questions, "B09 第一次區段考 / 問答題", "1")
        self.assertEqual(essay["question_type"], "essay")
        self.assertEqual(essay["options"], [])
        self.assertTrue(essay["needs_review"])

        no_answer_key = _record(self.answers, "手術前之評估及準備", "7")
        self.assertEqual(no_answer_key["answer_key"], "")
        self.assertTrue(no_answer_key["needs_review"])
        self.assertIn("B or BD", no_answer_key["source_text"])

        no_solution = _record(self.answers, "病史詢問及身體診察（不同老師） / 選擇題", "4")
        self.assertEqual(no_solution["answer_key"], "無解")
        self.assertTrue(no_solution["needs_review"])

        all_choices = _record(self.answers, "B08 第一次區段考 / 選擇題", "12")
        self.assertEqual(all_choices["answer_key"], "全")
        self.assertTrue(all_choices["needs_review"])

        choice_q1 = _record(self.answers, "B08 第一次區段考 / 選擇題", "1")
        self.assertEqual(choice_q1["answer_key"], "C")
        self.assertFalse(choice_q1["needs_review"])

        duplicate_tf_answer = _record(self.answers, "B08 第一次區段考 / 是非題", "1")
        self.assertEqual(duplicate_tf_answer["answer_key"], "O")
        self.assertTrue(duplicate_tf_answer["needs_review"])

        valid_tf_answer = _record(self.answers, "病歷書寫（不同老師）", "1")
        self.assertEqual(valid_tf_answer["answer_key"], "X")
        self.assertFalse(valid_tf_answer["needs_review"])

    def test_non_single_choice_keys_require_review(self):
        answer_keys = {
            ("頭頸部的身體診察", "17"): "ABD",
            ("外科傷口之癒合（不同老師）", "17"): "AC",
            ("燒傷 / 多選題", "1"): "BDE",
            ("燒傷 / 多選題", "2"): "CD",
            ("燒傷 / 多選題", "3"): "CD",
            ("燒傷 / 多選題", "4"): "BCD",
            ("B03 第一次區段考 / 選擇題", "25"): "B/D",
            ("B06 第一次區段考 / 選擇題", "2"): "X",
            ("B06 第一次區段考 / 選擇題", "4"): "X",
            ("B06 第一次區段考 / 選擇題", "8"): "X",
        }

        for (section, number), answer_key in answer_keys.items():
            with self.subTest(section=section, number=number):
                row = _record(self.answers, section, number)
                self.assertEqual(row["answer_key"], answer_key)
                self.assertTrue(row["needs_review"])

    def test_uuid_named_uploads_pass_question_types_through_jobs(self):
        old_data_dir, old_db_path = db.DATA_DIR, db.DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db.DATA_DIR = data_dir.resolve()
            db.DB_PATH = (db.DATA_DIR / "app.sqlite3").resolve()
            try:
                db.init_db()
                course_id = db.create_course("UUID PDF import test")["id"]
                question_path = db.DATA_DIR / "questions-550e8400-e29b-41d4-a716-446655440000.pdf"
                answer_path = db.DATA_DIR / "answers-550e8400-e29b-41d4-a716-446655440001.pdf"
                shutil.copyfile(QUESTION_PDF, question_path)
                shutil.copyfile(ANSWER_PDF, answer_path)
                db.set_asset(course_id, "questions", question_path, question_path.name, "application/pdf", question_path.stat().st_size)
                db.set_asset(course_id, "answers", answer_path, answer_path.name, "application/pdf", answer_path.stat().st_size)

                explicit_path_answers = import_answers(answer_path, question_pdf_path=question_path)
                book_true_false = _record(explicit_path_answers, "病歷書寫（不同老師）", "1")
                self.assertEqual(book_true_false["answer_key"], "X")
                self.assertFalse(book_true_false["needs_review"])
                self.assertEqual(sum(answer["needs_review"] for answer in explicit_path_answers), 17)

                warnings = jobs._import_pdfs(course_id)
                self.assertEqual(len(warnings), 1)
                self.assertIn("21 題", warnings[0])
                imported = db.list_questions(course_id)
                self.assertEqual(len(imported), 275)
                merged_book_true_false = next(
                    row for row in imported
                    if row["section"] == "病歷書寫（不同老師）" and row["number"] == "1"
                )
                self.assertEqual(merged_book_true_false["answer_key"], "X")
                self.assertFalse(merged_book_true_false["needs_review"])
            finally:
                db.DATA_DIR, db.DB_PATH = old_data_dir, old_db_path


if __name__ == "__main__":
    unittest.main()
