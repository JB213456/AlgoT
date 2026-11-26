"""Small deterministic local processing baseline; no provider traffic."""
from time import perf_counter_ns
from statistics import quantiles
from live_ingestion.dedup import Deduplicator

n = 10000
start = perf_counter_ns()
d = Deduplicator()
latencies = []
for i in range(n):
    t = perf_counter_ns()
    d.is_duplicate('AAPL', i)
    latencies.append((perf_counter_ns() - t) / 1e6)
elapsed = (perf_counter_ns() - start) / 1e9
p = quantiles(latencies, n=100)
print(f'events_per_second={n/elapsed:.0f} p50_ms={p[49]:.4f} p95_ms={p[94]:.4f} p99_ms={p[98]:.4f}')
assert n / elapsed >= 1000
