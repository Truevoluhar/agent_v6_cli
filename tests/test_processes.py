import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from agent_cli.paths import Workspace
from agent_cli.processes import Processes,shell_argv


def command(code):
    if os.name=='nt':
        import base64
        encoded=__import__('base64').b64encode(code.encode()).decode()
        return '& '+"'"+sys.executable.replace("'","''")+"'"+' -c '+"'import base64;exec(base64.b64decode(\""+encoded+'\"))\''
    return shlex.join([sys.executable,'-c',code])


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.previous=os.environ.get('AGENT_CLI_STATE_DIR');os.environ['AGENT_CLI_STATE_DIR']=str(self.path/'state')
        self.root=self.path/'workspace';self.root.mkdir();self.manager=Processes(Workspace(self.root),'test-user')
    def tearDown(self):
        for job in self.manager.listing():
            if job['status'] in ('starting','running'):self.manager.send(job['id'],stop=True,force=True)
        time.sleep(.2)
        if self.previous is None:os.environ.pop('AGENT_CLI_STATE_DIR',None)
        else:os.environ['AGENT_CLI_STATE_DIR']=self.previous
        self.temp.cleanup()
    def finish(self,job,seconds=25):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            result=self.manager.status(job['id'],wait_seconds=1)
            if result['status'] not in ('starting','running'):return result
        self.fail('Process did not finish: '+str(result))

    def test_powershell_preserves_unicode_quotes_and_failure_status(self):
        source='Write-Output "čšž C:\\folder with spaces"; exit 7'
        with patch('agent_cli.processes.shutil.which',return_value='pwsh.exe'):
            argv=shell_argv(source,'powershell')
        import base64
        script=base64.b64decode(argv[-1]).decode('utf-16-le')
        self.assertIn(source,script);self.assertIn('exit $LASTEXITCODE',script)
        self.assertEqual(argv[-2],'-EncodedCommand')

    def test_stdout_stderr_exit_code_and_durable_worker(self):
        result=self.manager.start(command('import sys;print("čšž");print("problem",file=sys.stderr);sys.exit(7)'),wait_seconds=0)
        result=self.finish(result);self.assertEqual(result['exit_code'],7,result)
        self.assertIn('čšž',result['stdout']['content']);self.assertIn('problem',result['stderr']['content'])
        again=Processes(Workspace(self.root),'test-user').status(result['id']);self.assertEqual(again['exit_code'],7)

    def test_stdin_and_explicit_stop(self):
        job=self.manager.start(command('import sys; print(input("Name? "),flush=True)'),wait_seconds=0)
        self.manager.send(job['id'],'Alice\n')
        result=self.finish(job);self.assertIn('Alice',result['stdout']['content'])
        sleeping=self.manager.start(command('import time;time.sleep(90)'),wait_seconds=0)
        deadline=time.monotonic()+15
        while self.manager.status(sleeping['id'])['status']=='starting' and time.monotonic()<deadline:time.sleep(.1)
        self.manager.send(sleeping['id'],stop=True,force=True)
        self.assertEqual(self.finish(sleeping)['status'],'failed')

    def test_native_terminal(self):
        code='import sys;print("TTY",sys.stdin.isatty());print(input("Value: "),flush=True)'
        job=self.manager.start(command(code),terminal=True,wait_seconds=0)
        self.manager.send(job['id'],'terminal-ok\n' if os.name!='nt' else 'terminal-ok\r\n')
        result=self.finish(job);self.assertEqual(result['status'],'completed',result)
        self.assertIn('True',result['stdout']['content']);self.assertIn('terminal-ok',result['stdout']['content'])

    def test_cross_workspace_and_user_job_access_denied(self):
        job=self.manager.start(command('print("ok")'))
        with self.assertRaises(FileNotFoundError):Processes(Workspace(self.root),'other-user').status(job['id'])
        self.finish(job)

if __name__=='__main__':unittest.main()
