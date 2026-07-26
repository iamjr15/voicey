# Build progress

Last updated: 2026-07-26

## Current checkpoint

- **Phase:** P0 — repository and walking skeleton
- **Current unit:** repository bootstrap and CI/security baseline
- **Next task:** create the typed package/test skeleton, quality tooling, CI scans, and protected data-directory helpers
- **Completed:** Step 0 authoritative reads; `.env.parley-backup` confirmed covered by `.gitignore`; Pipecat/LiveKit Python pins installed; JS client pins queried; volatile symbols introspected

## Gate status

| Gate | Status | Evidence / next command |
|---|---|---|
| P0 pins and Flows location | green | `pipecat-ai==1.6.0`; core `pipecat.flows`; `livekit-agents==1.6.7` installed on Python 3.14 |
| P0 spec sync | green | Standard Webhooks, recording, storage/relay, and Python matrix contracts aligned in spec + plan |
| P0 security baseline | pending | Bootstrap CI, permissions, secret/dependency scans |
| P0 Pipecat walking skeleton | pending | Build and run mocked browser/tool/results/phone path |
| P0 LiveKit walking skeleton | pending | Build and run mocked browser/tool/results/phone path |
| Physical handset/manual gates | pending-live | Harness/runbook will be recorded in `docs/GAPS.md` |

No credential-, paid-account-, cloud-, handset-, or wall-clock gate is marked green unless it actually ran.
