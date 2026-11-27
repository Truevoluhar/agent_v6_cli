import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from agent_cli.config import Config
from agent_cli.cli import safe_error
from agent_cli.mcp import BifrostMCP
from agent_cli.tools import Tools
import test_agent_model as helpers
from agent_cli.agent import Agent


class MCPTests(unittest.TestCase):
    tearDown=helpers.AgentModelTests.tearDown
    model=helpers.AgentModelTests.model

    def setUp(self):
        helpers.AgentModelTests.setUp(self)
        self.wire=[];self.client_options=[]
        self.config=Config(bifrost_mcp_enabled=True,bifrost_mcp_url='https://bifrost.invalid/mcp',bifrost_mcp_api_key='mcp-secret')
        original=httpx.AsyncClient
        def client(**kwargs):
            self.client_options.append(kwargs)
            return original(transport=httpx.MockTransport(self.respond),**kwargs)
        self.patcher=patch('agent_cli.mcp.httpx.AsyncClient',side_effect=client)
        self.patcher.start();self.addCleanup(self.patcher.stop)

    def respond(self,request):
        self.assertEqual(request.headers['authorization'],'Bearer mcp-secret')
        if request.method=='DELETE':return httpx.Response(200)
        body=json.loads(request.content);self.wire.append(body)
        if 'id' not in body:return httpx.Response(202)
        method=body['method'];params=body.get('params',{})
        if method=='initialize':result={'protocolVersion':params['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'test','version':'1'}}
        elif method=='tools/list':
            if not params.get('cursor'):
                result={'tools':[{'name':'read_file','description':'Remote lookup','inputSchema':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}],'nextCursor':'second'}
            else:result={'tools':[{'name':'server/fail','inputSchema':{'type':'object','properties':{}}}]}
        elif method=='tools/call':
            if params['name']=='read_file':result={'content':[{'type':'text','text':params['arguments']['query']}],'structuredContent':{'found':True},'isError':False}
            else:result={'content':[{'type':'text','text':'Remote failure'}],'isError':True}
        else:raise AssertionError(method)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result},headers={'mcp-session-id':'test-session'})

    def test_discovery_call_and_agent_history_through_real_sdk(self):
        db=helpers.FakeDB();tools=Tools(self.workspace,db,{'user_id':'test'},None)
        local=tools.registry['read_file']
        tools.initialize_mcp(self.config)
        self.assertIs(tools.registry['read_file'],local)
        self.assertEqual(len(tools.mcp_names),2)
        name=tools.mcp_names[0]
        self.assertLessEqual(len(name),64)
        model=self.model([{'message':helpers.tool(name,{'query':'remote answer'}),'finish_reason':'tool_calls'},
                          {'message':{'role':'assistant','content':'Done.'},'finish_reason':'stop'}])
        self.assertEqual(Agent(self.config,db,self.workspace,model,tools).run('thread','Lookup'),'Done.')
        result=db.steps[0]['result']
        self.assertEqual(result['content'][0]['text'],'remote answer')
        self.assertEqual(result['structuredContent'],{'found':True})
        self.assertFalse(result['isError'])
        self.assertEqual(len([r for r in self.wire if r['method']=='tools/call']),1)
        self.assertTrue(all(o['verify'] is False for o in self.client_options))
        sent=json.loads(self.requests[0].content)['tools']
        self.assertIn(name,[t['function']['name'] for t in sent])
        self.assertEqual(json.loads(self.requests[1].content)['messages'][-1]['role'],'tool')

    def test_remote_error_and_argument_validation(self):
        tools=Tools(self.workspace,helpers.FakeDB(),{'user_id':'test'},None);tools.initialize_mcp(self.config)
        result=tools.execute(tools.mcp_names[1],{})
        self.assertTrue(result['isError'])
        count=len(self.wire)
        self.assertIn('error',tools.execute(tools.mcp_names[0],{'query':23}))
        self.assertEqual(len(self.wire),count)

    def test_disabled_and_missing_url(self):
        tools=Tools(self.workspace,helpers.FakeDB(),{'user_id':'test'},None)
        tools.initialize_mcp(Config())
        self.assertEqual(self.wire,[])
        with self.assertRaisesRegex(ValueError,'MCP_URL'):
            tools.initialize_mcp(Config(bifrost_mcp_enabled=True))
        self.assertEqual(self.wire,[])

    def test_lost_call_response_is_not_replayed(self):
        tools=Tools(self.workspace,helpers.FakeDB(),{'user_id':'test'},None)
        tools.initialize_mcp(self.config)
        original=self.respond;calls=[]
        def lose_response(request):
            if request.method=='POST' and json.loads(request.content)['method']=='tools/call':
                calls.append(request)
                raise httpx.ReadError('Lost response',request=request)
            return original(request)
        self.respond=lose_response
        result=tools.execute(tools.mcp_names[0],{'query':'once'})
        self.assertIn('error',result)
        self.assertEqual(len(calls),1)


class MCPConfigTests(unittest.TestCase):
    def test_environment_override_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'config.json').write_text(json.dumps({'bifrost_mcp_enabled':False}))
            env={'AGENT_CLI_CONFIG_DIR':folder,'BIFROST_MCP_ENABLED':'true','BIFROST_MCP_URL':'https://example.invalid/mcp','BIFROST_MCP_API_KEY':'secret-test'}
            with patch.dict(os.environ,env):
                config=Config.load()
                self.assertTrue(config.bifrost_mcp_enabled)
                self.assertEqual(config.bifrost_mcp_url,env['BIFROST_MCP_URL'])
                self.assertNotIn('secret-test',safe_error(ValueError('secret-test'),config))
            with patch.dict(os.environ,{**env,'BIFROST_MCP_ENABLED':'invalid'}):
                with self.assertRaises(ValueError):Config.load()


if __name__=='__main__':unittest.main()
