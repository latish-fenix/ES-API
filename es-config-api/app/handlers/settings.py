"""Cluster settings (persistent only) and dynamic index settings.

Both are partial updates: send only the keys to change; ``null`` resets a key
to its default. Stored and compared in flat form (``a.b.c: "value"``).
"""
from __future__ import annotations

from typing import Any

from elasticsearch import Elasticsearch

from ..clusters import es_call
from ..errors import ApiError, bad_request, not_found, unprocessable
from ..util import flatten, matches_any
from .base import Handler, Plan, State

# Read-only / internal index settings: never snapshotted, never written.
INTERNAL_INDEX_KEYS = [
    "index.creation_date", "index.uuid", "index.version.*", "index.provided_name",
    "index.resize.*", "index.shrink.*", "index.routing.allocation.initial_recovery.*",
    "index.history.uuid", "index.verified_before_close", "index.downsample.*",
    "index.frozen", "index.search.throttled",
]

# Static index settings: need the index closed, so v1 refuses them.
STATIC_INDEX_KEYS = [
    "index.number_of_shards", "index.number_of_routing_shards", "index.codec",
    "index.routing_partition_size", "index.soft_deletes.*",
    "index.load_fixed_bitset_filters_eagerly", "index.shard.check_on_startup",
    "index.mode", "index.sort.*", "index.analysis.*", "index.similarity.*",
    "index.store.type", "index.store.preload", "index.routing_path",
    "index.mapping.source.mode", "index.time_series.start_time",
]


def _norm_value(key: str, value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return [_norm_value(key, v) for v in value]
    raise bad_request("INVALID_SETTING_VALUE", f"Unsupported value for '{key}'")


def normalise_request(config: Any, key_prefix: str | None = None) -> dict[str, Any]:
    if not isinstance(config, dict) or not config:
        raise bad_request("INVALID_CONFIG", "'config' must be a non-empty object of settings")
    flat = flatten(config)
    out: dict[str, Any] = {}
    for key, value in flat.items():
        if isinstance(value, dict):
            raise bad_request("INVALID_SETTING_VALUE", f"'{key}' has an empty object value")
        if key_prefix and not key.startswith(key_prefix):
            key = key_prefix + key
        out[key] = _norm_value(key, value)
    return out


def _payload(current: dict, target: dict) -> dict[str, Any]:
    payload = {k: v for k, v in target.items() if current.get(k) != v}
    payload.update({k: None for k in current if k not in target})
    return payload


class _SettingsHandler(Handler):
    allowlist_scope = "keys"
    key_prefix: str | None = None

    def plan_update(self, es, resource, current, config):
        requested = normalise_request(config, self.key_prefix)
        self._check_keys(list(requested))
        target = dict(current.config or {})
        for key, value in requested.items():
            if value is None:
                target.pop(key, None)
            else:
                target[key] = value
        return Plan(target=State(True, target))

    def _check_keys(self, keys: list[str]) -> None:
        return None


class ClusterSettingsHandler(_SettingsHandler):
    type_name = "cluster-settings"
    label = "cluster settings"
    resource_kind = "cluster"

    def fetch(self, es, resource):
        body = es_call(es, "GET", "/_cluster/settings", params={"flat_settings": "true"})
        return State(True, dict(body.get("persistent", {})))

    def read_warnings(self, es, resource):
        body = es_call(es, "GET", "/_cluster/settings", params={"flat_settings": "true"})
        transient = body.get("transient") or {}
        if transient:
            return [f"Cluster has {len(transient)} transient setting(s) "
                    f"({', '.join(sorted(transient)[:5])}); transient settings are deprecated "
                    "and not managed by this API"]
        return []

    def apply(self, es, resource, current, target, plan=None):
        payload = _payload(current.config or {}, target.config or {})
        if payload:
            es_call(es, "PUT", "/_cluster/settings", body={"persistent": payload})


class IndexSettingsHandler(_SettingsHandler):
    type_name = "index-settings"
    label = "index settings"
    resource_kind = "index"
    key_prefix = "index."

    def fetch(self, es, resource):
        try:
            body = es_call(es, "GET", f"/{resource}/_settings", params={"flat_settings": "true"})
        except ApiError as e:
            if e.status == 404:
                raise not_found("INDEX_NOT_FOUND", f"Index '{resource}' does not exist") from e
            raise
        if list(body) != [resource]:
            raise unprocessable("NOT_A_CONCRETE_INDEX",
                                f"'{resource}' resolves to {sorted(body)}; use the concrete index name")
        settings = body[resource].get("settings", {})
        return State(True, {k: v for k, v in settings.items()
                            if not matches_any(k, INTERNAL_INDEX_KEYS)})

    def _check_keys(self, keys):
        internal = [k for k in keys if matches_any(k, INTERNAL_INDEX_KEYS)]
        if internal:
            raise unprocessable("READ_ONLY_SETTING", "These settings are read-only", internal)
        static = [k for k in keys if matches_any(k, STATIC_INDEX_KEYS)]
        if static:
            raise unprocessable("STATIC_SETTING",
                                "Static settings need the index closed and are not supported",
                                static)

    def apply(self, es, resource, current, target, plan=None):
        payload = _payload(current.config or {}, target.config or {})
        self._check_keys(list(payload))
        if payload:
            es_call(es, "PUT", f"/{resource}/_settings", body=payload)
