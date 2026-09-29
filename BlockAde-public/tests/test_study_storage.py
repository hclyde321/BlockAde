"""Storage regressions for subjects, reusable banks, and learning rounds."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import db


class StudyStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_data_dir, self.old_db_path = db.DATA_DIR, db.DB_PATH
        db.DATA_DIR = Path(self.temp.name).resolve()
        db.DB_PATH = db.DATA_DIR / "test.sqlite3"
        db.init_db()

    def tearDown(self) -> None:
        db.DATA_DIR, db.DB_PATH = self.old_data_dir, self.old_db_path
        self.temp.cleanup()

    def test_subject_banks_sync_preserves_course_question_state(self) -> None:
        self.assertEqual([row["id"] for row in db.list_subjects()],
                         ["pathology", "pharmacology", "clinical", "laboratory"])
        course = db.create_course("Path 1", "pathology")
        other = db.create_course("Clinical 1")
        bank1 = db.create_bank("2024 exam", "pathology")
        bank2 = db.create_bank("2025 exam", "pathology")
        pdf = db.DATA_DIR / "questions.pdf"
        pdf.write_bytes(b"%PDF")
        db.set_bank_asset(bank1["id"], "questions", pdf, "questions.pdf", "application/pdf", 4)
        ids1 = db.replace_bank_questions(bank1["id"], [{"number": "1", "stem": "Which?", "options": ["A", "B"],
                                                         "answer_key": "A"}])
        ids2 = db.replace_bank_questions(bank2["id"], [{"number": "1", "stem": "Another?", "options": ["C", "D"]}])
        self.assertEqual(len(db.list_banks("pathology")), 2)
        self.assertEqual(db.get_bank(bank1["id"])["assets"]["questions"]["filename"], "questions.pdf")
        self.assertEqual(db.list_bank_questions(bank1["id"])[0]["id"], ids1[0])
        self.assertEqual(db.sync_subject_questions(course["id"]), 2)
        self.assertEqual(db.sync_subject_questions(other["id"]), 0)
        questions = db.list_questions(course["id"])
        self.assertEqual({q["bank_question_id"] for q in questions}, {ids1[0], ids2[0]})
        self.assertEqual({q["bank_title"] for q in questions}, {"2024 exam", "2025 exam"})
        self.assertEqual(db.course_payload(course["id"])["questions"][0]["bank_id"], bank1["id"])
        db.add_attempt(questions[0]["id"], "B", False)
        self.assertEqual(db.wrong_questions(course_id=course["id"])[0]["bank_title"], "2024 exam")
        db.save_match(questions[0]["id"], {"status": "pending_confirmation"})
        self.assertEqual(db.sync_subject_questions(course["id"]), 0)
        self.assertEqual(db.list_questions(course["id"])[0]["id"], questions[0]["id"])
        self.assertIsNotNone(db.latest_attempt(questions[0]["id"]))
        self.assertIsNotNone(db.get_match(questions[0]["id"]))
        with self.assertRaises(ValueError):
            db.replace_bank_questions(bank1["id"], [{"stem": "Changed"}])
        with self.assertRaises(ValueError):
            db.set_bank_asset(bank1["id"], "questions", pdf, "changed.pdf", "application/pdf", 4)

    def test_rounds_listening_union_and_wrong_questions(self) -> None:
        course = db.create_course("Clinical 1")
        db.set_course_status(course["id"], "ready", 100)
        question_id = db.add_question(course["id"], {"number": "1", "stem": "Question", "options": ["A", "B"]})
        self.assertIsNone(db.list_questions(course["id"])[0]["bank_id"])
        round1 = db.learning_summary(course["id"])["round_id"]
        db.record_listening(course["id"], 0, 50, round1)
        db.record_listening(course["id"], 25, 90, round1)
        db.add_attempt(question_id, "B", False)
        summary = db.learning_summary(course["id"])
        self.assertEqual(summary["listened_seconds"], 90)
        self.assertEqual(summary["progress"], 0.9)
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["completed_rounds"], 1)
        self.assertEqual(len(db.wrong_questions(course_id=course["id"])), 1)
        with self.assertRaises(ValueError):
            db.record_listening(course["id"], 90, 110)
        with self.assertRaises(ValueError):
            db.record_listening(course["id"], 50, 50)
        next_round = db.restart_learning(course["id"])
        self.assertEqual(next_round["round_number"], 2)
        self.assertEqual(next_round["listened_seconds"], 0)
        self.assertEqual(next_round["completed_rounds"], 1)
        self.assertEqual(db.list_attempts(course["id"]), [])
        self.assertIsNone(db.latest_attempt(question_id))
        self.assertEqual(len(db.list_attempts(course["id"], history=True)), 1)
        self.assertEqual(len(db.wrong_questions(course_id=course["id"])), 1)
        self.assertEqual(len(db.wrong_questions(course_id=course["id"], history=True)), 1)
        with self.assertRaises(ValueError):
            db.record_listening(course["id"], 0, 1, round1)
        db.add_attempt(question_id, "A", True)
        self.assertEqual(db.learning_history(course["id"])["rounds"][0]["round_number"], 1)
        self.assertEqual(db.wrong_questions(course_id=course["id"]), [])
        self.assertEqual(len(db.wrong_questions(course_id=course["id"], history=True)), 1)

    def test_audio_only_round_completes_at_ninety_percent(self) -> None:
        course = db.create_course("Audio only", "laboratory")
        db.set_course_status(course["id"], "ready", 10)
        summary = db.record_listening(course["id"], 0, 9)
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["completed_rounds"], 1)
        self.assertFalse(summary["quiz_complete"])

    def test_legacy_pdf_migration_keeps_question_attempt_and_asset(self) -> None:
        course = db.create_course("Old lecture")
        question_id = db.add_question(course["id"], {"number": "5", "stem": "Old PDF question", "options": ["A"]})
        attempt = db.add_attempt(question_id, "A", True)
        db.save_match(question_id, {"status": "confirmed"})
        pdf = db.DATA_DIR / "old.pdf"
        pdf.write_bytes(b"%PDF")
        db.set_asset(course["id"], "questions", pdf, "old.pdf", "application/pdf", 4)
        with db.connect() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE name='legacy_banks_v1'")
        db.init_db()
        bank = db.list_banks()[0]
        self.assertEqual(bank["title"], "Old lecture 題庫")
        self.assertEqual(db.get_bank_asset(bank["id"], "questions")["path"], str(pdf))
        self.assertEqual(db.list_questions(course["id"])[0]["id"], question_id)
        self.assertEqual(db.latest_attempt(question_id)["id"], attempt["id"])
        self.assertIsNotNone(db.get_match(question_id))
        bank_question = db.list_bank_questions(bank["id"])[0]
        self.assertEqual(db.list_questions(course["id"])[0]["bank_question_id"], bank_question["id"])
        db.init_db()
        self.assertEqual(len(db.list_banks()), 1)

    def test_legacy_unimported_pdf_migrates_to_draft_bank(self) -> None:
        course = db.create_course("Pending PDF")
        pdf = db.DATA_DIR / "pending.pdf"
        pdf.write_bytes(b"%PDF")
        db.set_asset(course["id"], "questions", pdf, "pending.pdf", "application/pdf", 4)
        with db.connect() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE name='legacy_banks_v1'")
        db.init_db()
        bank = db.list_banks()[0]
        self.assertEqual(bank["status"], "draft")
        self.assertEqual(bank["legacy_course_id"], course["id"])
        self.assertEqual(db.get_bank_asset(bank["id"], "questions")["path"], str(pdf))


if __name__ == "__main__":
    unittest.main()
