# voicekit

voicekit is a production-grade open-source toolchain around Pipecat and LiveKit. Projects keep their conversation logic in native framework code; voicekit supplies typed configuration, guided setup, telephony, browser development, testing, deployment, durable call results, and operational tooling.

The product is complete locally but is not publicly named or published. Start
with the [documentation index](docs/index.md), then choose the
[Pipecat](docs/quickstart-pipecat.md) or
[LiveKit](docs/quickstart-livekit.md) five-minute path. The
[completion report](docs/COMPLETION-REPORT.md) is written by the final P4
aggregate and separates green local evidence from credentialed, paid,
wall-clock, and physical-handset gates.

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Next: follow [the development guide](docs/development.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
