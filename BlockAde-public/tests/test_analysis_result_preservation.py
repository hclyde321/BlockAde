import unittest
from unittest.mock import patch

import match


class AnalysisPreservationTests(unittest.TestCase):
    def test_local_rematch_preserves_completed_openai_results(self):
        for status in ("unmatched", "pending_confirmation", "confirmed"):
            with self.subTest(status=status), \
                 patch.object(match.db, "list_segments", return_value=[]), \
                 patch.object(match.db, "get_course", return_value={}), \
                 patch.object(match.db, "list_questions", return_value=[{"id": "q"}]), \
                 patch.object(match.db, "get_match", return_value={"provider": "openai", "status": status}), \
                 patch.object(match.db, "save_match") as save:
                match.refresh_course_matches("course")
                save.assert_not_called()

    def test_local_rematch_preserves_codex_results(self):
        for status in ("unmatched", "pending_confirmation", "confirmed"):
            with self.subTest(status=status), \
                 patch.object(match.db, "list_segments", return_value=[]), \
                 patch.object(match.db, "get_course", return_value={}), \
                 patch.object(match.db, "list_questions", return_value=[{"id": "q"}]), \
                 patch.object(match.db, "get_match", return_value={"provider": "codex", "status": status}), \
                 patch.object(match.db, "save_match") as save:
                match.refresh_course_matches("course")
                save.assert_not_called()

    def test_new_transcript_can_invalidate_prior_negative_result(self):
        with patch.object(match.db, "list_segments", return_value=[]), \
             patch.object(match.db, "get_course", return_value={}), \
             patch.object(match.db, "list_questions", return_value=[{"id": "q"}]), \
             patch.object(match.db, "get_match", return_value={"provider": "openai", "status": "unmatched"}), \
             patch.object(match.db, "save_match") as save:
            match.refresh_course_matches("course", preserve_analysis=False)
            save.assert_called_once()
            self.assertEqual(save.call_args.args[1]["provider"], "local_text")
