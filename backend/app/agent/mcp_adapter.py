"""In-process MCP adapter for the shared read-only capability boundary.

The adapter deliberately binds one authenticated principal when it is created.
It does not expose an HTTP listener and it never accepts an owner id as a tool
argument. A later transport can wrap the returned ``MCPServer`` after its own
authentication and lifecycle decisions are reviewed.
"""

from __future__ import annotations

import inspect
from typing import Any

from mcp.server import MCPServer
from pydantic_core import PydanticUndefined

from .capabilities import (
    CapabilityPrincipal,
    CapabilityRegistry,
)


def _field_default(field: Any) -> Any:
    """Translate a Pydantic field into an inspect signature default."""
    if field.is_required():
        return inspect.Parameter.empty
    if field.default is not PydanticUndefined:
        return field.default
    if field.default_factory is not None:
        return field.default_factory()
    return inspect.Parameter.empty


def _tool_callable(
    name: str,
    description: str,
    request_model: type[Any],
    response_model: type[Any],
    registry: CapabilityRegistry,
    principal: CapabilityPrincipal,
) -> Any:
    """Create an SDK-compatible callable from the registry's typed model.

    The SDK derives a flat object schema from the callable signature. Building
    that signature from the existing request model keeps the protocol surface
    synchronized without re-declaring fields or handlers in this adapter.
    """
    parameters: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for field_name, field in request_model.model_fields.items():
        annotation = field.annotation
        annotations[field_name] = annotation
        parameters.append(
            inspect.Parameter(
                field_name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=_field_default(field),
            )
        )
    annotations["return"] = response_model

    def call(**arguments: Any) -> Any:
        return registry.invoke(name, arguments, principal=principal)

    call.__name__ = name
    call.__qualname__ = name
    call.__doc__ = description
    call.__annotations__ = annotations
    call.__signature__ = inspect.Signature(parameters, return_annotation=response_model)
    return call


class CapabilityMCPServer(MCPServer):
    """MCPServer bound to one registry and one trusted principal."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        principal: CapabilityPrincipal,
        *,
        name: str = "omicsprism-readonly",
        version: str = "readonly-tools.v1",
    ) -> None:
        super().__init__(
            name=name,
            version=version,
            description="Ownership-bound OmicsPrism read-only capabilities.",
        )
        self.registry = registry
        self.principal = principal
        for spec in registry.specs():
            request_model, response_model = registry.model_types(spec.name)
            self.add_tool(
                _tool_callable(
                    spec.name,
                    spec.description,
                    request_model,
                    response_model,
                    registry,
                    principal,
                ),
                name=spec.name,
                description=spec.description,
                structured_output=True,
            )
            # Publish the registry's strict schemas verbatim. The SDK's
            # generated argument model intentionally permits unknown keys for
            # general MCP tools; ``call_tool`` performs the strict boundary
            # validation before that model runs.
            tool = self._tool_manager.get_tool(spec.name)
            if tool is None:  # pragma: no cover - add_tool above is deterministic
                raise RuntimeError(f"MCP tool registration failed: {spec.name}")
            tool.parameters = spec.request_schema
            tool.fn_metadata.output_schema = spec.response_schema

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any | None = None,
    ) -> Any:
        """Preserve strict registry validation before SDK argument handling."""
        self.registry.validate(name, arguments, principal=self.principal)
        return await super().call_tool(name, arguments, context)


def build_readonly_mcp_server(
    registry: CapabilityRegistry,
    principal: CapabilityPrincipal,
    *,
    name: str = "omicsprism-readonly",
) -> CapabilityMCPServer:
    """Build a process-local MCP server with no transport side effects."""
    return CapabilityMCPServer(registry, principal, name=name)


__all__ = ["CapabilityMCPServer", "build_readonly_mcp_server"]
