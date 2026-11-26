"""Compatibility manifest validation without deployment."""
import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / 'deployment/scripts/validate-manifest.py'
spec = importlib.util.spec_from_file_location('validate_manifest', MODULE)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def _approved():
    return {
        'schema_version': 1,
        'status': 'approved',
        'services': {
            name: {
                'image': f'ghcr.io/jb213456/algot-{name}@sha256:' + ('a' if name == 'database' else 'b') * 64,
                'version': '1.2.3',
                'commit': 'c' * 40,
                'interface_version': 'market-data-v1',
            }
            for name in ('database', 'ingestion')
        },
    }


def test_valid_compatible_manifest(tmp_path):
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(_approved()))
    images = validator.validate(path)
    assert set(images) == {'database', 'ingestion'}


def test_interface_mismatch_blocks_system_deployment(tmp_path):
    data = _approved()
    data['services']['ingestion']['interface_version'] = 'market-data-v2'
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='interface versions differ'):
        validator.validate(path)


def test_unapproved_manifest_blocks_system_deployment(tmp_path):
    data = _approved()
    data['status'] = 'draft'
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='not approved'):
        validator.validate(path)
