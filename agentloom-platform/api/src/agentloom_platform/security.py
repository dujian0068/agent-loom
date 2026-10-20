import hashlib
import hmac
import secrets
import time

from fastapi import Request

from .persistence import LoginSession, Membership, User


class Problem(Exception):
    def __init__(self, status, code, message, violations=None):
        self.status, self.code, self.message = status, code, message
        self.violations = violations or []


def password_hash(password: str):
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + hashed.hex()


def password_matches(password, encoded):
    salt, hashed = encoded.split(":")
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return hmac.compare_digest(candidate.hex(), hashed)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(request: Request, db, *, csrf=True):
    with db.session() as s:
        login = s.get(LoginSession, token_hash(request.cookies.get("platform_session", "")))
        user = s.get(User, login.user_id) if login else None
        if not login or login.expires_at < time.time() or not user or not user.enabled:
            raise Problem(401, "UNAUTHENTICATED", "请先登录")
        if (
            csrf
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), login.csrf)
        ):
            raise Problem(403, "CSRF_REJECTED", "会话校验失败，请刷新后重试")
        return user, login


def require_role(s, actor_id, space_id, minimum="viewer"):
    membership = s.get(Membership, (actor_id, space_id))
    if not membership:
        raise Problem(404, "NOT_FOUND", "对象不存在")
    if {"viewer": 0, "editor": 1, "admin": 2}[membership.role] < {"viewer": 0, "editor": 1, "admin": 2}[
        minimum
    ]:
        raise Problem(403, "FORBIDDEN", "当前空间角色没有此操作权限")
    return membership
