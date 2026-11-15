"""Real PostgreSQL role/ownership/lease tests using an isolated disposable database only."""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import secrets
import subprocess
import time
import uuid
import psycopg
from psycopg import sql
from agent_cli.database import Database


def cli_end_to_end(client_dsn):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,value):
            data=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.headers['x-bf-mcp-include-clients']==''
            assert self.headers['x-bf-mcp-include-tools']==''
            last=body['messages'][-1]
            def call(name,args):return {'role':'assistant','content':None,'tool_calls':[{'id':str(uuid.uuid4()),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}
            if last['role']=='user':message=call('write_file',{'path':'native.txt','content':'native-file-ok'})
            else:
                result=json.loads(last['content'])
                if 'sha256' in result:
                    import shlex
                    command=shlex.join([sys.executable,'-c','from pathlib import Path;print(Path("native.txt").read_text())'])
                    message=call('run_shell',{'command':command,'wait_seconds':0})
                elif result.get('status') in ('starting','running'):message=call('process_status',{'job_id':result['id'],'wait_seconds':1})
                elif result.get('status')=='completed':
                    assert result['exit_code']==0 and 'native-file-ok' in result['stdout']['content'],result
                    message={'role':'assistant','content':'Actual local tools and shared database are working.'}
                else:raise AssertionError(result)
            self.respond({'choices':[{'message':message,'finish_reason':'tool_calls' if message.get('tool_calls') else 'stop'}]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);workspace=root/'workspace';workspace.mkdir()
            env=os.environ.copy();env.update(AGENT_CLI_DATABASE_URL=client_dsn,AGENT_CLI_CONFIG_DIR=str(root/'config'),AGENT_CLI_STATE_DIR=str(root/'state'),
                AGENT_CLI_TOKEN_STORE='file',BIFROST_BASE_URL='http://127.0.0.1:'+str(server.server_port)+'/openai',BIFROST_API_KEY='test-key')
            def run(args,stdin=None):
                result=subprocess.run([sys.executable,'-m','agent_cli',*args],input=stdin,capture_output=True,text=True,env=env,timeout=60)
                assert result.returncode==0,(args,result.stderr,result.stdout)
                return result
            run(['login','--username','ana'],'test-pass\n')
            response=run(['--root',str(workspace),'chat','--new','--json','Create a file and verify it using the native shell.'])
            parsed=json.loads(response.stdout);assert parsed['answer']=='Actual local tools and shared database are working.'
            assert (workspace/'native.txt').read_text()=='native-file-ok'
            history=json.loads(run(['history',parsed['thread_id']]).stdout)
            assert [m['role'] for m in history]==['user','assistant']
            assert history[0]['metadata']['root']==str(workspace.resolve())
            assert {p.name for p in workspace.iterdir()}=={'native.txt'}
            run(['logout'])
            print('Installed CLI subprocess: login -> Bifrost-compatible HTTP -> real file/shell tools -> persisted history -> logout: OK')
    finally:server.shutdown();server.server_close();thread.join()


def main():
    name='agent-cli-test-'+uuid.uuid4().hex[:10];password=secrets.token_urlsafe(24)
    started=False
    try:
        subprocess.run(['docker','run','-d','--rm','--name',name,'-e','POSTGRES_PASSWORD='+password,'-p','127.0.0.1::5432','postgres:17'],check=True,stdout=subprocess.DEVNULL)
        started=True
        port=subprocess.check_output(['docker','port',name,'5432/tcp'],text=True).strip().rsplit(':',1)[1]
        dsn='postgresql://postgres:'+password+'@127.0.0.1:'+port+'/postgres'
        deadline=time.monotonic()+120
        while True:
            try:
                with psycopg.connect(dsn,connect_timeout=2) as conn:conn.execute('SELECT 1')
                break
            except psycopg.OperationalError:
                if time.monotonic()>deadline:raise RuntimeError('Test Postgres did not start') from None
                time.sleep(1)
        with psycopg.connect(dsn) as conn:
            conn.execute('''CREATE TABLE app_users(id UUID PRIMARY KEY,username TEXT UNIQUE,password_plain TEXT,role TEXT,is_active BOOLEAN DEFAULT TRUE);
            CREATE TABLE auth_sessions(guid TEXT PRIMARY KEY,user_id UUID REFERENCES app_users(id),expires_at TIMESTAMPTZ,revoked_at TIMESTAMPTZ);''')
            conn.execute('INSERT INTO app_users(id,username,password_plain,role) VALUES(%s,%s,%s,%s),(%s,%s,%s,%s)',(uuid.uuid4(),'ana','test-pass','user',uuid.uuid4(),'boris','test-pass','user'))
        Database(dsn).initialize()
        with psycopg.connect(dsn) as conn:
            conn.execute("CREATE ROLE cli_login LOGIN PASSWORD 'test-client-password'")
            conn.execute('GRANT agent_cli_client TO cli_login')
        client_dsn='postgresql://cli_login:test-client-password@127.0.0.1:'+port+'/postgres'
        a=Database(client_dsn);b=Database(client_dsn)
        a.login('ana','test-pass');b.login('boris','test-pass')
        assert a.call('me')['username']=='ana'
        for query in ('SELECT password_plain FROM public.app_users','SELECT guid FROM public.auth_sessions','SELECT * FROM agent_cli.threads'):
            try:
                with psycopg.connect(client_dsn) as conn:conn.execute(query)
            except psycopg.errors.InsufficientPrivilege:pass
            else:raise AssertionError('Client unexpectedly has direct table access')
        thread=a.call('new_thread')['id'];turn=str(uuid.uuid4())
        a.call('begin_turn',thread_id=thread,turn_id=turn,message='hello',metadata={'root':'test'})
        try:b.call('messages',thread_id=thread)
        except psycopg.errors.InsufficientPrivilege:pass
        else:raise AssertionError('Cross-user access succeeded')
        try:a.call('begin_turn',thread_id=thread,turn_id=str(uuid.uuid4()),message='duplicate')
        except psycopg.errors.ObjectNotInPrerequisiteState:pass
        else:raise AssertionError('Concurrent turn was accepted')
        a.call('turn_step',thread_id=thread,turn_id=turn,step={'tool':'read_file','result':'ok'})
        a.call('finish_turn',thread_id=thread,turn_id=turn,content='done')
        messages=a.call('messages',thread_id=thread);assert [m['role'] for m in messages]==['user','assistant']
        assert a.call('turn',thread_id=thread,turn_id=turn)['steps'][0]['tool']=='read_file'
        assert a.call('turns',thread_id=thread)[0]['id']==turn
        memory=a.call('memory_get');a.call('memory_set',content='preference',version=memory['version'])
        try:a.call('memory_set',content='overwrite',version=memory['version'])
        except psycopg.errors.SerializationFailure:pass
        else:raise AssertionError('Lost-update memory overwrite accepted')
        assert b.call('memory_get')['content']==''
        cli_end_to_end(client_dsn)
        # Existing application's auth_sessions contract accepts the CLI-created own GUID.
        with psycopg.connect(dsn) as conn:
            assert conn.execute('SELECT u.username FROM auth_sessions s JOIN app_users u ON u.id=s.user_id WHERE s.guid=%s AND s.revoked_at IS NULL AND s.expires_at>now()',(a.token,)).fetchone()[0]=='ana'
            conn.execute("UPDATE app_users SET is_active=FALSE WHERE username='ana'")
        try:a.call('me')
        except psycopg.errors.InvalidAuthorizationSpecification:pass
        else:raise AssertionError('Deactivated user remained authenticated')
        b.call('logout')
        try:b.call('me')
        except psycopg.errors.InvalidAuthorizationSpecification:pass
        else:raise AssertionError('Revoked session remained valid')
        print('Real Postgres: login compatibility, restricted role, thread ownership, turn lease, durable steps, memory concurrency and revocation: OK')
    finally:
        if started:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

if __name__=='__main__':main()
