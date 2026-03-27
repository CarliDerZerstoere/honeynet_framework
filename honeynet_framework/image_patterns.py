"""
Shared image classification patterns.

Contains universal patterns (binary names, file extensions) that are
NOT image-specific.  Image-specific classification (daemon detection,
required env vars, exposed ports) is handled dynamically by
``image_introspector.ImageProfile`` via OCI registry introspection.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# One-shot / batch binaries that run once and exit.  If the *first* token
# in a container command matches, the container is not a long-running daemon.
# ---------------------------------------------------------------------------
ONE_SHOT_BINARIES: frozenset[str] = frozenset({
    "restic", "pg_dump", "pg_dumpall", "pg_restore",
    "mysqldump", "mysqlpump", "mongodump", "mongorestore",
    "tar", "gzip", "gunzip", "bzip2", "xz", "zip", "unzip",
    "rsync", "cp", "mv", "rm", "scp",
    "curl", "wget",  # as main process — fetches then exits
    "echo", "cat", "true", "false", "test",
    "certbot",  # runs once to issue/renew certs
})

# ---------------------------------------------------------------------------
# Script-like file extensions.
# ---------------------------------------------------------------------------
SCRIPT_EXTENSIONS = re.compile(r'\.(py|js|ts|rb|sh|bash|pl|php|go|java|rs)$', re.IGNORECASE)

# Module/runtime entry-point patterns (python, node, ruby, php).
MODULE_LAUNCHERS: frozenset[str] = frozenset({"python", "python3", "node", "ruby", "php"})

LIKELY_BUILTIN_PYTHON_MODULES: frozenset[str] = frozenset({
    "http.server", "json.tool", "venv", "pydoc", "unittest", "asyncio",
})
