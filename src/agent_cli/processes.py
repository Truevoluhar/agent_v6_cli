"""Native detached workers: execution survives CLI HTTP/model requests and CLI exit."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading
import uuid
from filelock import FileLock
from .config import private_write, state_dir


def shell_argv(command,shell='auto'):
    if shell=='auto':shell='powershell' if os.name=='nt' else ('bash' if shutil.which('bash') else 'sh')
    if shell in ('powershell','pwsh'):
        executable=shutil.which('pwsh') or shutil.which('powershell')
        if not executable:raise FileNotFoundError('PowerShell not installed')
        code='[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding; '+command
        code+='\n$agentCliSucceeded = $?; if (-not $agentCliSucceeded) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }'
        return [executable,'-NoLogo','-NoProfile','-EncodedCommand',base64.b64encode(code.encode('utf-16-le')).decode()]
    if shell=='cmd':return [os.environ.get('COMSPEC','cmd.exe'),'/D','/S','/C',command]
    executable=shutil.which(shell)
    if not executable:raise FileNotFoundError('Shell not installed: '+shell)
    return [executable,'-c',command]


class Processes:
    def __init__(self,workspace,owner):
        self.workspace=workspace;self.owner=str(owner)
        self.root=state_dir()/'jobs'/self.owner/workspace.key;self.root.mkdir(parents=True,exist_ok=True,mode=0o700)

    def folder(self,job_id):
        normalized=str(uuid.UUID(job_id));p=self.root/normalized
        if not p.is_dir():raise FileNotFoundError('Unknown process for this user/workspace')
        return p

    def start(self,command,cwd='.',shell='auto',env=None,terminal=False,wait_seconds=1):
        if not 0<=wait_seconds<=30:raise ValueError('wait_seconds must be 0..30; process execution has no timeout')
        directory=self.workspace.path(cwd)
        if not directory.is_dir():raise NotADirectoryError(str(directory))
        argv=shell_argv(command,shell)
        folder=self.root/str(uuid.uuid4());folder.mkdir(mode=0o700)
        spec={'argv':argv,'command':command,'cwd':str(directory),'env':env or {},'terminal':terminal}
        private_write(folder/'spec.json',json.dumps(spec,ensure_ascii=False))
        private_write(folder/'status.json',json.dumps({'id':folder.name,'status':'starting','created':time.time(),'cwd':str(directory),'terminal':terminal,'exit_code':None}))
        with (folder/'worker.log').open('wb') as log:
            kwargs={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS} if os.name=='nt' else {'start_new_session':True}
            worker=subprocess.Popen([sys.executable,'-m','agent_cli.worker',str(folder)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,**kwargs)
        threading.Thread(target=worker.wait,daemon=True).start()
        return self.status(folder.name,wait_seconds=wait_seconds)

    def status(self,job_id,wait_seconds=0,stdout_offset=0,stderr_offset=0,max_bytes=12000):
        if not 0<=wait_seconds<=30 or not 1<=max_bytes<=1048576:raise ValueError('Invalid wait/preview size')
        folder=self.folder(job_id);end=time.monotonic()+wait_seconds
        while True:
            data=json.loads((folder/'status.json').read_text(encoding='utf-8'))
            if data['status'] not in ('starting','running') or time.monotonic()>=end:break
            time.sleep(.1)
        if data['status']=='running' and data.get('worker_pid'):
            import psutil
            try:
                proc=psutil.Process(data['worker_pid'])
                alive=abs(proc.create_time()-data['worker_started'])<.01 and proc.is_running() and proc.status()!=psutil.STATUS_ZOMBIE
            except psutil.Error:alive=False
            if not alive:data['status']='interrupted';data['error']='Worker exited without a recorded exit code; command is not resubmitted.'
        for name,offset in (('stdout',stdout_offset),('stderr',stderr_offset)):
            if offset<0:raise ValueError('Offsets must be non-negative')
            p=folder/(name+'.log');raw=b''
            if p.exists():
                with p.open('rb') as f:f.seek(offset);raw=f.read(max_bytes)
            data[name]={'content':raw.decode('utf-8',errors='replace'),'next_offset':offset+len(raw),'total_bytes':p.stat().st_size if p.exists() else 0,'path':str(p)}
        return data

    def send(self,job_id,text='',eof=False,stop=False,force=False):
        folder=self.folder(job_id)
        status=self.status(job_id)
        if status['status'] not in ('starting','running'):return status
        with FileLock(str(folder/'control.lock')):
            with (folder/'control.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps({'text':text,'eof':eof,'stop':stop,'force':force})+'\n')
        return self.status(job_id,1)

    def listing(self):
        return [self.status(p.name) for p in self.root.iterdir() if p.is_dir() and (p/'status.json').exists()]
