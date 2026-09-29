"""Index mappings on existing indices: add-only, never rolled back.

Elasticsearch lets you add fields but not change or remove existing ones, so
an applied mapping change is permanent. The dry run says so explicitly.
"""
from __future__ import annotations

import copy

from ..clusters import es_call
from ..errors import ApiError, bad_request, not_found, unprocessable
from ..util import canonical_json, flatten
from .base import Handler, Plan, State

# Runtime fields are replaced wholesale by ES, so they are not add-only: not allowed here.
ALLOWED_TOP_LEVEL = {"properties"}


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


_MISSING = object()


def _check_fields(existing: dict, requested: dict, prefix: str, conflicts: list[dict]) -> None:
    """Existing fields may gain new sub-fields / params, but nothing they have may change."""
    for name, req in requested.items():
        path = f"{prefix}{name}"
        if not isinstance(req, dict):
            conflicts.append({"path": path, "current": None, "requested": req,
                              "reason": "field definition must be an object"})
            continue
        ex = existing.get(name)
        if not isinstance(ex, dict):
            continue  # a new field
        for key, value in req.items():
            if key == "properties" and isinstance(value, dict):
                _check_fields(ex.get("properties", {}), value, f"{path}.", conflicts)
            elif key == "fields" and isinstance(value, dict):
                _check_fields(ex.get("fields", {}), value, f"{path}.fields.", conflicts)
            else:
                current = ex.get(key, _MISSING)
                if current is _MISSING and key == "type" and "properties" in ex:
                    current = "object"
                if current is not _MISSING and canonical_json(current) != canonical_json(value):
                    conflicts.append({"path": f"{path}.{key}", "current": current, "requested": value})


class IndexMappingHandler(Handler):
    type_name = "index-mappings"
    label = "index mapping"
    resource_kind = "index"
    rollback_supported = False
    allowlist_scope = "resource"

    def fetch(self, es, resource):
        try:
            body = es_call(es, "GET", f"/{resource}/_mapping")
        except ApiError as e:
            if e.status == 404:
                raise not_found("INDEX_NOT_FOUND", f"Index '{resource}' does not exist") from e
            raise
        if list(body) != [resource]:
            raise unprocessable("NOT_A_CONCRETE_INDEX",
                                f"'{resource}' resolves to {sorted(body)}; use the concrete index name")
        return State(True, body[resource].get("mappings", {}))

    def plan_update(self, es, resource, current, config):
        if not isinstance(config, dict) or not config:
            raise bad_request("INVALID_CONFIG", "'config' must be {\"properties\": {...}}")
        extra = set(config) - ALLOWED_TOP_LEVEL
        if extra:
            raise unprocessable("MAPPING_NOT_ADD_ONLY",
                                f"Only {sorted(ALLOWED_TOP_LEVEL)} can be changed; got {sorted(extra)}")
        nulls = [p for p, v in flatten(config).items() if v is None or v == {}]
        if nulls:
            raise unprocessable("MAPPING_NOT_ADD_ONLY", "null / empty values are not allowed", nulls)
        if not isinstance(config["properties"], dict):
            raise bad_request("INVALID_CONFIG", "'properties' must be an object")
        conflicts: list[dict] = []
        _check_fields((current.config or {}).get("properties", {}), config["properties"], "",
                      conflicts)
        if conflicts:
            raise unprocessable("MAPPING_CONFLICT",
                                "Existing fields cannot be changed; only new fields can be added",
                                conflicts)
        target = State(True, deep_merge(current.config or {}, config))
        return Plan(target=target, permanent=True, apply_body=config,
                    warnings=["Mapping changes are permanent: new fields cannot be removed "
                              "and this change cannot be rolled back"])

    def apply(self, es, resource, current, target, plan=None):
        if plan is None or plan.apply_body is None:
            raise unprocessable("ROLLBACK_NOT_SUPPORTED", "Mappings cannot be rolled back")
        es_call(es, "PUT", f"/{resource}/_mapping", body=plan.apply_body)
