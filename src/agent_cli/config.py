from dataclasses import dataclass, asdict, fields
import json
import os
import tempfile
import time
from pathlib import Path
from platformdirs import user_config_path, user_state_path


def config_dir():return Path(os.getenv('AGENT_CLI_CONFIG_DIR',str(user_config_path('zpiz-agent-cli'))))
def state_dir():
    p=Path(os.getenv('AGENT_CLI_STATE_DIR',str(user_state_path('zpiz-agent-cli'))));p.mkdir(parents=True,exist_ok=True,mode=0o700);return p


def private_write(path,text):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as handle:handle.write(text);handle.flush();os.fsync(handle.fileno())
        for attempt in range(20):
            try:temporary.replace(path);break
            except PermissionError:
                if os.name!='nt' or attempt==19:raise
                time.sleep(.025)
        if os.name!='nt':path.chmod(0o600)
    finally:
        if temporary.exists():temporary.unlink()



@dataclass
class Config:
    database_url:str=''
    bifrost_url:str='http://localhost:8080/openai'
    model:str='openai/gpt-4o-mini'
    api_key:str=''
    vision_model:str=''
    context_tokens:int=128000
    max_output_tokens:int=8192
    vision_output_tokens:int=8192
    request_timeout:float=500
    max_tool_rounds:int=100
    thinking_enabled:bool=False
    thinking_control:str='chat_template_kwargs'
    verify_tls:bool=True
    ca_file:str=''
    bifrost_mcp_enabled:bool=False
    bifrost_mcp_url:str=''
    bifrost_mcp_api_key:str=''

    @classmethod
    def load(cls):
        path=config_dir()/'config.json';raw=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        allowed={f.name for f in fields(cls)}
        if set(raw)-allowed:raise ValueError('Unknown config fields: '+', '.join(set(raw)-allowed))
        for env,key in [('AGENT_CLI_DATABASE_URL','database_url'),('BIFROST_BASE_URL','bifrost_url'),('BIFROST_MODEL','model'),('BIFROST_API_KEY','api_key'),('BIFROST_MCP_URL','bifrost_mcp_url'),('BIFROST_MCP_API_KEY','bifrost_mcp_api_key')]:
            if os.environ.get(env):raw[key]=os.environ[env]
        if 'BIFROST_MCP_ENABLED' in os.environ:
            value=os.environ['BIFROST_MCP_ENABLED'].strip().lower()
            if value not in ('true','false','1','0','yes','no','on','off'):raise ValueError('Invalid BIFROST_MCP_ENABLED')
            raw['bifrost_mcp_enabled']=value in ('true','1','yes','on')
        obj=cls(**raw)
        if type(obj.bifrost_mcp_enabled) is not bool:raise ValueError('bifrost_mcp_enabled must be a boolean')
        for key in ('context_tokens','max_output_tokens','vision_output_tokens'):
            if type(getattr(obj,key)) is not int or getattr(obj,key)<1:raise ValueError(key+' must be a positive integer')
        if obj.context_tokens<4096:raise ValueError('context_tokens must be at least 4096')
        if obj.request_timeout<=0 or obj.max_tool_rounds<0:raise ValueError('Invalid timeout/tool-round setting')
        if obj.thinking_control not in ('chat_template_kwargs','reasoning_effort','none'):raise ValueError('Invalid thinking_control')
        return obj

    def save(self):private_write(config_dir()/'config.json',json.dumps(asdict(self),ensure_ascii=False,indent=2)+'\n')


def session_key(config):
    import hashlib
    return hashlib.sha256(config.database_url.encode()).hexdigest()


def save_token(config,token):
    # Never borrow another application's session. Only store this CLI's own login token.
    try:
        if os.getenv('AGENT_CLI_TOKEN_STORE')=='file':raise RuntimeError('File token store selected')
        import keyring
        keyring.set_password('zpiz-agent-cli',session_key(config),token)
        fallback=config_dir()/('session-'+session_key(config)+'.json')
        if fallback.exists():fallback.unlink()
        return 'OS credential store'
    except Exception:
        private_write(config_dir()/('session-'+session_key(config)+'.json'),json.dumps({'guid':token}))
        return 'private user configuration file'


def get_token(config):
    try:
        if os.getenv('AGENT_CLI_TOKEN_STORE')=='file':raise RuntimeError('File token store selected')
        import keyring
        token=keyring.get_password('zpiz-agent-cli',session_key(config))
        if token:return token
    except Exception:pass
    path=config_dir()/('session-'+session_key(config)+'.json')
    try:return json.loads(path.read_text())['guid'] if path.exists() else None
    except (OSError,ValueError,KeyError):return None


def delete_token(config):
    try:
        if os.getenv('AGENT_CLI_TOKEN_STORE')=='file':raise RuntimeError('File token store selected')
        import keyring
        keyring.delete_password('zpiz-agent-cli',session_key(config))
    except Exception:pass
    path=config_dir()/('session-'+session_key(config)+'.json')
    if path.exists():path.unlink()
