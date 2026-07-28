# Results, recovery, and webhooks

Voicekit makes one reliability promise for every call:

> Exactly one terminal event is persisted once, then delivery is attempted
> until the receiver acknowledges it or the delivery is visibly dead-lettered.

The event body is also the pull representation. A delivery retry cannot mutate
it or create a second terminal event.

## Lifecycle transaction

Before a call becomes externally visible, `StorageRepository.begin_call()`
durably creates its lifecycle row, result-delivery settings, and a generation-1
lease. The active owner incrementally flushes transcripts, result data, tool
observations, provider state, and latency.

Terminalization is one `BEGIN IMMEDIATE` transaction on SQLite:

1. verify owner id and generation;
2. CAS the call from `active` to `completed` or `failed`;
3. insert the canonical immutable event bytes;
4. insert the delivery outbox row.

A partial unique index permits one terminal event per call. A failed outbox
insert rolls the entire transition back. Repeating terminalization with the
same current lease returns the existing event; an older generation receives
`VK-RES-006`.

SQLite is the P1 Docker/self-host implementation of the runtime-blind
`StorageRepository` protocol. It uses WAL, `synchronous=FULL`, foreign keys,
secure deletion, a serialized writer, and schema migrations. The Postgres
implementation lands with the Fly companion in P3 and runs the same repository
contract and chaos suites.

## Payload

Terminal success uses `call.completed`; infrastructure failure uses
`call.failed`. Both include:

- call id, direction, endpoints, timestamps, duration, and fixed ended reason;
- agent name, runtime, and deterministic `config_hash`;
- business outcome and structured result data;
- ordered transcript turns;
- engine-owned recording reference or `null`;
- turn count, interruptions, and e2e p50/p95 latency.

`Results.include` controls the optional data, transcript, recording, and metrics
sections. `Results.redact` accepts dotted paths such as `data.email` and
`call.from`; a bare field name recursively redacts every matching key. Redaction
and secret scrubbing happen before canonical JSON bytes are persisted.

Push and pull return those exact bytes. The P1.8 CLI exposes them through
`voicekit calls show <call-id>`.

## Standard Webhooks signing

Every attempt includes:

```text
webhook-id: <stable event id>
webhook-timestamp: <fresh Unix timestamp>
webhook-signature: v1,<base64 HMAC> [v1,<previous-secret HMAC>]
```

The signed bytes are:

```text
HMAC-SHA256(base64decode(secret after "whsec_"),
            "<event-id>.<timestamp>.<raw-body>")
```

## Receiver examples

