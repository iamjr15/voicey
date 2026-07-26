# Build progress

Last updated: 2026-07-26

## Current checkpoint

- **Phase:** P1 — engine spine, Pipecat, Twilio, CLI/playground, recipe #1, Docker
- **Current unit:** P1.1 `config/`
- **Next task:** implement the complete typed Agent schema, fix-carrying validation, deterministic config hash, JSONC manifest, and provider catalog with tests/docs
- **Completed:** all P0 units: Step 0 reads; exact pins and volatile-symbol inspection; spec-sync #1; package/CI/security bootstrap; storage decision; protected filesystem primitives; native Pipecat + LiveKit walking skeletons

## Gate status

| Gate | Status | Evidence / next command |
|---|---|---|
| P0 pins and Flows location | green | `pipecat-ai==1.6.0`; core `pipecat.flows`; `livekit-agents==1.6.7` installed on Python 3.14 |
| P0 spec sync | green | Standard Webhooks, recording, storage/relay, and Python matrix contracts aligned in spec + plan |
| P0 security baseline | green | Four-version CI, edge integrations, pre-commit, secret scan, dependency audit, Apache-2.0, SECURITY.md, protected-file tests |
| P0 Pipecat walking skeleton | green | Native FlowManager/PipelineWorker, connected SmallWebRTC, tool/results, mocked termination, verified signed delivery |
| P0 LiveKit walking skeleton | green | Native AgentServer/AgentSession/function_tool, dispatch token, tool/results, mocked termination, verified signed delivery |
| P0 exact verification | green | `uv run pytest -m integration --no-cov tests/integration/test_p0_walking_skeleton.py` → 2 passed |
| Physical handset/manual gates | pending-live | Carrier/handset harnesses begin in P1; tracked without claiming P0 provider-mocked coverage |

No credential-, paid-account-, cloud-, handset-, or wall-clock gate is marked green unless it actually ran.
