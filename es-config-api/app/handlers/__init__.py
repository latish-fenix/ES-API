from __future__ import annotations

from ..errors import not_found
from .base import Handler, Plan, State
from .mapping import IndexMappingHandler
from .named import (ComponentTemplateHandler, IlmPolicyHandler, IndexTemplateHandler,
                    IngestPipelineHandler)
from .settings import ClusterSettingsHandler, IndexSettingsHandler

HANDLERS: dict[str, Handler] = {h.type_name: h for h in (
    ClusterSettingsHandler(), IndexSettingsHandler(), IndexMappingHandler(),
    IndexTemplateHandler(), ComponentTemplateHandler(), IlmPolicyHandler(),
    IngestPipelineHandler(),
)}

NAMED_TYPES = [t for t, h in HANDLERS.items() if h.resource_kind == "named"]


def get_handler(type_name: str) -> Handler:
    h = HANDLERS.get(type_name)
    if not h:
        raise not_found("UNKNOWN_CONFIG_TYPE", f"Unknown config type '{type_name}'")
    return h


__all__ = ["HANDLERS", "NAMED_TYPES", "get_handler", "Handler", "Plan", "State"]
