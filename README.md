# voicekit

voicekit is a production-grade open-source toolchain around Pipecat and LiveKit. Projects keep their conversation logic in native framework code; voicekit supplies typed configuration, guided setup, telephony, browser development, testing, deployment, durable call results, and operational tooling.

The product is under active construction and is not published. See [the product specification](docs/product-spec.md), [build plan](docs/build-plan.md), [runtime parity contract](docs/runtime-parity.md), [recipe quality contract](docs/recipes/quality-checklist.md), [tool contract](docs/tools.md), [Twilio guide](docs/carriers/twilio.md), [Telnyx guide](docs/carriers/telnyx.md), [Vobiz guide](docs/carriers/vobiz.md), [Plivo guide](docs/carriers/plivo.md), [generic SIP guide](docs/carriers/generic-sip.md), [Pipecat Cloud guide](docs/deploy/pipecat-cloud.md), [LiveKit Cloud guide](docs/deploy/livekit-cloud.md), and [live progress checkpoint](docs/PROGRESS.md).

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
