"""Dry-run deployment guards; never connects to Docker or provider accounts."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deployment/scripts/deploy-service.sh'
DIGEST = 'a' * 64


def _invoke(tmp_path, service, target, image):
    env_dir = tmp_path / target
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / f'{service}.env').write_text('TEST_ONLY=dummy\n')
    return subprocess.run(
        ['bash', str(SCRIPT), service, target, image, 'dry-run'],
        env={**os.environ, 'ALGOT_DEPLOY_CONFIG_ROOT': str(tmp_path)},
        text=True, capture_output=True, check=False,
    )


def test_database_staging_dry_run_uses_isolated_volume(tmp_path):
    result = _invoke(tmp_path, 'database', 'staging', f'ghcr.io/jb213456/algot-database@sha256:{DIGEST}')
    assert result.returncode == 0, result.stderr
    assert 'algot-staging-market-data-lake' in result.stdout
    assert 'algot-staging-net' in result.stdout


def test_ingestion_production_dry_run_preserves_checkpoint_volume(tmp_path):
    result = _invoke(tmp_path, 'ingestion', 'production', f'ghcr.io/jb213456/algot-ingestion@sha256:{DIGEST}')
    assert result.returncode == 0, result.stderr
    assert 'algot-production-ingestion-checkpoints' in result.stdout


def test_mismatched_image_is_rejected(tmp_path):
    result = _invoke(tmp_path, 'database', 'staging', f'ghcr.io/jb213456/algot-ingestion@sha256:{DIGEST}')
    assert result.returncode != 0


def _mock_docker(tmp_path, *, old=False, run_fails=False):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    mock = bin_dir / 'docker'
    mock.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$MOCK_DOCKER_LOG"
if [[ "$1 $2" == 'container inspect' ]]; then
    [[ "$MOCK_OLD" == 1 ]]
    exit $?
fi
if [[ "$1 $2" == 'network inspect' || "$1 $2" == 'volume inspect' ]]; then exit 1; fi
if [[ "$1" == run && "$MOCK_RUN_FAIL" == 1 ]]; then exit 1; fi
if [[ "$1" == inspect ]]; then echo healthy; fi
exit 0
''')
    mock.chmod(0o755)
    return bin_dir


def test_mock_replacement_audits_without_deleting_volume(tmp_path):
    env_dir = tmp_path / 'config/staging'
    env_dir.mkdir(parents=True)
    (env_dir / 'database.env').write_text('TEST_ONLY=dummy\n')
    bin_dir = _mock_docker(tmp_path)
    log = tmp_path / 'docker.log'
    state = tmp_path / 'state'
    result = subprocess.run(
        ['bash', str(SCRIPT), 'database', 'staging', f'ghcr.io/jb213456/algot-database@sha256:{DIGEST}'],
        env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
             'ALGOT_DEPLOY_CONFIG_ROOT': str(tmp_path / 'config'),
             'ALGOT_DEPLOY_STATE_ROOT': str(state), 'MOCK_DOCKER_LOG': str(log),
             'MOCK_OLD': '0', 'MOCK_RUN_FAIL': '0'},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert 'result=healthy' in (state / 'audit.log').read_text()
    assert 'volume rm' not in log.read_text()


def test_mock_failed_replacement_restores_old_container(tmp_path):
    env_dir = tmp_path / 'config/staging'
    env_dir.mkdir(parents=True)
    (env_dir / 'database.env').write_text('TEST_ONLY=dummy\n')
    bin_dir = _mock_docker(tmp_path, old=True, run_fails=True)
    log = tmp_path / 'docker.log'
    result = subprocess.run(
        ['bash', str(SCRIPT), 'database', 'staging', f'ghcr.io/jb213456/algot-database@sha256:{DIGEST}'],
        env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
             'ALGOT_DEPLOY_CONFIG_ROOT': str(tmp_path / 'config'),
             'ALGOT_DEPLOY_STATE_ROOT': str(tmp_path / 'state'),
             'MOCK_DOCKER_LOG': str(log), 'MOCK_OLD': '1', 'MOCK_RUN_FAIL': '1'},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    calls = log.read_text()
    assert 'rename algot-staging-database algot-staging-database-previous-' in calls
    assert 'start algot-staging-database' in calls
    assert 'volume rm' not in calls
