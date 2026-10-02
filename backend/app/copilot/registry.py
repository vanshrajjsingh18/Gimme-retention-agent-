"""The approved tools the Copilot may call, and the contract they return.

The AI never touches the database, SQL, Python or a shell. It chooses among
the functions registered here, each of which wraps an existing engine service
and declares how dangerous it is:

* ``READ`` runs as soon as the model asks for it;
* ``WRITE`` is planned, previewed, and only executed when a person confirms
  that specific plan;
* ``HIGH_RISK_WRITE`` is the same, for anything that switches sending on,
  reaches customers, or cancels work in flight.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.entities import CopilotConversation, User

logger = logging.getLogger("app.copilot")


class Risk(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    HIGH_RISK_WRITE = "HIGH_RISK_WRITE"


class ToolError(Exception):
    """A tool refused or failed; the message is safe to show the operator."""


@dataclass
class ToolResult:
    success: bool
    data: Any = None
    metadata: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    #: Where the conversation's focus should move, e.g. ("automation", 12).
    focus: tuple[str, int] | None = None
    #: A customer list later turns can refer to as "them".
    result_set: dict | None = None
    #: For writes: what the target looked like before and after.
    target_type: str | None = None
    target_id: int | str | None = None
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, data: Any = None, **kwargs) -> "ToolResult":
        return cls(success=True, data=data, **kwargs)

    @classmethod
    def fail(cls, *errors: str, **kwargs) -> "ToolResult":
        return cls(success=False, errors=list(errors), **kwargs)

    def as_dict(self) -> dict:
        """The contract every tool returns to the model and the UI."""
        metadata = {"generated_at": utcnow().isoformat(), "source": "database", **self.metadata}
        if isinstance(self.data, list) and "count" not in metadata:
            metadata["count"] = len(self.data)
        return {
            "success": self.success,
            "data": self.data,
            "metadata": metadata,
            "errors": self.errors,
        }


@dataclass
class ToolContext:
    db: Session
    user: User
    conversation: CopilotConversation | None
    now: datetime
    #: True while a write is being previewed inside a rolled-back transaction.
    previewing: bool = False
    #: Every user message in the conversation, for provenance checks.
    user_text: str = ""

    @property
    def active(self) -> tuple[str | None, int | None]:
        if self.conversation is None:
            return None, None
        return self.conversation.active_entity_type, self.conversation.active_entity_id

    @property
    def state(self) -> dict:
        return dict(self.conversation.working_state or {}) if self.conversation else {}


Handler = Callable[..., ToolResult]
Previewer = Callable[[ToolContext, dict, ToolResult], dict]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    risk: Risk
    handler: Handler
    group: str
    #: Extra preview work run inside the sandbox after the handler, e.g. a dry
    #: run of the campaign that was just created there.
    previewer: Previewer | None = None
    #: A confirmed create with identical arguments is not run twice.
    creates: bool = False
    #: Whether the handler is safe to run inside the preview sandbox. Only
    #: false for something with effects outside the database.
    sandbox_safe: bool = True
    summarize: Callable[[dict], str] | None = None

    @property
    def is_write(self) -> bool:
        return self.risk != Risk.READ

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": f"[{self.risk.value}] {self.description}",
            "parameters": self.parameters,
        }

    def describe(self) -> dict:
        return {
            "name": self.name,
            "group": self.group,
            "risk": self.risk.value,
            "description": self.description,
            "parameters": self.parameters,
            "requires_confirmation": self.is_write,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is registered twice.")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]


registry = ToolRegistry()


def tool(
    name: str,
    *,
    description: str,
    risk: Risk,
    group: str,
    properties: dict | None = None,
    required: list[str] | None = None,
    previewer: Previewer | None = None,
    creates: bool = False,
    sandbox_safe: bool = True,
    summarize: Callable[[dict], str] | None = None,
):
    """Register a function as a Copilot tool."""

    def decorator(fn: Handler) -> Handler:
        registry.register(
            Tool(
                name=name,
                description=description,
                parameters={
                    "type": "object",
                    "properties": properties or {},
                    "required": required or [],
                    "additionalProperties": False,
                },
                risk=risk,
                handler=fn,
                group=group,
                previewer=previewer,
                creates=creates,
                sandbox_safe=sandbox_safe,
                summarize=summarize,
            )
        )
        return fn

    return decorator


# --------------------------------------------------------------------------
# Argument validation
# --------------------------------------------------------------------------
_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_arguments(tool_def: Tool, args: Any) -> dict:
    """Check the model's arguments against the tool schema before anything runs.

    Deliberately strict: an unknown key is an error rather than ignored, so a
    model that invents a parameter is told so instead of having it silently
    dropped and the operator shown a plan that is not what was asked.
    """
    if not isinstance(args, dict):
        raise ToolError("Tool arguments must be a JSON object.")
    props = tool_def.parameters.get("properties", {})
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolError(f"Unknown argument(s) for {tool_def.name}: {', '.join(unknown)}.")
    missing = [k for k in tool_def.parameters.get("required", []) if args.get(k) in (None, "")]
    if missing:
        raise ToolError(f"Missing required argument(s) for {tool_def.name}: {', '.join(missing)}.")
    clean: dict = {}
    for key, value in args.items():
        if value is None:
            continue
        spec = props[key]
        expected = spec.get("type")
        python_type = _TYPES.get(expected)
        if expected == "integer" and isinstance(value, float) and value.is_integer():
            value = int(value)
        if expected == "number" and isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                pass
        if expected == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value)
        if python_type and (
            not isinstance(value, python_type) or (expected in ("integer", "number") and isinstance(value, bool))
        ):
            raise ToolError(f"Argument '{key}' for {tool_def.name} must be of type {expected}.")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolError(
                f"Argument '{key}' for {tool_def.name} must be one of: {', '.join(map(str, spec['enum']))}."
            )
        clean[key] = value
    return clean
