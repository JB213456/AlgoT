"""Validate an approved, compatible two-service deployment manifest."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SERVICES = ('database', 'ingestion')


def validate(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text())
    if data.get('schema_version') != 1 or data.get('status') != 'approved':
        raise ValueError('manifest is not approved')
    services = data.get('services')
    if not isinstance(services, dict) or set(services) != set(SERVICES):
        raise ValueError('manifest must contain exactly database and ingestion')
    versions = set()
    images = {}
    for service in SERVICES:
        entry = services[service]
        if not isinstance(entry, dict):
            raise ValueError(f'invalid {service} entry')
        image = entry.get('image', '')
        if not re.fullmatch(rf'ghcr\.io/jb213456/algot-{service}@sha256:[a-f0-9]{{64}}', image):
            raise ValueError(f'invalid {service} digest')
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', entry.get('version', '')):
            raise ValueError(f'invalid {service} version')
        if not re.fullmatch(r'[a-f0-9]{40}', entry.get('commit', '')):
            raise ValueError(f'invalid {service} commit')
        interface = entry.get('interface_version')
        if not isinstance(interface, str) or not interface:
            raise ValueError(f'missing {service} interface version')
        versions.add(interface)
        images[service] = image
    if len(versions) != 1:
        raise ValueError('database and ingestion interface versions differ')
    return images


def main() -> None:
    path = Path(sys.argv[1]).resolve()
    trusted = (Path.cwd() / 'deployment/manifests').resolve()
    if not path.is_relative_to(trusted):
        raise ValueError('manifest must be under deployment/manifests')
    images = validate(path)
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with Path(output).open('a') as f:
            for service, image in images.items():
                print(f'{service}_image={image}', file=f)
    print('Approved compatible manifest:', ', '.join(images))


if __name__ == '__main__':
    main()
