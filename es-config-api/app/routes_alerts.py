"""Alerts: /api/v1/admin/alerts/... (admins) and /api/v1/clusters/{id}/alerts (anyone with access).

See alerts.py for what is checked and when. Snoozing is done here (the console) only.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, Field

from .identity import User, current_user, require_admin, require_any_access
from .routes_admin import _audit

router = APIRouter(tags=["alerts"])
ADMIN = "/api/v1/admin/alerts"


def _svc(request: Request):
    return request.app.state.alerts


class WebhookBody(BaseModel):
    webhookUrl: str = Field(..., description="Slack incoming webhook, https://hooks.slack.com/services/…")


class SnoozeBody(BaseModel):
    clusterId: str
    metric: Literal["disk", "ram", "heap", "cpu", "shards", "health", "nodes"] | None = Field(
        None, description="Leave out to snooze every alert of the cluster")
    node: str | None = Field(None, description="Only this node (needs metric)")
    duration: Literal["1h", "4h", "24h", "7d", "resolved"] = Field(
        ..., description="'resolved' = until the alert clears")
    reason: str = Field(..., min_length=1, max_length=300)


@router.get(ADMIN, summary="Active alerts, snoozes, settings, Slack status and the last check")
def overview(request: Request, admin: User = Depends(require_admin)):
    svc = _svc(request)
    slack = request.app.state.slack
    return {**svc.overview(), "settings": svc.settings(),
            "slack": {"configured": slack.configured, "lastError": (slack.last_error or {}).get("error"),
                      "secret": request.app.state.secrets.full_name("slack")}}


@router.put(f"{ADMIN}/settings", summary="Thresholds, repeat intervals, per-cluster overrides, digest on/off")
def put_settings(request: Request, body: dict[str, Any] = Body(...), admin: User = Depends(require_admin)):
    svc = _svc(request)
    before = svc.settings()
    after = svc.save_settings(body, admin.username)
    strip = lambda d: {k: v for k, v in d.items() if k not in ("updatedBy", "updatedAt")}  # noqa: E731
    _audit(request, admin, "ALERT_SETTINGS_UPDATE", configType="alerts", before=strip(before), after=strip(after))
    return after


@router.put(f"{ADMIN}/slack", summary="Set the Slack webhook (stored in AWS Secrets Manager, never returned)")
def put_webhook(request: Request, body: WebhookBody, admin: User = Depends(require_admin)):
    slack = request.app.state.slack
    replaced = slack.configured
    slack.set_webhook(body.webhookUrl)
    _audit(request, admin, "ALERT_WEBHOOK_UPDATE", configType="alerts", replaced=replaced)
    return {"configured": True}


@router.delete(f"{ADMIN}/slack", summary="Remove the Slack webhook (alerts are then only shown in the console)")
def delete_webhook(request: Request, admin: User = Depends(require_admin)):
    request.app.state.slack.remove_webhook()
    _audit(request, admin, "ALERT_WEBHOOK_DELETE", configType="alerts")
    return {"configured": False}


@router.post(f"{ADMIN}/slack/_test", summary="Post a test message to the Slack channel")
def test_webhook(request: Request, admin: User = Depends(require_admin)):
    res = request.app.state.slack.post(
        f":wave: Test message from the ES Config console, sent by {admin.username}. Alerts will arrive here.")
    _audit(request, admin, "ALERT_TEST", configType="alerts", outcome="SUCCESS" if res.get("ok") else "FAILED",
           error=None if res.get("ok") else res.get("error"))
    return res


@router.post(f"{ADMIN}/snoozes", status_code=201, summary="Snooze alerts (console only; nothing is posted)")
def snooze(request: Request, body: SnoozeBody, admin: User = Depends(require_admin)):
    item = _svc(request).snooze(admin.username, body.clusterId, body.metric, body.node, body.duration, body.reason)
    _audit(request, admin, "ALERT_SNOOZE", configType="alerts", clusterId=body.clusterId,
           resource=item["id"], snooze={k: item[k] for k in ("metric", "node", "duration", "until")},
           reason=item["reason"])
    return item


@router.delete(f"{ADMIN}/snoozes/{{snooze_id}}", summary="End a snooze now")
def unsnooze(snooze_id: str, request: Request, admin: User = Depends(require_admin)):
    item = _svc(request).unsnooze(snooze_id)
    _audit(request, admin, "ALERT_UNSNOOZE", configType="alerts", clusterId=item.get("clusterId"),
           resource=snooze_id)
    return {"ended": snooze_id}


@router.get(f"{ADMIN}/history", summary="Alerts that started, changed or cleared (newest first)")
def history(request: Request, days: int = Query(7, ge=1, le=31), clusterId: str | None = Query(None),
            admin: User = Depends(require_admin)):
    items = _svc(request).history(days)
    if clusterId:
        items = [e for e in items if e.get("clusterId") == clusterId]
    return {"items": items[:1000]}


@router.post(f"{ADMIN}/_check", summary="Check every cluster now (posts to Slack like the 5-minute check)")
def check_now(request: Request, admin: User = Depends(require_admin)):
    _svc(request).sample_heap(only_if_due=True)   # a heap reading if one is due (never more than every 2 min)
    return _svc(request).run_once()


@router.post(f"{ADMIN}/active/{{key}}/_clear", summary="Forget a node that left for good")
def clear(key: str, request: Request, admin: User = Depends(require_admin)):
    a = _svc(request).clear(key, admin.username)
    _audit(request, admin, "ALERT_CLEAR", configType="alerts", clusterId=a["clusterId"], resource=a.get("node"))
    return {"cleared": key}


@router.get(f"{ADMIN}/digest/_preview", summary="This week's digest as it would be posted")
def digest_preview(request: Request, admin: User = Depends(require_admin)):
    svc = _svc(request)
    d = svc.digest()
    text, atts = svc.digest_message(d)
    return {"digest": d, "slack": {"text": text, "attachments": atts}}


@router.post(f"{ADMIN}/digest/_send", summary="Post the weekly digest now")
def digest_send(request: Request, admin: User = Depends(require_admin)):
    res = _svc(request).send_digest()
    _audit(request, admin, "ALERT_DIGEST_SEND", configType="alerts",
           outcome="SUCCESS" if res.get("ok") else "FAILED", error=res.get("error"))
    return {k: v for k, v in res.items() if k != "digest"}


@router.get("/api/v1/clusters/{cluster_id}/alerts", summary="Active alerts and thresholds for one cluster")
def cluster_alerts(cluster_id: str, request: Request, user: User = Depends(current_user)):
    require_any_access(user, cluster_id)
    request.app.state.registry.get(cluster_id)
    return _svc(request).for_cluster(cluster_id)
