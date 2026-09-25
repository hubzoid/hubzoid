# Sample data (synthetic)

Everything under `events/` is invented for this sample. No real service or
customer produced it.

`events/metrics.jsonl` has one line per service per minute over one hour:
`{"ts", "service", "p95_ms", "error_rate"}`. Lines without `p95_ms` are
events, such as a deploy.

In the sample, `checkout` gets a deploy at 09:45 and is slow and failing from
09:47. `search` and `auth` stay healthy.

`samples/broken.jsonl` holds a malformed line, used to show what a failed run
looks like. It is not read unless you copy it into `events/`.

To watch your own service, write the same shape of lines into `events/`, for
example from a scheduled export or a webhook task.
