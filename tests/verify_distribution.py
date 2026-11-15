"""Verify the built wheel, then run full integration from that wheel, not editable sources."""
import importlib.resources
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def main():
    root=Path(__file__).resolve().parents[1]
    wheel=Path(sys.argv[1]) if len(sys.argv)>1 else root/'dist'/'zpiz_agent_cli-0.1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel) as package:
        names=package.namelist()
        assert 'agent_cli/schema.sql' in names
        assert 'agent_cli/worker.py' in names
        assert not any('/.env' in name or name.endswith('config.json') or name.startswith('agent_api/') for name in names)
    with tempfile.TemporaryDirectory() as folder:
        subprocess.run([sys.executable,'-m','pip','install','--no-index','--no-deps','--target',folder,str(wheel)],check=True,stdout=subprocess.DEVNULL)
        env=os.environ.copy();env['PYTHONPATH']=folder
        check='import agent_cli,sys;from pathlib import Path;from importlib.resources import files;assert Path(agent_cli.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve());assert files("agent_cli").joinpath("schema.sql").is_file();print("Installed wheel and packaged SQL: OK")'
        subprocess.run([sys.executable,'-c',check,folder],check=True,env=env,cwd=folder)
        subprocess.run([sys.executable,'-m','agent_cli','--help'],check=True,env=env,cwd=folder,stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable,str(root/'tests'/'integration_database.py')],check=True,env=env,cwd=folder)

if __name__=='__main__':main()