All receivers must read the raw body before JSON parsing and reject timestamps
outside five minutes. The examples use the current
[Standard Webhooks libraries](https://github.com/standard-webhooks/standard-webhooks).

### Python

Voicekit's helper accepts a framework header mapping and supports the current
plus previous secret during rotation:

```python
import json
import os

from fastapi import FastAPI, Request, Response
from voicekit import verify_webhook

app = FastAPI()


@app.post("/voice-results")
async def voice_results(request: Request) -> Response:
    raw_body = await request.body()
    verify_webhook(
        request.headers,
        raw_body,
        current_secret=os.environ["VOICEKIT_WEBHOOK_SECRET"],
        previous_secret=os.environ.get("VOICEKIT_WEBHOOK_PREVIOUS_SECRET"),
    )
    event = json.loads(raw_body)
    persist_once(event["id"], event)
    return Response(status_code=204)
```

### JavaScript

Install `standardwebhooks@1.0.0`. `express.raw` is intentional; a JSON parser
must not run before signature verification.

```javascript
import express from "express";
import { Webhook } from "standardwebhooks";

const app = express();

app.post(
  "/voice-results",
  express.raw({ type: "application/json" }),
  async (request, response) => {
    const headers = {
      "webhook-id": request.get("webhook-id"),
      "webhook-timestamp": request.get("webhook-timestamp"),
      "webhook-signature": request.get("webhook-signature"),
    };
    const rawBody = request.body.toString("utf8");
    const event = new Webhook(process.env.VOICEKIT_WEBHOOK_SECRET).verify(
      rawBody,
      headers,
    );
    await persistOnce(event.id, event);
    response.sendStatus(204);
  },
);
```

### Go

The pinned Go library accepts the raw bytes and the request's `http.Header`
directly:

```go
package receiver

import (
	"encoding/json"
	"io"
	"net/http"
	"os"

	standardwebhooks "github.com/standard-webhooks/standard-webhooks/libraries/go"
)

func VoiceResults(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		http.Error(w, "invalid body", http.StatusBadRequest)
		return
	}
	webhook, err := standardwebhooks.NewWebhook(os.Getenv("VOICEKIT_WEBHOOK_SECRET"))
	if err != nil || webhook.Verify(body, r.Header) != nil {
		http.Error(w, "invalid signature", http.StatusUnauthorized)
		return
	}
	var event struct {
		ID string `json:"id"`
	}
	if json.Unmarshal(body, &event) != nil || event.ID == "" {
		http.Error(w, "invalid event", http.StatusBadRequest)
		return
	}
	persistOnce(event.ID, body)
	w.WriteHeader(http.StatusNoContent)
}
```

Return a 2xx response only after the receiver has durably claimed the stable
event id. The receiver should enforce its own bounded body size before
verification and never log the secret or full protected payload.

The checked-in interoperability vector is verified in CI by the official
Standard Webhooks Python 1.1.0, JavaScript 1.0.0, and pinned Go implementations.

## Retries and dead letters

Delivery attempts occur at:

```text
0s, +5s, +5m, +30m, +2h, +5h, +10h, +10h
```

Each delay is relative to the preceding failure with ±20% jitter. The outbox
uses an expiring lease and `UPDATE ... RETURNING`, so simultaneous workers
cannot send the same claimed attempt. Every retry uses the stable event id and
body with a fresh timestamp/signature.

After attempt eight, the row becomes `dead_lettered`; it remains visible to
`doctor`, metrics, and `calls list --undelivered`. Manual redelivery resets the
attempt schedule but preserves the event id and body.

## Crash recovery and fencing

Active calls have `{owner_id, generation, lease_expires_at}`. Heartbeats may
extend only the matching generation. A stale-call sweeper atomically increments
the generation before takeover, then asks the carrier/runtime reconciler for
provider truth:

- provider still active: keep the recovered call active;
- provider completed: emit `call.completed`;
- provider failed: emit `call.failed`;
- unknown after a successful lookup: emit `call.failed` with
  `recovery_unknown`.

Recovery never terminalizes before reconciliation. Two sweepers racing for one
call cannot both acquire its generation. The integration suite SIGKILLs a real
writer process and proves that the surviving partial transcript produces one
terminal event and one delivery.

## Recording readiness

When recording is enabled, terminal payloads immediately contain:

```json
{"id": "rec_...", "status": "pending", "url": null}
```

The terminal body never changes. After authenticated carrier download into the
configured artifact store, `call.recording.ready` carries the same stable
reference with an engine-owned authenticated URL. It is a separate
non-terminal event with its own event id and outbox delivery.

Raw carrier media URLs are never exposed.

Fetch the ready engine URL with the same current webhook secret used by the
receiver:

```text
Authorization: Bearer whsec_...
```

The previous configured secret is accepted during rotation overlap. The URL
contains no credential, responses are `private, no-store`, and pending,
failed, unknown, or unauthorized recording reads fail closed.

## Retention

SQLite retention uses each call's `purge_after_days` and covers the call row,
normalized observations, event/outbox/dead-letter rows, recording metadata,
WAL contents, and registered backups. Artifact deletions are first queued in
the same database transaction, making a crash or failed delete visible and
replayable. The local artifact implementation rejects path traversal and
symlinks and stores files at `0600` below a `0700` root.

Next step for receiver development: copy the verification example, then run
`voicekit doctor --send-test`.
