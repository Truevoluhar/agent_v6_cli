import argparse
from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path
import sys
import traceback
from .config import Config,config_dir,get_token,save_token,delete_token,state_dir,private_write
from .database import Database
from .paths import Workspace
from .model import Model
from .tools import Tools
from .agent import Agent


def parser():
    p=argparse.ArgumentParser(prog='agent-cli',description='Local agent: Postgres identity/history, Bifrost models, native computer tools.')
    p.add_argument('--root',default='.',help='Workspace (default: current directory); no OS sandbox is implied')
    p.add_argument('--debug',action='store_true')
    subs=p.add_subparsers(dest='command')
    subs.add_parser('configure',help='Configure Postgres/Bifrost connection paths')
    subs.add_parser('db-init',help='Administrator-only additive SQL setup; does not create/reset application users')
    login=subs.add_parser('login');login.add_argument('--username');login.add_argument('--guid',action='store_true',help='Enter your own existing valid local-auth GUID via hidden prompt')
    subs.add_parser('logout');subs.add_parser('whoami');subs.add_parser('doctor')
    subs.add_parser('threads');subs.add_parser('models')
    turns=subs.add_parser('turns');turns.add_argument('thread_id')
    turn=subs.add_parser('turn');turn.add_argument('thread_id');turn.add_argument('turn_id')
    messages=subs.add_parser('history');messages.add_argument('thread_id')
    memory=subs.add_parser('memory');memory.add_argument('--set-file',type=Path)
    chat=subs.add_parser('chat');chat.add_argument('message',nargs='?');chat.add_argument('--thread');chat.add_argument('--new',action='store_true');chat.add_argument('--json',action='store_true');chat.add_argument('--root',default=argparse.SUPPRESS)
    jobs=subs.add_parser('process');jobs.add_argument('job_id',nargs='?');jobs.add_argument('--stop',action='store_true');jobs.add_argument('--force',action='store_true')
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.table import Table
    console=Console(stderr=True)
    try:
        config=Config.load()
        if args.command=='configure':
            console.print('Connection settings stay in your user profile. No other service credentials will be read.')
            config.database_url=getpass.getpass('Postgres DSN (blank keeps current): ') or config.database_url
            config.bifrost_url=input('Bifrost base URL ['+config.bifrost_url+']: ').strip() or config.bifrost_url
            config.model=input('Model ['+config.model+']: ').strip() or config.model
            config.api_key=getpass.getpass('Bifrost key (blank keeps current): ') or config.api_key
            config.vision_model=input('Vision model ['+(config.vision_model or 'same as main')+']; enter - to use main: ').strip() or config.vision_model
            if config.vision_model=='-':config.vision_model=''
            enabled=input('Enable Bifrost MCP tools? ['+('yes' if config.bifrost_mcp_enabled else 'no')+']: ').strip().lower()
            if enabled:
                if enabled not in ('yes','no','y','n'):raise ValueError('Enter yes or no for MCP')
                config.bifrost_mcp_enabled=enabled in ('yes','y')
            if config.bifrost_mcp_enabled:
                config.bifrost_mcp_url=input('Bifrost MCP URL ['+config.bifrost_mcp_url+']: ').strip() or config.bifrost_mcp_url
                if not config.bifrost_mcp_url:raise ValueError('Bifrost MCP URL is required')
                config.bifrost_mcp_api_key=getpass.getpass('Bifrost MCP key (blank keeps current): ') or config.bifrost_mcp_api_key
            config.save();console.print('Saved:',str(config_dir()/'config.json'));return 0
        db=Database(config.database_url,get_token(config))
        if args.command=='db-init':
            db.initialize();console.print('Additive schema and restricted agent_cli_client group are ready. Grant that group to a dedicated LOGIN role for end users.');return 0
        if args.command=='login':
            if args.guid:
                db.token=getpass.getpass('Your own GUID: ');user=db.call('me');token=db.token
            else:
                username=args.username or input('Username: ');password=getpass.getpass('Password: ')
                user=db.login(username,password);token=user.pop('guid')
            location=save_token(config,token);console.print('Logged in as',user['username'],'· session stored in',location);return 0
        if args.command=='doctor':
            console.print('Python:',sys.version.split()[0],'· platform:',sys.platform)
            console.print('Workspace:',str(Workspace(args.root).root))
            with __import__('psycopg').connect(config.database_url,connect_timeout=10) as conn:
                console.print('Postgres:',conn.execute('SELECT version()').fetchone()[0].split(',')[0])
                present=conn.execute("SELECT to_regprocedure('agent_cli.rpc(text,text,jsonb)') IS NOT NULL").fetchone()[0]
                console.print('CLI schema:',present)
            model=Model(config)
            try:
                response=model.client.get('models');console.print('Bifrost /models HTTP:',response.status_code)
                if config.bifrost_mcp_enabled:
                    from .mcp import BifrostMCP
                    registry={};names=BifrostMCP(config).register(registry)
                    console.print('Bifrost MCP tools:',len(names))
                    for name in names:console.print('  '+registry[name].description,markup=False)
                else:console.print('Bifrost MCP: disabled')
            finally:model.close()
            console.print('Native filesystem/shell access uses your OS account. File tools constrain paths to the selected root.');return 0
        if args.command=='logout':
            try:
                if db.token:db.call('logout')
                console.print('CLI session revoked and local login cleared.')
            finally:delete_token(config)
            return 0
        if not db.token:raise ValueError('Login first: agent-cli login')
        user=db.call('me')
        if args.command=='whoami':print(json.dumps(user,ensure_ascii=False));return 0
        if args.command=='turns':print(json.dumps(db.call('turns',thread_id=args.thread_id),ensure_ascii=False,indent=2));return 0
        if args.command=='turn':print(json.dumps(db.call('turn',thread_id=args.thread_id,turn_id=args.turn_id),ensure_ascii=False,indent=2));return 0
        if args.command=='models':
            model=Model(config)
            try:
                response=model.client.get('models');response.raise_for_status();print(json.dumps(response.json(),ensure_ascii=False,indent=2))
            finally:model.close()
            return 0
        if args.command=='threads':
            table=Table('ID','Title','Updated')
            for row in db.call('threads'):table.add_row(row['id'],row['title'],row['updated_at'])
            console.print(table);return 0
        if args.command=='history':print(json.dumps(db.call('messages',thread_id=args.thread_id),ensure_ascii=False,indent=2));return 0
        if args.command=='memory':
            memory=db.call('memory_get')
            if args.set_file:db.call('memory_set',content=args.set_file.read_text(encoding='utf-8'),version=memory['version']);console.print('Memory saved.')
            else:print(memory['content'])
            return 0
        workspace=Workspace(args.root);model=Model(config);tools=Tools(workspace,db,user,model)
        if args.command=='process':
            manager=tools.processes
            result=manager.send(args.job_id,stop=True,force=args.force) if args.stop and args.job_id else manager.status(args.job_id) if args.job_id else manager.listing()
            print(json.dumps(result,ensure_ascii=False,indent=2));model.close();return 0
        pointer=state_dir()/'workspaces'/(str(user['user_id'])+'-'+workspace.key+'.json')
        thread=getattr(args,'thread',None)
        if not thread and not getattr(args,'new',False) and pointer.exists():thread=json.loads(pointer.read_text())['thread_id']
        if thread:
            db.call('messages',thread_id=thread) # verify ownership before recording/resuming
        else:thread=db.call('new_thread')['id']
        private_write(pointer,json.dumps({'thread_id':thread,'root':str(workspace.root)}))
        console.print('[bold]Agent CLI[/bold] ·',user['username'],'·',str(workspace.root))
        console.print('Thread:',thread,'· local tools use your account rights')
        indicator=[None]
        def event(kind,value):
            text=('Executing '+value+' …') if kind=='tool' else ('Turn: '+value) if kind=='turn' else value
            if indicator[0] is not None:indicator[0].update(text)
            if kind!='status' or not console.is_terminal:console.print(('→ '+value) if kind=='tool' else text,style='dim')
        agent=Agent(config,db,workspace,model,tools,event)
        def run_turn(message):
            with console.status('Agent is working …',spinner='dots') as status:
                indicator[0]=status
                try:return agent.run(thread,message)
                finally:indicator[0]=None
        try:
            tools.initialize_mcp(config)
            if config.bifrost_mcp_enabled:console.print('Bifrost MCP tools:',len(tools.mcp_names))
            message=getattr(args,'message',None)
            if message is not None:
                answer=run_turn(message)
                if getattr(args,'json',False):print(json.dumps({'thread_id':thread,'answer':answer},ensure_ascii=False))
                else:Console().print(Markdown(answer))
            else:
                console.print('Commands: /new, /threads, /use UUID, /memory, /jobs, /exit. Ctrl+C interrupts this turn; background jobs remain.')
                while True:
                    try:message=input('\nYou > ').strip()
                    except (EOFError,KeyboardInterrupt):break
                    if not message:continue
                    if message in ('/exit','/quit'):break
                    if message=='/new':thread=db.call('new_thread')['id'];private_write(pointer,json.dumps({'thread_id':thread}));console.print('Thread:',thread);continue
                    if message=='/help':console.print('Commands: /new, /threads, /use UUID, /memory, /jobs, /model NAME, /exit');continue
                    if message.startswith('/model '):config.model=message[7:].strip();console.print('Model for this session:',config.model);continue
                    if message.startswith('/use '):
                        candidate=message[5:].strip()
                        try:db.call('messages',thread_id=candidate)
                        except Exception as exc:console.print(safe_error(exc,config),markup=False,style='red');continue
                        thread=candidate;private_write(pointer,json.dumps({'thread_id':thread}));console.print('Thread:',thread);continue
                    if message=='/threads':console.print(db.call('threads'));continue
                    if message=='/memory':console.print(db.call('memory_get')['content']);continue
                    if message=='/jobs':console.print(tools.processes.listing());continue
                    try:Console().print(Markdown(run_turn(message)))
                    except KeyboardInterrupt:console.print('Turn interrupted. Local background processes were not automatically killed.')
                    except Exception as exc:console.print(safe_error(exc,config),markup=False,style='red')
        finally:model.close()
        return 0
    except KeyboardInterrupt:return 130
    except Exception as exc:
        if getattr(args,'debug',False):traceback.print_exc()
        else:console.print(safe_error(exc,locals().get('config')),markup=False,style='red')
        return 1


def safe_error(exc,config=None):
    text=type(exc).__name__+': '+str(exc)
    if config:
        for secret in (config.database_url,config.api_key,config.bifrost_mcp_api_key):
            if secret:text=text.replace(secret,'[redacted]')
    return text


if __name__=='__main__':raise SystemExit(main())
