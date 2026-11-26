# Monitoring

Monitoring here is deliberately lightweight. A Prometheus/Grafana stack would
consume a large share of 4 OCPUs and 24 GB that is reserved for the future
AlgoT services, and OCI Monitoring alarms and Logging both bill per use. So
this uses what the cluster already provides plus one small CronJob.

No `algot-monitoring` namespace is deployed at this stage.

## What is monitored, and how

| Signal | Mechanism | How to read it |
|---|---|---|
| Node health | kubelet / control plane | `kubectl get nodes` |
| Pod health | probes + restart counts | `kubectl -n algot-data get pods` |
| Database connectivity | `/health` runs `SELECT 1` against the metadata store | startup, readiness and liveness probes |
| Ingestion liveness | heartbeat file rewritten every flush tick | `ingestion_runtime.healthcheck` as the liveness probe |
| Market data freshness | `/v1/datasets/{ds}/row-count` | `algot-data-monitor` CronJob, every 30 min |
| Storage utilisation | `df` on the 80 GB volume | same CronJob; warns at 80% |
| Object Storage consumption | bucket object sizes | same CronJob, against the 20 GB allowance |
| Provider quotas | ceilings enforced in the app's own config | `LSE_MAX_*`, `ALPACA_*RATE_LIMIT*` env vars |
| Container restarts | pod status | `kubectl -n algot-data get pods` |
| OCI consumption and cost | Usage API | see [FREE-TIER.md](FREE-TIER.md) |

## The monitor CronJob

`algot-data-monitor` runs every 30 minutes and writes a report to its pod log.
It exits non-zero on a problem, so a failure is visible as a failed Job rather
than requiring someone to read logs.

```bash
# Most recent report
kubectl -n algot-data logs -l app.kubernetes.io/name=algot-data-monitor --tail=40

# Any failures recorded
kubectl -n algot-data get jobs -l app.kubernetes.io/name=algot-data-monitor
```

It checks database reachability, volume utilisation (warning at 80%, because
the volume **cannot be expanded** without leaving the free tier), per-dataset
row counts as a freshness signal, and total backup bytes.

## Routine checks

```bash
# Whole-platform assertion suite
deployment/scripts/verify-deployment.sh

# Network isolation still enforced
deployment/scripts/verify-network-policies.sh

# Resource consumption against the namespace quota
kubectl -n algot-data describe resourcequota algot-data-quota

# Per-pod usage (requires metrics-server, which is not installed by default)
kubectl top pods -n algot-data
```

`kubectl top` needs metrics-server. It is not installed because it is another
always-on workload; add it only if the figures are needed routinely, and
remember it consumes part of the headroom reserved for future services.

## Thresholds worth acting on

| Observation | Meaning | Action |
|---|---|---|
| `/data` above 80% | 80 GB volume filling; cannot be expanded for free | compact datasets, prune backups, review retention |
| Ingestion restarts climbing | provider disconnects or a wedged flush loop | check logs; `rollout restart` if the heartbeat is stale |
| Row counts flat during market hours | feed is connected but delivering nothing | check Alpaca credentials and subscriptions |
| Backup Job failing | archive or upload broken; **pruning is skipped on purpose** | read the Job log before freeing space |
| Any non-zero OCI cost | something outside the free envelope exists | identify the SKU, remove the resource |

## Deliberate gaps

- **No alerting.** Nothing pages anyone; failures surface as failed Jobs and
  non-ready pods. Adding notifications means OCI Notifications (free email
  tier) or an external service — a decision to make explicitly.
- **No metric history.** The monitor reports point-in-time values to a log.
  Trends need a time-series store, which is what the deferred
  `algot-monitoring` namespace is for.
- **No distributed tracing or log aggregation.** `kubectl logs` only, with
  the default container log rotation.
