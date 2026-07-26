# voicekit

voicekit is a production-grade open-source toolchain around Pipecat and LiveKit. Projects keep their conversation logic in native framework code; voicekit supplies typed configuration, guided setup, telephony, browser development, testing, deployment, durable call results, and operational tooling.

The product is under active construction and is not published. See [the product specification](docs/product-spec.md), [build plan](docs/build-plan.md), [tool contract](docs/tools.md), [Twilio guide](docs/carriers/twilio.md), and [live progress checkpoint](docs/PROGRESS.md).

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
