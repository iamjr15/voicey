# Contributing

voicekit is spec-first. Read `CLAUDE.md`, `docs/build-plan.md`, and `docs/product-spec.md` before changing a public contract.

Every change must:

1. keep conversation logic native to Pipecat Flows or LiveKit workflows;
2. avoid MCP in the product;
3. update the product spec in the same commit as any contract change;
4. include tests and surface documentation;
5. pass `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, and `uv run pytest`;
6. never commit `.env*`, credentials, recordings, call records, or provider payloads containing PII.

Provider-paid, PSTN, cloud, handset, and soak gates must be reported honestly in `docs/GAPS.md`; an unexecuted gate is never marked green.
