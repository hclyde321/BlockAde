import unittest
from pathlib import Path
import fitz,db,handouts,test_backend
class HandoutPagesTests(unittest.TestCase):
    setUp=test_backend.BackendApiSmokeTests.setUp
    tearDown=test_backend.BackendApiSmokeTests.tearDown
    request=test_backend.BackendApiSmokeTests.request
    def test_pages_are_images_and_scoped_to_course(self):
        cid=db.create_course('lecture')['id'];other=db.create_course('other')['id']
        folder=db.DATA_DIR/cid/'handouts';folder.mkdir(parents=True)
        path=folder/'test.pdf'
        with fitz.open() as pdf:
            for text in ['First page','Second page']:
                page=pdf.new_page();page.insert_text((40,40),text)
            pdf.save(path)
        item=db.add_handout(cid,path,'test.pdf','application/pdf',path.stat().st_size,handouts.extract_pdf(path))
        url=f'/api/courses/{cid}/handouts/{item["id"]}/pages/'
        for n in [1,2]:
            status,body,_,headers=self.request('GET',url+str(n))
            self.assertEqual(status,200);self.assertTrue(body.startswith(b'\x89PNG'))
            self.assertEqual(headers['Content-Type'],'image/png')
        self.assertEqual(self.request('GET',url+'3')[0],404)
        self.assertEqual(self.request('GET',url.replace(cid,other)+'1')[0],404)
