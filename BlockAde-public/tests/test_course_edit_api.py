"""API validation and persistence for course naming and range transcription."""
import unittest
from unittest.mock import patch

import db
import jobs
import test_backend


class CourseEditApiTests(unittest.TestCase):
    setUp = test_backend.BackendApiSmokeTests.setUp
    tearDown = test_backend.BackendApiSmokeTests.tearDown
    request = test_backend.BackendApiSmokeTests.request

    def test_rename_preserves_course_content_and_progress(self):
        course = db.create_course('Original')
        cid = course['id']
        db.add_segments(cid, [{'start_ms':0, 'end_ms':1000, 'text':'原文'}])
        db.update_playback(cid, 23, 1.7)
        status, _, result, _ = self.request('PATCH', f'/api/courses/{cid}', {'title':'  新課程名稱  '})
        self.assertEqual(status, 200)
        self.assertEqual(result['title'], '新課程名稱')
        self.assertEqual(result['playback_position_seconds'], 23)
        self.assertEqual(result['current_round_id'], course['current_round_id'])
        self.assertEqual(len(db.list_segments(cid)), 1)
        self.assertEqual(db.list_courses()[0]['title'], '新課程名稱')
        for invalid in ('', '  ', 'a'*201, None, 123):
            status, _, _, _ = self.request('PATCH', f'/api/courses/{cid}', {'title':invalid})
            self.assertEqual(status, 400)
        self.assertEqual(db.get_course(cid)['title'], '新課程名稱')

    def test_range_route_requires_finite_numbers_and_forwards_interval(self):
        cid = db.create_course('Course')['id']
        url = f'/api/courses/{cid}/retranscribe-range'
        for start, end in ((True, 30), ('0', 30), (None, 30), (0, float('inf')), (float('nan'), 3)):
            status, _, _, _ = self.request('POST', url, {'start_seconds':start, 'end_seconds':end})
            self.assertEqual(status, 400)
        with patch('jobs.start_range_job', return_value='job') as start_job:
            status, _, result, _ = self.request('POST', url, {'start_seconds':1566, 'end_seconds':1800})
            self.assertEqual(status, 202)
            start_job.assert_called_once_with(cid, 1566, 1800)
        with patch('jobs.start_range_job', side_effect=jobs.JobBusyError('已有工作')):
            self.assertEqual(self.request('POST', url, {'start_seconds':0, 'end_seconds':30})[0], 409)
        with patch('jobs.start_range_job', side_effect=ValueError('結束時間超出錄音')):
            self.assertEqual(self.request('POST', url, {'start_seconds':0, 'end_seconds':30})[0], 400)
