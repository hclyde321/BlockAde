import unittest
import db
import test_backend

class CourseBankSyncTests(unittest.TestCase):
    setUp = test_backend.BackendApiSmokeTests.setUp
    tearDown = test_backend.BackendApiSmokeTests.tearDown
    request = test_backend.BackendApiSmokeTests.request

    def test_each_subject_loads_only_its_bank_and_does_not_duplicate(self):
        for subject in ['pathology','pharmacology','clinical','laboratory']:
            bank=db.create_bank(subject,subject)
            db.replace_bank_questions(bank['id'],[{'number':'1','stem':subject,'options':[]}])
        for subject in ['pathology','pharmacology','clinical','laboratory']:
            course=db.create_course(subject,subject)['id']
            for expected in [1,0]:
                status,_,body,_=self.request('POST',f'/api/courses/{course}/sync-bank')
                self.assertEqual(status,200)
                self.assertEqual(body,{'added':expected,'total':1})
            self.assertEqual(db.list_questions(course)[0]['stem'],subject)
