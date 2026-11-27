"""Bifrost Streamable HTTP MCP tools for the synchronous CLI agent."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import re

import httpx


class BifrostMCP:
    def __init__(self,config):
        self.config=config

    @asynccontextmanager
    async def session(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        c=self.config
        if not c.bifrost_mcp_url:
            raise ValueError('BIFROST_MCP_ENABLED=true but BIFROST_MCP_URL is empty.')
        headers={'Authorization':'Bearer '+c.bifrost_mcp_api_key} if c.bifrost_mcp_api_key else {}
        # Same internal Bifrost MCP TLS policy as agent_v6_deep.
        async with httpx.AsyncClient(headers=headers,verify=False,
                                     timeout=httpx.Timeout(c.request_timeout,connect=15)) as client:
            async with streamable_http_client(c.bifrost_mcp_url,http_client=client) as (read,write,_):
                async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=c.request_timeout)) as session:
                    await session.initialize()
                    yield session

    async def discover(self):
        async with self.session() as session:
            tools=[];cursor=None;seen=set()
            while True:
                page=await session.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor=page.nextCursor
                if not cursor:return tools
                if cursor in seen:raise RuntimeError('Bifrost MCP returned a repeated pagination cursor')
                seen.add(cursor)

    async def call(self,name,arguments):
        # One session per operation, as with the reference's stateless MCP adapter.
        # Never retry calls: a lost response may already have caused side effects.
        async with self.session() as session:
            result=await session.call_tool(name,arguments)
            return result.model_dump(mode='json',by_alias=True,exclude_none=True)

    def register(self,registry):
        from .tools import Tool
        discovered=asyncio.run(self.discover())
        additions={}
        for remote in discovered:
            safe=re.sub(r'[^a-zA-Z0-9_-]','_',remote.name)[:40]
            name='bifrost_'+safe+'_'+hashlib.sha256(remote.name.encode()).hexdigest()[:12]
            if name in registry or name in additions:
                raise ValueError('Duplicate Bifrost MCP tool: '+remote.name)
            # Bind each remote name independently of the discovery loop.
            def bind(remote_name):
                return lambda **arguments: asyncio.run(self.call(remote_name,arguments))
            additions[name]=Tool(name,'Bifrost MCP ('+remote.name+'): '+(remote.description or ''),remote.inputSchema,bind(remote.name))
        registry.update(additions)
        return list(additions)
