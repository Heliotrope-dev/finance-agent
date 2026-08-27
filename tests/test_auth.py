"""auth.py 的纯逻辑单元测试——不碰真实 Supabase，用 monkeypatch 换掉网络层。

覆盖今晚复核过的那部分：密码哈希/校验的正确性，以及修掉的时序侧信道
（邮箱不存在时也要跑一次陪跑哈希，两条路径耗时应当接近）。
"""

import hashlib
import time

import auth


def test_hash_pw_roundtrip():
    """_hash_pw 产出的哈希，用同样的密码能验证通过；salt 每次不同。"""
    h1 = auth._hash_pw("correct horse battery staple")
    h2 = auth._hash_pw("correct horse battery staple")
    assert h1 != h2, "两次哈希应该用不同的随机salt，结果不能相同"

    salt, digest = h1.split("$", 1)
    recomputed = hashlib.pbkdf2_hmac(
        "sha256", "correct horse battery staple".encode(), salt.encode(), 100000
    ).hex()
    assert recomputed == digest, "用同一个salt重算应该得到同一个哈希"


def test_hash_pw_wrong_password_fails():
    h = auth._hash_pw("right-password")
    salt, digest = h.split("$", 1)
    wrong = hashlib.pbkdf2_hmac("sha256", "wrong-password".encode(), salt.encode(), 100000).hex()
    assert wrong != digest


def test_check_user_nonexistent_email_no_leak_in_message(monkeypatch):
    """邮箱不存在时的报错文案，必须跟"邮箱存在但密码错"完全一样——
    不能让攻击者靠文案内容判断邮箱是否注册过。"""
    monkeypatch.setattr(auth, "_sb_get", lambda table, params: [])
    ok, msg = auth._check_user("nobody@nowhere.invalid", "whatever")
    assert ok is False
    assert msg == "邮箱或密码不正确"


def test_check_user_timing_equalized_for_nonexistent_email(monkeypatch):
    """今晚修的时序侧信道：邮箱不存在时也要陪跑一次同成本的PBKDF2，
    耗时应该接近"邮箱存在但密码错"那条路径，而不是明显更快。
    用宽松的阈值（3倍）避免这个测试本身在慢速CI机器上偶发失败——
    目的是抓"完全没跑陪跑哈希"这种量级的回归，不是精确测时序。"""
    monkeypatch.setattr(auth, "_sb_get", lambda table, params: [])

    t0 = time.perf_counter()
    auth._check_user("nobody@nowhere.invalid", "whatever")
    elapsed_missing = time.perf_counter() - t0

    # 单独测一次"真实哈希"耗时做参照，不经过_check_user的其它逻辑
    t0 = time.perf_counter()
    hashlib.pbkdf2_hmac("sha256", b"whatever", b"0" * 32, 100000)
    elapsed_one_hash = time.perf_counter() - t0

    assert elapsed_missing >= elapsed_one_hash * 0.5, (
        f"邮箱不存在的路径耗时({elapsed_missing:.4f}s)明显小于一次PBKDF2"
        f"耗时({elapsed_one_hash:.4f}s)，陪跑哈希可能被跳过了，时序侧信道回归了"
    )


def test_check_user_correct_password_succeeds(monkeypatch):
    stored = auth._hash_pw("s3cr3t")
    monkeypatch.setattr(
        auth,
        "_sb_get",
        lambda table, params: [
            {"email": "a@b.com", "password_hash": stored, "failed_attempts": 0, "locked_until": None}
        ],
    )
    patched = {}
    monkeypatch.setattr(auth, "_sb_patch", lambda table, data, params: patched.update(data) or True)

    ok, msg = auth._check_user("a@b.com", "s3cr3t")
    assert ok is True
    assert msg == ""
    assert patched.get("failed_attempts") == 0


def test_check_user_wrong_password_fails_without_leaking(monkeypatch):
    stored = auth._hash_pw("s3cr3t")
    monkeypatch.setattr(
        auth,
        "_sb_get",
        lambda table, params: [
            {"email": "a@b.com", "password_hash": stored, "failed_attempts": 0, "locked_until": None}
        ],
    )
    monkeypatch.setattr(auth, "_sb_patch", lambda table, data, params: True)

    ok, msg = auth._check_user("a@b.com", "wrong-guess")
    assert ok is False
    assert msg == "邮箱或密码不正确"


def test_check_user_legacy_sha256_hash_gets_upgraded(monkeypatch):
    """老账号（迁移前用纯sha256存的密码）登录一次后，应该自动升级成
    带salt的PBKDF2格式，不需要用户重新设置密码。"""
    legacy_hash = hashlib.sha256("old-password".encode()).hexdigest()
    monkeypatch.setattr(
        auth,
        "_sb_get",
        lambda table, params: [
            {"email": "legacy@b.com", "password_hash": legacy_hash, "failed_attempts": 0, "locked_until": None}
        ],
    )
    patched = {}
    monkeypatch.setattr(auth, "_sb_patch", lambda table, data, params: patched.update(data) or True)

    ok, msg = auth._check_user("legacy@b.com", "old-password")
    assert ok is True
    new_hash = patched.get("password_hash", "")
    assert "$" in new_hash, "升级后应该是salt$digest格式，不再是裸sha256"
    assert new_hash != legacy_hash
