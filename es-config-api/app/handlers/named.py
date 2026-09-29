"""Whole-object resources: index templates, component templates, ILM policies,
ingest pipelines. Update = full replace; rollback = put the old body back, or
delete the resource if it did not exist before."""
from __future__ import annotations

from elasticsearch import Elasticsearch

from ..clusters import es_call
from ..errors import ApiError, bad_request
from .base import Handler, Plan, State


class _NamedHandler(Handler):
    resource_kind = "named"
    base_path = ""

    def _extract(self, body: dict, name: str) -> dict | None:
        raise NotImplementedError

    def _names(self, body: dict) -> list[str]:
        raise NotImplementedError

    def fetch(self, es, resource):
        try:
            body = es_call(es, "GET", f"{self.base_path}/{resource}")
        except ApiError as e:
            if e.status == 404:
                return State(False, None)
            raise
        found = self._extract(body or {}, resource)
        return State(found is not None, found)

    def list(self, es):
        try:
            return sorted(self._names(es_call(es, "GET", self.base_path) or {}))
        except ApiError as e:
            if e.status == 404:
                return []
            raise

    def _validate(self, config) -> None:
        if not isinstance(config, dict) or not config:
            raise bad_request("INVALID_CONFIG", f"'config' must be the full {self.label} body")

    def plan_update(self, es, resource, current, config):
        self._validate(config)
        warnings = [] if current.exists else [f"{self.label} '{resource}' does not exist yet; "
                                              "it will be created (rollback deletes it)"]
        return Plan(target=State(True, config), warnings=warnings)

    def apply(self, es, resource, current, target, plan=None):
        if target.exists:
            es_call(es, "PUT", f"{self.base_path}/{resource}", body=target.config)
        elif current.exists:
            es_call(es, "DELETE", f"{self.base_path}/{resource}")


class IndexTemplateHandler(_NamedHandler):
    type_name = "index-templates"
    label = "index template"
    base_path = "/_index_template"

    def _extract(self, body, name):
        for item in body.get("index_templates", []):
            if item.get("name") == name:
                return item.get("index_template")
        return None

    def _names(self, body):
        return [i["name"] for i in body.get("index_templates", [])]

    def _validate(self, config):
        super()._validate(config)
        if not config.get("index_patterns"):
            raise bad_request("INVALID_CONFIG", "Index template needs 'index_patterns'")

    def simulate(self, es, resource, target, sample_docs):
        if not target.exists:
            return None
        body = es_call(es, "POST", f"/_index_template/_simulate/{resource}", body=target.config)
        return {"kind": "index_template_simulate",
                "resolvedTemplate": body.get("template"),
                "overlapping": body.get("overlapping", [])}


class ComponentTemplateHandler(_NamedHandler):
    type_name = "component-templates"
    label = "component template"
    base_path = "/_component_template"

    def _extract(self, body, name):
        for item in body.get("component_templates", []):
            if item.get("name") == name:
                return item.get("component_template")
        return None

    def _names(self, body):
        return [i["name"] for i in body.get("component_templates", [])]

    def _validate(self, config):
        super()._validate(config)
        if "template" not in config:
            raise bad_request("INVALID_CONFIG", "Component template needs a 'template' object")


class IlmPolicyHandler(_NamedHandler):
    type_name = "ilm-policies"
    label = "ILM policy"
    base_path = "/_ilm/policy"

    def _extract(self, body, name):
        item = body.get(name)
        return {"policy": item["policy"]} if item and "policy" in item else None

    def _names(self, body):
        return list(body)

    def _validate(self, config):
        super()._validate(config)
        if not isinstance(config.get("policy"), dict) or set(config) != {"policy"}:
            raise bad_request("INVALID_CONFIG", "ILM config must be {\"policy\": {...}}")


class IngestPipelineHandler(_NamedHandler):
    type_name = "ingest-pipelines"
    label = "ingest pipeline"
    base_path = "/_ingest/pipeline"

    def _extract(self, body, name):
        return body.get(name)

    def _names(self, body):
        return list(body)

    def _validate(self, config):
        super()._validate(config)
        if not isinstance(config.get("processors"), list):
            raise bad_request("INVALID_CONFIG", "Ingest pipeline needs a 'processors' list")

    def simulate(self, es, resource, target, sample_docs):
        if not target.exists:
            return None
        docs = sample_docs or [{}]
        body = es_call(es, "POST", "/_ingest/pipeline/_simulate",
                       body={"pipeline": target.config, "docs": [{"_source": d} for d in docs]})
        return {"kind": "ingest_pipeline_simulate",
                "validationOnly": not sample_docs,
                "note": None if sample_docs else "Pipeline definition is valid. Add 'sampleDocs' "
                                                 "to see what it does to real documents.",
                "docs": body.get("docs", [])}
