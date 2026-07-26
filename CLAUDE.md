# voicekit (placeholder name — see RENAME step in build plan P4)

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

- Phase: **P0 not started** (repo + docs import done 2026-07-26). Next: version pinning, spec-sync commit #1, walking-skeleton spike per runtime — see build plan "Repo bootstrap (Phase 0)".
- Open user decisions with deadlines: product name (before first publish); reference model set sign-off (P0 exit); sim-judge default (P1 exit).
- `.env.parley-backup` holds provider API keys carried over from the predecessor project (uncommitted; for dev convenience).
