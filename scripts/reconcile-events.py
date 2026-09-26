#!/usr/bin/env python3
"""Event identity repair and reporting (dry-run unless --apply).

Runs `python -m app.event_reconcile` in the live web container through operator SSH, or
locally with --local (uses DATABASE_URL). All changes go through app.event_identity, the
same service the web UI uses; the JSON report contains IDs, counts, times and digests only.

Examples:
  python3 scripts/reconcile-events.py report
  python3 scripts/reconcile-events.py list --email you@example.com --date 2026-09-27
  python3 scripts/reconcile-events.py same --email you@example.com --events A,B,C          # dry run
  python3 scripts/reconcile-events.py same --email you@example.com --events A,B,C --apply
  python3 scripts/reconcile-events.py mode --value observe --apply
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get('MORESOCIAL_HOST', 'ubuntu@54.37.204.161')
KEY = Path(os.environ.get('MORESOCIAL_SSH_KEY', Path.home() / '.ssh/qoc_vps_ed25519'))
CONTAINER = os.environ.get('MORESOCIAL_CONTAINER', 'moresocial-web-1')


def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    if '--local' in args:
        args.remove('--local')
        return subprocess.call([sys.executable, '-m', 'app.event_reconcile', *args], cwd=ROOT)
    command = ['sudo', 'docker', 'exec', CONTAINER, 'python', '-m', 'app.event_reconcile', *args]
    ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-i', str(KEY.expanduser()), HOST]
    return subprocess.call(ssh + [shlex.join(command)])


if __name__ == '__main__':
    sys.exit(main())
