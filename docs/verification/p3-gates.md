# P3 phase verification

P3 extends the P2 aggregate with four production recipes, Vobiz and Plivo on
both runtimes, generic SIP Beta, the guarded tier-3 PSTN harness, signed relay
and managed-storage contracts, Fly/Pipecat Cloud/LiveKit Cloud automation, and
the Pipecat/Twilio warm-transfer conference bridge.

## Local aggregate

Start a disposable PostgreSQL 17 database, build the exact wheel under test,
and run:

```bash
export VOICEY_TEST_POSTGRES_DSN='postgresql://voicey:voicey-test@127.0.0.1:5432/voicey' # pragma: allowlist secret
uv build --wheel --out-dir dist
uv run python tests/verification/run_p3_gate.py \
  --wheel dist/voicey-1.0.0-py3-none-any.whl
```

The runner writes `.voicey/verification/p3-gate-report.json` atomically and
also retains the nested P2 report. It fails when PostgreSQL is absent rather
than treating skipped managed-backend tests as evidence. Its local groups are:

- the entire P2 phase aggregate;
- all four first-party recipe and native workflow contracts;
- Vobiz Pipecat and LiveKit certification;
- Plivo Pipecat/LiveKit plus generic SIP certification;
- the independent tier-3 Pipecat and LiveKit PSTN harness;
- relay, Postgres/S3, results companion, Fly, and both managed-cloud drivers;
- signed, durable Pipecat/Twilio warm transfer and the closed parity exclusion.

## External continuation

The report remains `pending-live` after every local row is green. Exact
commands and prerequisites in [`docs/GAPS.md`](../GAPS.md) cover:

- six provider-backed recipe/runtime audio/JUnit runs (all 34 text cases are
  green first attempt through the Anthropic API override);
- remaining Vobiz paid/media checks (account and LiveKit no-call control-plane
  are green), plus Plivo and generic-SIP account mutations and paid PSTN;
- both independent tier-3 paid loopbacks;
- S3-compatible storage;
- Fly, Pipecat Cloud, and LiveKit Cloud deployments and smoke calls;
- the two-handset Pipecat/Twilio private-briefing handoff and crash checklist.

No local mock, compiler, or control-plane fake is promoted as carrier, cloud,
physical-handset, or paid-call evidence.

Next: make the local report green, then run external rows only in explicitly
acknowledged funded environments.
