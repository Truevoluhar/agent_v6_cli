from importlib.resources import files
import threading
import time
import psycopg
from psycopg.types.json import Jsonb


class Database:
    def __init__(self,dsn,token=None):
        if not dsn:raise ValueError('Configure database_url first: agent-cli configure')
        self.dsn=dsn;self.token=token

    def call(self,action,**data):
        with psycopg.connect(self.dsn,connect_timeout=10,options='-c statement_timeout=30000 -c lock_timeout=5000') as conn:
            return conn.execute('SELECT agent_cli.rpc(%s,%s,%s)',(action,self.token,Jsonb(data))).fetchone()[0]

    def initialize(self):
        with psycopg.connect(self.dsn,connect_timeout=10) as conn:
            conn.execute(files('agent_cli').joinpath('schema.sql').read_text(encoding='utf-8'))

    def login(self,username,password):
        result=self.call('login',username=username,password=password);self.token=result['guid'];return result

    def heartbeat(self,thread_id,turn_id):
        return Heartbeat(self,thread_id,turn_id)


class Heartbeat:
    def __init__(self,db,thread_id,turn_id):self.db=db;self.thread_id=thread_id;self.turn_id=turn_id;self.stop=threading.Event();self.error=None
    def __enter__(self):
        def run():
            while not self.stop.wait(30):
                try:self.db.call('heartbeat',thread_id=self.thread_id,turn_id=self.turn_id)
                except Exception as exc:self.error=exc
        self.thread=threading.Thread(target=run,daemon=True);self.thread.start();return self
    def __exit__(self,*args):self.stop.set();self.thread.join(timeout=12)
