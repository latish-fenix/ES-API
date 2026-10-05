"""Email through AWS SES (v2 API, the EC2 role needs ses:SendEmail on the sender identity, and
on each recipient while the SES account is in the sandbox).

No MAIL_FROM = no email: approvals still work in the console. Each recipient gets their own
message, so one address SES refuses doesn't stop the others. Messages never carry document
values or secrets: who, what, where, the reason and field names, plus a link.
"""
from __future__ import annotations

import html
import logging
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger("es_config_api.mail")


class Mailer(Protocol):
    enabled: bool

    def send(self, to: list[str], subject: str, text: str, html_body: str) -> dict: ...


class NullMailer:
    enabled = False

    def send(self, to, subject, text, html_body) -> dict:
        return {"sent": [], "failed": [], "skipped": "MAIL_FROM is not set"}


class SesMailer:
    enabled = True

    def __init__(self, sender: str, region: str | None = None, client=None):
        self.sender = sender
        self.ses = client or boto3.client("sesv2", region_name=region,
                                          config=Config(retries={"max_attempts": 3, "mode": "standard"}))

    def send(self, to: list[str], subject: str, text: str, html_body: str) -> dict:
        sent, failed = [], []
        for addr in sorted(set(to)):
            try:
                self.ses.send_email(
                    FromEmailAddress=self.sender,
                    Destination={"ToAddresses": [addr]},
                    Content={"Simple": {"Subject": {"Data": subject[:900], "Charset": "UTF-8"},
                                        "Body": {"Text": {"Data": text, "Charset": "UTF-8"},
                                                 "Html": {"Data": html_body, "Charset": "UTF-8"}}}})
                sent.append(addr)
            except (ClientError, BotoCoreError) as e:
                code = e.response["Error"]["Code"] if isinstance(e, ClientError) else type(e).__name__
                msg = e.response["Error"].get("Message", "") if isinstance(e, ClientError) else str(e)
                hint = ""
                if code in ("AccessDeniedException", "AccessDenied"):
                    hint = " (add ses:SendEmail for this identity to the EC2 role; in the SES sandbox the " \
                           "recipient must be verified and allowed too)"
                elif code == "MessageRejected":
                    hint = " (in the SES sandbox every recipient must be a verified address)"
                log.warning("SES could not send to %s: %s %s%s", addr, code, msg, hint)
                failed.append({"to": addr, "error": f"{code}: {msg}{hint}"[:500]})
        return {"sent": sent, "failed": failed}


class RecordingMailer:
    """Tests: keeps every message instead of sending it."""
    enabled = True

    def __init__(self):
        self.messages: list[dict] = []

    def send(self, to, subject, text, html_body) -> dict:
        for addr in sorted(set(to)):
            self.messages.append({"to": addr, "subject": subject, "text": text, "html": html_body})
        return {"sent": sorted(set(to)), "failed": []}


def build_mailer(settings) -> Mailer:
    if not settings.mail_from:
        return NullMailer()
    return SesMailer(settings.mail_from, settings.ses_region or settings.aws_region)


def render(title: str, rows: list[tuple[str, str]], intro: str, link: str | None,
           link_label: str = "Open in the console") -> tuple[str, str]:
    """(plain text, HTML) for a short notification."""
    width = max((len(k) for k, _ in rows), default=0)
    text = [intro, ""] + [f"{k + ':':<{width + 1}} {v}" for k, v in rows if v]
    if link:
        text += ["", f"{link_label}: {link}"]
    text += ["", "-- ES Config Console"]
    e = html.escape
    trs = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#555;vertical-align:top">{e(k)}</td>'
        f'<td style="padding:4px 0">{e(v)}</td></tr>' for k, v in rows if v)
    btn = (f'<p style="margin:20px 0"><a href="{e(link)}" style="background:#1f5fbf;color:#fff;'
           f'padding:9px 16px;border-radius:6px;text-decoration:none">{e(link_label)}</a></p>') if link else ""
    body = (f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1b1f24;max-width:640px">'
            f'<h2 style="font-size:18px;margin:0 0 12px">{e(title)}</h2><p>{e(intro)}</p>'
            f'<table style="border-collapse:collapse;font-size:14px">{trs}</table>{btn}'
            f'<p style="color:#888;font-size:12px;margin-top:24px">ES Config Console</p></div>')
    return "\n".join(text), body
