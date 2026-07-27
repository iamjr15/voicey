import asyncio
import sys
import threading
from types import ModuleType
from typing import cast

import httpx
import pytest

from voicekit import results, tool
from voicekit.errors import VoicekitError
from voicekit.obs.records import ToolCallObservation
from voicekit.tools import (
    HttpTool,
    RepositoryToolObservationSink,
    ToolExecutor,
    get_tool_metadata,
    load_tools,
    tool_execution_context,
)


class MemoryObservationSink:
    def __init__(self) -> None:
        self.observations: list[tuple[str, ToolCallObservation]] = []

    async def record(
        self,
        call_id: str,
        observation: ToolCallObservation,
    ) -> None:
        self.observations.append((call_id, observation))


class MemoryObservationStore:
    def __init__(self) -> None:
        self.observations: list[tuple[str, ToolCallObservation]] = []

    async def record_tool_call(
        self,
        call_id: str,
        observation: ToolCallObservation,
    ) -> None:
        self.observations.append((call_id, observation))


def test_tool_keeps_plain_function_and_records_metadata() -> None:
    @tool(say_while_running="One moment.")
    def lookup_slot(day: str) -> str:
        """Look up one appointment slot."""
        return f"{day}:10:00"

    assert lookup_slot("Monday") == "Monday:10:00"
    assert get_tool_metadata(lookup_slot).name == "lookup_slot"
    assert get_tool_metadata(lookup_slot).description == "Look up one appointment slot."
    assert get_tool_metadata(lookup_slot).say_while_running == "One moment."
    assert get_tool_metadata(lookup_slot).mutating is False
    assert get_tool_metadata(lookup_slot).parameters_schema == {
        "type": "object",
        "properties": {
            "day": {"type": "string"},
        },
        "additionalProperties": False,
        "required": ["day"],
    }
    assert get_tool_metadata(lookup_slot).return_schema == {"type": "string"}
    assert not get_tool_metadata(lookup_slot).is_async


def test_bare_tool_decorator() -> None:
    @tool
    async def ping() -> str:
        """Return a health response."""
        return "pong"

    assert get_tool_metadata(ping).name == "ping"
    assert get_tool_metadata(ping).is_async


def test_tool_records_explicit_mutation_semantics() -> None:
    @tool(mutating=True)
    def reserve(reference: str) -> str:
        """Reserve one external resource."""
        return reference

    assert get_tool_metadata(reserve).mutating is True


def test_tool_discovery_supports_modules_and_explicit_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("voicekit_test_discovery")

    @tool
    def zebra() -> str:
        """Return the last alphabetical tool."""
        return "z"

    @tool
    def alpha() -> str:
        """Return the first alphabetical tool."""
        return "a"

    module.__dict__.update(
        {
            "zebra": zebra,
            "alpha": alpha,
            "undecorated": lambda: None,
        }
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)

    discovered = load_tools(module.__name__)
    explicit = load_tools([zebra, alpha])

    assert [get_tool_metadata(item).name for item in discovered] == ["alpha", "zebra"]
    assert [get_tool_metadata(item).name for item in explicit] == ["alpha", "zebra"]


def test_tool_discovery_catalogs_import_and_duplicate_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("voicekit_test_duplicate_tools")

    def first() -> str:
        """Return the first value."""
        return "first"

    def second() -> str:
        """Return the second value."""
        return "second"

    first.__name__ = "duplicate"
    second.__name__ = "duplicate"
    module.__dict__.update({"first": tool(first), "second": tool(second)})
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(VoicekitError, match="VK-TOL-002"):
        load_tools(module.__name__)
    with pytest.raises(VoicekitError, match="VK-TOL-001"):
        load_tools("voicekit_module_that_does_not_exist")


def test_undecorated_callable_is_rejected() -> None:
    def plain() -> None:
        return None

    with pytest.raises(VoicekitError, match="VK-TOL-001"):
        get_tool_metadata(plain)


def test_tool_schema_includes_defaults_arrays_and_required_fields() -> None:
    @tool
    def lookup(date: str, party_size: int = 1) -> list[str]:
        """Return open slots."""
        return [f"{date}:{party_size}"]

    schema = get_tool_metadata(lookup).parameters_schema

    assert schema["required"] == ["date"]
    assert schema["properties"]["date"] == {"type": "string"}
    assert schema["properties"]["party_size"] == {"default": 1, "type": "integer"}
    assert get_tool_metadata(lookup).return_schema == {
        "type": "array",
        "items": {"type": "string"},
    }


