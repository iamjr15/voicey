"""Environment-authenticated HTTP-backed tool implementation."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from string import Formatter
from typing import Any, Literal, TypeAlias, cast
from urllib.parse import quote, urlsplit

import httpx
from pydantic import JsonValue, TypeAdapter

from voicey.errors import VoiceyError
from voicey.tools.core import ToolMetadata, set_tool_metadata, validate_tool_name

HttpMethod: TypeAlias = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
_ENV_TEMPLATE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_HEADER_ENV_VALUE = re.compile(
    r"(?:[A-Z][A-Z0-9_]*|(?:[A-Za-z][A-Za-z0-9._~-]* )?\$\{[A-Z][A-Z0-9_]*\})"
)
_PATH_PARAMETER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class HttpTool:
    """Callable HTTP endpoint with secret-free metadata and GET-only retry."""

    def __init__(
        self,
        *,
        name: str,
        url: str,
        method: str,
        headers_env: Mapping[str, str],
        timeout_s: float,
        say_while_running: str | None,
        mutating: bool = False,
        description: str | None = None,
        client: httpx.AsyncClient | None = None,
        retry_delay_s: float = 0.1,
    ) -> None:
        validate_tool_name(name)
        normalized_method = method.upper()
        if normalized_method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise VoiceyError("VY-TOL-004", detail=f"unsupported method {method!r}.")
        if timeout_s <= 0:
            raise VoiceyError("VY-TOL-004", detail="timeout_s must be positive.")
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise VoiceyError("VY-TOL-004", detail=f"invalid HTTP tool URL {url!r}.")
        for header, template in headers_env.items():
            if not header or not _HEADER_ENV_VALUE.fullmatch(template):
                raise VoiceyError(
                    "VY-TOL-004",
                    detail="headers_env values must reference environment variables.",
                )
        self.__name__ = name
        self.url = url
        self.method = normalized_method
        self.headers_env = dict(headers_env)
        self.timeout_s = timeout_s
        self._client = client
        self._retry_delay_s = retry_delay_s
        try:
            parsed_fields = [
                field_name
                for _, field_name, format_spec, conversion in Formatter().parse(url)
                if field_name is not None
                and not format_spec
                and conversion is None
                and _PATH_PARAMETER.fullmatch(field_name)
            ]
            field_count = sum(
                field_name is not None for _, field_name, _, _ in Formatter().parse(url)
            )
        except ValueError as exc:
            raise VoiceyError("VY-TOL-004", detail="invalid URL parameter template.") from exc
        if len(parsed_fields) != field_count:
            raise VoiceyError(
                "VY-TOL-004",
                detail="URL parameters must be simple names without conversion or formatting.",
            )
        path_parameters = tuple(dict.fromkeys(parsed_fields))
        self._path_parameters = frozenset(path_parameters)
        self._allowed_arguments = self._path_parameters | {"_query"}
        if normalized_method != "GET":
            self._allowed_arguments = self._allowed_arguments | {"_json"}
        properties: dict[str, Any] = {
            parameter: {"type": "string"} for parameter in path_parameters
        }
        properties["_query"] = {
            "type": "object",
            "additionalProperties": {"type": ["string", "number", "integer", "boolean", "null"]},
        }
        if normalized_method != "GET":
            properties["_json"] = {}
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if path_parameters:
            schema["required"] = list(path_parameters)
        set_tool_metadata(
            self,
            ToolMetadata(
                name=name,
                description=description or f"{normalized_method} {url}",
                parameters_schema=schema,
                return_schema={},
                say_while_running=say_while_running,
                mutating=mutating,
                is_async=True,
                source="http",
            ),
        )

    def validate_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Check required path fields and reject undeclared HTTP inputs."""
        supplied = set(arguments)
        missing = self._path_parameters - supplied
        extra = supplied - self._allowed_arguments
        if missing or extra:
            raise ValueError("HTTP tool arguments do not match its schema")
        query = arguments.get("_query")
        if query is not None and not isinstance(query, Mapping):
            raise ValueError("HTTP _query must be a mapping")
        if any(not isinstance(arguments[name], str) for name in self._path_parameters):
            raise ValueError("HTTP URL parameters must be strings")
        query_mapping = None if query is None else cast("Mapping[object, object]", query)
        if query_mapping is not None and any(
            not isinstance(key, str) or not isinstance(value, (str, int, float, bool, type(None)))
            for key, value in query_mapping.items()
        ):
            raise ValueError("HTTP _query values must be scalar")
        validated = dict(arguments)
        if "_json" in validated:
            validated["_json"] = JsonValueAdapter.validate_python(validated["_json"])
        return validated

    async def __call__(self, **arguments: Any) -> JsonValue:
        """Resolve env headers, interpolate path args, and execute the request."""
        headers = {
            header: _expand_environment(template) for header, template in self.headers_env.items()
        }
        query = arguments.pop("_query", None)
        json_body = arguments.pop("_json", None)
        try:
            url = self.url.format(
                **{key: quote(str(value), safe="") for key, value in arguments.items()}
            )
        except KeyError as exc:
            raise VoiceyError(
                "VY-TOL-004",
                detail=f"missing URL parameter {exc.args[0]!r}.",
            ) from exc
        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        owns_client = self._client is None
        attempts = 2 if self.method == "GET" else 1
        last_status: int | None = None
        try:
            for attempt in range(attempts):
                try:
                    response = await client.request(
                        self.method,
                        url,
                        headers=headers,
                        params=query,
                        json=json_body,
                        timeout=self.timeout_s,
                    )
                    last_status = response.status_code
                    if response.status_code < 500 and response.status_code != 429:
                        response.raise_for_status()
                        return _response_value(response)
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt + 1 >= attempts:
                        raise
                if attempt + 1 < attempts:
                    await asyncio.sleep(self._retry_delay_s)
            raise VoiceyError(
                "VY-TOL-004",
                detail=f"remote tool returned HTTP {last_status}.",
            )
        finally:
            if owns_client:
                await client.aclose()


def _expand_environment(template: str) -> str:
    names = _ENV_TEMPLATE.findall(template)
    if not names and re.fullmatch(r"[A-Z][A-Z0-9_]*", template):
        names = [template]
        template = f"${{{template}}}"
    expanded = template
    for name in names:
        value = os.environ.get(name)
        if not value:
            raise VoiceyError("VY-TOL-004", detail=f"missing environment variable {name}.")
        expanded = expanded.replace(f"${{{name}}}", value)
    return expanded


def _response_value(response: httpx.Response) -> JsonValue:
    if not response.content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        return response.json()
    return response.text


JsonValueAdapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
