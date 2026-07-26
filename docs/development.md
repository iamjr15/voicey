# Development

## Prerequisites

- Python 3.11–3.14
- `uv`
- Node.js for the playground build (P1 onward)
- Docker for image and deployment verification (P1 onward)

## Setup and verification

```bash
uv sync
uv run pre-commit install
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Runtime extras are isolated:

```bash
uv sync --extra pipecat
uv sync --extra livekit
uv sync --extra twilio
```

Do not install the standalone `pipecat-ai-flows` package. Pipecat 1.6.0 provides `pipecat.flows` in core.

## Protected local data

Runtime state belongs under `data/`. The engine creates the root with mode `0700` and lifecycle databases, outbox data, recordings, and backups with mode `0600`. `data/` and `.env*` are ignored by git. Never weaken these permissions to work around a local setup problem.

## Change discipline

Public contract changes amend `docs/product-spec.md` in the same commit. Run all four checks before every commit. Update `docs/PROGRESS.md` at least every few commits and place unexecuted external gates with exact commands in `docs/GAPS.md`.

Next: inspect `docs/PROGRESS.md` for the current unit.
