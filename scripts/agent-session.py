#!/usr/bin/env python3
"""Issue/revoke a live browser session through existing operator SSH access.

No deployment or public login endpoint is needed. Only the session row changes.
Credentials are transferred straight into a mode-0600 file, never printed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('issue', 'revoke'))
    parser.add_argument('--email', help='Existing allowlisted account; required for issue.')
    parser.add_argument('--minutes', type=int, default=120)
    parser.add_argument('--state', type=Path, default=ROOT / '.dev/agent-browser.json')
    parser.add_argument('--host', default='ubuntu@54.37.204.161')
    parser.add_argument('--ssh-key', type=Path, default=Path.home() / '.ssh/qoc_vps_ed25519')
    parser.add_argument('--container', default='moresocial-web-1')
    args = parser.parse_args()
    if args.command == 'issue' and (not args.email or not 1 <= args.minutes <= 1440):
        parser.error('issue requires --email and --minutes between 1 and 1440')
    state = args.state.expanduser().absolute()
    metadata = state.with_suffix('.session.json')
    if metadata == state:
        parser.error('--state must not end in .session.json')
    module = (ROOT / 'app/agent_session.py').read_bytes()
    ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
           '-i', str(args.ssh_key.expanduser()), args.host]

    def remote(command, *, code=None, stdout=subprocess.PIPE):
        return subprocess.run(ssh + [shlex.join(command)], input=code, stdout=stdout, check=True)

    def run_module(*command):
        completed = remote(['sudo', 'docker', 'exec', '-i', args.container, 'python', '-', *command], code=module)
        return json.loads(completed.stdout)

    if args.command == 'revoke':
        info = json.loads(metadata.read_text())
        session_id = str(uuid.UUID(info['session_id']))
        result = run_module('revoke', '--session-id', session_id)
        state.unlink(missing_ok=True)
        metadata.unlink()
        print(json.dumps(result))
        return

    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    remote_path = '/tmp/moresocial-agent-' + uuid.uuid4().hex + '.json'
    info = None
    state_created = metadata_created = False
    try:
        # Reserve both local paths first. Refuse overwrite, including symlinks.
        state_fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        state_created = True
        with os.fdopen(state_fd, 'wb') as f:
            metadata_fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            metadata_created = True
            with os.fdopen(metadata_fd, 'w') as meta:
                info = run_module('issue', '--email', args.email, '--minutes', str(args.minutes),
                                  '--output', remote_path)
                # Retain the non-secret ID before downloading so failures can be revoked.
                info['output'] = str(state)
                json.dump(info, meta)
                meta.write('\n')
                meta.flush()
                os.fsync(meta.fileno())
                remote(['sudo', 'docker', 'exec', args.container, 'cat', remote_path], stdout=f)
                f.flush()
                os.fsync(f.fileno())
        data = json.loads(state.read_text())
        assert len(data['cookies']) == 1 and data['cookies'][0]['name'] == 'moresocial_session'
    except BaseException:
        if info:
            try:
                run_module('revoke', '--session-id', str(uuid.UUID(info['session_id'])))
            except Exception:
                # Keep the metadata so the operator can retry revocation; TTL still applies.
                print('Session cleanup failed. Retry revoke using the saved metadata file.', file=sys.stderr)
                metadata_created = False
        if state_created:
            state.unlink(missing_ok=True)
        if metadata_created:
            metadata.unlink(missing_ok=True)
        raise
    finally:
        if info:
            remote(['sudo', 'docker', 'exec', args.container, 'rm', '-f', remote_path], stdout=subprocess.DEVNULL)
    print(json.dumps(info))


if __name__ == '__main__':
    main()
