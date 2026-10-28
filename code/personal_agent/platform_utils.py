"""Small platform boundaries; Windows inherits the containing directory's ACL."""

import os
import sys

IS_WINDOWS = os.name == "nt"


def private_file_permissions(fd):
    """Keep POSIX secrets at 0600; chmod is not a Windows ACL mechanism."""
    if not IS_WINDOWS:
        os.fchmod(fd, 0o600)


def configure_stdio():
    """JSON and Chinese CLI output use UTF-8, including redirected Windows output."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
