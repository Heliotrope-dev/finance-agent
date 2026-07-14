"""登录认证 —— 从 math-agent 移植过来的同一套机制，复用同一个 Supabase 项目/账号体系。

一套账号两个项目都能登，不用重新注册。这里只保留登录相关的部分
（math-agent 里还有错题本、对话历史那些跟理财项目无关的表，没搬过来）。
"""

import hashlib
import os
import secrets as _secrets
from datetime import datetime, timedelta, timezone

import requests

_TOKEN_DAYS = 7
_LOCKOUT_THRESHOLD = 5
_LOCKOUT_SECONDS = 60


def _sb_url() -> str:
    url = os.environ.get("SUPABASE_URL", "")
    return url.rstrip("/") + "/rest/v1" if url else ""


def _sb_headers() -> dict:
    key = os.environ.get("SUPABASE_KEY", "")
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _sb_ready() -> bool:
    return bool(_sb_url() and os.environ.get("SUPABASE_KEY"))


def _sb_get(table: str, params: dict) -> list:
    if not _sb_ready():
        return []
    try:
        r = requests.get(f"{_sb_url()}/{table}", headers=_sb_headers(), params=params, timeout=8)
        return r.json() if r.ok else []
    except Exception:
        return []


def _sb_post(table: str, data) -> bool:
    if not _sb_ready():
        return False
    try:
        r = requests.post(f"{_sb_url()}/{table}", headers=_sb_headers(), json=data, timeout=8)
        return r.ok
    except Exception:
        return False


def _sb_delete(table: str, params: dict) -> bool:
    if not _sb_ready():
        return False
    try:
        r = requests.delete(f"{_sb_url()}/{table}", headers=_sb_headers(), params=params, timeout=8)
        return r.ok
    except Exception:
        return False


def _sb_patch(table: str, data: dict, params: dict) -> bool:
    if not _sb_ready():
        return False
    try:
        r = requests.patch(f"{_sb_url()}/{table}", headers=_sb_headers(), json=data, params=params, timeout=8)
        return r.ok
    except Exception:
        return False


def _hash_pw(pw: str) -> str:
    salt = _secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000)
    return f"{salt}${h.hex()}"


def _user_exists(email: str) -> bool:
    return len(_sb_get("users", {"email": f"eq.{email}", "select": "email"})) > 0


def _check_user(email: str, pw: str) -> tuple:
    """返回 (是否登录成功, 失败时的提示信息)。失败次数/锁定时间存在 users 表，跨会话/跨设备生效。"""
    rows = _sb_get("users", {
        "email": f"eq.{email}", "select": "email,password_hash,failed_attempts,locked_until",
    })
    if not rows:
        return False, "邮箱或密码不正确"
    row = rows[0]

    locked_until = row.get("locked_until")
    if locked_until:
        try:
            lu = datetime.fromisoformat(locked_until.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if now < lu:
                wait = int((lu - now).total_seconds()) + 1
                return False, f"密码错误次数过多，请等待 {wait} 秒后重试"
        except Exception:
            pass

    stored = row["password_hash"]
    ok = False
    upgrade_hash = None
    try:
        salt, h = stored.split("$", 1)
        computed = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000).hex()
        ok = _secrets.compare_digest(computed, h)
    except Exception:
        pass
    if not ok and _secrets.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored):
        ok = True
        new_salt = os.urandom(16).hex()
        upgrade_hash = f"{new_salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), new_salt.encode(), 100000).hex()}"

    if ok:
        patch = {"failed_attempts": 0, "locked_until": None}
        if upgrade_hash:
            patch["password_hash"] = upgrade_hash
        _sb_patch("users", patch, {"email": f"eq.{email}"})
        return True, ""

    attempts = (row.get("failed_attempts") or 0) + 1
    if attempts >= _LOCKOUT_THRESHOLD:
        locked_at = (datetime.now(timezone.utc) + timedelta(seconds=_LOCKOUT_SECONDS)).isoformat()
        _sb_patch("users", {"failed_attempts": 0, "locked_until": locked_at}, {"email": f"eq.{email}"})
        return False, f"密码连续错误{_LOCKOUT_THRESHOLD}次，请等待{_LOCKOUT_SECONDS}秒后重试"
    _sb_patch("users", {"failed_attempts": attempts}, {"email": f"eq.{email}"})
    return False, f"邮箱或密码不正确（还有 {_LOCKOUT_THRESHOLD - attempts} 次机会）"


def _register_user(email: str, pw_hash: str):
    _sb_post("users", {"email": email, "password_hash": pw_hash})


def _create_token(email: str) -> str:
    token = _secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    exp = (now + timedelta(days=_TOKEN_DAYS)).isoformat()
    _sb_delete("sessions", {"email": f"eq.{email}", "expires_at": f"lt.{now.isoformat()}"})
    _sb_post("sessions", {"token": token, "email": email, "expires_at": exp})
    return token


def _validate_token(token: str):
    rows = _sb_get("sessions", {
        "token": f"eq.{token}", "expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}", "select": "email"
    })
    return rows[0]["email"] if rows else None


def _invalidate_token(token: str):
    _sb_delete("sessions", {"token": f"eq.{token}"})
