"""Who is calling, and what may they do.

v1 trusts the username in a request header (default ``X-User``). This is NOT
authentication; restrict network access to the API until SSO/JWT is added.
To add real auth later, change ``resolve_username`` only.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from .errors import ApiError, forbidden

_NAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")

# Declared as a security scheme only so the /docs page gets an "Authorize" button
# that sends the username on every request. The value is read in resolve_username.
USER_HEADER_SCHEME = APIKeyHeader(
    name=os.environ.get("USER_HEADER", "X-User"), auto_error=False, scheme_name="Username",
    description="Your username. v1 trusts this value; it is not a password.")


@dataclass
class User:
    username: str
    admin: bool = False
    clusters: dict[str, str] = field(default_factory=dict)

    def level(self, cluster_id: str) -> str | None:
        if self.admin:
            return "delete"
        return self.clusters.get(cluster_id) or self.clusters.get("*")

    def can(self, cluster_id: str, needed: str) -> bool:
        order = {"view": 1, "edit": 2, "delete": 3}
        return order.get(self.level(cluster_id) or "", 0) >= order[needed]


def resolve_username(request: Request) -> str:
    header = request.app.state.settings.user_header
    name = (request.headers.get(header) or "").strip()
    if not name:
        raise ApiError(401, "MISSING_USER", f"Send your username in the '{header}' header")
    if not _NAME_RE.match(name):
        raise ApiError(400, "INVALID_USER", "Username may contain letters, digits, . _ @ -")
    return name


def current_user(request: Request, _header: str | None = Security(USER_HEADER_SCHEME)) -> User:
    name = resolve_username(request)
    rec = request.app.state.users.get(name)
    if rec is None:
        raise forbidden("UNKNOWN_USER", f"User '{name}' is not registered; ask an admin to add you")
    user = User(username=name, admin=bool(rec.get("admin")), clusters=rec.get("clusters", {}))
    request.state.user = user
    return user


def require_admin(request: Request, _header: str | None = Security(USER_HEADER_SCHEME)) -> User:
    user = current_user(request)
    if not user.admin:
        raise forbidden("ADMIN_REQUIRED", "This endpoint needs admin rights")
    return user


def require_cluster(user: User, cluster_id: str, needed: str) -> None:
    if not user.can(cluster_id, needed):
        raise forbidden("PERMISSION_DENIED",
                        f"'{user.username}' needs '{needed}' access on cluster '{cluster_id}'",
                        {"has": user.level(cluster_id)})