def test_invalid_tool_declarations_raise_catalog_error() -> None:
    def untyped(value: str) -> str:
        return value

    def missing_return(value: str) -> str:
        return value

    untyped.__annotations__.pop("value")
    missing_return.__annotations__.pop("return")
    for invalid in (untyped, missing_return):
        with pytest.raises(VoicekitError) as caught:
            tool(invalid)
        assert caught.value.code == "VK-TOL-002"


def test_variadic_tool_declaration_is_rejected() -> None:
    def variadic(*values: str) -> list[str]:
        return list(values)

    with pytest.raises(VoicekitError) as caught:
        tool(variadic)

    assert caught.value.code == "VK-TOL-002"


def test_non_json_schema_tool_declaration_is_cataloged() -> None:
    class Opaque:
        pass

    def opaque(value: Opaque) -> str:
        return str(value)

    with pytest.raises(VoicekitError) as caught:
        tool(opaque)

    assert caught.value.code == "VK-TOL-002"


@pytest.mark.asyncio
async def test_sync_tool_runs_in_thread_with_result_context_and_observation() -> None:
    main_thread = threading.get_ident()
    sink = MemoryObservationSink()
    buffer = results.CallResultBuffer(call_id="call_sync")

    @tool
    def choose_slot(slot: str) -> dict[str, object]:
        """Choose a slot and record it."""
        results.set("slot", slot)
        return {"slot": slot, "worker_thread": threading.get_ident()}

    with (
        results.result_context(buffer),
        tool_execution_context("call_sync", sink),
    ):
        execution = await ToolExecutor().execute(choose_slot, {"slot": "10:00"})

    value = cast("dict[str, object]", execution.value)
    assert execution.ok
    assert value["slot"] == "10:00"
    assert value["worker_thread"] != main_thread
    assert buffer.data == {"slot": "10:00"}
    assert sink.observations[0][0] == "call_sync"
    observation = sink.observations[0][1]
    assert observation.tool_name == "choose_slot"
    assert observation.arguments == {"slot": "10:00"}
    assert observation.status == "succeeded"
    assert observation.duration_ms >= 0


@pytest.mark.asyncio
async def test_repository_observation_sink_delegates_to_call_store() -> None:
    memory = MemoryObservationStore()
    sink = RepositoryToolObservationSink(memory)

    @tool
    def ping(value: str) -> str:
        """Echo a value."""
        return value

    with tool_execution_context("call_repository", sink):
        await ToolExecutor().execute(ping, {"value": "pong"})

    assert memory.observations[0][0] == "call_repository"


@pytest.mark.asyncio
async def test_observation_persistence_failure_is_cataloged() -> None:
    class BrokenSink:
        async def record(
            self,
            call_id: str,
            observation: ToolCallObservation,
        ) -> None:
            del call_id, observation
            raise OSError("disk detail")

    @tool
    def ping() -> str:
        """Return a value."""
        return "pong"

    with (
        tool_execution_context("call_broken", BrokenSink()),
        pytest.raises(VoicekitError) as caught,
    ):
        await ToolExecutor().execute(ping, {})

    assert caught.value.code == "VK-TOL-005"
    assert "disk detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_async_tool_and_structured_failures_never_expose_stack_traces() -> None:
    @tool
    async def add(left: int, right: int) -> int:
        """Add two integers."""
        return left + right

    @tool
    def explode() -> None:
        """Raise an internal error."""
        raise RuntimeError("database password leaked")  # pragma: allowlist secret

    executor = ToolExecutor()
    success = await executor.execute(add, {"left": 2, "right": 3})
    invalid = await executor.execute(add, {"left": "wrong", "right": 3})
    failed = await executor.execute(explode, {})

    assert success.for_llm() == {"ok": True, "value": 5}
    assert invalid.error is not None
    assert invalid.error.code == "invalid_arguments"
    assert failed.error is not None
    assert failed.error.code == "tool_failed"
    assert "password" not in str(failed.for_llm())
    assert "Traceback" not in str(failed.for_llm())


@pytest.mark.asyncio
async def test_tool_timeout_is_structured_and_retryable() -> None:
    @tool
    async def slow() -> str:
        """Wait too long."""
        await asyncio.sleep(0.05)
        return "late"

    result = await ToolExecutor(timeout_s=0.001).execute(slow, {})

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "tool_timeout"
    assert result.error.retryable


