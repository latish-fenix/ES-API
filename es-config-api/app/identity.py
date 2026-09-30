"""Who is calling, and what may they do.

AUTH_MODE=password (default, production): every request carries a signed session,
either the ``esc_session`` cookie set by /auth/login (browser) or
``Authorization: Bearer <token>`` (scripts, curl, /docs). The token names the user and a
token version; a password change or reset bumps the version, ending older sessions.

AUTH_MODE=header (dev and tests only): the username in ``X-User`` is trusted as-is.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from fastapi import Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from .auth import normalize_username, read_token
from .errors import ApiError, forbidden

_NAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,128}$")
SESSION_COOKIE = "esc_session"
CSRF_HEADER = "x-requested-with"
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

_MODE = os.environ.get("AUTH_MODE", "password").lower()
if _MODE == "header":
    SCHEME = APIKeyHeader(name=os.environ.get("USER_HEADER", "X-User"), auto_error=False,
                          scheme_name="Username",
                          description="Dev/test mode: your username, trusted as-is.")
else:
    SCHEME = HTTPBearer(auto_error=False, scheme_name="Session token",
                        description="Get a token from POST /api/v1/auth/login, then paste it here.")


ORDER = {"none": 0, "view": 1, "edit": 2, "delete": 3}


def normalize_access(entry: Any) -> dict | None:
    """A user's access on one cluster, as {"default": level|None, "indices": [rules]}.

    Stored either as a plain level ("view") or as
    {"default": "view"|None, "indices": [{"pattern": "shop*-2024.*", "level": "edit"}, ...]}.
    A rule's level may be "none" to exclude matching indices from the default."""
    if entry is None:
        return None
    if isinstance(entry, str):
        return {"default": entry if entry != "none" else None, "indices": []}
    default = entry.get("default")
    return {"default": default if default not in (None, "", "none") else None,
            "indices": [dict(r) for r in entry.get("indices") or []]}


def _specificity(pattern: str) -> tuple:
    literal = len(pattern.replace("*", "").replace("?", ""))
    return ("*" in pattern or "?" in pattern, -literal, -len(pattern))


def index_level_from(access: dict | None, index: str) -> str | None:
    """Level on one index: the most specific matching rule, else the cluster default."""
    if not access:
        return None
    matches = [r for r in access["indices"] if fnmatchcase(index, r.get("pattern", ""))]
    if matches:
        best = min(matches, key=lambda r: _specificity(r["pattern"]))
        return best["level"] if best["level"] != "none" else None
    return access["default"]


@dataclass
class User:
    username: str
    admin: bool = False
    clusters: dict[str, Any] = field(default_factory=dict)

    def access(self, cluster_id: str) -> dict | None:
        if self.admin:
            return {"default": "delete", "indices": []}
        return normalize_access(self.clusters.get(cluster_id, self.clusters.get("*")))

    def level(self, cluster_id: str) -> str | None:
        """Cluster-wide level (templates, ILM policies, pipelines, and indices without a rule)."""
        a = self.access(cluster_id)
        return a["default"] if a else None

    def has_any_access(self, cluster_id: str) -> bool:
        a = self.access(cluster_id)
        return bool(a and (a["default"] or any(r.get("level") not in (None, "none") for r in a["indices"])))

    def can(self, cluster_id: str, needed: str) -> bool:
        return ORDER.get(self.level(cluster_id) or "none", 0) >= ORDER[needed]

    def index_level(self, cluster_id: str, index: str) -> str | None:
        return index_level_from(self.access(cluster_id), index)

    def can_index(self, cluster_id: str, index: str, needed: str) -> bool:
        return ORDER.get(self.index_level(cluster_id, index) or "none", 0) >= ORDER[needed]

    def has_index_rules(self, cluster_id: str) -> bool:
        a = self.access(cluster_id)
        return bool(a and a["indices"])


def _from_header(request: Request) -> str:
    header = request.app.state.settings.user_header
    name = normalize_username(request.headers.get(header) or "")
    if not name:
        raise ApiError(401, "MISSING_USER", f"Send your username in the '{header}' header")
    if not _NAME_RE.match(name):
        raise ApiError(400, "INVALID_USER", "Username may contain letters, digits, . _ @ + -")
    return name


def session_from_request(request: Request) -> tuple[dict, dict]:
    """(token payload, full user record) for a valid session, else 401."""
    settings = request.app.state.settings
    auth = request.headers.get("authorization") or ""
    token, via_cookie = None, False
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif request.cookies.get(SESSION_COOKIE):
        token, via_cookie = request.cookies[SESSION_COOKIE], True
    if not token:
        raise ApiError(401, "NOT_AUTHENTICATED", "Sign in first (POST /api/v1/auth/login)")
    payload = read_token(settings.session_secret, token)
    if payload is None:
        raise ApiError(401, "SESSION_EXPIRED", "Your session has expired; sign in again")
    rec = request.app.state.users.get_auth(payload["sub"])
    if rec is None or int(rec.get("tokenVersion", 0)) != int(payload.get("tv", -1)):
        raise ApiError(401, "SESSION_EXPIRED", "Your session has ended; sign in again")
    if via_cookie and request.method in _UNSAFE and not request.headers.get(CSRF_HEADER):
        # Cross-site forms cannot set custom headers; the UI always sends this one.
        raise forbidden("CSRF_CHECK_FAILED", f"Missing {CSRF_HEADER} header")
    return payload, rec


def current_user(request: Request, _cred: HTTPAuthorizationCredentials | str | None = Security(SCHEME)) -> User:
    settings = request.app.state.settings
    if settings.auth_mode == "header":
        name = _from_header(request)
        rec = request.app.state.users.get_auth(name)
        if rec is None:
            raise forbidden("UNKNOWN_USER", f"User '{name}' is not registered; ask an admin to add you")
    else:
        _, rec = session_from_request(request)
        name = rec["username"]
    user = User(username=name, admin=bool(rec.get("admin")), clusters=rec.get("clusters", {}))
    request.state.user = user
    return user


def require_admin(request: Request, _cred: HTTPAuthorizationCredentials | str | None = Security(SCHEME)) -> User:
    user = current_user(request)
    if not user.admin:
        raise forbidden("ADMIN_REQUIRED", "This endpoint needs admin rights")
    return user


def require_cluster(user: User, cluster_id: str, needed: str) -> None:
    if not user.can(cluster_id, needed):
        raise forbidden("PERMISSION_DENIED",
                        f"'{user.username}' needs '{needed}' access on cluster '{cluster_id}'",
                        {"has": user.level(cluster_id)})


def require_any_access(user: User, cluster_id: str) -> None:
    if not user.has_any_access(cluster_id):
        raise forbidden("PERMISSION_DENIED", f"'{user.username}' has no access to cluster '{cluster_id}'",
                        {"has": None})


def require_index(user: User, cluster_id: str, index: str, needed: str) -> None:
    if not user.can_index(cluster_id, index, needed):
        raise forbidden("PERMISSION_DENIED",
                        f"'{user.username}' needs '{needed}' access on index '{index}' in cluster "
                        f"'{cluster_id}'", {"index": index, "has": user.index_level(cluster_id, index)})


def require_admin_user(user: User) -> None:
    if not user.admin:
        raise forbidden("ADMIN_REQUIRED", "Only admins can do this")
