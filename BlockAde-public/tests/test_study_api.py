"""End-to-end API contracts for reusable banks and independent study rounds."""
import unittest
from unittest.mock import patch

import db
import jobs
import test_backend


class StudyApiTests(unittest.TestCase):
    setUp = test_backend.BackendApiSmokeTests.setUp
    tearDown = test_backend.BackendApiSmokeTests.tearDown
    request = test_backend.BackendApiSmokeTests.request

    def bank(self, subject='clinical'):
        status, _, bank, _ = self.request('POST', '/api/banks', {'title': '113 年題庫', 'subject_id': subject})
        self.assertEqual(status, 201)
        for kind in ('questions', 'answers'):
            status, _, _, _ = self.request('PUT', f'/api/banks/{bank["id"]}/assets/{kind}', b'%PDF-1.4\nfixture',
                                          {'Content-Type': 'application/pdf', 'X-Filename': kind+'.pdf'})
            self.assertEqual(status, 201)
        question = {'number': '1', 'stem': 'Beta blocker lowers heart rate',
                    'options': [{'key': 'A', 'text': 'yes'}, {'key': 'B', 'text': 'no'}]}
        answer = {'number': '1', 'answer_key': 'A', 'explanation': 'Explanation'}
        with patch('pdf_import.import_questions', return_value=[question]), patch('pdf_import.import_answers', return_value=[answer]):
            status, _, payload, _ = self.request('POST', f'/api/banks/{bank["id"]}/import')
        self.assertEqual(status, 200)
        self.assertEqual(payload['question_count'], 1)
        return bank['id']

    def course(self, subject='clinical'):
        status, _, course, _ = self.request('POST', '/api/courses', {'title':'Lecture', 'subject_id':subject})
        self.assertEqual(status, 201)
        db.add_segments(course['id'], [{'start_ms':0,'end_ms':100000,'text':'Beta blocker lowers heart rate'}])
        db.set_course_status(course['id'], 'ready', 100)
        return course['id']

    def test_shared_banks_rematch_preserves_attempts_and_subject_boundaries(self):
        self.bank()
        course_id = self.course()
        other = self.course('pathology')
        status, _, _, _ = self.request('POST', f'/api/courses/{course_id}/rematch')
        self.assertEqual(status, 200)
        qid = db.list_questions(course_id)[0]['id']
        self.request('POST', f'/api/questions/{qid}/attempt', {'selected_option':'B'})
        self.bank()
        self.request('POST', f'/api/courses/{course_id}/rematch')
        self.assertEqual(len(db.list_questions(course_id)), 2)
        self.assertEqual(len(db.list_attempts(course_id)), 1)
        self.assertEqual(db.list_questions(course_id)[0]['id'], qid)
        status, _, _, _ = self.request('POST', f'/api/courses/{other}/rematch')
        self.assertEqual(status, 409)
        self.assertEqual(db.list_questions(other), [])

    def test_rounds_and_wrong_question_recovery(self):
        self.bank()
        course_id = self.course()
        self.request('POST', f'/api/courses/{course_id}/rematch')
        qid = db.list_questions(course_id)[0]['id']
        self.request('POST', f'/api/questions/{qid}/attempt', {'selected_option':'B'})
        _, _, payload, _ = self.request('GET', '/api/wrong-questions?subject_id=clinical')
        self.assertEqual(len(payload['questions']), 1)
        self.assertNotIn('answer_key', payload['questions'][0])
        status, _, payload, _ = self.request('POST', f'/api/courses/{course_id}/listening', {'start_seconds':0,'end_seconds':90})
        self.assertEqual(status, 200)
        self.assertTrue(payload['learning']['completed'])
        self.request('POST', f'/api/courses/{course_id}/restart')
        self.assertEqual(db.list_attempts(course_id), [])
        _, _, detail, _ = self.request('GET', f'/api/courses/{course_id}')
        self.assertFalse(detail['questions'][0]['answered'])
        self.assertEqual(detail['learning']['completed_rounds'], 1)
        self.request('POST', f'/api/questions/{qid}/attempt', {'selected_option':'A'})
        _, _, payload, _ = self.request('GET', '/api/wrong-questions')
        self.assertEqual(payload['questions'], [])
        _, _, payload, _ = self.request('GET', '/api/wrong-questions?history=1')
        self.assertEqual(len(payload['questions']), 1)
        _, _, history, _ = self.request('GET', f'/api/courses/{course_id}/history')
        self.assertEqual(len(history['attempts']), 2)
        status, _, _, _ = self.request('POST', f'/api/courses/{course_id}/listening', {'start_seconds':0,'end_seconds':float('nan')})
        self.assertEqual(status, 400)

    def test_audio_upload_starts_processing_without_pdfs(self):
        course = db.create_course('Only recording', 'pharmacology')
        with patch('jobs.start_job') as start:
            status, _, payload, _ = self.request('PUT', f'/api/courses/{course["id"]}/assets/audio', b'audio-fixture',
                                                {'Content-Type':'audio/mpeg','X-Filename':'lecture.mp3'})
        self.assertEqual(status, 201)
        start.assert_called_once_with(course['id'])
        self.assertEqual(payload['asset']['filename'], 'lecture.mp3')
        self.assertIsNone(db.get_asset(course['id'], 'questions'))

    def test_replacing_draft_bank_pdf_keeps_legacy_shared_file(self):
        course = db.create_course('Legacy draft')
        bank = db.create_bank('Legacy bank', 'clinical')
        path = db.DATA_DIR / 'legacy.pdf'; path.write_bytes(b'%PDF-1.4\nlegacy')
        db.set_asset(course['id'], 'questions', path, path.name, 'application/pdf', path.stat().st_size)
        db.set_bank_asset(bank['id'], 'questions', path, path.name, 'application/pdf', path.stat().st_size)
        status, _, _, _ = self.request('PUT', f'/api/banks/{bank["id"]}/assets/questions', b'%PDF-1.4\nnew',
                                      {'Content-Type':'application/pdf','X-Filename':'new.pdf'})
        self.assertEqual(status, 201)
        self.assertEqual(path.read_bytes(), b'%PDF-1.4\nlegacy')
        self.assertNotEqual(db.get_bank_asset(bank['id'], 'questions')['path'], str(path))

    def test_audio_only_job_completes_with_progress(self):
        course = db.create_course('Only recording')
        path = db.DATA_DIR / 'audio.mp3'; path.write_bytes(b'fixture')
        db.set_asset(course['id'], 'audio', path, path.name, 'audio/mpeg', 7)
        job_id = db.create_job(course['id'])
        def transcribe(path, work_dir, progress_callback, chunk_minutes, chunk_callback=None):
            self.assertEqual(chunk_minutes, 5)
            progress_callback(.5, 'test progress')
            self.assertIn('50%', db.latest_job(course['id'])['message'])
            return {'segments':[{'start_ms':0,'end_ms':10000,'text':'測試內容'}], 'duration_seconds':10}
        with patch('transcribe.transcribe_audio', side_effect=transcribe):
            jobs._run_job(job_id, course['id'])
        self.assertEqual(db.latest_job(course['id'])['status'], 'completed')
        self.assertEqual(len(db.list_segments(course['id'])), 1)

    def test_transcription_chunk_settings_validate_persist_and_lock(self):
        status, _, course, _ = self.request('POST', '/api/courses',
            {'title': '分段課程', 'transcription_chunk_minutes': 10})
        self.assertEqual(status, 201)
        self.assertEqual(course['transcription_chunk_minutes'], 10)
        _, _, detail, _ = self.request('GET', f"/api/courses/{course['id']}")
        self.assertEqual(detail['transcription_chunk_minutes'], 10)
        url = f"/api/courses/{course['id']}/transcription-settings"
        for invalid in (0, 15, '5', True, None, 5.0):
            status, _, _, _ = self.request('PATCH', url, {'transcription_chunk_minutes': invalid})
            self.assertEqual(status, 400)
            self.assertEqual(db.get_course(course['id'])['transcription_chunk_minutes'], 10)
        status, _, saved, _ = self.request('PATCH', url, {'transcription_chunk_minutes': 5})
        self.assertEqual(status, 200)
        self.assertEqual(saved['transcription_chunk_minutes'], 5)
        db.create_job(course['id'])
        status, _, _, _ = self.request('PATCH', url, {'transcription_chunk_minutes': 10})
        self.assertEqual(status, 409)
        self.assertEqual(db.get_course(course['id'])['transcription_chunk_minutes'], 5)
        status, _, _, _ = self.request('POST', '/api/courses',
            {'title': '無效分段', 'transcription_chunk_minutes': 20})
        self.assertEqual(status, 400)

    def test_failed_retranscription_keeps_existing_transcript_and_attempts(self):
        course = db.create_course('Existing recording', transcription_chunk_minutes=10)
        cid = course['id']
        old_ids = db.add_segments(cid, [{'start_ms':0, 'end_ms':1000, 'text':'原始逐字稿'}], source='whisper')
        path = db.DATA_DIR / 'existing.mp3'; path.write_bytes(b'audio')
        db.set_asset(cid, 'audio', path, path.name, 'audio/mpeg', 5)
        qid = db.add_question(cid, {'number':'1', 'stem':'測試題', 'options':[{'key':'A','text':'答案'}], 'answer_key':'A'})
        db.add_attempt(qid, 'A', True)
        job_id = db.create_job(cid)
        with patch('transcribe.transcribe_audio', side_effect=RuntimeError('第 2 段轉錄失敗')) as recognize:
            jobs._run_job(job_id, cid)
        self.assertEqual(recognize.call_args.kwargs['chunk_minutes'], 10)
        self.assertEqual(db.latest_job(cid)['status'], 'error')
        self.assertEqual([s['id'] for s in db.list_segments(cid)], old_ids)
        self.assertEqual(len(db.list_attempts(cid)), 1)

    def test_api_exposes_completed_chunk_preview_before_job_finishes(self):
        cid = db.create_course('逐段預覽')['id']
        jid = db.create_job(cid)
        db.update_job(jid, 'running', 'transcription', .3, '第 2/3 段辨識中')
        db.append_transcription_preview_chunk(jid, [{'start_ms':0, 'end_ms':3000, 'text':'先顯示第一段'}], 1, 3)
        status, _, detail, _ = self.request('GET', f'/api/courses/{cid}')
        self.assertEqual(status, 200)
        self.assertEqual(detail['segments'], [])
        self.assertEqual(detail['transcription_preview']['segments'][0]['text'], '先顯示第一段')
        _, _, result, _ = self.request('GET', f'/api/courses/{cid}/job')
        self.assertEqual(result['job']['preview_revision'], 1)
        db.update_job(jid, 'completed', 'done', 1, '已完成')
        _, _, detail, _ = self.request('GET', f'/api/courses/{cid}')
        self.assertIsNone(detail['transcription_preview'])
