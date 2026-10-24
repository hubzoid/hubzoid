# Watchtower thresholds

Watchtower checks the last 15 minutes of samples for each service.

| Signal | Threshold | Meaning |
|---|---|---|
| p95 latency | 800 ms | 95% of requests finished faster than this. Above it, users notice slowness. |
| Error rate | 2% | Share of requests that failed. Above it, users see failures. |

A service counts as breaching when at least 3 samples in the window are over a
threshold, so one slow minute does not raise an alert.

Severity is the model's judgement from the numbers: `critical` when users are
likely failing to complete work, `warning` otherwise.

The thresholds are set in `workflows/settings.yaml`. Changing them does not
need a restart: the next run reads them.
