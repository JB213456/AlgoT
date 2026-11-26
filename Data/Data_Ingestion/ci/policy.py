"""Release policy for the ingestion boundary and free-tier configuration."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
code = [p for branch in ('Live/live_ingestion', 'Backfill/backfill_engine', 'ingestion_runtime') for p in (ROOT / branch).rglob('*.py') if 'tests' not in p.parts]
joined = '\n'.join(p.read_text() for p in code)
for forbidden in (r'\bsubmit_order\b', r'\bplace_order\b', r'\bTradingClient\b', r'https?://api\.alpaca\.markets'):
    assert not re.search(forbidden, joined), f'Forbidden execution or trading endpoint: {forbidden}'
assert 'https://data.alpaca.markets' in joined
assert 'https://download.londonstrategicedge.com' in joined
assert 'if self.data_feed != "iex"' in joined
assert 'market-data-db' not in (ROOT / 'Dockerfile').read_text().split('COPY')[-1]
print('Ingestion provider and execution boundary policy passed')
