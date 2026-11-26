"""Fail CI on expired or undocumented scanner suppressions."""
from datetime import date
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
record = json.loads((ROOT / 'deployment/security/exceptions.json').read_text())
if record.get('schema_version') != 1:
    raise SystemExit('invalid security exception schema')
entries = record.get('exceptions', [])
if not isinstance(entries, list):
    raise SystemExit('security exceptions must be a list')
expected = set()
for entry in entries:
    if entry.get('scanner') != 'gitleaks' or not entry.get('reason'):
        raise SystemExit('unsupported or unjustified security exception')
    if date.fromisoformat(entry['expires']) < date.today():
        raise SystemExit('expired security exception: ' + entry['fingerprint'])
    expected.add(entry['fingerprint'])
actual = {line.strip() for line in (ROOT / '.gitleaksignore').read_text().splitlines() if line.strip() and not line.startswith('#')}
if actual != expected:
    raise SystemExit('undocumented or missing Gitleaks suppression')
print(f'{len(expected)} documented, unexpired scanner exception(s)')
