import base64
import io
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from agent_cli.paths import Workspace
from agent_cli.tools import Tools


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.root=self.base/'root';self.root.mkdir()
        self.env=tempfile.TemporaryDirectory();self.previous=os.environ.get('AGENT_CLI_STATE_DIR');os.environ['AGENT_CLI_STATE_DIR']=self.env.name
        self.vision=[]
        def inspect(images,question):self.vision.append((images,question));return 'Verified test image observation'
        self.tools=Tools(Workspace(self.root),SimpleNamespace(call=lambda *a,**kw:{}),{'user_id':'test-user'},SimpleNamespace(vision=inspect))
    def tearDown(self):
        if self.previous is None:os.environ.pop('AGENT_CLI_STATE_DIR',None)
        else:os.environ['AGENT_CLI_STATE_DIR']=self.previous
        self.temp.cleanup();self.env.cleanup()

    def test_unicode_binary_and_atomic_edit_preserve_newlines(self):
        name='podmapa/čšž 😀.txt';result=self.tools.write_file(name,'prva\r\ndruga\r\n')
        self.tools.edit_file(name,'druga','zadnja',expected_sha256=result['sha256'])
        self.assertEqual((self.root/name).read_bytes(),b'prva\r\nzadnja\r\n')
        with self.assertRaises(ValueError):self.tools.edit_file(name,'prva','x',expected_sha256=result['sha256'])
        self.tools.write_file('binary',base64.b64encode(bytes(range(256))).decode(),binary=True)
        got=self.tools.read_file('binary',binary=True,offset=20,max_bytes=10)
        self.assertEqual(base64.b64decode(got['base64']),bytes(range(20,30)))
        self.assertEqual(got['next_offset'],30)
        self.tools.write_file('legacy.txt','čšž',encoding='cp1250')
        self.tools.edit_file('legacy.txt','č','Č',encoding='cp1250')
        self.assertEqual((self.root/'legacy.txt').read_bytes(),'Čšž'.encode('cp1250'))

    def test_scope_and_symlink_remove_does_not_remove_target(self):
        with self.assertRaises(PermissionError):self.tools.read_file('../outside')
        self.tools.write_file('target','keep')
        try:(self.root/'link').symlink_to(self.root/'target')
        except OSError:self.skipTest('Symlink privilege unavailable')
        self.tools.remove_path('link')
        self.assertTrue((self.root/'target').exists());self.assertFalse((self.root/'link').is_symlink())
        (self.base/'outside').write_text('outside');(self.root/'escape').symlink_to(self.base/'outside')
        with self.assertRaises(PermissionError):self.tools.read_file('escape')
        self.tools.remove_path('escape');self.assertTrue((self.base/'outside').exists())

    def test_no_overwrite_on_ambiguous_edit_or_copy(self):
        self.tools.write_file('a','same same')
        with self.assertRaises(ValueError):self.tools.edit_file('a','same','x')
        self.tools.write_file('b','original')
        with self.assertRaises(FileExistsError):self.tools.copy_path('a','b')
        self.assertEqual((self.root/'b').read_text(),'original')
        self.tools.make_directory('folder')
        with self.assertRaises(ValueError):self.tools.copy_path('folder','folder/backup')

    def test_search_and_text_pagination(self):
        self.tools.write_file('a','first\nfind one\nfind two\nlast\n')
        one=self.tools.search_files('find',limit=1);two=self.tools.search_files('find',offset=one['next_offset'],limit=1)
        self.assertTrue(one['more']);self.assertEqual(two['matches'][0]['line'],3)
        page=self.tools.read_file('a',start_line=2,end_line=3)
        self.assertEqual(page['text'],'find one\nfind two\n');self.assertEqual(page['next_line'],4)
        self.assertTrue(page['more'])

    def test_image_frames_crop_and_resources_closed(self):
        from PIL import Image
        image=self.root/'image.tif'
        a=Image.new('RGB',(300,200),'white');b=Image.new('RGB',(300,200),'blue');a.save(image,save_all=True,append_images=[b])
        result=self.tools.inspect_image(str(image),'Read',frame=1,crop=[0,0,100,100])
        self.assertEqual(result['frame_count'],2);self.assertEqual(result['sent_size'],[100,100])
        image.rename(self.root/'renamed.tif')
        payload=base64.b64decode(self.vision[0][0][0][1]);self.assertTrue(payload.startswith(b'\x89PNG'))

    def test_pdf_text_pages_password_render_and_closed_handles(self):
        from pypdf import PdfWriter
        writer=PdfWriter()
        for _ in range(3):writer.add_blank_page(width=300,height=200)
        writer.encrypt('secret');path=self.root/'encrypted.pdf'
        with path.open('wb') as f:writer.write(f)
        with self.assertRaises(ValueError):self.tools.read_pdf(str(path))
        result=self.tools.read_pdf(str(path),start_page=2,page_count=2,password='secret')
        self.assertEqual([p['page'] for p in result['pages']],[2,3]);self.assertIsNone(result['next_page'])
        self.assertTrue(result['pages'][0]['scanned_or_empty'])
        rendered=self.tools.inspect_pdf(str(path),'Read all visible content',[3],password='secret')
        self.assertEqual(rendered['inspected_pages'][0]['page'],3)
        path.rename(self.root/'moved.pdf')

    def test_schema_validation_and_unknown_tool_are_recoverable(self):
        self.assertIn('error',self.tools.execute('run_shell',{'command':'echo test','wait_seconds':-1}))
        self.assertIn('error',self.tools.execute('unknown',{}))
        self.assertIn('error',self.tools.execute('read_file',{'path':'missing'}))

if __name__=='__main__':unittest.main()
