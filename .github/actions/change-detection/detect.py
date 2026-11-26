"""Classify changed tracked paths for CI. Never reads proprietary Strategies files."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


FUTURE_SERVICES = {'features': 'Core/Feature_Engineering', 'aggregator': 'Core/Signal_Aggregator', 'portfolio': 'Core/Portfolio_Manager', 'risk': 'Core/Risk_Management', 'execution': 'Core/Execution_Engine'}


def classify(paths: list[str], active_future: set[str] | None = None) -> dict[str, bool]:
    active_future = active_future or set()
    changed = set(paths)
    shared = any(p.startswith('.github/workflows/reusable-') or p.startswith('.github/actions/') for p in changed)
    database = shared or any(p.startswith('Data/Database/') or p == '.github/workflows/database-pipeline.yml' for p in changed)
    ingestion = shared or any(p.startswith('Data/Data_Ingestion/') or p == '.github/workflows/ingestion-pipeline.yml' for p in changed)
    interface = any(p.startswith(('Data/Database/market_data_db/schema.py', 'Data/Database/server/', 'Data/Database/market_data_db/adapters/')) for p in changed)
    if interface:
        ingestion = True
    system = shared or database or ingestion or any(p.startswith(('system_tests/', 'deployment/manifests/', '.github/workflows/system-')) for p in changed)
    result = {'database': database, 'ingestion': ingestion, 'system': system}
    for name, folder in FUTURE_SERVICES.items():
        result[name] = any(p.startswith(folder + '/') for p in changed) or (name in active_future and (shared or f'.github/workflows/{name}-pipeline.yml' in changed))
    return result


def main() -> None:
    before = os.environ['BASE_SHA']
    after = os.environ['HEAD_SHA']
    if before == '0' * 40:
        before = subprocess.check_output(['git', 'rev-parse', f'{after}^'], text=True).strip()
    active_future = {name for name, folder in FUTURE_SERVICES.items() if subprocess.check_output(['git', 'ls-files', '--', folder + '/'], text=True).strip()}
    if before == after:
        result = {'database': True, 'ingestion': True, 'system': True, **{name: name in active_future for name in FUTURE_SERVICES}}
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            for name, affected in result.items():
                print(f'{name}={str(affected).lower()}', file=output)
        return
    paths = subprocess.check_output(['git', 'diff', '--name-only', before, after, '--', '.github', 'Data/Database', 'Data/Data_Ingestion', 'system_tests', 'deployment', *FUTURE_SERVICES.values()], text=True).splitlines()
    result = classify(paths, active_future)
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        for name, affected in result.items():
            print(f'{name}={str(affected).lower()}', file=output)
    print('Affected services:', result)


if __name__ == '__main__':
    main()
