"""Lecture handout storage and API behavior."""

import unittest

import fitz

import db
import handouts
import test_backend


class HandoutApiTests(unittest.TestCase):
    setUp = test_backend.BackendApiSmokeTests.setUp
    tearDown = test_backend.BackendApiSmokeTests.tearDown
    request = test_backend.BackendApiSmokeTests.request

    @staticmethod
    def pdf(*pages: str) -> bytes:
        doc = fitz.open()
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_text((50, 50), text)
        payload = doc.tobytes()
        doc.close()
        return payload

    def upload(self, course_id: str, payload: bytes, name: str = "lecture.pdf", method: str = "PUT"):
        return self.request(method, f"/api/courses/{course_id}/handouts", payload, {
            "X-Filename": name, "Content-Type": "application/pdf"
        })

    def test_multiple_handouts_are_listed_and_page_context_retains_provenance(self):
        course_id = db.create_course("Lecture")["id"]
        first = self.upload(course_id, self.pdf("Cardiology\nBeta blocker", "Arrhythmia\nAtrial fibrillation"))[2]
        self.assertEqual(first["handout"]["page_count"], 2)
        second = self.upload(course_id, self.pdf("Neurology\nStroke"), name="second.pdf", method="POST")[2]
        self.assertEqual(second["handout"]["page_count"], 1)
        listed = self.request("GET", f"/api/courses/{course_id}/handouts")[2]["handouts"]
        self.assertEqual(len(listed), 2)
        self.assertEqual(db.course_payload(course_id)["handouts"], listed)
        context = handouts.course_context(course_id)
        self.assertEqual(len(context), 3)
        self.assertEqual(context[0]["page"], 1)
        self.assertEqual(context[1]["section"], "Arrhythmia")
        self.assertEqual(context[2]["filename"], "second.pdf")
        self.assertEqual(context[2]["id"], f"handout-{second['handout']['id']}-p1")
        self.assertLessEqual(sum(len(page["text"]) for page in handouts.course_context(course_id, max_chars=20)), 20)
        self.assertEqual(self.request("DELETE", f"/api/courses/{course_id}/handouts/{first['handout']['id']}")[0], 200)
        self.assertEqual(len(handouts.course_context(course_id)), 1)
        self.assertEqual(len(db.list_handouts(course_id)), 1)

    def test_invalid_or_scanned_pdf_does_not_leave_file_or_row(self):
        course_id = db.create_course("Lecture")["id"]
        for payload, expected in ((b"not a pdf", 415), (self.pdf(""), 422)):
            status, _, result, _ = self.upload(course_id, payload)
            self.assertEqual(status, expected)
            self.assertIn("error", result)
        self.assertEqual(db.list_handouts(course_id), [])
        folder = db.DATA_DIR / course_id / "handouts"
        self.assertEqual(list(folder.iterdir()), [])

    def test_upload_and_delete_blocked_while_course_job_is_active(self):
        course_id = db.create_course("Lecture")["id"]
        uploaded = self.upload(course_id, self.pdf("Cardiology\nBeta blocker"))[2]["handout"]
        job_id = db.create_job(course_id, "match_analysis")
        self.assertEqual(self.upload(course_id, self.pdf("Neurology\nStroke"))[0], 409)
        self.assertEqual(self.request("DELETE", f"/api/courses/{course_id}/handouts/{uploaded['id']}")[0], 409)
        db.update_job(job_id, "done", "complete", 1, "完成")
        self.assertEqual(self.request("DELETE", f"/api/courses/{course_id}/handouts/{uploaded['id']}")[0], 200)

    def test_context_change_reopens_only_uncertain_analysis(self):
        course_id = db.create_course("Lecture")["id"]
        uncertain = db.add_question(course_id, {"stem": "What is a beta blocker?"})
        confirmed = db.add_question(course_id, {"stem": "What is aspirin?"})
        manual = db.add_question(course_id, {"stem": "What is warfarin?"})
        db.save_match(uncertain, {"provider": "openai", "status": "unmatched"})
        db.save_match(confirmed, {"provider": "openai", "status": "confirmed"})
        db.save_match(manual, {"provider": "manual", "status": "confirmed"})
        self.upload(course_id, self.pdf("Cardiology\nBeta blocker"))
        self.assertIsNone(db.get_match(uncertain))
        self.assertEqual(db.get_match(confirmed)["status"], "confirmed")
        self.assertEqual(db.get_match(manual)["provider"], "manual")

    def test_explicit_chapter_heading_takes_priority_over_repeated_title(self):
        course_id = db.create_course("Lecture")["id"]
        self.upload(course_id, self.pdf("Teacher Slides\nChapter 2 Cardiology\nBeta blocker"))
        self.assertEqual(handouts.course_context(course_id)[0]["section"], "Chapter 2 Cardiology")


if __name__ == "__main__":
    unittest.main()
