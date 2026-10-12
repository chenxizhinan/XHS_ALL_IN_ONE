from __future__ import annotations

import threading
from typing import Any

from backend.app.adapters.xhs.request_env import direct_xhs_request_env

# 登录签名链路依赖单个 XHSLoginApi 实例积累的会话状态：首次匿名轮询通过后调用
# ensure_webprofile() 上报指纹并取得 gid，之后请求切换到稳态档（0301 + b1）。
# 每次轮询新建实例会停留在冷启动档（0101、无 b1、无 gid），确认交换接口会返回
# success 但不下发正式会话（2026-10-12 实抓验证）。因此按 qr_id 持久化实例，
# 与 CLI 登录流程（qrcode_login）行为对齐。
_login_api_by_qr_id: dict[str, Any] = {}
_login_api_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()
_REGISTRY_LIMIT = 64


def _registry_register(qr_id: str, api: Any) -> None:
    with _registry_lock:
        while len(_login_api_by_qr_id) >= _REGISTRY_LIMIT:
            oldest = next(iter(_login_api_by_qr_id))
            stale = _login_api_by_qr_id.pop(oldest, None)
            _login_api_locks.pop(oldest, None)
            if stale is not None:
                try:
                    stale.close()
                except Exception:
                    pass
        _login_api_by_qr_id[qr_id] = api
        _login_api_locks.setdefault(qr_id, threading.Lock())


def _registry_take(qr_id: str) -> Any:
    with _registry_lock:
        return _login_api_by_qr_id.get(qr_id)


def _registry_drop(qr_id: str) -> Any:
    with _registry_lock:
        return _login_api_by_qr_id.pop(qr_id, None) if qr_id in _login_api_by_qr_id else None


def _pop_lock(qr_id: str) -> None:
    with _registry_lock:
        _login_api_locks.pop(qr_id, None)


class XhsPcLoginAdapter:
    def __init__(self) -> None:
        self._bound_api: Any = None

    def create_qrcode(self) -> dict[str, Any]:
        with direct_xhs_request_env():
            from apis.xhs_pc_login_apis import XHSLoginApi

            api = XHSLoginApi()
            cookies = api.generate_init_cookies()
            success, message, payload = api.generate_qrcode(cookies)
        if not success or not payload:
            raise RuntimeError(message)
        _registry_register(payload["qr_id"], api)
        return {
            "cookies": payload["cookies"],
            "qr_id": payload["qr_id"],
            "code": payload["code"],
            "qr_url": payload["qr_url"],
        }

    def _cold_check(self, qr_id: str, code: str, cookies: dict[str, Any]) -> dict[str, Any]:
        """Fallback when the warm instance is gone (e.g. backend restart)."""
        with direct_xhs_request_env():
            from apis.xhs_pc_login_apis import XHSLoginApi

            api = XHSLoginApi()
            success, message, updated_cookies = api.check_qrcode_status(qr_id, code, cookies)
        return _poll_status(success, message, updated_cookies)

    def check_qrcode_status(self, qr_id: str, code: str, cookies: dict[str, Any]) -> dict[str, Any]:
        with _registry_lock:
            lock = _login_api_locks.get(qr_id)
        if lock is None:
            return self._cold_check(qr_id, code, cookies)
        with lock:
            api = _registry_take(qr_id)
            if api is None:
                return self._cold_check(qr_id, code, cookies)
            self._bound_api = api
            with direct_xhs_request_env():
                success, message, updated_cookies = api.check_qrcode_status(qr_id, code, cookies)
            status = _resolve_status(success, message)
            reported = getattr(api, "_webprofile_reported", True)
            if status in {"pending", "scanned"} and not reported:
                # CLI 对齐：首次匿名轮询通过后立即上报 webprofile（扫码过快时
                # 第一次轮询可能已是 scanned，此时也必须补报，否则确认交换
                # 仍处于冷启动档，服务端不下发正式会话）。
                with direct_xhs_request_env():
                    api.ensure_webprofile(cookies)
            if status in {"confirmed", "expired"}:
                stale = _registry_drop(qr_id)
                _pop_lock(qr_id)
                if status == "expired" and stale is not None:
                    try:
                        stale.close()
                    except Exception:
                        pass
            return {"status": status, "cookies": updated_cookies}

    def get_user_info(self, cookies: dict[str, Any]) -> dict[str, Any]:
        if self._bound_api is not None:
            with direct_xhs_request_env():
                success, data, _ = self._bound_api.get_user_info(cookies)
        else:
            with direct_xhs_request_env():
                from apis.xhs_pc_login_apis import XHSLoginApi

                api = XHSLoginApi()
                success, data, _ = api.get_user_info(cookies)
        if not success:
            raise RuntimeError("Failed to fetch XHS user info")
        return {
            "external_user_id": data.get("user_id", ""),
            "nickname": data.get("nickname", ""),
            "avatar_url": data.get("images") or data.get("imageb") or "",
            "profile": {
                "red_id": data.get("red_id") or data.get("redId") or "",
                "followers": data.get("fans") or data.get("followers") or data.get("follower_count"),
                "following": data.get("follows") or data.get("following") or data.get("following_count"),
                "likes": data.get("liked_count") or data.get("likes") or data.get("like_count"),
                "raw": data,
            },
        }

    def create_phone_session(self, phone: str) -> dict[str, Any]:
        with direct_xhs_request_env():
            from apis.xhs_pc_login_apis import XHSLoginApi

            api = XHSLoginApi()
            cookies = api.generate_init_cookies()
            success, message, _ = api.send_phone_code(phone, cookies)
        if not success:
            raise RuntimeError(message)
        return {"cookies": cookies, "message": message or "sent"}

    def confirm_phone_login(self, phone: str, code: str, cookies: dict[str, Any]) -> dict[str, Any]:
        with direct_xhs_request_env():
            from apis.xhs_pc_login_apis import XHSLoginApi

            api = XHSLoginApi()
            success, message, payload = api.login_by_phone(phone, code, cookies)
        if not success or not payload:
            raise RuntimeError(message)
        return {"status": "confirmed", "cookies": payload["cookies"]}


def _resolve_status(success: bool, message: str) -> str:
    status = "confirmed" if success else "pending"
    if "过期" in message or "expired" in message.lower():
        status = "expired"
    if "确认" in message or "confirm" in message.lower():
        status = "scanned"
    return status


def _poll_status(success: bool, message: str, updated_cookies: dict[str, Any]) -> dict[str, Any]:
    return {"status": _resolve_status(success, message), "cookies": updated_cookies}
