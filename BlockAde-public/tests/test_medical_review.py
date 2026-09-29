import json
import math
import unittest
import tempfile
from pathlib import Path
from unittest import mock

import db
import medical_review as m
import jobs
import test_backend
from local_review_worker import merge_english_subwords, retain_reviewed_candidates, parse_vocabulary_output, structured_response


class MedicalReviewTests(unittest.TestCase):
    def test_malformed_json_regenerates_without_salvaging_candidates(self):
        generate = mock.Mock(side_effect=['{"suggestions":[{"replacement":"bad"}',
                                         '```json\n{"suggestions":[]}\n```'])
        result, failures = structured_response(generate)
        self.assertEqual(result, {"suggestions": []})
        self.assertEqual(len(failures), 1)
        self.assertIn('bad', failures[0]['output'])
        self.assertEqual(generate.call_args_list, [mock.call(0), mock.call(1)])
        with self.assertRaises(ValueError):
            structured_response(lambda attempt: '[]')

    setUp = test_backend.BackendApiSmokeTests.setUp
    tearDown = test_backend.BackendApiSmokeTests.tearDown
    request = test_backend.BackendApiSmokeTests.request

    def fixture(self):
        course = db.create_course('藥理學')['id']
        sid = db.add_segments(course, [{'start_ms': 1000, 'end_ms': 3000, 'text': '嗯，anti gonist。'}], source='whisper')[0]
        return course, db.get_segment(sid)

    def test_english_subword_alignment_preserves_text_and_untimed_words(self):
        words=[{'word':' 「C','start_ms':100,'end_ms':100},
               {'word':'hol','start_ms':100,'end_ms':200},
               {'word':'inergic','start_ms':200,'end_ms':400},
               {'word':' Biology','start_ms':400,'end_ms':600},
               {'word':'」','start_ms':600,'end_ms':600}]
        merged=merge_english_subwords(words)
        self.assertEqual([w['word'] for w in merged],[' 「Cholinergic',' Biology」'])
        self.assertEqual(merged[0]['start_ms'],100)
        self.assertEqual(merged[0]['end_ms'],400)
        self.assertEqual(''.join(w['word'] for w in merged),''.join(w['word'] for w in words))
        untimed=[{'word':' missing','start_ms':400,'end_ms':400},{'word':' next','start_ms':400,'end_ms':500}]
        self.assertEqual(merge_english_subwords(untimed),untimed)
        backwards=[{'word':'ab','start_ms':200,'end_ms':300},{'word':'cd','start_ms':100,'end_ms':200}]
        self.assertEqual(merge_english_subwords(backwards),backwards)

    def proposal(self, segment):
        return {'suggestions':[{'segment_id':segment['id'], 'original':'anti gonist',
                                'replacement':'antagonist', 'reason':'疑似拮抗劑術語，請重聽。'}]}

    def test_only_exact_bounded_replacements_and_apply_keeps_audit(self):
        course, s = self.fixture()
        proposals = m.validate_suggestions(self.proposal(s), [s])
        sid, before, after, reason = proposals[0]
        with db.connect() as conn:
            rid = m.add_revision(conn, course, sid, 'suggest', before, after, reason)
        self.assertEqual(db.get_segment(sid)['text'], s['text'])
        other = db.create_course('Other')['id']
        self.assertEqual(self.request('PATCH', f'/api/courses/{other}/medical-review/{rid}', {'action':'apply'})[0],409)
        self.assertEqual(self.request('PATCH', f'/api/courses/{course}/medical-review/{rid}', {'action':'apply'})[0],200)
        self.assertEqual(db.get_segment(sid)['text'], '嗯，antagonist。')
        self.assertEqual(db.get_segment(sid)['original_text'], s['text'])
        self.assertTrue(db.course_payload(course)['segments'][0]['inferred'])
        self.assertEqual(m.history(course)[0]['before']['text'], s['text'])
        self.assertEqual(m.history(course)[0]['status'],'applied')
        with self.assertRaises(ValueError): m.decide(course,rid,'apply')

    def test_stale_suggestion_does_not_overwrite_manual_edit(self):
        course, s = self.fixture()
        sid, before, after, reason = m.validate_suggestions(self.proposal(s),[s])[0]
        with db.connect() as conn: rid = m.add_revision(conn,course,sid,'suggest',before,after,reason)
        db.patch_segment(sid,{'text':'人工已校正'})
        with self.assertRaises(ValueError): m.decide(course,rid,'apply')
        self.assertEqual(db.get_segment(sid)['text'],'人工已校正')
        self.assertEqual(len(m.history(course)),2)

    def test_invalid_model_output_cannot_inject_another_segment_or_rewrite(self):
        _, s = self.fixture()
        for original, replacement, sid in [('不存在','詞',s['id']),('anti gonist','x'*1000,s['id']),('anti gonist','詞','foreign')]:
            payload={'suggestions':[dict(segment_id=sid, original=original,replacement=replacement,reason='reason')]}
            self.assertEqual(m.validate_suggestions(payload,[s]),[])
        with self.assertRaises(ValueError): m.validate_suggestions({},[s])

    def test_numbers_negation_and_sentence_rewrites_are_not_term_suggestions(self):
        _,s=self.fixture()
        for text,original,replacement in [('劑量是 10 mg','10','20'), ('不是 antagonist','不是','是'),
                                          ('第一句。第二句。','第一句。第二句。','第三句。')]:
            segment={**s,'text':text}
            payload={'suggestions':[dict(segment_id=s['id'],original=original,replacement=replacement,reason='context')]}
            self.assertEqual(m.validate_suggestions(payload,[segment]),[])

    def test_real_recording_translation_rewrite_and_cosmetic_edits_rejected(self):
        for original, replacement in [
            ('neurotransmitter release', '神經傳導物質釋放'),
            ('水解變成Acetylcholine跟Choline', '水解成為Acetylcholine與Choline'),
            ('neurexin c-texin', 'neurexin C-texin'),
            ('Nutrients need', 'Nutrients needed'),
            ('漏毒桿去讀書', '毒蛇毒去讀書，因為中毒'),
        ]:
            self.assertFalse(m.term_edit_allowed(original, replacement))
        self.assertTrue(m.term_edit_allowed('Hemicholinine', 'Hemicholinium'))
        self.assertTrue(m.term_edit_allowed('肉乳桿菌毒素', '肉毒桿菌毒素'))

    def test_same_misheard_term_cannot_change_to_conflicting_terms(self):
        _, s = self.fixture()
        s = {**s, 'text': 'Vasco'}
        payload = {'suggestions': [dict(segment_id=s['id'], original='Vasco',
            replacement='Vasculature', reason='推定')]}
        self.assertEqual(m.validate_suggestions(payload, [s], {'vasco': 'vesicle'}), [])

    def test_second_review_cannot_add_terms_or_accept_boolean_ids(self):
        candidates = [dict(candidate_id=0, original='abc', replacement='abcd'),
                      dict(candidate_id=1, original='def', replacement='defg')]
        result = retain_reviewed_candidates(candidates, {'accepted_ids':[True, 0, 9, '1']})
        self.assertEqual(result, {'suggestions':[dict(original='abc',replacement='abcd')]})
        with self.assertRaises(ValueError): retain_reviewed_candidates(candidates, {})

    def test_audio_agreement_must_be_near_the_actual_segment(self):
        segment = {'start_ms': 10000, 'end_ms': 12000}
        evidence = {'segments': [{'text':'motor nerve', 'start_ms':10000, 'end_ms':12000},
                                 {'text':'vesicle', 'start_ms':40000, 'end_ms':45000}]}
        self.assertTrue(m.evidence_for_term(evidence, 'Motor Nerve', segment))
        self.assertFalse(m.evidence_for_term(evidence, 'vesicle', segment))
        self.assertFalse(m.evidence_for_term({'error':'failed'}, 'vesicle', segment))
        self.assertFalse(m.evidence_for_term({'segments':[
            {'text':'antagonist','start_ms':10000,'end_ms':12000}]}, 'agonist', segment))

    def test_phonetic_chinese_to_english_requires_nearby_second_asr(self):
        _, s = self.fixture()
        s={**s,'text':'被受痛'}
        proposal={'suggestions':[dict(segment_id=s['id'],original='被受痛',replacement='basal tone',reason='音似推定')]}
        self.assertEqual(m.validate_suggestions(proposal,[s]), [])
        evidence={'segments':[dict(text='basal tone',start_ms=s['start_ms'],end_ms=s['end_ms'])]}
        self.assertEqual(len(m.validate_suggestions(proposal,[s],audio_evidence=evidence)),1)
        self.assertFalse(m.term_edit_allowed('neurotransmitter release','神經傳導物質釋放',phonetic_restore=True))

    def test_failed_audio_recheck_is_visible_and_does_not_edit_original(self):
        course, s = self.fixture()
        job = db.create_job(course, kind='medical_suggest')
        def worker(mode, payload, **kwargs):
            if mode == 'vocabulary': return {'terms':[]}
            if mode == 'listen': raise RuntimeError('mock decoding failure')
            self.assertIn('error', payload['audio_evidence'])
            return self.proposal(s)
        with mock.patch.object(m, 'worker', side_effect=worker):
            m._run(job, course, 'suggest', [s], '', 'fixture.wav')
        self.assertEqual(db.get_segment(s['id'])['text'], s['text'])
        self.assertIn('error', m.history(course)[0]['after']['audio_evidence'])
        self.assertFalse(m.history(course)[0]['after']['second_asr_agrees'])

    def test_inferred_glossary_is_bounded_and_not_labeled_as_handout(self):
        terms=m.inferred_vocabulary({'terms':['synapse','Synapse',None,'a whole sentence, not a term','motor nerve']})
        self.assertEqual([r['term'] for r in terms], ['synapse','motor nerve'])
        self.assertTrue(all('推定' in r['source'] for r in terms))
        with self.assertRaises(ValueError):m.inferred_vocabulary({'suggestions':[]})

    def test_truncated_glossary_keeps_only_complete_strings(self):
        self.assertEqual(parse_vocabulary_output('{"terms":["motor nerve","incompl'),
                         {'terms':['motor nerve'],'recovered_complete_terms':True})
        self.assertEqual(parse_vocabulary_output('```json\n{"terms":["vesicle"]}\n```'), {'terms':['vesicle']})
        with self.assertRaises(ValueError):parse_vocabulary_output('not JSON')

    def test_multiple_disjoint_terms_form_one_atomic_revision(self):
        course,s=self.fixture()
        s={**s,'text':'Mortimer 跟 Skeleton Muscle'}
        items=[dict(segment_id=s['id'],original=a,replacement=b,reason='推定') for a,b in
               [('Mortimer','Motor nerve'),('Skeleton Muscle','Skeletal muscle')]]
        result=m.validate_suggestions({'suggestions':items},[s])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0][2]['text'],'Motor nerve 跟 Skeletal muscle')
        self.assertEqual(len(result[0][2]['term_edits']),2)
        overlap=items+[dict(segment_id=s['id'],original='Skeleton',replacement='Skeletal',reason='推定')]
        self.assertEqual(m.validate_suggestions({'suggestions':overlap},[s])[0][2],result[0][2])

    def test_time_batches_and_handout_spelling_references(self):
        course, s = self.fixture()
        segments = [{**s, 'start_ms': i*10000} for i in range(20)]
        batches = list(m.review_batches(segments))
        self.assertEqual([len(b) for b in batches], [6,6,6,2])
        db.add_handout(course, db.APP_ROOT/'fake.pdf', 'slides.pdf', 'application/pdf', 1,
                       [{'page':2,'text':'Hemicholinium、acetylcholine','section':'A'}])
        vocabulary = m.course_vocabulary(course, '')
        self.assertIn({'term':'Hemicholinium','source':'slides.pdf p.2'}, vocabulary)
        self.assertEqual(m.select_vocabulary(vocabulary,[{**s,'text':'Hemicholinine'}],1)[0]['term'], 'Hemicholinium')

    def test_missing_model_shard_cannot_report_ready(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(m, 'MODEL_DIR', Path(directory)):
            p=Path(directory)
            (p/'config.json').write_text('{}')
            (p/'tokenizer.json').write_text('{}')
            manifest={'sha':m.MODEL_PROFILES[m.MODEL_PROFILE][2], 'siblings':[
                {'rfilename':'part1.safetensors','size':3}, {'rfilename':'part2.safetensors','size':3}]}
            (p/'download-manifest.json').write_text(json.dumps(manifest))
            (p/'part1.safetensors').write_bytes(b'123')
            self.assertFalse(m.model_installed())
            (p/'part2.safetensors').write_bytes(b'123')
            self.assertTrue(m.model_installed())
            (p/'part2.safetensors').write_bytes(b'12')
            self.assertFalse(m.model_installed())

    def test_alignment_validates_coverage_times_and_stale_results(self):
        course,s = self.fixture()
        aligned={'clip_start_ms':0,'clip_end_ms':4000,'words':[{'word':s['text'],'start_ms':1100,'end_ms':2900}]}
        m.save_alignment(course,s,aligned)
        self.assertEqual(db.get_segment(s['id'])['start_ms'],1100)
        self.assertEqual(db.get_segment(s['id'])['text'],s['text'])
        self.assertEqual(m.history(course)[0]['after']['words'],aligned['words'])
        with self.assertRaises(ValueError): m.save_alignment(course,s,aligned)
        s=db.get_segment(s['id'])
        for words in [[],[{'word':'缺字','start_ms':1100,'end_ms':2900}],
                      [{'word':s['text'],'start_ms':float('nan'),'end_ms':2900}],
                      [{'word':s['text'],'start_ms':2900,'end_ms':1100}]]:
            with self.assertRaises(ValueError): m.save_alignment(course,s,{**aligned,'words':words})
        self.assertEqual(db.get_segment(s['id'])['start_ms'],1100)

    def test_alignment_rejects_overlapping_neighbor(self):
        course,s=self.fixture()
        db.add_segments(course,[{'start_ms':3000,'end_ms':5000,'text':'下一句'}])
        with self.assertRaises(ValueError):
            m.save_alignment(course,s,{'clip_start_ms':0,'clip_end_ms':5000,
                'words':[{'word':s['text'],'start_ms':1000,'end_ms':3500}]})
        self.assertEqual(db.get_segment(s['id'])['end_ms'],3000)

    def test_job_validation_busy_edits_and_restart(self):
        course,s=self.fixture()
        with mock.patch.object(m,'readiness',return_value={'suggest_ready':True,'align_ready':True}):
            for value in [-1,float('nan'),'0']:
                with self.assertRaises(ValueError): m.start(course,'suggest',value)
            with mock.patch.dict(jobs._active_by_course,{course:'busy'}):
                self.assertEqual(self.request('PATCH',f'/api/segments/{s["id"]}',{'text':'新文字'})[0],409)
                with self.assertRaises(jobs.JobBusyError): m.start(course,'suggest',0)
        job=db.create_job(course,kind='medical_suggest')
        self.assertEqual(jobs.get_job(course)['status'],'error')

    def test_background_suggestions_persist_without_changing_transcript(self):
        course,s=self.fixture()
        job=db.create_job(course,kind='medical_suggest')
        with mock.patch.object(m,'worker',return_value=self.proposal(s)) as run:
            m._run(job,course,'suggest',[s],'膽素能藥物',None)
        self.assertEqual(db.latest_job(course)['status'],'completed')
        self.assertEqual(db.get_segment(s['id'])['text'],s['text'])
        self.assertEqual(m.history(course)[0]['status'],'pending')
        self.assertEqual(run.call_args.args[0],'suggest')
        # New evidence metadata must not duplicate the same pending text change.
        with mock.patch.object(m,'worker',return_value=self.proposal(s)):
            m._run(db.create_job(course,kind='medical_suggest'),course,'suggest',[s],'新的背景',None)
        self.assertEqual(len(m.history(course)),1)

    def test_alignment_retry_skips_unchanged_successes(self):
        course,s=self.fixture()
        db.patch_segment(s['id'],{'text':'已校訂'})
        s=db.get_segment(s['id'])
        m.save_alignment(course,s,{'clip_start_ms':0,'clip_end_ms':4000,
            'words':[{'word':s['text'],'start_ms':1000,'end_ms':3000}]})
        s=db.get_segment(s['id'])
        job=db.create_job(course,kind='medical_align')
        with mock.patch.object(m,'worker') as worker:
            m._run(job,course,'align',[s],'','unused.wav')
        worker.assert_not_called()
        self.assertEqual(db.latest_job(course)['status'],'completed')
