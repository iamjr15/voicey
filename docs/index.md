# Documentation

Voicekit is the production toolchain around native Pipecat and LiveKit voice
agents. Start with the runtime you intend to operate:

- [Pipecat five-minute quickstart](quickstart-pipecat.md)
- [LiveKit five-minute quickstart](quickstart-livekit.md)
- [Concepts and ownership boundaries](concepts.md)
- [Configuration](configuration.md)
- [CLI](cli.md)
- [Browser playground](playground.md)

## Build conversation behavior

- [Typed tools](tools.md)
- [Testing and simulated callers](testing.md)
- [Runtime parity](runtime-parity.md)
- [Pipecat runtime](runtimes/pipecat.md)
- [LiveKit runtime](runtimes/livekit.md)

Recipes:

- [Appointment booking](recipes/appointment-booking.md)
- [Front desk](recipes/front-desk.md)
- [Lead intake](recipes/lead-intake.md)
- [Restaurant reservations](recipes/restaurant-reservations.md)
- [Recipe quality checklist](recipes/quality-checklist.md)

## Carriers

- [Twilio](carriers/twilio.md)
- [Telnyx](carriers/telnyx.md)
- [Vobiz](carriers/vobiz.md)
- [Plivo Beta](carriers/plivo.md)
- [Generic SIP Beta](carriers/generic-sip.md)

Each guide names both runtime paths, exact provisioning ownership, security
boundary, current certification evidence, and the external checks that still
require a funded account or physical endpoint.

## Deploy and operate

- [Docker/self-host](deploy/docker.md)
- [Pipecat Cloud](deploy/pipecat-cloud.md)
- [LiveKit Cloud](deploy/livekit-cloud.md)
- [Fly results companion](deploy/fly-companion.md)
- [Railway results companion](deploy/railway.md)
- [Managed storage](managed-storage.md)
- [Stored-data map and retention](data-map.md)
- [Results relay](results-relay.md)
- [Prometheus and OTLP](observability.md)
- [Hardening, drain, and soak](hardening.md)

## Integrate and maintain

- [Results and webhook receiver examples](results-webhooks.md)
- [Testing](testing.md)
- [Upgrading and recipe drift](upgrading.md)
- [Runtime compatibility](compatibility.md)
- [Release engineering](releasing.md)
- [Generated API reference](api/index.md)
- [Troubleshooting](troubleshooting.md)
- [Error catalog](errors.md)
- [External verification gaps](GAPS.md)
- [Security policy](../SECURITY.md)
- [Human-only rename procedure](../RENAME.md)

Next: choose a runtime quickstart. Do not begin with carrier or deployment
provisioning; the local browser path proves configuration, native workflow,
tools, and signed results before an external mutation can spend money.
