"""Persistent board pairing token. Never returned by a public HTTP endpoint."""
import os
import re
import secrets
import stat
from pathlib import Path


def read_token(path):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('Pairing file must be a regular file with mode 0600')
        value = os.read(fd, 256).decode('ascii').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', value):
            raise ValueError('Invalid pairing file; not replacing an existing token')
        return value
    finally:
        os.close(fd)


def load_or_create(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError('Token directory must not be a symlink')
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        return read_token(path)
    with os.fdopen(fd, 'w', encoding='ascii') as stream:
        stream.write(token + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return read_token(path)
