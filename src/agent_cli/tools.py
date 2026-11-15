import base64
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import tempfile
from typing import Callable

from .processes import Processes
from .config import state_dir,private_write


@dataclass
class Tool:
    name:str
    description:str
    parameters:dict
    function:Callable
    def schema(self):return {'type':'function','function':{'name':self.name,'description':self.description,'parameters':self.parameters}}


def params(properties,required=()):return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}
def string(description=''):return {'type':'string','description':description}
def integer(default,minimum=0,maximum=None):
    x={'type':'integer','default':default,'minimum':minimum}
    if maximum is not None:x['maximum']=maximum
    return x
BOOL={'type':'boolean'}


class Tools:
    def __init__(self,workspace,db,user,model):
        self.workspace=workspace;self.db=db;self.model=model;self.processes=Processes(workspace,user['user_id'])
        self.results_root=state_dir()/'results'/str(user['user_id'])/workspace.key
        self.registry={}
        def add(name,desc,props,required,fn):self.registry[name]=Tool(name,desc,params(props,required),fn)
        add('read_tool_result','Read a complete locally stored large tool result by artifact_id, with character pagination.',{'artifact_id':string(),'offset':integer(0),'max_chars':integer(12000,1,100000)},['artifact_id'],self.read_tool_result)
        add('environment','Identify OS, selected workspace and available native shells/tools before executing commands.',{},[],self.environment)
        add('list_files','List a directory inside the selected workspace; no silent truncation.',{'path':string(),'offset':integer(0),'limit':integer(100,1,1000)},[],self.list_files)
        add('file_info','Stat and SHA256 of a local file or metadata for directory.',{'path':string()},['path'],self.file_info)
        add('read_file','Read a text file by line range, or exact binary bytes as base64. Offsets and truncation are explicit.',
            {'path':string(),'start_line':integer(1,1),'end_line':integer(300,1),'encoding':string('utf-8 by default; specify cp1250 etc when needed'),'max_chars':integer(24000,1,1000000),'binary':BOOL,'offset':integer(0),'max_bytes':integer(24000,1,1048576)},['path'],self.read_file)
        add('write_file','Atomically write UTF-8 text or base64 binary in workspace. Optional expected_sha256 prevents overwriting concurrent edits.',
            {'path':string(),'content':string(),'binary':BOOL,'expected_sha256':string(),'encoding':string('utf-8 by default')},['path','content'],self.write_file)
        add('edit_file','Replace an exact unique text fragment. Ambiguous/missing matches or changed SHA256 produce an error without edits.',
            {'path':string(),'old_text':string(),'new_text':string(),'expected_sha256':string(),'encoding':string('utf-8 by default; preserve the file encoding')},['path','old_text','new_text'],self.edit_file)
        add('make_directory','Create a directory and parents within workspace.',{'path':string()},['path'],self.make_directory)
        add('copy_path','Copy a file or directory; destination must not already exist.',{'source':string(),'destination':string()},['source','destination'],self.copy_path)
        add('move_path','Move/rename a file or directory; destination must not already exist.',{'source':string(),'destination':string()},['source','destination'],self.move_path)
        add('remove_path','Remove an explicitly selected file/directory. recursive=true is required for a nonempty directory. Workspace root cannot be removed.',
            {'path':string(),'recursive':BOOL},['path'],self.remove_path)
        add('search_files','Search text in workspace files, literal or regex. Skips binary data; reports unreadable/skipped paths and pagination.',
            {'pattern':string(),'path':string(),'regex':BOOL,'offset':integer(0),'limit':integer(100,1,1000)},['pattern'],self.search_files)
        add('run_shell','Run a native shell command with this OS user privileges, no execution timeout. Returns a persistent job ID; use process_status and process_input. Shell is NOT an OS sandbox. Choose powershell/pwsh/cmd/bash/sh or auto. terminal=true enables Unix PTY or Windows ConPTY; output is then combined.',
            {'command':string(),'cwd':string(),'shell':string(),'env':{'type':'object','additionalProperties':{'type':'string'}},'terminal':BOOL,'wait_seconds':{'type':'number','minimum':0,'maximum':30}},['command'],self.processes.start)
        add('process_status','Read actual job state and paginated stdout/stderr; wait_seconds limits response wait, not execution.',
            {'job_id':string(),'wait_seconds':{'type':'number','minimum':0,'maximum':30},'stdout_offset':integer(0),'stderr_offset':integer(0),'max_bytes':integer(12000,1,1048576)},['job_id'],self.processes.status)
        add('process_input','Send stdin/terminal text, EOF, or explicitly stop a process tree. No stop happens unless requested.',
            {'job_id':string(),'text':string(),'eof':BOOL,'stop':BOOL,'force':BOOL},['job_id'],self.processes.send)
        add('process_list','List jobs belonging to this user and selected workspace.',{},[],self.processes.listing)
        add('inspect_image','Read/recognize a local image with the configured vision model. Supports EXIF orientation and pixel crop after orientation.',
            {'path':string(),'question':string(),'frame':integer(0),'max_side':integer(2048,128,8192),'crop':{'type':'array','items':{'type':'integer'},'minItems':4,'maxItems':4}},['path','question'],self.inspect_image)
        add('read_pdf','Extract text from PDF pages, including encrypted PDFs with supplied password. Scanned pages require inspect_pdf. Explicit page/text pagination.',
            {'path':string(),'start_page':integer(1,1),'page_count':integer(8,1,32),'text_offset':integer(0),'max_chars':integer(20000,1,100000),'password':string()},['path'],self.read_pdf)
        add('inspect_pdf','Render selected PDF pages and inspect them using vision (including scans); one-based page numbers. Reports exactly which pages were inspected.',
            {'path':string(),'question':string(),'pages':{'type':'array','items':{'type':'integer','minimum':1},'minItems':1,'maxItems':8},'dpi':integer(144,36,300),'password':string()},['path','question','pages'],self.inspect_pdf)
        add('saved_turns','List saved turns/statuses of your own thread, including interrupted work.',{'thread_id':string()},['thread_id'],lambda thread_id:db.call('turns',thread_id=thread_id))
        add('saved_turn','Read saved tool steps of an owned turn before resuming interrupted work. Recheck current local state before repeating actions.',{'thread_id':string(),'turn_id':string()},['thread_id','turn_id'],lambda thread_id,turn_id:db.call('turn',thread_id=thread_id,turn_id=turn_id))
        add('memory_get','Read this authenticated user\'s PostgreSQL memory with version for optimistic concurrency.',{},[],lambda:db.call('memory_get'))
        add('memory_set','Save durable user preferences/facts to PostgreSQL; first read memory_get. Never store passwords or transient tool dumps.',
            {'content':string(),'version':integer(0)},['content','version'],lambda **kw:db.call('memory_set',**kw))

    def execute(self,name,args):
        import jsonschema
        if name not in self.registry:return {'error':'Unknown tool: '+name}
        tool=self.registry[name]
        try:
            jsonschema.validate(args,tool.parameters)
            return tool.function(**args)
        except Exception as exc:return {'error':type(exc).__name__+': '+str(exc)}

    def save_tool_result(self,turn_id,call_id,content):
        artifact_id=turn_id+'/'+hashlib.sha256(call_id.encode()).hexdigest()+'.json'
        private_write(self.results_root/artifact_id,content)
        return artifact_id

    def read_tool_result(self,artifact_id,offset=0,max_chars=12000):
        p=(self.results_root/artifact_id).resolve()
        if not p.is_relative_to(self.results_root.resolve()):raise PermissionError('Invalid artifact_id')
        with p.open(encoding='utf-8') as f:
            # Offset is in characters, not UTF-8 bytes.
            remaining=offset
            while remaining:
                chunk=f.read(min(65536,remaining))
                if not chunk:break
                remaining-=len(chunk)
            text=f.read(max_chars);more=bool(f.read(1))
        return {'artifact_id':artifact_id,'text':text,'next_offset':offset+len(text),'more':more}

    def environment(self):
        return {'root':str(self.workspace.root),'os':platform.system(),'release':platform.release(),'python':platform.python_version(),
                'shell_is_sandboxed':False,'tools':{name:shutil.which(name) for name in ('pwsh','powershell','cmd','bash','sh','git','rg','python','python3','node','tesseract')}}

    def list_files(self,path='.',offset=0,limit=100):
        p=self.workspace.path(path);items=sorted(p.iterdir(),key=lambda e:(not e.is_dir(),e.name.casefold()))
        return {'path':str(p),'total':len(items),'next_offset':offset+len(items[offset:offset+limit]),'entries':[{'name':e.name,'directory':e.is_dir(),'symlink':e.is_symlink()} for e in items[offset:offset+limit]]}

    def file_info(self,path):
        p=self.workspace.path(path);s=p.stat();result={'path':str(p),'directory':p.is_dir(),'bytes':s.st_size,'modified_ns':s.st_mtime_ns}
        if p.is_file():
            with p.open('rb') as f:result['sha256']=hashlib.file_digest(f,'sha256').hexdigest()
        return result

    def read_file(self,path,start_line=1,end_line=300,encoding='utf-8',max_chars=24000,binary=False,offset=0,max_bytes=24000):
        p=self.workspace.path(path)
        if binary:
            with p.open('rb') as f:f.seek(offset);data=f.read(max_bytes)
            return {'path':str(p),'base64':base64.b64encode(data).decode(),'next_offset':offset+len(data),'total_bytes':p.stat().st_size}
        if end_line<start_line:raise ValueError('end_line must be >= start_line')
        selected=[];used=0;more=False;last=start_line-1
        with p.open('r',encoding=encoding,newline='') as f:
            for n,line in enumerate(f,1):
                if n<start_line:continue
                if n>end_line or used+len(line)>max_chars:more=True;break
                selected.append(line);used+=len(line);last=n
        return {'path':str(p),'text':''.join(selected),'first_line':start_line,'last_line':last,'more':more,'next_line':last+1,
                'note':'If a single line exceeds max_chars, raise max_chars or read exact bytes with binary=true.'}

    def write_file(self,path,content,binary=False,expected_sha256=None,encoding='utf-8'):
        p=self.workspace.path(path)
        if expected_sha256 is not None and (not p.exists() or self.file_info(path)['sha256']!=expected_sha256):raise ValueError('File changed; read again before writing')
        p.parent.mkdir(parents=True,exist_ok=True)
        data=base64.b64decode(content,validate=True) if binary else content.encode(encoding)
        mode=stat.S_IMODE(p.stat().st_mode) if p.exists() else None
        fd,temporary=tempfile.mkstemp(prefix='.agent-cli-',dir=p.parent)
        try:
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            if mode is not None:os.chmod(temporary,mode)
            os.replace(temporary,p)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
        return self.file_info(path)

    def edit_file(self,path,old_text,new_text,expected_sha256=None,encoding='utf-8'):
        p=self.workspace.path(path)
        with p.open(encoding=encoding,newline='') as f:text=f.read()
        if not old_text or text.count(old_text)!=1:raise ValueError('old_text must match exactly once; file was not modified')
        digest=self.file_info(path)['sha256']
        if expected_sha256 is not None and expected_sha256!=digest:raise ValueError('File changed; read again')
        return self.write_file(path,text.replace(old_text,new_text,1),expected_sha256=digest,encoding=encoding)

    def make_directory(self,path):p=self.workspace.path(path);p.mkdir(parents=True,exist_ok=True);return {'path':str(p)}
    def copy_path(self,source,destination):
        a=self.workspace.path(source);b=self.workspace.entry(destination)
        if b.exists() or b.is_symlink():raise FileExistsError(str(b))
        if a.is_dir():
            if b.is_relative_to(a):raise ValueError('Destination must not be inside the source directory')
            for child in a.rglob('*'):self.workspace.path(str(child))
            shutil.copytree(a,b,symlinks=True)
        else:b.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(a,b)
        return {'path':str(b)}
    def move_path(self,source,destination):
        a=self.workspace.entry(source);b=self.workspace.entry(destination)
        if a==self.workspace.root:raise ValueError('Cannot move workspace root')
        if b.exists() or b.is_symlink():raise FileExistsError(str(b))
        b.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(a),str(b));return {'path':str(b)}
    def remove_path(self,path,recursive=False):
        p=self.workspace.entry(path)
        if p==self.workspace.root:raise ValueError('Cannot remove workspace root')
        if p.is_symlink():p.unlink()
        elif p.is_dir():shutil.rmtree(p) if recursive else p.rmdir()
        else:p.unlink()
        return {'removed':str(p)}

    def search_files(self,pattern,path='.',regex=False,offset=0,limit=100):
        import re
        matcher=re.compile(pattern) if regex else None
        root=self.workspace.path(path);matches=[];skipped=[];seen=0;more=False
        candidates=[root] if root.is_file() else root.rglob('*')
        for candidate in candidates:
            try:
                p=self.workspace.path(str(candidate))
                if not p.is_file():continue
                with p.open('rb') as f:
                    if b'\0' in f.read(4096):skipped.append(str(p));continue
                with p.open(encoding='utf-8',errors='strict') as f:
                    for n,line in enumerate(f,1):
                        if bool(matcher.search(line)) if regex else pattern in line:
                            if seen>=offset:
                                if len(matches)>=limit:more=True;break
                                matches.append({'path':str(p),'line':n,'text':line[:2000],'line_truncated':len(line)>2000})
                            seen+=1
                if more:break
            except (OSError,UnicodeError) as exc:skipped.append(str(candidate)+': '+type(exc).__name__)
        return {'matches':matches,'more':more,'next_offset':offset+len(matches),'skipped':skipped[:100],'skipped_count':len(skipped)}

    @staticmethod
    def image_payload(image,max_side=2048):
        image=image.convert('RGB');image.thumbnail((max_side,max_side));out=io.BytesIO();image.save(out,format='PNG')
        return ('image/png',base64.b64encode(out.getvalue()).decode()),list(image.size)

    def inspect_image(self,path,question,frame=0,max_side=2048,crop=None):
        from PIL import Image,ImageOps
        p=self.workspace.path(path)
        with Image.open(p) as source:
            frames=getattr(source,'n_frames',1)
            if frame>=frames:raise ValueError('frame outside image')
            source.seek(frame);image=ImageOps.exif_transpose(source).copy()
        size=list(image.size)
        if crop:
            x0,y0,x1,y1=crop
            if not 0<=x0<x1<=image.width or not 0<=y0<y1<=image.height:raise ValueError('Invalid crop bounds')
            image=image.crop(crop)
        payload,sent=self.image_payload(image,max_side)
        return {'path':str(p),'frame':frame,'frame_count':frames,'original_size':size,'sent_size':sent,'observation':self.model.vision([payload],question)}

    def read_pdf(self,path,start_page=1,page_count=8,text_offset=0,max_chars=20000,password=''):
        from pypdf import PdfReader
        p=self.workspace.path(path);pages=[]
        with p.open('rb') as f:
            reader=PdfReader(f)
            if reader.is_encrypted and not reader.decrypt(password):raise ValueError('PDF requires a valid password')
            total=len(reader.pages)
            if start_page>total:raise ValueError('Page outside PDF')
            for index in range(start_page-1,min(start_page-1+page_count,total)):
                text=reader.pages[index].extract_text() or ''
                pages.append({'page':index+1,'text':text[text_offset:text_offset+max_chars],'total_chars':len(text),'next_text_offset':text_offset+len(text[text_offset:text_offset+max_chars]),'text_truncated':text_offset+max_chars<len(text),'scanned_or_empty':not text.strip()})
        return {'path':str(p),'page_count':total,'pages':pages,'next_page':start_page+len(pages) if start_page+len(pages)<=total else None}

    def inspect_pdf(self,path,question,pages,dpi=144,password=''):
        import pypdfium2 as pdfium
        p=self.workspace.path(path);result=[]
        document=pdfium.PdfDocument(str(p),password=password or None)
        try:
            total=len(document)
            if any(n>total for n in pages):raise ValueError('Page outside PDF')
            for n in pages:
                page=document[n-1]
                try:
                    width,height=page.get_size()
                    scale=min(dpi/72,(16000000/max(1,width*height))**.5)
                    bitmap=page.render(scale=scale)
                    try:payload,size=self.image_payload(bitmap.to_pil(),4096)
                    finally:bitmap.close()
                finally:page.close()
                result.append({'page':n,'render_dpi':round(scale*72,2),'sent_size':size,'observation':self.model.vision([payload],question)})
        finally:document.close()
        return {'path':str(p),'page_count':total,'inspected_pages':result,'coverage':'Only the explicitly listed pages were inspected.'}
