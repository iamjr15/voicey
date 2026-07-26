# Build progress

Last updated: 2026-07-26

## Current checkpoint

- **Phase:** P0 — repository and walking skeleton
- **Current unit:** P0 dual-runtime walking skeleton
- **Next task:** implement one shared mocked call lifecycle and native Pipecat/LiveKit adapters proving tool, result, browser-token/session, signed delivery, and provider-mocked phone termination
- **Completed:** Step 0 authoritative reads; `.env.parley-backup` confirmed covered by `.gitignore`; Pipecat/LiveKit pins resolved and symbols introspected; spec-sync commit #1; installable package/bootstrap; Python 3.11/3.14 runtime-pin resolution; CI/security baseline; protected `0700`/`0600` filesystem primitives

## Gate status

| Gate | Status | Evidence / next command |
|---|---|---|
| P0 pins and Flows location | green | `pipecat-ai==1.6.0`; core `pipecat.flows`; `livekit-agents==1.6.7` installed on Python 3.14 |
| P0 spec sync | green | Standard Webhooks, recording, storage/relay, and Python matrix contracts aligned in spec + plan |
| P0 security baseline | green | Four-version CI, edge integrations, pre-commit, secret scan, dependency audit, Apache-2.0, SECURITY.md, protected-file tests |
| P0 Pipecat walking skeleton | pending | Build and run mocked browser/tool/results/phone path |
| P0 LiveKit walking skeleton | pending | Build and run mocked browser/tool/results/phone path |
| Physical handset/manual gates | pending-live | Harness/runbook will be recorded in `docs/GAPS.md` |

No credential-, paid-account-, cloud-, handset-, or wall-clock gate is marked green unless it actually ran.
