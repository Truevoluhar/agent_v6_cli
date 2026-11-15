import hashlib
from pathlib import Path


class Workspace:
    def __init__(self,root):
        self.root=Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():raise ValueError('Workspace must be a directory')
        self.key=hashlib.sha256(str(self.root).encode()).hexdigest()[:20]

    def path(self,value='.'):
        p=Path(value).expanduser()
        resolved=(p if p.is_absolute() else self.root/p).resolve()
        if not resolved.is_relative_to(self.root):raise PermissionError('Path is outside the selected workspace (including symlink targets)')
        return resolved

    def entry(self,value):
        # Validate the parent, but keep the final directory entry for unlink/rename of symlinks.
        p=Path(value).expanduser()
        p=p if p.is_absolute() else self.root/p
        parent=p.parent.resolve()
        if not parent.is_relative_to(self.root):raise PermissionError('Entry parent is outside workspace')
        if p.name in ('.','..'):raise PermissionError('Ambiguous directory entry')
        return parent/p.name
