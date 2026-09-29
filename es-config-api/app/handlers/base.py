"""Common contract every config type implements."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from elasticsearch import Elasticsearch

from ..errors import bad_request
from ..util import config_hash

_INDEX_RE = re.compile(r"^[a-z0-9.][a-z0-9._+-]{0,254}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9@._+-]{1,255}$")


@dataclass
class State:
    """A config as it exists (or doesn't) in the cluster."""
    exists: bool
    config: dict | None

    @property
    def version(self) -> str:
        return config_hash(self.exists, self.config)

    def to_dict(self) -> dict:
        return {"exists": self.exists, "config": self.config}

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        return cls(exists=bool(d.get("exists")), config=d.get("config"))


@dataclass
class Plan:
    target: State
    warnings: list[str] = field(default_factory=list)
    permanent: bool = False          # true when the change cannot be rolled back
    apply_body: Any = None           # set when the ES call body differs from the target


class Handler:
    type_name: str = ""              # URL segment and allowlist key, e.g. "ilm-policies"
    label: str = ""
    resource_kind: str = "named"     # "cluster" | "index" | "named"
    rollback_supported: bool = True
    allowlist_scope: str = "resource"  # "keys" = setting keys, "resource" = resource name

    # -- resource names -------------------------------------------------------
    def check_resource(self, name: str) -> None:
        if self.resource_kind == "cluster":
            return
        if self.resource_kind == "index":
            if name in ("_all",) or any(c in name for c in "*?,") or not _INDEX_RE.match(name):
                raise bad_request("INVALID_INDEX_NAME",
                                  "Use one concrete index name (no wildcards, commas or _all)")
            return
        if not _NAME_RE.match(name):
            raise bad_request("INVALID_RESOURCE_NAME", f"Invalid {self.label} name '{name}'")

    # -- to implement ---------------------------------------------------------
    def fetch(self, es: Elasticsearch, resource: str) -> State:
        raise NotImplementedError

    def list(self, es: Elasticsearch) -> list[str]:
        raise NotImplementedError

    def plan_update(self, es: Elasticsearch, resource: str, current: State, config: Any) -> Plan:
        raise NotImplementedError

    def apply(self, es: Elasticsearch, resource: str, current: State, target: State,
              plan: Plan | None = None) -> None:
        raise NotImplementedError

    # -- optional -------------------------------------------------------------
    def allowlist_names(self, resource: str, changed_paths: list[str]) -> list[str]:
        return changed_paths if self.allowlist_scope == "keys" else [resource]

    def simulate(self, es: Elasticsearch, resource: str, target: State,
                 sample_docs: list[dict] | None) -> dict | None:
        return None

    def read_warnings(self, es: Elasticsearch, resource: str) -> list[str]:
        return []
