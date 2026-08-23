# Voicey

Greenfield open-source product: **the production toolchain around Pipecat and LiveKit** — a thin typed `Agent` config + 100% native flow code per runtime, with the engine supplying the guided CLI (init/dev/test/deploy/doctor), browser playground, three-tier simulated-caller testing, telephony adapters, and a signed results-webhook contract. Predecessor project (parley) was fully scrapped; nothing was salvaged except lessons.

## Authoritative documents (read before designing or building anything)

- `docs/product-spec.md` — the approved product specification (21 sections, quality gates in §17).
- `docs/build-plan.md` — the executable build plan: research findings (pinned APIs/versions), P0–P4 phase breakdown, verification. **Codex-review APPROVED after 8 adversarial rounds (gpt-5.6-sol, 2026-07-26).** Treat its "Decisions already locked" section as settled — do not relitigate.
- `docs/research/pipecat-api-reference.md` — expanded Pipecat API notes (the framework churns fast; verify every symbol against the installed pin, ideally via the `pipecat-ai-context-hub` MCP).

## Hard product rules (locked by Jigyansu — do not violate)

- Dual runtime at launch: Pipecat AND LiveKit, chosen per-project at `init`; toolchain parity CI-enforced.
- Conversation logic is 100% NATIVE framework code (pipecat-flows / LiveKit agent workflows). No custom flow DSL, ever. Engine touchpoint inside flow code = `results.set()`/`set_outcome()` only.
- No MCP anywhere in the product. Tools = plain typed Python functions or HTTP endpoints.
- CLI: wizard-first (questionary + rich, NOT Textual), zero recommendations or pre-selected defaults — every question is an explicit user choice with neutral factual one-liners; "where will people talk to it" is multi-select; scratch path still yields a working talking agent; keys collected in-flow (CLI writes `.env` itself — users are never told to edit a file); every command prints the next step.
- Production mandate: NOT an MVP. Each phase exits at production quality for its contents, gated by spec §17. Integration tiers (Certified/Beta) are acceptable; half-working defaults are not.
- Spec-first rule: any contract change amends `docs/product-spec.md` in the same commit. Plan and spec must never disagree.
- License Apache-2.0. `.env*` never committed (see `.gitignore`).

## Current state

- Phase: **P4 implementation complete; remaining paid/human/time certification and public release pending**. Every P0–P4 numbered unit and credential-free local gate is implemented. The final aggregate reruns P3, both storage backends, hardening/soak, observability, Railway, upgrade, installed-wheel release, verbatim docs quickstarts, and actual container security; every local row is green. The current post-certification suite is 1,177 passed, 41 truthful skips, and 90.00% coverage; Ruff, formatting, strict Pyright, frontend tests/build, and Python/npm dependency audits are green. API-only text certification passed all 48 appointment and P3 recipe cases first attempt across both runtimes. Twilio/Vobiz account readiness and their LiveKit no-call provision/reuse/rollback gates are green. A two-replica Railway companion with managed Postgres/private bucket and both Pipecat Cloud and LiveKit Cloud workers passed real platform and Deepgram→Gemini→Cartesia model/audio smokes on 2026-08-23, with no Ollama request; all Voicey-created cloud workers, Railway resources, Pipecat credentials, and temporary Railway SSH access were then removed through ownership-scoped teardown. The exact provider-audio/JUnit, paid PSTN, full 24-hour soak, active-call rolling drain, Fly/managed-object, human microphone/wizard/doctor/handset, and Python release gates remain pending in `docs/GAPS.md` and `docs/COMPLETION-REPORT.md`. Continue to verify every Pipecat/LiveKit symbol against the installed pins.
- Public name: **Voicey**, finalized 2026-07-31. The unscoped npm package is reserved as `voicey@0.0.1`; the PyPI distribution is not published yet. Hosting org and docs domain remain human-only decisions. Reference model and sim-judge defaults were accepted in the implementation mandate and recorded in `docs/decisions.md`.
- `.env.parley-backup` holds provider API keys carried over from the predecessor project (uncommitted; for dev convenience).
