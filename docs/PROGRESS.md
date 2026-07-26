# Build progress

Last updated: 2026-07-26

## Current checkpoint

- **Phase:** P1 — engine spine, Pipecat, Twilio, CLI/playground, recipe #1, Docker
- **Current unit:** P1.5 `telephony/` and Twilio certification
- **Next task:** implement the adapter Protocol/entry-point registry, then the durable Twilio configuration snapshot, outbound intent ledger, verified webhook/media paths, call controls, recording handling, and mocked/live certification harness
- **Completed:** all P0 units; P1.1 configuration; P1.2 observability; P1.3 repository/results reliability; P1.4 typed Python and environment-authenticated HTTP tools with isolated execution and protected observations

## Gate status

| Gate | Status | Evidence / next command |
|---|---|---|
| P0 pins and Flows location | green | `pipecat-ai==1.6.0`; core `pipecat.flows`; `livekit-agents==1.6.7` installed on Python 3.14 |
| P0 spec sync | green | Standard Webhooks, recording, storage/relay, and Python matrix contracts aligned in spec + plan |
| P0 security baseline | green | Four-version CI, edge integrations, pre-commit, secret scan, dependency audit, Apache-2.0, SECURITY.md, protected-file tests |
| P0 Pipecat walking skeleton | green | Native FlowManager/PipelineWorker, connected SmallWebRTC, tool/results, mocked termination, verified signed delivery |
| P0 LiveKit walking skeleton | green | Native AgentServer/AgentSession/function_tool, dispatch token, tool/results, mocked termination, verified signed delivery |
| P0 exact verification | green | `uv run pytest -m integration --no-cov tests/integration/test_p0_walking_skeleton.py` → 2 passed |
| P1.1 configuration unit | green | `uv run pytest` → 54 passed; ruff and strict pyright green; total branch coverage 90.57% |
| P1.2 observability unit | green | Correlation/PII leak, latency, WAL/FULL durability, reopen, schema, and parallel-write tests green; `uv run pytest` → 73 passed |
| P1.3 results/reliability unit | green | Terminal/outbox rollback, fencing, dual sweeper, actual SIGKILL, retry/DLQ, recording, retention, pull parity, and official Python/Node/Go Standard Webhooks interop green; `uv run pytest` → 103 passed |
| P1.4 tools unit | green | Typed schemas, async/thread execution, 8s timeout, safe errors, GET-only retry, env auth, protected observations, and 40-call context isolation green; `uv run pytest` → 117 passed, 91.84% branch coverage |
| Physical handset/manual gates | pending-live | Carrier/handset harnesses begin in P1; tracked without claiming P0 provider-mocked coverage |

No credential-, paid-account-, cloud-, handset-, or wall-clock gate is marked green unless it actually ran.
