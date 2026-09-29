import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
import db
import pbl_api

class PBLTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.patchers=[patch.object(db,'APP_ROOT',root),patch.object(db,'DB_PATH',root/'test.sqlite3'),
            patch.object(db,'list_courses',return_value=[{'id':'c','title':'炎症','subject_id':'pathology'}]),
            patch.object(db,'list_segments',return_value=[{'id':'s','text':'inflammation 發炎','start_ms':120000,'end_ms':130000}]),
            patch.object(pbl_api.openai_settings,'credentials',return_value=('test-key','test-model'))]
        for p in self.patchers:p.start()
        source=root/'web/pbl-materials';source.mkdir(parents=True);(source/'search.json').write_text(json.dumps({'2':'inflammation 發炎'}))
        self.payload={'week':2,'subjects':['pathology']}
        self.raw={'matches':[{'course_id':'c','segment_ids':['s'],'topic':'發炎'}]}
    def tearDown(self):
        for p in reversed(self.patchers):p.stop()
        self.tmp.cleanup()
    def response(self, raw):
        response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(raw)}]}]}).encode();return response
    def test_success_persistence_and_no_credentials(self):
        with patch.object(pbl_api,'urlopen',return_value=self.response(self.raw)) as call:
            result=pbl_api.analyze(self.payload)
        self.assertEqual(result['matches'][0]['excerpts'][0]['start'],120)
        self.assertEqual(pbl_api.saved()['2'],result)
        self.assertNotIn('test-key',json.dumps(pbl_api.saved()))
        body=json.loads(call.call_args.args[0].data)
        self.assertFalse(body['store']);self.assertTrue(body['text']['format']['strict'])
    def test_invalid_evidence_preserves_results(self):
        with patch.object(pbl_api,'urlopen',return_value=self.response(self.raw)):pbl_api.analyze(self.payload)
        old=pbl_api.saved();self.raw['matches'][0]['segment_ids']=['made-up']
        with patch.object(pbl_api,'urlopen',return_value=self.response(self.raw)),self.assertRaises(ValueError):pbl_api.analyze(self.payload)
        self.assertEqual(old,pbl_api.saved())
    def test_missing_key_does_not_call(self):
        with patch.object(pbl_api.openai_settings,'credentials',return_value=('','model')),patch.object(pbl_api,'urlopen') as call,self.assertRaises(ValueError):pbl_api.analyze(self.payload)
        call.assert_not_called()
    def test_invalid_request(self):
        for payload in [{'week':True,'subjects':['pathology']},{'week':17,'subjects':['pathology']},{'week':2,'subjects':[]},{'week':2,'subjects':['unknown']}]:
            with self.assertRaises(ValueError):pbl_api.analyze(payload)
    def test_no_transcript_no_call(self):
        with patch.object(db,'list_segments',return_value=[]),patch.object(pbl_api,'urlopen') as call,self.assertRaises(ValueError):pbl_api.analyze(self.payload)
        call.assert_not_called()
    def test_error_sanitized_and_lock_released(self):
        with patch.object(pbl_api,'urlopen',side_effect=HTTPError('secret',401,'test-key',{},None)),self.assertRaisesRegex(ValueError,'金鑰無效'):pbl_api.analyze(self.payload)
        self.assertFalse(pbl_api._lock.locked())
    def test_empty_result_valid(self):
        with patch.object(pbl_api,'urlopen',return_value=self.response({'matches':[]})):
            self.assertEqual(pbl_api.analyze(self.payload)['matches'],[])
    def test_unknown_course_rejected(self):
        self.raw['matches'][0]['course_id']='invented'
        with patch.object(pbl_api,'urlopen',return_value=self.response(self.raw)),self.assertRaises(ValueError):pbl_api.analyze(self.payload)

if __name__=='__main__':unittest.main()