@pytest.mark.asyncio
async def test_parallel_sync_tools_preserve_call_and_result_isolation() -> None:
    sink = MemoryObservationSink()
    executor = ToolExecutor()

    @tool
    def record_call(call_number: int) -> int:
        """Record one result from a worker thread."""
        results.set("call_number", call_number)
        return call_number

    async def run(call_number: int) -> results.CallResultBuffer:
        buffer = results.CallResultBuffer(call_id=f"call_{call_number}")
        with (
            results.result_context(buffer),
            tool_execution_context(buffer.call_id, sink),
        ):
            execution = await executor.execute(
                record_call,
                {"call_number": call_number},
            )
            assert execution.value == call_number
        return buffer

    buffers = await asyncio.gather(*(run(index) for index in range(40)))

    assert [buffer.data["call_number"] for buffer in buffers] == list(range(40))
    assert {call_id for call_id, _ in sink.observations} == {f"call_{index}" for index in range(40)}
    assert len({item.invocation_id for _, item in sink.observations}) == 40


@pytest.mark.asyncio
async def test_http_get_injects_env_and_retries_only_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    statuses = iter([503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            next(statuses),
            request=request,
            json={"found": True},
        )

    monkeypatch.setenv("CRM_API_KEY", "test-http-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        function = HttpTool(
            name="get_customer",
            url="https://crm.example.test/customers/{customer_id}",
            method="GET",
            headers_env={"Authorization": "Bearer ${CRM_API_KEY}"},
            timeout_s=8,
            say_while_running="I'm checking the customer record.",
            description="Fetch a customer.",
            client=client,
            retry_delay_s=0,
        )
        execution = await ToolExecutor().execute(
            function,
            {
                "customer_id": "A/B",
                "_query": {"active": True},
            },
        )

    assert execution.value == {"found": True}
    assert len(requests) == 2
    assert requests[0].url.raw_path.startswith(b"/customers/A%2FB")
    assert requests[0].url.params["active"] == "true"
    assert requests[0].headers["authorization"] == "Bearer test-http-key"
    metadata = get_tool_metadata(function)
    assert metadata.source == "http"
    assert metadata.say_while_running == "I'm checking the customer record."
    assert metadata.parameters_schema["required"] == ["customer_id"]


@pytest.mark.asyncio
async def test_http_post_does_not_retry_and_error_is_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, request=request, text="internal stack trace")

    monkeypatch.setenv("CRM_API_KEY", "test-http-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        function = HttpTool(
            name="create_customer",
            url="https://crm.example.test/customers",
            method="POST",
            headers_env={"X-API-Key": "CRM_API_KEY"},
            timeout_s=8,
            say_while_running=None,
            description=None,
            client=client,
            retry_delay_s=0,
        )
        execution = await ToolExecutor().execute(
            function,
            {"_json": {"name": "Ada"}},
        )

    assert attempts == 1
    assert execution.error is not None
    assert execution.error.code == "tool_failed"
    assert "stack trace" not in str(execution.for_llm())


@pytest.mark.asyncio
async def test_http_missing_env_is_safe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_API_KEY", raising=False)
    function = tool.http(
        name="missing_key",
        url="https://api.example.test/items",
        headers_env={"Authorization": "MISSING_API_KEY"},
    )

    execution = await ToolExecutor().execute(function, {})

    assert execution.error is not None
    assert execution.error.code == "tool_failed"
    assert "MISSING_API_KEY" not in str(execution.for_llm())

    invalid_arguments = await ToolExecutor().execute(
        function,
        {"undeclared": "value"},
    )
    assert invalid_arguments.error is not None
    assert invalid_arguments.error.code == "invalid_arguments"

    invalid_query = await ToolExecutor().execute(
        function,
        {"_query": {"nested": ["not", "scalar"]}},
    )
    assert invalid_query.error is not None
    assert invalid_query.error.code == "invalid_arguments"


def test_invalid_executor_and_http_configuration_are_cataloged() -> None:
    with pytest.raises(VoicekitError) as timeout:
        ToolExecutor(timeout_s=0)
    with pytest.raises(VoicekitError) as method:
        tool.http(
            name="invalid",
            url="https://api.example.test",
            method="TRACE",
        )
    with pytest.raises(VoicekitError) as inline_secret:
        tool.http(
            name="inline_secret",
            url="https://api.example.test",
            headers_env={"Authorization": "Bearer inline-value"},
        )
    with pytest.raises(VoicekitError) as invalid_name:
        tool.http(
            name="not-valid",
            url="https://api.example.test",
        )
    with pytest.raises(VoicekitError) as unsafe_template:
        tool.http(
            name="unsafe_template",
            url="https://api.example.test/{customer.id}",
        )

    assert timeout.value.code == "VK-TOL-003"
    assert method.value.code == "VK-TOL-004"
    assert inline_secret.value.code == "VK-TOL-004"
    assert invalid_name.value.code == "VK-TOL-002"
    assert unsafe_template.value.code == "VK-TOL-004"
