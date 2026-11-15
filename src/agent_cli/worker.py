"""Detached local process supervisor. Never connects to Postgres or Bifrost."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import psutil
from filelock import FileLock
from .config import private_write


def main():
    folder=Path(sys.argv[1]);spec=json.loads((folder/'spec.json').read_text(encoding='utf-8'))
    status=json.loads((folder/'status.json').read_text(encoding='utf-8'))
    proc=None;master=None;windows_pty=False;read_thread=None
    def save():private_write(folder/'status.json',json.dumps(status,ensure_ascii=False))
    try:
        env=os.environ.copy();env.update(spec['env']);env.setdefault('PYTHONIOENCODING','utf-8')
        # Do not pass database/session/model credentials to ordinary child commands by default.
        for key in ('AGENT_CLI_DATABASE_URL','BIFROST_API_KEY','DATABASE_URL'):
            if key not in spec['env']:env.pop(key,None)
        out=(folder/'stdout.log').open('wb');err=(folder/'stderr.log').open('wb')
        if spec['terminal'] and os.name=='nt':
            from winpty import PtyProcess
            proc=PtyProcess.spawn(spec['argv'],cwd=spec['cwd'],env=env);windows_pty=True
            def pump():
                try:
                    while True:
                        text=proc.read(4096)
                        if text:out.write(text.encode('utf-8'));out.flush()
                        elif not proc.isalive():break
                except (EOFError,OSError,ValueError):pass
            read_thread=threading.Thread(target=pump,daemon=True);read_thread.start()
        else:
            kwargs={}
            if spec['terminal']:
                import pty,fcntl,termios
                master,slave=pty.openpty()
                def terminal_setup():os.setsid();fcntl.ioctl(slave,termios.TIOCSCTTY,0)
                kwargs.update(stdin=slave,stdout=slave,stderr=slave,preexec_fn=terminal_setup)
            else:
                kwargs.update(stdin=subprocess.PIPE,stdout=out,stderr=err)
                if os.name=='nt':kwargs['creationflags']=subprocess.CREATE_NEW_PROCESS_GROUP
                else:kwargs['start_new_session']=True
            proc=subprocess.Popen(spec['argv'],cwd=spec['cwd'],env=env,**kwargs)
            if master is not None:
                os.close(slave)
                def pump():
                    try:
                        while True:
                            raw=os.read(master,65536)
                            if not raw:break
                            out.write(raw);out.flush()
                    except (OSError,ValueError):pass
                read_thread=threading.Thread(target=pump,daemon=True);read_thread.start()
        status.update(status='running',pid=proc.pid,worker_pid=os.getpid(),worker_started=psutil.Process().create_time());save()
        offset=0
        while proc.isalive() if windows_pty else proc.poll() is None:
            commands=[]
            with FileLock(str(folder/'control.lock')):
                control=folder/'control.jsonl'
                if control.exists():
                    with control.open(encoding='utf-8') as f:
                        f.seek(offset);text=f.read();offset=f.tell()
                        commands=[json.loads(line) for line in text.splitlines() if line.strip()]
            for command in commands:
                if command['stop']:
                    try:
                        parent=psutil.Process(proc.pid)
                        for child in parent.children(recursive=True):
                            try:child.kill() if command['force'] else child.terminate()
                            except psutil.Error:pass
                        if os.name!='nt' and not windows_pty:os.killpg(proc.pid,signal.SIGKILL if command['force'] else signal.SIGTERM)
                        else:parent.kill() if command['force'] else parent.terminate()
                    except (ProcessLookupError,psutil.Error):pass
                elif command['text']:
                    try:
                        if windows_pty:proc.write(command['text'])
                        elif master is not None:os.write(master,command['text'].encode('utf-8'))
                        else:proc.stdin.write(command['text'].encode('utf-8'));proc.stdin.flush()
                    except (OSError,ValueError,EOFError) as exc:
                        status['last_input_error']=type(exc).__name__+': '+str(exc);save()
                if command['eof']:
                    try:
                        if windows_pty:proc.write('\x04')
                        elif master is not None:os.write(master,b'\x04')
                        else:proc.stdin.close()
                    except (OSError,ValueError,EOFError) as exc:
                        status['last_input_error']=type(exc).__name__+': '+str(exc);save()
            time.sleep(.1)
        if read_thread:read_thread.join(timeout=2)
        code=proc.exitstatus if windows_pty else proc.returncode
        status.update(status='completed' if code==0 else 'failed',exit_code=code,finished=time.time());save()
    except Exception as exc:
        status.update(status='failed',error=type(exc).__name__+': '+str(exc),finished=time.time());save()
    finally:
        if master is not None:
            try:os.close(master)
            except OSError:pass
        for handle in ('out','err'):
            obj=locals().get(handle)
            if obj:obj.close()
        # Commands can contain credentials. Keep status/logs, discard transient argv/environment specification.
        try:(folder/'spec.json').unlink()
        except OSError:pass


if __name__=='__main__':main()
