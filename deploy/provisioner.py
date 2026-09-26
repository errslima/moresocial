#!/usr/bin/env python3
"""Moresocial connector provisioner (host service `moresocial-connectors.service`).

Reconciles per-workspace WhatsApp connector containers with the desired state recorded by
the web app, read from the private management API on 127.0.0.1:8773 with the operator key.
No Docker socket is ever given to web, worker or connector containers.

Safety rules:
- Workspace IDs must be 32 lowercase hex characters; they are the only variable part of
  container, network and directory names. No user-supplied path or shell fragment is used.
- Only resources labelled moresocial.managed=1 / named ms-wa-<id> are touched.
- If the management API cannot be read, nothing is stopped or deleted.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

WORKSPACE = re.compile(r'^[0-9a-f]{32}$')
IMAGE = re.compile(r'^[a-z0-9][a-z0-9._/-]{0,127}(:[A-Za-z0-9._-]{1,128})?(@sha256:[0-9a-f]{64})?$')
API = os.environ.get('MORESOCIAL_MANAGEMENT_URL', 'http://127.0.0.1:8773')
KEY_FILE = Path(os.environ.get('MORESOCIAL_OPERATOR_KEY_FILE', '/srv/moresocial/secrets/operator_key'))
BASE = Path(os.environ.get('MORESOCIAL_WHATSAPP_DIR', '/srv/moresocial/data/whatsapp'))
WEB_CONTAINER = os.environ.get('MORESOCIAL_WEB_CONTAINER', 'moresocial-web-1')
# Persistent diagnostic store (deploy/logs.py). Used only when the directory exists.
LOGS = Path(os.environ.get('MORESOCIAL_LOG_DIR', '/srv/moresocial/logs'))
CONNECTOR_UID = 1000
LIMITS = {'memory': os.environ.get('CONNECTOR_MEMORY', '1536m'), 'pids': os.environ.get('CONNECTOR_PIDS', '256'),
          'shm': '256m', 'cpus': os.environ.get('CONNECTOR_CPUS', '1.0')}


LOG_FIELDS = {'count': int, 'running': int, 'stopped': int, 'deleted': int, 'orphans': int, 'status': int,
              'error_type': str, 'code': str, 'outcome': str, 'workspace': str, 'release': str}


def log(event: str, level: str = 'info', **fields) -> None:
    """One JSON event in the schema shared with the app (app/diagnostics.py); journald keeps it."""
    row = {'schema': 1, 'ts': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
           'level': level, 'service': 'provisioner', 'event': event, 'reference': secrets.token_hex(8)}
    for key, value in fields.items():
        kind = LOG_FIELDS.get(key)
        if kind is int and isinstance(value, int) and not isinstance(value, bool):
            row[key] = value
        elif kind is str and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', value):
            if key == 'workspace' and WORKSPACE.match(value):
                row['workspace_id'] = str(uuid.UUID(value))  # the app's opaque workspace UUID
            elif key != 'workspace':
                row[key] = value
    line = json.dumps(row, separators=(',', ':'))
    print(line, flush=True)
    try:
        if (LOGS / 'host').is_dir():
            fd = os.open(LOGS / 'host' / f'provisioner-{datetime.now(timezone.utc):%Y%m%d}.jsonl',
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (line + '\n').encode())
            finally:
                os.close(fd)
    except OSError:
        pass  # logging is never a prerequisite for reconciling


def image_release(image: str) -> str | None:
    tag = image.rsplit(':', 1)[-1] if ':' in image.split('/')[-1] else ''
    return tag if re.fullmatch(r'[0-9a-f]{7,40}', tag) else None


class Docker:
    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(['docker', *args], check=check, capture_output=True, text=True, timeout=180)


def names(ws: str) -> tuple[str, str]:
    if not WORKSPACE.match(ws):
        raise ValueError('invalid workspace id')
    return f'ms-wa-{ws}', f'ms-wa-{ws}'


def workspace_dir(ws: str) -> Path:
    if not WORKSPACE.match(ws):
        raise ValueError('invalid workspace id')
    path = (BASE / ws).resolve()
    if path.parent != BASE.resolve():
        raise ValueError('path escapes the WhatsApp data directory')
    return path


class Provisioner:
    def __init__(self, docker: Docker | None = None, fetch=None, report=None, base: Path | None = None,
                 logs: Path | None = None):
        self.docker = docker or Docker()
        self.fetch = fetch or self._fetch
        self.report = report or self._report
        self.orphan_seen: dict[str, int] = {}
        global BASE, LOGS
        if base is not None:
            BASE = base
        if logs is not None:
            LOGS = logs

    def diagnostics_dir(self, ws: str) -> Path | None:
        """The connector's persistent event directory, when the host log store exists."""
        connectors = LOGS / 'connectors'
        if not LOGS.is_dir():
            return None
        path = connectors / ws
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._chown(path)  # the bind mount itself must be writable by the connector uid
        return path

    # ---- management API
    def _key(self) -> str:
        return KEY_FILE.read_text().strip()

    def _fetch(self) -> dict:
        req = urllib.request.Request(API + '/internal/manage/connectors', headers={'Authorization': 'Bearer ' + self._key()})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def _report(self, ws: str, state: str, detail: str | None = None) -> None:
        body = json.dumps({'state': state, **({'detail': detail} if detail else {})}).encode()
        req = urllib.request.Request(f'{API}/internal/manage/connectors/{ws}/observed', data=body, method='POST',
                                     headers={'Authorization': 'Bearer ' + self._key(), 'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=15).read()

    # ---- docker state
    def managed(self) -> set[str]:
        out = self.docker.run('ps', '-a', '--filter', 'label=moresocial.managed=1', '--format', '{{.Names}}').stdout
        return {n[len('ms-wa-'):] for n in out.split() if n.startswith('ms-wa-') and WORKSPACE.match(n[len('ms-wa-'):])}

    def state(self, container: str) -> str | None:
        r = self.docker.run('inspect', '--format', '{{.State.Status}}', container, check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    # ---- actions
    def ensure_running(self, ws: str, key: str, image: str) -> str:
        if not IMAGE.match(image or ''):
            raise ValueError('invalid connector image reference')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,100}', key or ''):
            raise ValueError('invalid connector key')
        container, network = names(ws)
        root = workspace_dir(ws)
        session, secret = root / 'session', root / 'secret'
        for d in (root, session, secret):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_file = secret / 'connector_key'
        if not key_file.exists() or key_file.read_text().strip() != key:
            tmp = secret / 'connector_key.tmp'
            tmp.write_text(key)
            tmp.chmod(0o400)
            os.replace(tmp, key_file)
        self._chown(root, session, secret, key_file)
        if self.docker.run('network', 'inspect', network, check=False).returncode != 0:
            self.docker.run('network', 'create', '--driver', 'bridge', '--label', 'moresocial.managed=1',
                            '--label', f'moresocial.workspace={ws}', network)
        self._attach_web(network)
        diagnostics = self.diagnostics_dir(ws)
        options = ['--network', network, '--restart', 'unless-stopped',
                   '--label', 'moresocial.managed=1', '--label', f'moresocial.workspace={ws}',
                   '--read-only', '--tmpfs', '/tmp:uid=1000,gid=1000,mode=1777',
                   '--tmpfs', '/home/node:uid=1000,gid=1000,mode=0700',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                   '--memory', LIMITS['memory'], '--memory-swap', LIMITS['memory'], '--pids-limit', LIMITS['pids'],
                   '--cpus', LIMITS['cpus'], '--shm-size', LIMITS['shm'],
                   '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
                   '-e', 'WEB_INTERNAL_URL=http://web:8001', '-e', 'XDG_CONFIG_HOME=/tmp/chromium-config',
                   '-e', 'XDG_CACHE_HOME=/tmp/chromium-cache',
                   '-v', f'{session}:/session', '-v', f'{key_file}:/run/secrets/connector_key:ro']
        if diagnostics is not None:
            options += ['-e', 'DIAGNOSTICS_DIR=/diagnostics', '-v', f'{diagnostics}:/diagnostics']
        # The container spec (excluding the image, which is compared by ID) is pinned in a
        # label, so a changed mount or limit also replaces the container; the session is kept.
        spec = hashlib.sha256('\0'.join(options).encode()).hexdigest()[:16]
        current = self.state(container)
        if current is not None:
            # Verify the replacement exists before stopping a working connector.
            # Compare image IDs as well as references so a retagged image is noticed.
            wanted_image = self.docker.run('image', 'inspect', '--format', '{{.Id}}', image).stdout.strip()
            actual_image = self.docker.run('inspect', '--format', '{{.Image}}', container).stdout.strip()
            if not wanted_image or not actual_image:
                raise ValueError('cannot verify connector image identity')
            actual_spec = self.docker.run('inspect', '--format', '{{index .Config.Labels "moresocial.spec"}}',
                                          container, check=False).stdout.strip()
            if actual_image != wanted_image or actual_spec != spec:
                self.docker.run('stop', '--time', '30', container)
                self.docker.run('rm', container)
                log('connector_replaced', workspace=ws, release=image_release(image),
                    code='image' if actual_image != wanted_image else 'spec')
                current = None
        if current is None:
            self.docker.run('run', '-d', '--name', container, *options, '--label', f'moresocial.spec={spec}',
                            '-e', 'MORESOCIAL_RELEASE=' + (image_release(image) or ''), image)
            log('connector_started', workspace=ws, release=image_release(image), outcome='created')
            return 'starting'
        if current != 'running':
            self.docker.run('start', container)
            log('connector_started', workspace=ws, outcome='restarted', code=current)
            return 'starting'
        return 'running'

    def stop(self, ws: str) -> None:
        container, network = names(ws)
        if self.state(container) is not None:
            self.docker.run('rm', '-f', container)
            log('connector_stopped', workspace=ws)
        self._detach_web(network)
        self.docker.run('network', 'rm', network, check=False)

    def delete(self, ws: str) -> None:
        self.stop(ws)
        root = workspace_dir(ws)
        if root.exists():
            shutil.rmtree(root)
            log('connector_session_deleted', workspace=ws)

    def _attach_web(self, network: str) -> None:
        nets = self.docker.run('inspect', '--format', '{{json .NetworkSettings.Networks}}', WEB_CONTAINER, check=False)
        if nets.returncode == 0 and network not in json.loads(nets.stdout or '{}'):
            self.docker.run('network', 'connect', '--alias', 'web', network, WEB_CONTAINER)

    def _detach_web(self, network: str) -> None:
        self.docker.run('network', 'disconnect', network, WEB_CONTAINER, check=False)

    def _chown(self, *paths: Path) -> None:
        if os.geteuid() == 0:
            for p in paths:
                os.chown(p, CONNECTOR_UID, CONNECTOR_UID)

    # ---- reconcile
    def reconcile(self) -> dict:
        try:
            data = self.fetch()
        except Exception as exc:
            log('management_api_unavailable', 'warn', error_type=type(exc).__name__)
            return {'skipped': True}
        rows = [r for r in data.get('connectors', []) if isinstance(r, dict) and WORKSPACE.match(str(r.get('workspace', '')))]
        wanted = {r['workspace']: r for r in rows}
        outcome = {'running': 0, 'stopped': 0, 'deleted': 0, 'orphans': 0}
        for ws, row in wanted.items():
            try:
                if row['desired'] == 'running':
                    state = self.ensure_running(ws, row.get('key') or '', data.get('image', ''))
                    outcome['running'] += 1
                    if state == 'starting' or row.get('observed') in ('pending', 'stopped', 'removed'):
                        self.report(ws, 'starting')
                elif row['desired'] == 'stopped':
                    self.stop(ws)
                    outcome['stopped'] += 1
                    if row.get('observed') != 'stopped':
                        self.report(ws, 'stopped')
                elif row['desired'] == 'deleted':
                    self.delete(ws)
                    outcome['deleted'] += 1
                    self.report(ws, 'removed')
            except Exception as exc:
                log('connector_reconcile_failed', 'error', workspace=ws, error_type=type(exc).__name__)
                try:
                    self.report(ws, 'error', 'The WhatsApp connection could not be started; it will retry.')
                except Exception:
                    pass
        # Workspaces deleted in the app (account deletion) leave orphans. Remove one only when the
        # listing is marked complete, the database is not empty, and it was missing three times in
        # a row, so a failed restore or a transient bug cannot wipe WhatsApp sessions.
        dirs = {p.name for p in BASE.iterdir() if p.is_dir() and WORKSPACE.match(p.name)} if BASE.exists() else set()
        orphans = (self.managed() | dirs) - set(wanted)
        if not data.get('complete') or not data.get('workspace_count'):
            if orphans:
                log('orphan_cleanup_skipped', 'warn', count=len(orphans))
            return outcome
        self.orphan_seen = {ws: self.orphan_seen.get(ws, 0) + 1 for ws in orphans}
        for ws in sorted(w for w, n in self.orphan_seen.items() if n >= 3):
            try:
                self.delete(ws)
                outcome['orphans'] += 1
                self.orphan_seen.pop(ws, None)
            except Exception as exc:
                log('orphan_cleanup_failed', 'error', workspace=ws, error_type=type(exc).__name__)
        return outcome


def main() -> None:
    os.umask(0o077)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = Provisioner()
    interval = int(os.environ.get('PROVISION_INTERVAL', '10'))
    log('provisioner_started')
    while True:
        result = p.reconcile()
        if any(result.get(k) for k in ('deleted', 'orphans')):
            log('reconciled', **{k: v for k, v in result.items() if isinstance(v, int)})
        time.sleep(interval)


if __name__ == '__main__':
    sys.exit(main())
