import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import db
import transcribe
import transcription_quality as q
import test_backend


class QualityTests(unittest.TestCase):
    def test_flags_are_review_evidence_not_rewrites(self):
        segments = [{'start_ms': 12000, 'end_ms': 13000, 'text': '嗯，嗯。'},
                    {'start_ms': 14000, 'end_ms': 15000, 'text': '嗯，嗯。'}]
        before = json.dumps(segments)
        raw = [{'audio_start_ms': 0, 'payload': {'transcription': [
            {'offsets': {'from': 12000, 'to': 13000}, 'tokens': [
                {'text': '[_BEG_]', 'p': .001}, {'text': '嗯', 'p': .9}]}]}}]
        flags = q.review_flags(segments, raw, 30)
        self.assertEqual({f['reason'] for f in flags}, {'gap', 'repeat'})
        self.assertEqual(before, json.dumps(segments))
        raw[0]['payload']['transcription'][0]['tokens'][1]['p'] = .1
        self.assertIn('low_confidence', {f['reason'] for f in q.review_flags(segments, raw, 30)})

    def test_windows_are_bounded_and_disabled_means_no_second_pass(self):
        flags = [{'start_ms': i * 60000, 'end_ms': i * 60000 + 1000, 'reason': 'low_confidence'} for i in range(8)]
        windows = q.review_windows(flags, 480, 3)
        self.assertEqual(len(windows), 3)
        self.assertTrue(all(0 <= w['start_ms'] < w['end_ms'] <= 480000 and w['end_ms'] - w['start_ms'] <= 60000 for w in windows))
        self.assertEqual(q.review_windows(flags, 480, 0), [])
        self.assertEqual(q.review_windows([], 2)[0]['end_ms'], 2000)

    def test_export_preserves_silence_and_absolute_time(self):
        segments = [{'start_ms': 194000, 'end_ms': 195230, 'text': '原話'}]
        self.assertIn('00:33:14.000 --> 00:33:15.230', q.subtitle(segments, 'vtt', 1800000))
        self.assertIn('00:33:14,000 --> 00:33:15,230', q.subtitle(segments, 'srt', 1800000))
        self.assertEqual(q.subtitle(segments, 'txt'), '原話\n')

    def test_second_pass_does_not_overwrite_first_and_failure_keeps_primary(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'recording.wav'; source.touch()
                first = {'segments': [{'start_ms': 0, 'end_ms': 1000, 'text': '原話'}],
                         'duration_seconds': 30, 'model': 'large-v3.bin',
                         'raw_outputs': [{'audio_start_ms': 0, 'payload': {'transcription': []}}]}
                second = {'segments': [{'text': '另一個結果'}], 'raw_outputs': []}
                def extract(command, **kwargs):
                    Path(command[-1]).touch()
                    return subprocess.CompletedProcess(command, 0, '', '')
                with (mock.patch.object(db, 'DATA_DIR', root / 'data'),
                      mock.patch.dict(os.environ, {'WHISPER_REVIEW_LIMIT': '1'}),
                      mock.patch.object(transcribe, '_transcribe_audio_impl', return_value=first),
                      mock.patch.object(transcribe, '_tool', return_value='ffmpeg'),
                      mock.patch.object(transcribe, '_run_streaming', side_effect=extract),
                      mock.patch.object(transcribe, '_transcribe_audio_single', side_effect=RuntimeError('failed') if failed else None, return_value=second)):
                    result = transcribe.transcribe_audio(source)
                self.assertEqual(result['segments'][0]['text'], '原話')
                report = json.loads(Path(result['raw_archive']).read_text())['quality']
                self.assertEqual(report['reviews'][0]['status'], 'failed' if failed else 'compared')


class QualityApiTests(unittest.TestCase):
    setUp = test_backend.BackendApiSmokeTests.setUp
    tearDown = test_backend.BackendApiSmokeTests.tearDown
    request = test_backend.BackendApiSmokeTests.request

    def test_report_and_exports_are_course_scoped_and_offset_correct(self):
        course = db.create_course('Test')['id']
        other = db.create_course('Other')['id']
        folder = db.DATA_DIR / 'transcription_raw'; folder.mkdir()
        record = {'course_id': course, 'created_at_unix': 1, 'source_offset_ms': 1800000,
                  'source': '/private/recording', 'model': 'large-v3.bin',
                  'segments': [{'start_ms': 194000, 'end_ms': 195000, 'text': '軟件'}],
                  'quality': {'flags': [], 'reviews': [], 'review_limit': 3},
                  'raw_outputs': [{'audio_start_ms': 0, 'payload': {'transcription': [
                      {'offsets': {'from': 194000, 'to': 195000}, 'text': '软件'}]}}]}
        (folder / 'record.json').write_text(json.dumps(record))
        self.assertTrue(self.request('GET', f'/api/courses/{course}/transcription-review')[2]['available'])
        self.assertFalse(self.request('GET', f'/api/courses/{other}/transcription-review')[2]['available'])
        result = self.request('GET', f'/api/courses/{course}/transcription-review?format=vtt')[2]
        self.assertIn('00:33:14.000', result['text'])
        self.assertIn('软件', result['text'])
        self.assertEqual(self.request('GET', f'/api/courses/{course}/transcription-review?format=bad')[0], 400)
