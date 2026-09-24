# Gateway usage performance harness

Local, disposable-database measurement for the account cost/gateway usage
queries. It exists to answer the issue #914 questions:

- How long does the one-year account summary take, cold and warm?
- Is the session breakdown aggregating before it joins descriptive tables?
- Did the daily timeseries lose the external merge sort?
- Is the one-year warm median under 500 ms, or do the conditional rollups and
  response cache from #368 become necessary?

## Run

```bash
PRELOOP_DISABLE_TELEMETRY=true \
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/preloop \
    python scripts/perf/benchmark_gateway_usage.py \
        --output scripts/perf/results/latest.json
```

Defaults match the issue's measured shape: 200,000 rows over 400 days,
10 runtime principals, 16 models, 400 sessions. For a quick smoke run use
`--rows 3000 --days 30`. The seeder runs `ANALYZE` on the touched tables so
the planner's plan and the baseline are not measured against stale
statistics. Pass `--cleanup` to delete the seeded account when the run
finishes; without it the account stays for a same-dataset re-run.

To compare a code change against the exact same rows, run once, read
`account_id` from the JSON, then re-run with `--account-id <id>` after the
change. `scripts/perf/results/issue-914.json` records one such before/after
pair.

The script is safe on a shared dev database: it creates its own account,
never drops tables, and only deletes rows that belong to that account.

## Output

`--output` receives a JSON document with:

- `dataset`: the generated shape.
- `timings`: first request, warm median of five, min and max (ms) for the
  7d/30d/1y account windows, the one-year totals-only request, and the
  one-year per-principal window.
- `thresholds.one_year_warm_under_budget`: whether the one-year warm median
  is below 500 ms.
- `explain`: `EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)` plans for
  `get_gateway_usage_by_session` and `get_gateway_usage_timeseries`, captured
  from the real CRUD query builders so the plans cannot drift from shipped
  code.

Results files are machine-local measurements. The default
`results/latest.json` and any ad-hoc runs are scratch: do not commit them,
especially when they carry a real account id. The one versioned file is the
recorded per-issue baseline (`results/issue-914.json`), kept so later changes
can be compared against the same numbers.
