"""Image rollback refuses incompatible schema before modifying release state."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('removed,compatible,success,assistant,assistant_image', [
    ('t', False, False, 'f', True), ('t', True, True, 'f', True), ('f', False, True, 'f', True),
    # Schema 0014 present: an image without the assistant migration is refused.
    ('t', True, False, 't', False), ('t', True, True, 't', True)])
def test_rollback_checks_schema_before_changing_release(tmp_path, removed, compatible, success, assistant, assistant_image):
    root = tmp_path / 'moresocial'
    old = root / 'releases' / 'aaaaaaa' / 'src'
    target = root / 'releases' / 'bbbbbbb' / 'src'
    old.mkdir(parents=True)
    target.mkdir(parents=True)
    (root / 'src').symlink_to(old)
    release = root / 'release.env'
    release.write_text('MORESOCIAL_REVISION=aaaaaaa\n')
    bins = tmp_path / 'bin'
    bins.mkdir()
    docker = bins / 'docker'
    docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$ROLLBACK_CALLS"
case "$1" in
  exec) case "$*" in *producer*) printf '%s\\n' "$ASSISTANT_SCHEMA" ;; *) printf '%s\\n' "$SCHEMA_REMOVED" ;; esac ;;
  run) case "$*" in *0014_assistant*) exit "$ASSISTANT_IMAGE_STATUS" ;; *) exit "$IMAGE_STATUS" ;; esac ;;
esac
''')
    docker.chmod(0o755)
    for name in ('systemctl', 'curl'):
        stub = bins / name
        stub.write_text('#!/bin/sh\nexit 0\n')
        stub.chmod(0o755)
    # The deployment host uses GNU mv; the workstation may have BSD mv without -T.
    move = bins / 'mv'
    move.write_text(f'#!{sys.executable}\nimport os, sys\nos.replace(sys.argv[-2], sys.argv[-1])\n')
    move.chmod(0o755)
    calls = tmp_path / 'calls'
    env = {**os.environ, 'PATH': str(bins) + ':' + os.environ['PATH'], 'MORESOCIAL_ROOT': str(root),
           'ROLLBACK_CALLS': str(calls), 'SCHEMA_REMOVED': removed, 'IMAGE_STATUS': '0' if compatible else '1',
           'ASSISTANT_SCHEMA': assistant, 'ASSISTANT_IMAGE_STATUS': '0' if assistant_image else '1'}
    result = subprocess.run(['bash', str(Path(__file__).resolve().parents[1] / 'deploy/rollback.sh'), 'bbbbbbb'],
                            env=env, capture_output=True, text=True)
    if success:
        assert result.returncode == 0, result.stderr
        assert release.read_text() == 'MORESOCIAL_REVISION=bbbbbbb\n'
        assert (root / 'src').resolve() == target
        assert 'compose ' in calls.read_text()
    else:
        assert result.returncode == 2
        assert 'coordinated downgrade' in result.stderr
        assert release.read_text() == 'MORESOCIAL_REVISION=aaaaaaa\n'
        assert (root / 'src').resolve() == old
        assert 'compose ' not in calls.read_text()
