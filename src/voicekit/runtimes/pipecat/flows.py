"""Adapters from shared tools to native Pipecat Flows function schemas."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from pipecat.flows import FlowManager, FlowsFunctionSchema, NodeConfig
from pipecat.frames.frames import EndFrame, TTSSpeakFrame

from voicekit.config.models import ToolReference
from voicekit.errors import VoicekitError
from voicekit.storage.models import EndedReason
from voicekit.tools import ToolExecutor, get_tool_metadata, load_tools
from voicekit.tools.execution import ToolObservationSink, tool_execution_context

FlowHandler = Callable[[dict[str, Any], FlowManager], Awaitable[tuple[Any, NodeConfig | None]]]


class TransferHandler(Protocol):
    """Runtime hook used by the native transfer tool."""

    async def __call__(self, call_id: str, number: str) -> None: ...


class WarmTransferHandler(Protocol):
    """Runtime hook for a consented private-briefing conference handoff."""

    async def __call__(
        self,
        call_id: str,
        number: str,
        briefing: str,
        set_reason: Callable[[EndedReason | None], None],
    ) -> None: ...


class LanguageFallbackHandler(Protocol):
    """Runtime hook used by the native language-fallback tool."""

    async def __call__(self) -> None: ...


def shared_flow_tools(
    references: str | list[ToolReference],
    *,
    call_id: str,
    sink: ToolObservationSink,
    executor: ToolExecutor | None = None,
) -> list[FlowsFunctionSchema]:
    """Expose shared decorated tools as global native Flows schemas."""
    active_executor = executor or ToolExecutor()
    schemas: list[FlowsFunctionSchema] = []
    for function in load_tools(references):
        metadata = get_tool_metadata(function)

        async def handler(
            args: dict[str, Any],
            flow_manager: FlowManager,
            *,
            _function: Callable[..., Any] = function,
            _say: str | None = metadata.say_while_running,
        ) -> tuple[Any, NodeConfig | None]:
            if _say:
                await flow_manager.worker.queue_frame(TTSSpeakFrame(text=_say))
            with tool_execution_context(call_id, sink):
                result = await active_executor.execute(_function, args)
            return result.for_llm(), None

        schemas.append(
            FlowsFunctionSchema(
                name=metadata.name,
                description=metadata.description,
                properties=cast(
                    "dict[str, Any]",
                    dict(metadata.parameters_schema.get("properties", {})),
                ),
                required=list(cast("list[str]", metadata.parameters_schema.get("required", []))),
                handler=cast(FlowHandler, handler),
                cancel_on_interruption=False,
                timeout_secs=active_executor.timeout_s + 1,
            )
        )
    return schemas


def transfer_flow_tool(
    *,
    call_id: str,
    number: str,
    transfer: TransferHandler,
) -> FlowsFunctionSchema:
    """Create the native global tool only when transfer is configured."""

    async def handler(
        _args: dict[str, Any],
        flow_manager: FlowManager,
    ) -> tuple[dict[str, object], NodeConfig | None]:
        await transfer(call_id, number)
        await flow_manager.worker.queue_frame(EndFrame(reason="transferred"))
        return {"ok": True, "status": "transferred"}, None

    return FlowsFunctionSchema(
        name="transfer_to_human",
        description="Transfer the current phone call to the configured human destination.",
        properties={},
        required=[],
        handler=handler,
        cancel_on_interruption=False,
        timeout_secs=15,
    )


def warm_transfer_flow_tool(
    *,
    call_id: str,
    number: str,
    transfer: WarmTransferHandler,
    set_reason: Callable[[EndedReason | None], None],
    timeout_s: float,
) -> FlowsFunctionSchema:
    """Expose a native, consent-gated private warm-handoff function."""

    async def handler(
        args: dict[str, Any],
        flow_manager: FlowManager,
    ) -> tuple[dict[str, object], NodeConfig | None]:
        briefing = args.get("briefing")
        if args.get("caller_consented") is not True:
            raise VoicekitError(
                "VK-TEL-012",
                detail="warm transfer requires explicit caller consent.",
            )
        if not isinstance(briefing, str):
            raise VoicekitError(
                "VK-TEL-012",
                detail="warm transfer requires one private briefing string.",
            )
        await transfer(call_id, number, briefing, set_reason)
        await flow_manager.worker.queue_frame(EndFrame(reason="transferred"))
        return {"ok": True, "status": "transferred"}, None

    return FlowsFunctionSchema(
        name="warm_transfer_to_human",
        description=(
            "After explicit caller consent, privately brief the configured human, "
            "wait for their acceptance, then bridge the caller."
        ),
        properties={
            "briefing": {
                "type": "string",
                "description": (
                    "A concise private handoff summary of at most 500 characters; "
                    "exclude credentials and unrelated sensitive data."
                ),
                "minLength": 1,
                "maxLength": 500,
            },
            "caller_consented": {
                "type": "boolean",
                "description": "Must be true only after the caller explicitly consents.",
                "const": True,
            },
        },
        required=["briefing", "caller_consented"],
        handler=handler,
        cancel_on_interruption=False,
        timeout_secs=timeout_s + 5,
    )


def language_fallback_flow_tool(
    *,
    language: str,
    activate: LanguageFallbackHandler,
) -> FlowsFunctionSchema:
    """Allow a native flow/model to switch both speech services atomically."""

    async def handler(
        _args: dict[str, Any],
        _flow_manager: FlowManager,
    ) -> tuple[dict[str, object], NodeConfig | None]:
        await activate()
        return {"ok": True, "language": language}, None

    return FlowsFunctionSchema(
        name="switch_to_fallback_language",
        description=f"Switch speech recognition and synthesis to {language}.",
        properties={},
        required=[],
        handler=handler,
        cancel_on_interruption=False,
        timeout_secs=5,
    )


async def initialize_native_flow(reference: str, flow_manager: FlowManager) -> NodeConfig:
    """Load a native NodeConfig factory and initialize FlowManager once."""
    module_name, attribute = reference.split(":", maxsplit=1)
    try:
        entry = getattr(importlib.import_module(module_name), attribute)
        node = await _call_entry(entry, flow_manager)
        _validate_node(node)
        await flow_manager.initialize(node)
        return cast(NodeConfig, node)
    except VoicekitError:
        raise
    except Exception as exc:
        raise VoicekitError(
            "VK-RUN-003",
            detail=f"native Pipecat flow {reference!r} could not initialize.",
        ) from exc


async def _call_entry(entry: object, flow_manager: FlowManager) -> Any:
    if isinstance(entry, Mapping):
        mapping = cast("Mapping[str, Any]", entry)
        return dict(mapping)
    if not callable(entry):
        raise VoicekitError(
            "VK-RUN-003",
            detail="flow entrypoint must be a NodeConfig or callable factory.",
        )
    signature = inspect.signature(entry)
    if len(signature.parameters) == 0:
        result = entry()
    elif len(signature.parameters) == 1:
        result = entry(flow_manager)
    else:
        raise VoicekitError(
            "VK-RUN-003",
            detail="flow entrypoint accepts only zero args or one FlowManager arg.",
        )
    if inspect.isawaitable(result):
        return await result
    return result


def _validate_node(node: object) -> None:
    if not isinstance(node, dict) or not isinstance(node.get("task_messages"), list):
        raise VoicekitError(
            "VK-RUN-003",
            detail="flow entrypoint must return a native NodeConfig with task_messages.",
        )
