"""Regression tests for the QR-confirmed poll 500.

The real XHS response to GET /api/sns/web/v1/login/qrcode/status after the
user confirms login on their phone is

    {"code": 0, "success": true, "msg": "成功", "data": {}}

with the authenticated session delivered via Set-Cookie only. The rewritten
login API (commit 17d42a6) required data.code_status == 2 in the JSON body,
raised RuntimeError("成功") on this success response, and the unguarded
login-sessions route surfaced it as a 500. Captured live 2026-10-12.
"""
import json

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.core.database import get_db
from backend.app.adapters.xhs.pc_login_adapter import XhsPcLoginAdapter


def _override_database(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.app.core.database import Base

    engine = create_engine(
        f"sqlite:///{tmp_path / 'login-test.db'}",
        connect_args={"check_same_thread": False},
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestingSessionLocal


class _FakeResponse:
    """Minimal stand-in for a curl_cffi response."""

    def __init__(self, url, payload, set_cookie=""):
        self.url = url
        self._payload = payload
        self.cookies = {}
        self.headers = {"set-cookie": set_cookie} if set_cookie else {}

    def json(self):
        return self._payload


# Captured wire shape: success=True with empty data dict, session via Set-Cookie.
CONFIRMED_PAYLOAD = {"code": 0, "success": True, "msg": "成功", "data": {}}


def test_login_qrcode_status_accepts_empty_data_success_response(monkeypatch):
    """The confirmed-poll exchange must not raise on success=True, data={}.

    The real session arrives via Set-Cookie (web_session), not the JSON body.
    """
    from apis.xhs_pc_login_apis import XHSLoginApi
    from xhs_utils.xhs_core.http import BrowserHttpClient

    api = XHSLoginApi()
    a1 = "0123456789012345678901234567890123456789012345678901"
    cookies = {"a1": a1, "web_session": "visitor-old"}

    captured_url = {}

    def fake_get(self, url, **kwargs):
        captured_url["value"] = str(url)
        return _FakeResponse(
            "https://edith.xiaohongshu.com/api/sns/web/v1/login/qrcode/status",
            CONFIRMED_PAYLOAD,
            set_cookie="web_session=REAL-NEW; Path=/; Domain=.xiaohongshu.com",
        )

    monkeypatch.setattr(BrowserHttpClient, "get", fake_get)
    # Provide minimal session/profile state so signing works without network.
    monkeypatch.setattr(
        XHSLoginApi, "_ensure_profile", lambda self, _cookies: None, raising=False
    )
    monkeypatch.setattr(
        XHSLoginApi,
        "_signed_request_params",
        lambda self, cookies, api_path, data="", method="POST", **kwargs: ({}, data),
        raising=False,
    )
    monkeypatch.setattr(
        XHSLoginApi, "_cookies_for_url", lambda self, url, cookies: dict(cookies),
        raising=False,
    )
    monkeypatch.setattr(
        XHSLoginApi,
        "_merge_response_cookies",
        lambda self, cookies, response, **kwargs: _merge_set_cookie(cookies, response),
        raising=False,
    )
    # The wire header contract is not what this test exercises; pass through.
    # xhs_pc_login_apis binds build_pc_login_headers at import time, so patch
    # the name where it is used.
    import apis.xhs_pc_login_apis as login_api_module

    monkeypatch.setattr(
        login_api_module,
        "build_pc_login_headers",
        lambda headers, cookies=None, **kwargs: dict(headers),
        raising=False,
    )

    result = api._login_by_qrcode_status("qr-id-123", "code-456", cookies)

    assert "login/qrcode/status" in captured_url["value"]
    assert result["web_session"] == "REAL-NEW"


def _merge_set_cookie(cookies, response):
    """Mirror HostCookieStore.merge_response's Set-Cookie handling."""
    raw = response.headers.get("set-cookie", "")
    for part in raw.split(", "):
        name, _, value = part.partition(";")[0].partition("=")
        if name.strip():
            cookies[name.strip()] = value.strip()


def test_qr_confirmed_poll_completes_without_500(tmp_path):
    """End-to-end: poll after phone confirmation returns confirmed, not 500."""
    TestingSessionLocal = _override_database(tmp_path)

    from backend.app.core.security import create_access_token, encrypt_text
    from backend.app.models import LoginSession, User
    from backend.app.api import login_sessions as ls

    with TestingSessionLocal() as db:
        user = User(username="poller", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        session = LoginSession(
            user_id=user.id,
            platform="xhs",
            sub_type="pc",
            login_method="qr",
            status="scanned",
            qr_id="debug-qr-id",
            code="debug-code",
            qr_url="https://example.com/qr",
            encrypted_temp_cookies=encrypt_text(
                json.dumps(
                    {"a1": "0123456789012345678901234567890123456789012345678901",
                     "webId": "debugwebid"},
                    ensure_ascii=False, separators=(",", ":"),
                )
            ),
        )
        db.add(session)
        db.commit()
        session_id = session.id
        user_id = user.id

    class StubCreator:
        def exchange_from_user_cookies(self, cookies):
            return {"cookies": {"web_session": "creator-fake"}}

        def get_user_info(self, cookies):
            return {"external_user_id": "creator_1", "nickname": "C",
                    "avatar_url": "", "profile": {}}

    class StubPcAdapter(XhsPcLoginAdapter):
        """Poll result mirrors a successful confirmed exchange."""

        def check_qrcode_status(self, qr_id, code, cookies):
            return {
                "status": "confirmed",
                "cookies": {**cookies, "web_session": "REAL-NEW"},
            }

        def get_user_info(self, cookies):
            return {
                "external_user_id": "poll_user_1",
                "nickname": "Poller",
                "avatar_url": "",
                "profile": {"red_id": "poll_red_id"},
            }

    app.dependency_overrides[ls.get_pc_login_adapter] = lambda: StubPcAdapter()
    app.dependency_overrides[ls.get_creator_login_adapter] = lambda: StubCreator()

    token = create_access_token(user_id)
    client = TestClient(app, raise_server_exceptions=True)
    try:
        response = client.get(
            f"/api/xhs/login-sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "confirmed"
        assert payload["account"]["nickname"] == "Poller"
    finally:
        app.dependency_overrides.pop(ls.get_pc_login_adapter, None)
        app.dependency_overrides.pop(ls.get_creator_login_adapter, None)
        app.dependency_overrides.pop(get_db, None)


def test_adapter_reuses_login_instance_and_reports_webprofile(monkeypatch):
    """The adapter must keep ONE XHSLoginApi per qr_id and call
    ensure_webprofile after the first anonymous poll (CLI-aligned warm state).
    A fresh instance per poll stays in cold state and the confirmed exchange
    never receives the real session."""
    from backend.app.adapters.xhs import pc_login_adapter as pla

    class FakeProfileSession:
        fingerprint_ready = False

    class FakeProfile:
        session = FakeProfileSession()

        def update_cookies(self, _):
            pass

    class FakeLoginApi:
        def __init__(self):
            self.webprofile_calls = 0
            self.polls = 0
            self.profile = FakeProfile()
            self._webprofile_reported = False

        def check_qrcode_status(self, qr_id, code, cookies):
            self.polls += 1
            if self.polls == 1:
                return False, "请扫描二维码", dict(cookies)
            return True, "验证成功", dict(cookies, web_session="REAL-NEW")

        def ensure_webprofile(self, cookies):
            self.webprofile_calls += 1
            self._webprofile_reported = True
            cookies["gid"] = "g"

        def get_user_info(self, cookies):
            return True, {"user_id": "u1", "nickname": "N"}, cookies

        def close(self):
            pass

    fake = FakeLoginApi()
    monkeypatch.setattr(pla, "_registry_register", lambda qr_id, api: None)
    monkeypatch.setattr(pla, "_registry_take", lambda qr_id: fake)
    monkeypatch.setattr(pla, "_registry_drop", lambda qr_id: fake)
    monkeypatch.setattr(pla, "_pop_lock", lambda qr_id: None)
    # A real threading lock works fine here; no need to stub the lock type.
    import threading as _threading

    with pla._registry_lock:
        pla._login_api_locks["qr-warm"] = _threading.Lock()

    adapter = pla.XhsPcLoginAdapter()
    cookies = {"a1": "x"}
    r1 = adapter.check_qrcode_status("qr-warm", "code", cookies)
    assert r1["status"] == "pending"
    assert fake.webprofile_calls == 1, "ensure_webprofile must run after first anonymous poll"
    assert cookies.get("gid") == "g"

    r2 = adapter.check_qrcode_status("qr-warm", "code", cookies)
    assert r2["status"] == "confirmed"
    assert r2["cookies"]["web_session"] == "REAL-NEW"
    assert fake.polls == 2

    # Same instance used for the follow-up user info call (warm signature state)
    info = adapter.get_user_info(cookies)
    assert info["external_user_id"] == "u1"
