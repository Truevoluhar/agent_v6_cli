from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import httpx
from agent_cli.agent import Agent
from agent_cli.config import Config
from agent_cli.model import Model,ModelError
from agent_cli.paths import Workspace
from agent_cli.tools import Tools


class FakeDB:
    def __init__(self):self.messages=[];self.steps=[];self.status='running'
    def heartbeat(self,*args):return nullcontext(SimpleNamespace(error=None))
    def call(self,action,**data):
        if action=='begin_turn':self.messages.append({'role':'user','content':data['message']})
        if action=='messages':return self.messages[:]
        if action=='memory_get':return {'content':'','version':0}
        if action=='me':return {'user_id':'test','username':'ana'}
        if action=='turn':return {'status':self.status}
        if action=='turn_step':self.steps.append(data['step'])
        if action=='finish_turn':self.status='completed';self.messages.append({'role':'assistant','content':data['content']})
        if action=='fail_turn':self.status='failed'
        return {}


def tool(name,args,id='one'):
    return {'role':'assistant','content':None,'tool_calls':[{'id':id,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}


class AgentModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.workspace=Workspace(self.root)
        self.previous=os.environ.get('AGENT_CLI_STATE_DIR');os.environ['AGENT_CLI_STATE_DIR']=str(self.root/'state')
    def tearDown(self):
        if self.previous is None:os.environ.pop('AGENT_CLI_STATE_DIR',None)
        else:os.environ['AGENT_CLI_STATE_DIR']=self.previous
        self.temp.cleanup()
    def model(self,responses):
        config=Config();model=Model(config);model.client.close();self.requests=[]
        def transport(req):
            self.requests.append(req)
            response=responses.pop(0)
            return httpx.Response(200,json={'choices':[response]})
        model.client=httpx.Client(base_url=config.bifrost_url+'/',transport=httpx.MockTransport(transport),headers={'x-bf-mcp-include-clients':'','x-bf-mcp-include-tools':''})
        self.addCleanup(model.close);return model

    def test_real_tool_loop_writes_file_and_records_final_answer(self):
        first={'message':tool('write_file',{'path':'done.txt','content':'written by actual tool'}),'finish_reason':'tool_calls'}
        last={'message':{'role':'assistant','content':'Done.'},'finish_reason':'stop'}
        model=self.model([first,last]);db=FakeDB();tools=Tools(self.workspace,db,{'user_id':'test'},model)
        answer=Agent(Config(),db,self.workspace,model,tools).run('thread','Write it')
        self.assertEqual(answer,'Done.');self.assertEqual((self.root/'done.txt').read_text(),'written by actual tool')
        self.assertEqual(db.steps[0]['tool'],'write_file');self.assertEqual(db.status,'completed')
        body=json.loads(self.requests[1].content);self.assertEqual(body['messages'][-1]['role'],'tool')

    def test_truncated_model_response_never_executes_partial_tool(self):
        model=self.model([{'message':tool('write_file',{'path':'bad','content':'not allowed'}),'finish_reason':'length'}]);db=FakeDB()
        with self.assertRaises(ModelError):Agent(Config(),db,self.workspace,model,Tools(self.workspace,db,{'user_id':'test'},model)).run('thread','test')
        self.assertFalse((self.root/'bad').exists());self.assertEqual(db.status,'failed')

    def test_invalid_second_call_prevents_first_side_effect(self):
        first=tool('write_file',{'path':'bad','content':'should not execute'})
        second=tool('write_file',{})['tool_calls'][0];second['id']='two';second['function']['arguments']='{"path":'
        first['tool_calls'].append(second)
        model=self.model([{'message':first,'finish_reason':'tool_calls'}]);db=FakeDB()
        with self.assertRaises(ModelError):Agent(Config(),db,self.workspace,model,Tools(self.workspace,db,{'user_id':'test'},model)).run('thread','test')
        self.assertFalse((self.root/'bad').exists())

    def test_large_results_remain_readable_without_workspace_escape(self):
        db=FakeDB();tools=Tools(self.workspace,db,{'user_id':'test'},None)
        identifier=tools.save_tool_result('turn','../../hostile','čšž'*20000)
        self.assertEqual(len(tools.read_tool_result(identifier,max_chars=300)['text']),300)
        with self.assertRaises(PermissionError):tools.read_tool_result('../../../../config.json')

    def test_vision_wire_format_and_gateway_tool_filter(self):
        # Use the constructor's headers, not the test helper's replacement client.
        config=Config();model=Model(config);self.addCleanup(model.close)
        headers=dict(model.client.headers);model.client.close();requests=[]
        def handler(req):requests.append(req);return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'visible'},'finish_reason':'stop'}]})
        model.client=httpx.Client(base_url=config.bifrost_url+'/',headers=headers,transport=httpx.MockTransport(handler))
        self.assertEqual(model.vision([('image/png','AA==')],'Read'),'visible')
        req=requests[0];body=json.loads(req.content)
        self.assertEqual(req.headers['x-bf-mcp-include-clients'],'');self.assertEqual(req.headers['x-bf-mcp-include-tools'],'')
        self.assertEqual(body['messages'][0]['content'][1]['image_url']['url'],'data:image/png;base64,AA==')
        self.assertEqual(body['max_tokens'],config.vision_output_tokens)

if __name__=='__main__':unittest.main()
