# Tools

Voicekit tools are plain typed Python functions or authenticated HTTP
endpoints. They are registered into each runtime's native tool mechanism; this
module does not define conversation flow, routing, or a separate tool protocol.

## Typed Python tools

```python
from voicekit import tool


@tool(say_while_running="Let me check that for you.")
def available_slots(date: str, party_size: int = 1) -> list[str]:
    """Return available appointment slots for a date."""
    return ["10:00", "14:30"]
```

The decorator keeps the original callable intact and derives its name,
description, parameter schema, and return schema from the signature and
docstring. Every parameter and the return value must have a JSON-representable
type annotation. Variadic parameters and undeclared types fail at import time
with `VK-TOL-002`.

Both synchronous and asynchronous functions are supported. Synchronous tools
run in a worker thread with the active call and `results` context copied into
that thread. The default execution timeout is eight seconds. A timed-out Python
thread cannot be forcibly stopped by the interpreter, so mutating synchronous
tools must be idempotent; prefer cancellable asynchronous clients for remote
writes.

## HTTP tools

```python
from voicekit import tool

get_customer = tool.http(
    name="get_customer",
    url="https://crm.example.com/customers/{customer_id}",
    method="GET",
    headers_env={"Authorization": "Bearer ${CRM_API_KEY}"},
    timeout_s=8,
    say_while_running="I'm checking the customer record.",
)
```

Credentials are resolved from the process environment at invocation time and
never appear in tool metadata. A header value must be an environment-variable
name, `${ENV_NAME}`, or a scheme followed by `${ENV_NAME}`. Static header
secrets are rejected.

URL fields are required string arguments. Callers may also provide `_query`
with scalar query values. `POST`, `PUT`, `PATCH`, and `DELETE` tools accept a
JSON-compatible `_json` body:

```python
result = await ToolExecutor().execute(
    get_customer,
    {
        "customer_id": "customer-123",
        "_query": {"include_history": True},
    },
)
```

Only `GET` is retried, once, and only for a network/timeout failure, HTTP 429,
or HTTP 5xx response. Mutating methods are attempted exactly once.

## Safe execution

Runtime adapters use `ToolExecutor` rather than invoking a registered tool
directly:

```python
from voicekit.tools import ToolExecutor

execution = await ToolExecutor().execute(available_slots, {"date": "2026-08-03"})
llm_value = execution.for_llm()
```

The model receives one of these stable shapes:

```json
{"ok": true, "value": ["10:00", "14:30"]}
```

```json
{
  "ok": false,
  "error": {
    "code": "tool_timeout",
    "message": "The tool timed out before it returned.",
    "retryable": true
  }
}
```

Argument mismatches, timeouts, and tool exceptions become structured errors.
Exception messages and stack traces are never returned to the model. Runtime
adapters may translate this shape into their native recoverable-tool error
surface.

## Per-call observations and results

Bind both contexts around runtime tool dispatch:

```python
from voicekit import results
from voicekit.tools import (
    RepositoryToolObservationSink,
    ToolExecutor,
    tool_execution_context,
)

buffer = results.CallResultBuffer(call_id=call_id)
sink = RepositoryToolObservationSink(call_record_store)

with results.result_context(buffer), tool_execution_context(call_id, sink):
    execution = await ToolExecutor().execute(available_slots, {"date": "2026-08-03"})
```

Every completed invocation records its stable invocation id, arguments,
structured result, duration, and status in the protected call record. Failure
to persist that final observation raises `VK-TOL-005` so the runtime can stop
accepting calls instead of silently losing audit data. Secret-shaped values
are scrubbed by protected storage.

`results.set()` and `results.set_outcome()` are scoped by context variables.
They fail closed outside an active call and remain isolated across concurrent
calls and worker-thread handoff.

Next step: register these tools through the selected native runtime bootstrap.
