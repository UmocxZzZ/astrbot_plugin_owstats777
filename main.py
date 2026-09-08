
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import re
import secrets
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, StarTools
from quart import jsonify, request as web_request


PLUGIN_NAME = "astrbot_plugin_owstats777"
DASHEN_CREDENTIAL_KV_KEY = "dashen_credentials/v1"
DASHEN_CREDENTIAL_FILE = "dashen_credentials.json"
DASHEN_QR_PRODUCT = "godlike_web"
DASHEN_QR_API_ROOT = "https://q.reg.163.com/qrcode"
DASHEN_INFO_API_ROOT = "https://inf.ds.163.com"
DASHEN_QR_TTL_SECONDS = 300
DASHEN_QR_MAX_SESSIONS = 6
DASHEN_QR_POLL_INTERVAL_MS = 2000
DASHEN_QUERY_TOOL_CONFIG_URL = "https://s.166.net/config/ds_ow/ow_record_query_tool.json"
DASHEN_SEARCH_NOTICE_TTL_SECONDS = 30
DASHEN_SEARCH_NOTICE_FAILURE_TTL_SECONDS = 5


class DashenSearchMaintenanceError(RuntimeError):
    pass


class OWStatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.overstats_url: str = config.get("overstats_url", "http://127.0.0.1:18080").rstrip("/")
        self.default_timeout: int = config.get("default_timeout", 60)
        self.summary_timeout: int = config.get("summary_timeout", 90)
        self.ai_timeout: int = config.get("ai_timeout", 180)
        self.llm_provider_id: str = str(config.get("llm_provider_id", "") or "").strip()
        self.shop_timeout: int = config.get("shop_timeout", 180)  # 3 minutes for slow devices
        self.ai_whitelist: list = config.get("ai_whitelist", [])
        self.ai_cooldown: int = config.get("ai_cooldown_seconds", 300)
        self.embedded_overstats: bool = config.get("embedded_overstats", True)
        self.overstats_port: int = config.get("overstats_port", 18080)
        self.client = httpx.AsyncClient(timeout=self.default_timeout)
        self._overstats_server = None
        self._overstats_thread = None
        self._shop_prefetch_task = None
        self._dashen_credential: Optional[Dict[str, Any]] = None
        self._dashen_credential_source = "none"
        self._dashen_credential_lock = asyncio.Lock()
        self._dashen_search_notice = ""
        self._dashen_search_notice_expires_at = 0.0
        self._dashen_search_notice_lock = asyncio.Lock()
        self._dashen_qr_sessions: Dict[str, Dict[str, Any]] = {}
        self._dashen_qr_sessions_lock = asyncio.Lock()
        self._register_dashen_auth_apis()

    async def initialize(self) -> None:
        """加载插件凭证后启动内置服务。"""
        await self._load_dashen_credential()
        if self.embedded_overstats:
            self._start_embedded_overstats()
            self._start_shop_prefetch()

    def _register_dashen_auth_apis(self) -> None:
        api_prefix = f"/{PLUGIN_NAME}/dashen-auth"
        self.context.register_web_api(
            f"{api_prefix}/status",
            self._dashen_auth_status,
            ["GET"],
            "Get Dashen credential status",
        )
        self.context.register_web_api(
            f"{api_prefix}/save",
            self._dashen_auth_save,
            ["POST"],
            "Save Dashen credential",
        )
        self.context.register_web_api(
            f"{api_prefix}/migrate",
            self._dashen_auth_migrate,
            ["POST"],
            "Migrate legacy Dashen credential",
        )
        self.context.register_web_api(
            f"{api_prefix}/unbind",
            self._dashen_auth_unbind,
            ["POST"],
            "Delete Dashen credential",
        )
        self.context.register_web_api(
            f"{api_prefix}/qr/start",
            self._dashen_qr_start,
            ["POST"],
            "Start Dashen QR authorization",
        )
        self.context.register_web_api(
            f"{api_prefix}/qr/status",
            self._dashen_qr_status,
            ["POST"],
            "Poll Dashen QR authorization",
        )
        self.context.register_web_api(
            f"{api_prefix}/qr/complete",
            self._dashen_qr_complete,
            ["POST"],
            "Exchange Dashen web session for Overwatch credential",
        )
        self.context.register_web_api(
            f"{api_prefix}/qr/cancel",
            self._dashen_qr_cancel,
            ["POST"],
            "Cancel Dashen QR authorization",
        )
        self.context.register_web_api(
            f"{api_prefix}/signer/wasm",
            self._dashen_signer_wasm,
            ["GET"],
            "Load bundled Dashen signer runtime",
        )

    async def _dashen_signer_wasm(self):
        signer_path = Path(__file__).resolve().parent / (
            "pages/dashen-auth/vendor/sig/7952eec11d6277f8be47.module.wasm"
        )
        try:
            signer_bytes = signer_path.read_bytes()
        except OSError:
            logger.error("大神签名 WASM 资源缺失")
            return self._page_error("大神签名组件缺失，请重新安装插件")
        if not signer_bytes.startswith(b"\x00asm") or len(signer_bytes) > 256 * 1024:
            logger.error("大神签名 WASM 资源格式无效")
            return self._page_error("大神签名组件损坏，请重新安装插件")
        return self._page_ok({"wasm_base64": base64.b64encode(signer_bytes).decode("ascii")})

    @staticmethod
    def _normalize_dashen_credential(
        role_id: Any,
        token: Any,
        *,
        updated_at: Any = None,
    ) -> Dict[str, Any]:
        role_text = str(role_id or "").strip()
        if not re.fullmatch(r"[0-9]{1,20}", role_text) or int(role_text) <= 0:
            raise ValueError("role_id 必须是正整数")

        token_text = str(token or "").strip()
        if not token_text:
            raise ValueError("token 不能为空")
        if len(token_text) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in token_text):
            raise ValueError("token 格式无效")

        try:
            saved_at = int(updated_at or time.time())
        except (TypeError, ValueError):
            saved_at = int(time.time())
        return {
            "version": 1,
            "role_id": int(role_text),
            "token": token_text,
            "updated_at": saved_at,
        }

    def _dashen_credential_from_config(self) -> Optional[Dict[str, Any]]:
        role_id = self.config.get("dashen_role_id", "")
        token = self.config.get("dashen_token", "")
        if not role_id or not token:
            return None
        try:
            return self._normalize_dashen_credential(role_id, token)
        except ValueError:
            logger.warning("插件配置中的大神凭证格式无效")
            return None

    @staticmethod
    def _dashen_credential_path() -> Path:
        return StarTools.get_data_dir(PLUGIN_NAME) / DASHEN_CREDENTIAL_FILE

    def _read_dashen_credential_file(self) -> Optional[Dict[str, Any]]:
        path = self._dashen_credential_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"读取大神凭证文件失败: {type(exc).__name__}")
            return None
        if not isinstance(payload, dict):
            logger.warning("大神凭证文件格式无效")
            return None
        return payload

    def _write_dashen_credential_file(self, credential: Dict[str, Any]) -> None:
        path = self._dashen_credential_path()
        temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temp_path.write_text(
                json.dumps(credential, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            temp_path.replace(path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _delete_dashen_credential_file(self) -> None:
        self._dashen_credential_path().unlink(missing_ok=True)

    async def _load_dashen_credential(self) -> None:
        stored = self._read_dashen_credential_file()
        if isinstance(stored, dict) and stored:
            try:
                self._dashen_credential = self._normalize_dashen_credential(
                    stored.get("role_id"),
                    stored.get("token"),
                    updated_at=stored.get("updated_at"),
                )
                self._dashen_credential_source = "plugin_data"
                try:
                    await self.delete_kv_data(DASHEN_CREDENTIAL_KV_KEY)
                except Exception as exc:
                    logger.warning(f"清理旧版大神凭证 KV 失败: {type(exc).__name__}")
                return
            except ValueError:
                logger.warning("插件数据目录中的大神凭证格式无效，尝试读取旧存储")

        stored = await self.get_kv_data(DASHEN_CREDENTIAL_KV_KEY, {})
        if isinstance(stored, dict) and stored:
            try:
                credential = self._normalize_dashen_credential(
                    stored.get("role_id"),
                    stored.get("token"),
                    updated_at=stored.get("updated_at"),
                )
                self._write_dashen_credential_file(credential)
                try:
                    await self.delete_kv_data(DASHEN_CREDENTIAL_KV_KEY)
                except Exception as exc:
                    logger.warning(f"清理旧版大神凭证 KV 失败: {type(exc).__name__}")
                self._dashen_credential = credential
                self._dashen_credential_source = "plugin_data"
                logger.info("旧版大神凭证已从 AstrBot KV 迁移到插件数据目录")
                return
            except (OSError, ValueError) as exc:
                logger.warning(f"迁移 AstrBot KV 中的大神凭证失败: {type(exc).__name__}")

        legacy = self._dashen_credential_from_config()
        self._dashen_credential = legacy
        self._dashen_credential_source = "plugin_config" if legacy else "none"

    @staticmethod
    def _masked_role_id(role_id: Any) -> str:
        role_text = str(role_id or "")
        if not role_text:
            return ""
        visible = min(4, len(role_text))
        return f"{'*' * (len(role_text) - visible)}{role_text[-visible:]}"

    def _legacy_dashen_config_present(self) -> bool:
        return bool(self.config.get("dashen_role_id", "") or self.config.get("dashen_token", ""))

    def _dashen_status_payload(self) -> Dict[str, Any]:
        credential = self._dashen_credential
        return {
            "configured": bool(credential),
            "source": self._dashen_credential_source,
            "role_id_masked": self._masked_role_id(credential.get("role_id")) if credential else "",
            "updated_at": int(credential.get("updated_at") or 0) if credential else 0,
            "embedded_overstats": self.embedded_overstats,
            "runtime_applied": bool(credential and self._overstats_server),
            "legacy_config_present": self._legacy_dashen_config_present(),
            "can_migrate": self._dashen_credential_source == "plugin_config",
        }

    def _clear_legacy_dashen_config(self) -> bool:
        old_role_id = self.config.get("dashen_role_id", "")
        old_token = self.config.get("dashen_token", "")
        if not old_role_id and not old_token:
            return True
        self.config["dashen_role_id"] = ""
        self.config["dashen_token"] = ""
        try:
            save_config = getattr(self.config, "save_config", None)
            if callable(save_config):
                save_config()
            return True
        except Exception as exc:
            self.config["dashen_role_id"] = old_role_id
            self.config["dashen_token"] = old_token
            logger.warning(f"清理旧版大神配置失败: {type(exc).__name__}")
            return False

    async def _persist_dashen_credential(self, credential: Dict[str, Any]) -> bool:
        self._write_dashen_credential_file(credential)
        try:
            await self.delete_kv_data(DASHEN_CREDENTIAL_KV_KEY)
        except Exception as exc:
            logger.warning(f"清理旧版大神凭证 KV 失败: {type(exc).__name__}")
        self._dashen_credential = credential
        self._dashen_credential_source = "plugin_data"
        legacy_cleared = self._clear_legacy_dashen_config()
        self._apply_dashen_credential()
        return legacy_cleared

    def _apply_dashen_credential(self) -> None:
        if "src.client.apiclient" not in sys.modules:
            return
        try:
            from src.client.apiclient import DashenCredential, dashen_api_client

            credential = self._dashen_credential
            if credential is None:
                dashen_api_client.clear_credentials()
                logger.info("大神凭证已从运行时移除")
                return

            default_server = int(dashen_api_client.client_config.accounts[0].server)
            dashen_api_client.replace_credentials(
                [
                    DashenCredential(
                        name="plugin-account",
                        role_id=int(credential["role_id"]),
                        token=str(credential["token"]),
                        dts=int(dashen_api_client.client_config.bigdata_dts),
                        server=default_server,
                    )
                ]
            )
            logger.info(
                "大神凭证已应用到运行时: role_id=%s source=%s",
                self._masked_role_id(credential["role_id"]),
                self._dashen_credential_source,
            )
        except Exception as exc:
            logger.error(f"更新大神运行时凭证失败: {type(exc).__name__}: {exc}")
            raise

    @staticmethod
    def _page_ok(data: Dict[str, Any]):
        return jsonify({"status": "ok", "data": data})

    @staticmethod
    def _page_error(message: str):
        return jsonify({"status": "error", "message": message})

    async def _dashen_auth_status(self):
        return self._page_ok(self._dashen_status_payload())

    async def _dashen_auth_save(self):
        payload = await web_request.get_json(silent=True)
        if not isinstance(payload, dict):
            return self._page_error("请求格式无效")
        try:
            credential = self._normalize_dashen_credential(
                payload.get("role_id"),
                payload.get("token"),
            )
        except ValueError as exc:
            return self._page_error(str(exc))

        try:
            async with self._dashen_credential_lock:
                legacy_cleared = await self._persist_dashen_credential(credential)
            result = self._dashen_status_payload()
            result["legacy_config_cleared"] = legacy_cleared
            return self._page_ok(result)
        except Exception as exc:
            logger.error(f"保存大神凭证失败: {type(exc).__name__}")
            return self._page_error("保存凭证失败，请查看 AstrBot 日志")

    async def _dashen_auth_migrate(self):
        legacy = self._dashen_credential_from_config()
        if legacy is None:
            return self._page_error("没有可迁移的旧版大神配置")
        try:
            async with self._dashen_credential_lock:
                legacy_cleared = await self._persist_dashen_credential(legacy)
            result = self._dashen_status_payload()
            result["legacy_config_cleared"] = legacy_cleared
            return self._page_ok(result)
        except Exception as exc:
            logger.error(f"迁移大神凭证失败: {type(exc).__name__}")
            return self._page_error("迁移凭证失败，请查看 AstrBot 日志")

    async def _dashen_auth_unbind(self):
        try:
            async with self._dashen_credential_lock:
                if not self._clear_legacy_dashen_config():
                    return self._page_error("旧配置清理失败，未执行解绑")
                await self.delete_kv_data(DASHEN_CREDENTIAL_KV_KEY)
                self._delete_dashen_credential_file()
                self._dashen_credential = None
                self._dashen_credential_source = "none"
                self._apply_dashen_credential()
            return self._page_ok(self._dashen_status_payload())
        except Exception as exc:
            logger.error(f"删除大神凭证失败: {type(exc).__name__}")
            return self._page_error("解绑失败，请查看 AstrBot 日志")

    @staticmethod
    def _dashen_qr_session_id(value: Any) -> str:
        session_id = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,96}", session_id):
            raise ValueError("扫码会话无效，请重新获取二维码")
        return session_id

    @staticmethod
    def _dashen_cookie_value(client: httpx.AsyncClient, name: str) -> str:
        for cookie in client.cookies.jar:
            if cookie.name == name:
                return str(cookie.value or "")
        return ""

    @staticmethod
    def _dashen_web_headers(session: Dict[str, Any]) -> Dict[str, str]:
        client: httpx.AsyncClient = session["client"]
        uid = OWStatsPlugin._dashen_cookie_value(client, "GOD_UUID")
        return {
            "Accept": "application/json, text/plain, */*",
            "GL-ClientType": "61",
            "GL-DeviceId": str(session["device_id"]),
            "GL-Uid": uid,
            "GL-X-XSRF-TOKEN": OWStatsPlugin._dashen_cookie_value(client, "GL-XSRF-TOKEN"),
            "Origin": "https://ds.163.com",
            "Referer": "https://ds.163.com/",
        }

    @staticmethod
    def _dashen_report_body(role_id: Any) -> str:
        return json.dumps(
            {
                "appKey": "bn",
                "roleId": str(role_id),
                "server": "1",
                "source": 1,
                "type": "yearly",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _dashen_signed_web_headers(
        session: Dict[str, Any],
        body: str,
        signature: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        xsrf_token = OWStatsPlugin._dashen_cookie_value(session["client"], "GL-XSRF-TOKEN")
        if not xsrf_token:
            raise ValueError("网易大神登录凭证缺少 XSRF 信息")
        headers = OWStatsPlugin._dashen_web_headers(session)
        checksum = (
            str(signature["sign"])
            if signature
            else hashlib.md5(f"{body}{xsrf_token}".encode("utf-8")).hexdigest()
        )
        nonce = str(signature["timestamp"]) if signature else str(int(time.time() * 1000))
        headers.update(
            {
                "Content-Type": "application/json;charset=UTF-8",
                "GL-CheckSum": checksum,
                "GL-Nonce": nonce,
            }
        )
        if signature and signature.get("user_agent"):
            headers["User-Agent"] = str(signature["user_agent"])
        return headers

    @staticmethod
    def _normalize_dashen_signature(value: Any) -> Dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError("大神签名尚未生成，请刷新页面后重试")
        sign = str(value.get("sign") or "").strip()
        try:
            timestamp = int(value.get("timestamp"))
        except (TypeError, ValueError):
            timestamp = 0
        if (
            not 16 <= len(sign) <= 512
            or any(ord(char) < 33 or ord(char) == 127 for char in sign)
            or abs(int(time.time() * 1000) - timestamp) > 5 * 60 * 1000
        ):
            raise ValueError("大神页面签名无效或已过期，请重试")
        user_agent = str(value.get("user_agent") or "").strip()
        if (
            not 16 <= len(user_agent) <= 512
            or any(ord(char) < 32 or ord(char) == 127 for char in user_agent)
        ):
            raise ValueError("大神页面签名缺少浏览器标识，请刷新页面后重试")
        return {
            "sign": sign,
            "timestamp": str(timestamp),
            "user_agent": user_agent,
        }

    @staticmethod
    def _normalize_dashen_signer_version(value: Any) -> str:
        version = str(value or "").strip()
        if (
            not 1 <= len(version) <= 256
            or any(ord(char) < 33 or ord(char) == 127 for char in version)
        ):
            raise ValueError("大神签名组件版本无效，请刷新页面后重试")
        return version

    @staticmethod
    def _dashen_auth_url_candidates(payload: Any) -> List[str]:
        """Extract login hand-off URLs without exposing unrelated response values."""
        url_keys = {
            "crosssetcookieurl",
            "crosssetcookieurls",
            "crosscookieurl",
            "crosscookieurls",
            "setcookieurl",
            "setcookieurls",
            "redirecturl",
            "redirecturls",
            "loginurl",
            "loginurls",
        }
        envelope_keys = {"content", "data", "result"}
        candidates: List[str] = []

        def append_value(value: Any, depth: int = 0, allow_plain_url: bool = False) -> None:
            if depth > 6 or len(candidates) >= 24:
                return
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return
                if text[:1] in {"[", "{"}:
                    try:
                        append_value(json.loads(text), depth + 1, allow_plain_url)
                        return
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                parts = re.split(r"\s*,\s*(?=(?:https?:)?//)", text)
                candidates.extend(part for part in parts if part.strip())
                return
            if isinstance(value, (list, tuple)):
                for item in value[:24]:
                    append_value(item, depth + 1, allow_plain_url)
                return
            if not isinstance(value, dict):
                return
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in url_keys or (allow_plain_url and normalized in {"url", "urls"}):
                    append_value(item, depth + 1, True)
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in envelope_keys:
                    append_value(item, depth + 1)

        append_value(payload)
        return list(dict.fromkeys(item.strip() for item in candidates if item.strip()))[:24]

    @staticmethod
    def _dashen_payload_key_paths(payload: Any) -> List[str]:
        paths: List[str] = []

        def visit(value: Any, prefix: str = "", depth: int = 0) -> None:
            if depth > 3 or len(paths) >= 32:
                return
            if isinstance(value, dict):
                for raw_key, item in list(value.items())[:32]:
                    key = re.sub(r"[^A-Za-z0-9_-]", "", str(raw_key))[:48] or "?"
                    path = f"{prefix}.{key}" if prefix else key
                    paths.append(path)
                    if isinstance(item, (dict, list)):
                        visit(item, path, depth + 1)
                    elif isinstance(item, str) and item.strip()[:1] in {"[", "{"}:
                        try:
                            visit(json.loads(item), path, depth + 1)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            pass
            elif isinstance(value, list):
                for item in value[:3]:
                    visit(item, prefix, depth + 1)

        visit(payload)
        return paths

    @staticmethod
    def _dashen_cross_cookie_url(raw_url: Any, session: Dict[str, Any]) -> Optional[str]:
        url = str(raw_url or "").strip()
        if not url or len(url) > 2048:
            return None
        if url.startswith("//"):
            url = "https:" + url
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        hostname = (parts.hostname or "").lower().rstrip(".")
        allowed_suffixes = (".163.com", ".126.com", ".yeah.net", ".netease.com", ".166.net")
        if (
            parts.scheme not in {"http", "https"}
            or not hostname
            or parts.username is not None
            or parts.password is not None
            or not any(hostname == suffix[1:] or hostname.endswith(suffix) for suffix in allowed_suffixes)
        ):
            return None
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in {"uuid", "product", "rtid"}
        ]
        query.extend(
            [
                ("uuid", str(session["urs_uuid"])),
                ("product", DASHEN_QR_PRODUCT),
            ]
        )
        return urlunsplit(("https", parts.netloc, parts.path, urlencode(query), parts.fragment))

    async def _dashen_qr_lookup(self, session_id: str) -> Optional[Dict[str, Any]]:
        async with self._dashen_qr_sessions_lock:
            return self._dashen_qr_sessions.get(session_id)

    async def _dashen_qr_drop(
        self,
        session_id: str,
        *,
        expected: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self._dashen_qr_sessions_lock:
            session = self._dashen_qr_sessions.get(session_id)
            if session is None or (expected is not None and session is not expected):
                return
            self._dashen_qr_sessions.pop(session_id, None)
        async with session["lock"]:
            if not session.get("closed"):
                session["closed"] = True
                await session["client"].aclose()

    async def _dashen_qr_prune(self) -> None:
        now = time.time()
        async with self._dashen_qr_sessions_lock:
            sessions = sorted(
                self._dashen_qr_sessions.items(),
                key=lambda item: float(item[1].get("created_at") or 0),
            )
            stale_ids = [
                session_id
                for session_id, session in sessions
                if float(session.get("expires_at") or 0) <= now
            ]
            remaining = len(sessions) - len(stale_ids)
            if remaining >= DASHEN_QR_MAX_SESSIONS:
                for session_id, _session in sessions:
                    if session_id not in stale_ids:
                        stale_ids.append(session_id)
                        remaining -= 1
                        if remaining < DASHEN_QR_MAX_SESSIONS:
                            break
        for session_id in stale_ids:
            await self._dashen_qr_drop(session_id)

    @staticmethod
    def _dashen_qr_uuid(payload: Any) -> str:
        if isinstance(payload, dict) and isinstance(payload.get("content"), str):
            try:
                payload = json.loads(payload["content"])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        value = payload.get("l", {}).get("i") if isinstance(payload, dict) else ""
        uuid = str(value or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", uuid):
            raise ValueError("网易二维码服务返回了无效会话")
        return uuid

    @staticmethod
    def _dashen_response_json(response: httpx.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            text = response.text.strip()
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < start or len(text) > 1024 * 1024:
                raise
            payload = json.loads(text[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("网易服务返回格式无效")
        return payload

    async def _dashen_qr_start(self):
        await self._dashen_qr_prune()
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
            },
        )
        try:
            response = await client.get(
                f"{DASHEN_QR_API_ROOT}/getqrcodeid",
                params={"product": DASHEN_QR_PRODUCT, "usage": 0},
            )
            response.raise_for_status()
            urs_uuid = self._dashen_qr_uuid(self._dashen_response_json(response))
            now = time.time()
            session_id = secrets.token_urlsafe(32)
            session = {
                "session_id": session_id,
                "urs_uuid": urs_uuid,
                "device_id": str(uuid.uuid4()),
                "created_at": now,
                "expires_at": now + DASHEN_QR_TTL_SECONDS,
                "state": "waiting_scan",
                "confirming": False,
                "authorized": False,
                "roles": [],
                "client": client,
                "lock": asyncio.Lock(),
                "closed": False,
            }
            image_params = {
                "uuid": urs_uuid,
                "size": 260,
                "format": "png",
                "product": DASHEN_QR_PRODUCT,
                "url": "https://ds.163.com",
                "url2": "https://ds.163.com",
            }
            image_bytes = b""
            for attempt in range(3):
                image_response = await client.get(
                    f"{DASHEN_QR_API_ROOT}/getGeneralUrlQrcode",
                    params=image_params,
                    headers={"Accept": "image/png,image/*;q=0.9,*/*;q=0.8"},
                )
                image_response.raise_for_status()
                image_bytes = image_response.content
                if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    break
                if attempt < 2:
                    await asyncio.sleep(0.15)
            if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n") or len(image_bytes) > 256 * 1024:
                raise ValueError("网易二维码服务返回了无效图片")
            async with self._dashen_qr_sessions_lock:
                self._dashen_qr_sessions[session_id] = session
            return self._page_ok(
                {
                    "session_id": session_id,
                    "state": "waiting_scan",
                    "qr_image": "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii"),
                    "expires_in": DASHEN_QR_TTL_SECONDS,
                    "poll_interval_ms": DASHEN_QR_POLL_INTERVAL_MS,
                }
            )
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            await client.aclose()
            logger.warning(f"创建大神扫码会话失败: {type(exc).__name__}")
            return self._page_error("获取网易大神二维码失败，请稍后重试")

    async def _dashen_apply_cross_cookies(self, session: Dict[str, Any], payload: Dict[str, Any]) -> None:
        urls = [
            url
            for url in (
                self._dashen_cross_cookie_url(item, session)
                for item in self._dashen_auth_url_candidates(payload)
            )
            if url is not None
        ][:12]
        if not urls:
            key_paths = self._dashen_payload_key_paths(payload)
            shape = ",".join(key_paths[:16]) or "empty"
            logger.warning(f"网易扫码授权响应缺少可用地址，字段路径: {shape}")
            raise ValueError(f"网易登录响应未包含有效授权地址（字段：{shape}）")
        succeeded = 0
        for url in urls:
            try:
                response = await session["client"].get(url)
                if response.status_code < 500:
                    succeeded += 1
            except httpx.HTTPError:
                continue
        if succeeded == 0:
            raise ValueError("网易登录授权 Cookie 写入失败")

    async def _dashen_prepare_web_login(self, session: Dict[str, Any]) -> bool:
        client: httpx.AsyncClient = session["client"]
        preflight_response = await client.get(
            f"{DASHEN_INFO_API_ROOT}/v1/web/base/mine/userInfo",
            headers=self._dashen_web_headers(session),
        )
        preflight_response.raise_for_status()
        preflight_payload = self._dashen_response_json(preflight_response)
        if int(preflight_payload.get("code") or 0) == 200:
            preflight_result = preflight_payload.get("result")
            preflight_user = (
                preflight_result.get("user") if isinstance(preflight_result, dict) else None
            )
            if isinstance(preflight_user, dict):
                session["uid"] = str(preflight_user.get("uid") or "")
            cst = self._dashen_cookie_value(client, "cst").strip()
            time_diff_text = self._dashen_cookie_value(client, "time_diff").strip()
            if cst and len(cst) <= 4096 and not any(ord(char) < 32 for char in cst):
                try:
                    time_diff = int(time_diff_text or 0)
                except ValueError:
                    time_diff = 0
                session["signing_context"] = {
                    "csrf": self._dashen_cookie_value(client, "GL-XSRF-TOKEN"),
                    "cst": cst,
                    "time_diff": str(time_diff),
                }
                return True

        csrf = self._dashen_cookie_value(client, "GL-XSRF-TOKEN").strip()
        if not csrf:
            raise ValueError("网易大神网页登录缺少 XSRF 签名上下文")
        session["login_signing_context"] = {"csrf": csrf}
        return False

    async def _dashen_web_login(
        self,
        session: Dict[str, Any],
        signer_version: str,
        signature: Dict[str, str],
    ) -> None:
        client: httpx.AsyncClient = session["client"]
        response = await client.post(
            f"{DASHEN_INFO_API_ROOT}/v1/web/base/login",
            params={"csv": signer_version},
            content=b"null",
            headers=self._dashen_signed_web_headers(session, "null", signature),
        )
        response.raise_for_status()
        payload = self._dashen_response_json(response)
        code = int(payload.get("code") or 0)
        if code != 200:
            logger.warning(f"网易大神网页登录初始化失败，响应码: {code}")
            message = str(payload.get("errmsg") or payload.get("message") or "")[:120]
            raise ValueError(message or "网易大神网页登录初始化失败")

        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("网易大神网页登录初始化返回格式无效")
        cst = str(result.get("cst") or "").strip()
        try:
            server_timestamp = int(result.get("timestamp"))
        except (TypeError, ValueError):
            server_timestamp = 0
        if cst and server_timestamp > 0:
            time_diff = int(time.time() * 1000) - server_timestamp
            client.cookies.set("cst", cst, domain=".ds.163.com", path="/")
            client.cookies.set("time_diff", str(time_diff), domain=".ds.163.com", path="/")
            session["signing_context"] = {
                "csrf": self._dashen_cookie_value(client, "GL-XSRF-TOKEN"),
                "cst": cst,
                "time_diff": str(time_diff),
            }
            session.pop("login_signing_context", None)
        else:
            raise ValueError("网易大神登录响应缺少签名上下文")
        user = result.get("user")
        if isinstance(user, dict):
            session["uid"] = str(user.get("uid") or "")

    async def _dashen_fetch_roles(
        self,
        session: Dict[str, Any],
        signature: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        client: httpx.AsyncClient = session["client"]
        user_response = await client.get(
            f"{DASHEN_INFO_API_ROOT}/v1/web/base/mine/userInfo",
            headers=self._dashen_web_headers(session),
        )
        user_response.raise_for_status()
        user_payload = self._dashen_response_json(user_response)
        user_code = int(user_payload.get("code") or 0)
        if user_code != 200:
            logger.warning(f"网易大神 userInfo 登录校验失败，响应码: {user_code}")
            raise ValueError("网易大神网页登录状态未生效")
        user_result = user_payload.get("result")
        user = user_result.get("user", {}) if isinstance(user_result, dict) else {}
        if isinstance(user, dict):
            session["uid"] = str(user.get("uid") or "")

        role_body = json.dumps({"appKey": "bn"}, ensure_ascii=False, separators=(",", ":"))
        role_response = await client.post(
            f"{DASHEN_INFO_API_ROOT}/v1/web/role-web/list/getRoleBindingListByAppKey",
            content=role_body.encode("utf-8"),
            headers=self._dashen_signed_web_headers(session, role_body, signature),
        )
        role_response.raise_for_status()
        role_payload = self._dashen_response_json(role_response)
        role_code = int(role_payload.get("code") or 0)
        if role_code != 200:
            message = str(role_payload.get("errmsg") or role_payload.get("message") or "")[:120]
            logger.warning(
                "网易大神战网账号 ID 列表失败，响应码: %s，消息: %s",
                role_code,
                message or "-",
            )
            raise ValueError(message or f"读取战网账号 ID 失败（响应码 {role_code}）")

        raw_roles = role_payload.get("result")
        if not isinstance(raw_roles, list):
            raw_roles = []
        roles: List[Dict[str, Any]] = []
        seen = set()
        for raw_role in raw_roles[:30]:
            if not isinstance(raw_role, dict):
                continue
            app_role = raw_role.get("appRoleDto")
            if not isinstance(app_role, dict):
                app_role = raw_role
            role_text = str(app_role.get("roleId") or app_role.get("role_id") or "").strip()
            if not re.fullmatch(r"[0-9]{1,20}", role_text) or int(role_text) <= 0 or role_text in seen:
                continue
            seen.add(role_text)
            name = str(
                app_role.get("nick")
                or app_role.get("name")
                or app_role.get("roleName")
                or "战网账号"
            ).strip()[:80]
            server = str(
                app_role.get("serverName")
                or app_role.get("subtitle")
                or app_role.get("server")
                or ""
            ).strip()[:80]
            roles.append(
                {
                    "role_id": role_text,
                    "name": name or "战网账号",
                    "server": server,
                }
            )
        if not roles:
            raise ValueError("该网易大神账号尚未绑定战网账号 ID")
        return roles

    async def _dashen_finish_qr_login(
        self,
        session: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> None:
        await self._dashen_apply_cross_cookies(session, payload)
        session["authorized"] = True
        session["state"] = "processing"
        if await self._dashen_prepare_web_login(session):
            session["state"] = "signing_required"
        else:
            session["state"] = "login_signature_required"

    @staticmethod
    def _dashen_qr_public_state(session: Dict[str, Any]) -> Dict[str, Any]:
        state = str(session.get("state") or "waiting_scan")
        data: Dict[str, Any] = {
            "state": state,
            "expires_in": max(0, int(float(session.get("expires_at") or 0) - time.time())),
            "poll_interval_ms": DASHEN_QR_POLL_INTERVAL_MS,
        }
        if state == "login_signature_required":
            context = session.get("login_signing_context")
            if isinstance(context, dict):
                data["signing_context"] = {
                    "csrf": str(context.get("csrf") or ""),
                }
        elif state == "signing_required":
            context = session.get("signing_context")
            if isinstance(context, dict):
                data["signing_context"] = {
                    "csrf": str(context.get("csrf") or ""),
                    "cst": str(context.get("cst") or ""),
                    "time_diff": str(context.get("time_diff") or "0"),
                }
        elif state == "roles_ready":
            data["roles"] = session.get("roles") or []
        return data

    async def _dashen_qr_status(self):
        payload = await web_request.get_json(silent=True)
        if not isinstance(payload, dict):
            return self._page_error("请求格式无效")
        try:
            session_id = self._dashen_qr_session_id(payload.get("session_id"))
        except ValueError as exc:
            return self._page_error(str(exc))
        session = await self._dashen_qr_lookup(session_id)
        if session is None:
            return self._page_error("扫码会话已失效，请重新获取二维码")
        role_signature: Optional[Dict[str, str]] = None
        if payload.get("role_signature") is not None:
            try:
                role_signature = self._normalize_dashen_signature(payload.get("role_signature"))
            except ValueError as exc:
                return self._page_error(str(exc))
        login_signature: Optional[Dict[str, str]] = None
        if payload.get("login_signature") is not None:
            try:
                login_signature = self._normalize_dashen_signature(payload.get("login_signature"))
            except ValueError as exc:
                return self._page_error(str(exc))
        signer_version: Optional[str] = None
        if payload.get("signer_version") is not None:
            try:
                signer_version = self._normalize_dashen_signer_version(
                    payload.get("signer_version")
                )
            except ValueError as exc:
                return self._page_error(str(exc))

        should_drop = False
        result: Optional[Dict[str, Any]] = None
        error_message = ""
        async with session["lock"]:
            if session.get("closed") or float(session.get("expires_at") or 0) <= time.time():
                should_drop = True
                result = {"state": "expired", "expires_in": 0}
            elif session.get("authorized"):
                try:
                    if (
                        session.get("state") == "login_signature_required"
                        and login_signature
                        and signer_version
                    ):
                        session["state"] = "processing"
                        await self._dashen_web_login(
                            session,
                            signer_version,
                            login_signature,
                        )
                        session["state"] = "signing_required"
                    elif session.get("state") == "signing_required" and role_signature:
                        session["state"] = "processing"
                        session["roles"] = await self._dashen_fetch_roles(session, role_signature)
                        session["state"] = "roles_ready"
                    result = self._dashen_qr_public_state(session)
                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    logger.warning(f"大神扫码授权签名处理失败: {type(exc).__name__}")
                    session["state"] = "error"
                    should_drop = True
                    error_message = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else "连接网易大神失败，请稍后重试"
                    )
                    result = {"state": "error", "expires_in": 0}
            else:
                try:
                    endpoint = "qrcodeauth" if session.get("confirming") else "qrcodeauthstatus"
                    params: Dict[str, Any] = {
                        "uuid": session["urs_uuid"],
                        "product": DASHEN_QR_PRODUCT,
                    }
                    if session.get("confirming"):
                        params.update({"domains": "", "newQrCode": 1})
                    response = await session["client"].get(f"{DASHEN_QR_API_ROOT}/{endpoint}", params=params)
                    response.raise_for_status()
                    qr_payload = self._dashen_response_json(response)
                    ret_code = str(qr_payload.get("retCode") or "")
                    if ret_code == "408":
                        session["state"] = "waiting_confirm" if session.get("confirming") else "waiting_scan"
                    elif ret_code == "409" or (ret_code == "200" and not qr_payload.get("userName")):
                        session["confirming"] = True
                        session["state"] = "waiting_confirm"
                    elif ret_code == "200":
                        await self._dashen_finish_qr_login(session, qr_payload)
                    elif ret_code in {"401", "404"}:
                        should_drop = True
                        session["state"] = "expired"
                    else:
                        should_drop = True
                        session["state"] = "error"
                        error_message = "网易扫码登录失败，请重新获取二维码"
                    result = self._dashen_qr_public_state(session)
                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    logger.warning(f"大神扫码状态处理失败: {type(exc).__name__}")
                    session["state"] = "error"
                    should_drop = True
                    error_message = str(exc) if isinstance(exc, ValueError) else "连接网易大神失败，请稍后重试"
                    result = {"state": "error", "expires_in": 0}

        if should_drop:
            await self._dashen_qr_drop(session_id, expected=session)
        if error_message:
            return self._page_error(error_message)
        return self._page_ok(result or {"state": "error", "expires_in": 0})

    async def _dashen_qr_complete(self):
        payload = await web_request.get_json(silent=True)
        if not isinstance(payload, dict):
            return self._page_error("请求格式无效")
        try:
            session_id = self._dashen_qr_session_id(payload.get("session_id"))
            role_id = str(payload.get("role_id") or "").strip()
            signature = self._normalize_dashen_signature(payload.get("signature"))
        except ValueError as exc:
            return self._page_error(str(exc))
        session = await self._dashen_qr_lookup(session_id)
        if session is None:
            return self._page_error("扫码会话已失效，请重新获取二维码")

        completed = False
        response_data: Dict[str, Any] = {}
        async with session["lock"]:
            if session.get("closed") or float(session.get("expires_at") or 0) <= time.time():
                return self._page_error("扫码会话已过期，请重新获取二维码")
            role_ids = {str(role.get("role_id")) for role in session.get("roles") or []}
            if session.get("state") != "roles_ready" or role_id not in role_ids:
                return self._page_error("战网账号 ID 无效，请重新扫码")
            body = self._dashen_report_body(role_id)
            try:
                token_response = await session["client"].post(
                    f"{DASHEN_INFO_API_ROOT}/v1/web/game/report/getReportToken",
                    content=body.encode("utf-8"),
                    headers=self._dashen_signed_web_headers(session, body, signature),
                )
                token_response.raise_for_status()
                token_payload = self._dashen_response_json(token_response)
                token_result = token_payload.get("result")
                token = token_result.get("token") if isinstance(token_result, dict) else ""
                result_role_id = token_result.get("roleId") if isinstance(token_result, dict) else role_id
                if int(token_payload.get("code") or 0) != 200 or not token:
                    message = str(token_payload.get("errmsg") or token_payload.get("message") or "")[:120]
                    raise ValueError(message or "网易大神 token 转换失败")
                credential = self._normalize_dashen_credential(result_role_id or role_id, token)
                async with self._dashen_credential_lock:
                    legacy_cleared = await self._persist_dashen_credential(credential)
                response_data = self._dashen_status_payload()
                response_data["legacy_config_cleared"] = legacy_cleared
                response_data["qr_completed"] = True
                session["state"] = "success"
                completed = True
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                logger.warning(f"大神 token 转换失败: {type(exc).__name__}")
                if isinstance(exc, ValueError):
                    return self._page_error(str(exc))
                return self._page_error("连接网易大神失败，请稍后重试")

        if completed:
            await self._dashen_qr_drop(session_id, expected=session)
        return self._page_ok(response_data)

    async def _dashen_qr_cancel(self):
        payload = await web_request.get_json(silent=True)
        if not isinstance(payload, dict):
            return self._page_error("请求格式无效")
        try:
            session_id = self._dashen_qr_session_id(payload.get("session_id"))
        except ValueError as exc:
            return self._page_error(str(exc))
        await self._dashen_qr_drop(session_id)
        return self._page_ok({"cancelled": True})

    def _start_embedded_overstats(self):
        """启动内置的 Overstats HTTP 服务"""
        try:
            # 添加 Overstats 目录到 Python 路径
            overstats_dir = str(Path(__file__).parent / "Overstats")
            if overstats_dir not in sys.path:
                sys.path.insert(0, overstats_dir)

            # 清除模块缓存，确保读取到修改后的配置文件
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("config") or mod_name.startswith("overstats"):
                    del sys.modules[mod_name]

            # 导入 config.config 模块（不是 config 包）
            from config import config as overstats_config

            if self._dashen_credential:
                overstats_config.DASHEN_ACCOUNTS = [
                    {
                        "name": "plugin-account",
                        "role_id": int(self._dashen_credential["role_id"]),
                        "token": str(self._dashen_credential["token"]),
                    }
                ]
            else:
                logger.warning("尚未配置大神凭证，请在插件详情页打开“大神账号授权”")

            # 禁用 Overstats 内置 AI，使用 Astrbot 的 LLM
            overstats_config.ANALYSIS_BASE_URL = ""
            overstats_config.ANALYSIS_API_KEY = ""

            # 确保必要的配置字段有默认值
            if not getattr(overstats_config, "DASHEN_USER_AGENT", ""):
                overstats_config.DASHEN_USER_AGENT = (
                    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36 "
                    "app/df_client dfVersion/100111"
                )

            # 配置注入完成后再导入 server（server 会导入 client，client 会读取 config）
            from config.loader import APIConfig, get_api_config
            from src import create_server
            from src.cache import init_global_cache

            # 初始化 Astrbot KV 缓存
            self._kv_cache = init_global_cache(
                kv_get=self.get_kv_data,
                kv_put=self.put_kv_data,
                kv_delete=self.delete_kv_data,
                prefix="ow_cache",
            )
            logger.info("Astrbot KV 缓存已初始化")

            api_config = get_api_config()
            # 创建新实例替换 port（APIConfig 是 frozen dataclass）
            api_config = APIConfig(
                host=api_config.host,
                port=self.overstats_port,
                use_stream_response=api_config.use_stream_response,
                enable_database_write=api_config.enable_database_write,
                dashen_max_concurrent_requests=api_config.dashen_max_concurrent_requests,
                dashen_max_accepted_requests=api_config.dashen_max_accepted_requests,
            )
            self._overstats_server = create_server(api_config)
            self._apply_dashen_credential()

            # 在后台线程中运行服务器
            self._overstats_thread = threading.Thread(
                target=self._overstats_server.serve_forever,
                daemon=True,
                name="overstats-server"
            )
            self._overstats_thread.start()

            # 更新 URL 为内置服务地址
            self.overstats_url = f"http://127.0.0.1:{self.overstats_port}"

            logger.info(f"Overstats 内置服务已启动: {self.overstats_url}")
        except Exception as e:
            logger.error(f"Overstats 内置服务启动失败: {e}")
            logger.warning("内置 Overstats 服务启动失败，请检查插件配置中的大神 role_id 和 token 是否正确。")

    def _start_shop_prefetch(self):
        """启动商店和补丁数据预加载"""
        import asyncio
        import threading

        async def _prefetch_data():
            """预加载商店和补丁数据"""
            # 创建独立的 HTTP 客户端（不能跨线程共享）
            client = httpx.AsyncClient(timeout=self.default_timeout)
            try:
                # 等待服务启动完成
                await asyncio.sleep(10)

                # 检查服务是否健康
                try:
                    resp = await client.get(f"{self.overstats_url}/healthz", timeout=5)
                    if resp.status_code != 200:
                        logger.warning("Overstats 服务未就绪，跳过预加载")
                        return
                except Exception:
                    logger.warning("Overstats 服务未就绪，跳过预加载")
                    return

                # 预加载商店数据（异步，不阻塞）
                logger.info("开始预加载商店数据...")
                async def _prefetch_shop():
                    try:
                        await client.post(f"{self.overstats_url}/api/v2/ow-shop/image", json={}, timeout=180)
                        logger.info("商店数据预加载完成")
                    except Exception as e:
                        logger.warning(f"商店预加载失败: {type(e).__name__}: {e}")

                # 预加载补丁数据（异步，不阻塞）
                logger.info("开始预加载补丁数据...")
                async def _prefetch_patch():
                    try:
                        await client.post(f"{self.overstats_url}/api/v2/patch-notes/image", json={}, timeout=300)
                        logger.info("补丁数据预加载完成")
                    except Exception as e:
                        logger.warning(f"补丁预加载失败: {type(e).__name__}: {e}")

                # 并行执行预加载
                await asyncio.gather(
                    _prefetch_shop(),
                    _prefetch_patch(),
                    return_exceptions=True
                )

                # 每天 8 点刷新
                while True:
                    now = datetime.datetime.now()
                    tomorrow_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
                    if tomorrow_8am <= now:
                        tomorrow_8am += datetime.timedelta(days=1)
                    wait_seconds = (tomorrow_8am - now).total_seconds()
                    logger.info(f"下次数据刷新: {tomorrow_8am}")
                    await asyncio.sleep(wait_seconds)

                    # 并行刷新商店和补丁数据
                    logger.info("开始刷新数据...")
                    async def _refresh_shop():
                        try:
                            await client.post(f"{self.overstats_url}/api/v2/ow-shop/image", json={}, timeout=180)
                            logger.info("商店数据刷新完成")
                        except Exception as e:
                            logger.warning(f"商店刷新失败: {type(e).__name__}: {e}")

                    async def _refresh_patch():
                        try:
                            await client.post(f"{self.overstats_url}/api/v2/patch-notes/image", json={}, timeout=300)
                            logger.info("补丁数据刷新完成")
                        except Exception as e:
                            logger.warning(f"补丁刷新失败: {type(e).__name__}: {e}")

                    await asyncio.gather(
                        _refresh_shop(),
                        _refresh_patch(),
                        return_exceptions=True
                    )
            except Exception as e:
                logger.error(f"数据预加载失败: {e}")
            finally:
                await client.aclose()

        def _run_prefetch():
            asyncio.run(_prefetch_data())

        self._prefetch_task = threading.Thread(
            target=_run_prefetch,
            daemon=True,
            name="data-prefetch"
        )
        self._prefetch_task.start()

    async def terminate(self):
        """插件卸载时停止服务"""
        async with self._dashen_qr_sessions_lock:
            qr_session_ids = list(self._dashen_qr_sessions)
        for session_id in qr_session_ids:
            await self._dashen_qr_drop(session_id)
        await self.client.aclose()

        # 停止内置 Overstats 服务
        if self._overstats_server:
            try:
                self._overstats_server.shutdown()
                self._overstats_server.server_close()
                logger.info("Overstats 内置服务已停止")
            except Exception as e:
                logger.error(f"停止 Overstats 服务失败: {e}")

        # 等待服务线程完全退出
        if self._overstats_thread and self._overstats_thread.is_alive():
            self._overstats_thread.join(timeout=5)

        # 清除全局缓存实例
        try:
            from src.cache import set_global_cache
            set_global_cache(None)
        except ImportError:
            pass

        # 清除 Overstats 模块缓存，确保热重载时重新加载
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith(("config", "src", "overstats")):
                del sys.modules[mod_name]

    # ======================== AI 分析方法 ========================

    def _import_overstats_modules(self):
        """延迟导入 Overstats 模块"""
        try:
            overstats_dir = str(Path(__file__).parent / "Overstats")
            if overstats_dir not in sys.path:
                sys.path.insert(0, overstats_dir)

            from src.modules.dashen_match.enhanced_render import (
                generate_match_summary_text,
                generate_detailed_stats_text,
                calculate_match_scores,
                render_analysis_report,
                render_court_report,
                build_carry_index_data,
                build_target_hero_icons,
                map_name_for_match,
                map_icon_image_for_match,
            )
            from src.modules.dashen_match.render import (
                _extract_match_detail_data,
            )
            return {
                "generate_match_summary_text": generate_match_summary_text,
                "generate_detailed_stats_text": generate_detailed_stats_text,
                "calculate_match_scores": calculate_match_scores,
                "render_analysis_report": render_analysis_report,
                "render_court_report": render_court_report,
                "build_carry_index_data": build_carry_index_data,
                "build_target_hero_icons": build_target_hero_icons,
                "map_name_for_match": map_name_for_match,
                "map_icon_image_for_match": map_icon_image_for_match,
                "_extract_match_detail_data": _extract_match_detail_data,
            }
        except Exception as e:
            logger.error(f"导入 Overstats 模块失败: {e}")
            return None

    def _build_analysis_prompt(self, match_data: dict, target_id: str, mode: str = "analysis") -> str:
        """构建 AI 分析 prompt"""
        modules = self._import_overstats_modules()
        if not modules:
            return ""

        summary_text = modules["generate_match_summary_text"](match_data, target_id)
        detailed_text = modules["generate_detailed_stats_text"](match_data.get("_all_player_details", []), target_id)
        score_bundle = modules["calculate_match_scores"](match_data)

        if mode == "court":
            return f"""你是电竞法庭的主审法官。本庭今日审理的是一场守望先锋对局。你需要以绝对中立的视角，基于数据证据，做出公正判决。

【输出要求】
1. 必须严格使用中文，只输出纯 JSON，不要 markdown，不要解释，不要前后缀。
2. 判决必须基于数据事实，不允许主观臆测或无端指责。
3. 好的表现必须肯定，差的表现必须严厉批判，不留情面。

【审判任务】
1. 只审判焦点玩家所在队伍的队友（不含对手），从队友中找出本局 MVP（表现最佳者），给出判决理由。
2. 只审判焦点玩家所在队伍的队友（不含对手），从队友中找出本局最差玩家（被告），给出判决理由。
3. 为被告列出"原罪清单"——具体犯了哪些错误，用数据说话。
4. 对焦点玩家做出判决：是功臣还是罪人，给出评分 S/A/B/C/D。
5. 对焦点玩家所在队伍的所有玩家（含焦点玩家自己）逐一做出有功/有过/无功无过的判决，附一句话理由。
6. 必须严格比较三路：坦克位、输出位、辅助位的对位差距（含对手对比）。

【输出 JSON 模板】
{{
  "player_id": "{target_id}",
  "score": "S/A/B/C/D",
  "verdict": "焦点玩家判决：功臣/罪人/无功无过",
  "mvp": {{"player_id": "MVP玩家ID", "reason": "当选理由"}},
  "worst": {{"player_id": "最差玩家ID", "reason": "判决理由"}},
  "sins": ["原罪1：具体错误", "原罪2：具体错误", "原罪3：具体错误"],
  "player_verdicts": [
    {{"player_id": "玩家1", "verdict": "有功", "reason": "理由"}},
    {{"player_id": "玩家2", "verdict": "有过", "reason": "理由"}}
  ],
  "role_comparison": ["坦克位：对比", "输出位：对比", "辅助位：对比"],
  "key_moment": "关键转折点",
  "attribute_scores": {{
    "anti_pressure": {score_bundle["anti_pressure"]},
    "teamwork": {score_bundle["teamwork"]},
    "aggressiveness": {score_bundle["aggressiveness"]},
    "match_quality": {score_bundle["match_quality"]}
  }},
  "closing": "法官结案陈词（50字以内）"
}}

【原始比赛数据如下】
{summary_text}
--------------------------------------------------
{detailed_text}
--------------------------------------------------"""
        else:
            return f"""请扮演一位资深的电竞数据分析师，根据提供的比赛数据，输出一份犀利、简明扼要的分析报告。

【输出要求】
1. 必须严格使用中文，只输出纯 JSON，不要 markdown，不要解释，不要前后缀。
2. 分析必须专业、简明、直接，结论要清晰，不能硬夸，不能胡编。
3. 所有判断都必须以给定数据为准，不得虚构不存在的对局细节。

【任务】
1. 客观给焦点玩家评分，只能填 S/A/B/C/D。
2. 用一句话指出焦点玩家最大优点或最大问题。
3. 只写一个最关键的胜负手。
4. 必须明确点出真正的 MVP 或背锅位，不能强行偏袒焦点玩家。
5. 必须严格比较三路：坦克位、输出位、辅助位。
6. `attribute_scores` 的数值必须原样保留，不允许改动。

【输出 JSON 模板】
{{
  "player_id": "{target_id}",
  "score": "S/A/B/C/D",
  "general_summary": "一句话核心总结",
  "key_to_win_loss": "决定胜负的唯一核心点",
  "red_black_list": {{
    "mvp_or_potg": "真正的 MVP 或背锅位及依据",
    "role_comparison": ["坦克位：对比", "输出位：对比", "辅助位：对比"],
    "outstanding_performance": "最突出的个人表现"
  }},
  "summary": "一句话总结本场整体观感",
  "attribute_scores": {{
    "anti_pressure": {score_bundle["anti_pressure"]},
    "teamwork": {score_bundle["teamwork"]},
    "aggressiveness": {score_bundle["aggressiveness"]},
    "match_quality": {score_bundle["match_quality"]}
  }},
  "evaluation": "基于四项属性分的针对性评价",
  "extra": "100字以内的人格化鼓励/安慰/小结",
  "carry_index_data": []
}}

【原始比赛数据如下】
{summary_text}
--------------------------------------------------
{detailed_text}
--------------------------------------------------"""

    def _parse_ai_json(self, text: str) -> Optional[dict]:
        """从 LLM 响应中解析 JSON"""
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON 块
        patterns = [
            r'```json\s*([\s\S]*?)\s*```',
            r'```\s*([\s\S]*?)\s*```',
            r'\{[\s\S]*\}',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    json_str = match.group(1) if match.lastindex else match.group(0)
                    return json.loads(json_str)
                except (json.JSONDecodeError, IndexError):
                    continue
        return None

    async def _call_astrbot_llm(self, event: AstrMessageEvent, prompt: str) -> Optional[str]:
        """调用 Astrbot 的 LLM Provider"""
        try:
            provider_id = self.llm_provider_id
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            if not provider_id:
                return None
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            return resp.completion_text
        except Exception as e:
            logger.error(f"调用 Astrbot LLM 失败: {e}")
            return None

    async def _get_match_raw_data(self, bnet_id: str, index: int) -> Optional[dict]:
        """获取对局原始数据"""
        try:
            # 获取对局详情 JSON
            detail = await self._call_overstats("/api/v2/dashen-match/detail", {
                "bnet_id": bnet_id,
                "index": index,
            })
            if not detail.get("ok"):
                return None
            return detail
        except Exception as e:
            logger.error(f"获取对局数据失败: {e}")
            return None

    # ======================== 工具方法 ========================

    def _get_user_key(self, event: AstrMessageEvent) -> str:
        """获取平台用户唯一标识，如 aiocqhttp:123456789"""
        return f"{event.get_platform_name()}:{event.get_sender_id()}"

    async def _resolve_bnet_id(self, event: AstrMessageEvent, arg: Optional[str] = None) -> str:
        """解析玩家 ID：命令参数 > 用户绑定"""
        if arg:
            return arg
        # 查用户绑定
        bind_key = f"bind:{self._get_user_key(event)}"
        bound = await self.get_kv_data(bind_key, "")
        if bound:
            return bound
        return ""

    async def _check_ai_cooldown(self, event: AstrMessageEvent) -> tuple[bool, int]:
        """检查 AI 冷却，返回 (可用, 剩余秒数)"""
        user_key = self._get_user_key(event)
        sender_id = event.get_sender_id()
        if user_key in self.ai_whitelist or sender_id in self.ai_whitelist:
            return True, 0
        cooldown_key = f"ai_cooldown:{user_key}"
        last_used = await self.get_kv_data(cooldown_key, 0)
        now = int(time.time())
        elapsed = now - last_used
        if elapsed >= self.ai_cooldown:
            return True, 0
        return False, self.ai_cooldown - elapsed

    async def _set_ai_cooldown(self, event: AstrMessageEvent):
        """设置 AI 冷却时间戳"""
        cooldown_key = f"ai_cooldown:{self._get_user_key(event)}"
        await self.put_kv_data(cooldown_key, int(time.time()))

    async def _check_overstats_health(self) -> bool:
        """检查 Overstats 服务是否健康"""
        try:
            resp = await self.client.get(f"{self.overstats_url}/healthz", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _uses_dashen_player_search(endpoint: str, payload: dict) -> bool:
        if "/dashen-" not in str(endpoint or ""):
            return False
        bnet_keys = (
            "bnet_id",
            "bnetId",
            "full_id",
            "fullId",
            "player1_bnet_id",
            "player1BnetId",
            "player2_bnet_id",
            "player2BnetId",
        )
        return any(str(payload.get(key) or "").strip() for key in bnet_keys)

    async def _get_dashen_search_maintenance_notice(self) -> str:
        now = time.monotonic()
        if now < self._dashen_search_notice_expires_at:
            return self._dashen_search_notice

        async with self._dashen_search_notice_lock:
            now = time.monotonic()
            if now < self._dashen_search_notice_expires_at:
                return self._dashen_search_notice

            notice = ""
            ttl = DASHEN_SEARCH_NOTICE_TTL_SECONDS
            try:
                response = await self.client.get(
                    DASHEN_QUERY_TOOL_CONFIG_URL,
                    headers={"Cache-Control": "no-cache"},
                    timeout=6,
                )
                response.raise_for_status()
                config_payload = response.json()
                if isinstance(config_payload, dict):
                    candidate = str(config_payload.get("noticeMessage") or "").strip()
                    if "搜索" in candidate and any(
                        keyword in candidate for keyword in ("维护", "暂停", "不可用")
                    ):
                        notice = candidate
            except Exception as exc:
                ttl = DASHEN_SEARCH_NOTICE_FAILURE_TTL_SECONDS
                logger.warning(f"网易大神搜索维护状态检查失败: {type(exc).__name__}: {exc}")

            self._dashen_search_notice = notice
            self._dashen_search_notice_expires_at = time.monotonic() + ttl
            return notice

    async def _raise_if_dashen_search_maintenance(self, endpoint: str, payload: dict) -> None:
        if not self._uses_dashen_player_search(endpoint, payload):
            return
        notice = await self._get_dashen_search_maintenance_notice()
        if notice:
            raise DashenSearchMaintenanceError(notice)

    async def _call_overstats(self, endpoint: str, payload: dict, timeout: Optional[int] = None) -> dict:
        """调用 Overstats API"""
        await self._raise_if_dashen_search_maintenance(endpoint, payload)
        url = f"{self.overstats_url}{endpoint}"
        resp = await self.client.post(url, json=payload, timeout=timeout or self.default_timeout)
        resp.raise_for_status()
        return resp.json()

    async def _call_overstats_image(self, endpoint: str, payload: dict, timeout: Optional[int] = None) -> bytes:
        """调用 Overstats 图片 API，返回 PNG 二进制"""
        await self._raise_if_dashen_search_maintenance(endpoint, payload)
        url = f"{self.overstats_url}{endpoint}"
        resp = await self.client.post(url, json=payload, timeout=timeout or self.default_timeout)
        resp.raise_for_status()
        return resp.content

    async def _save_temp_image(self, data: bytes, suffix: str = ".png") -> str:
        """保存临时图片并返回路径"""
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(data)
        tmp.close()
        return tmp.name

    async def _send_image_from_base64(self, event: AstrMessageEvent, b64: str, media_type: str = "image/png"):
        """从 base64 发送图片"""
        suffix = ".png" if "png" in media_type else ".jpg"
        img_bytes = base64.b64decode(b64)
        path = await self._save_temp_image(img_bytes, suffix)
        yield event.image_result(path)

    def _parse_replies(self, result: dict):
        """解析 /replies 端点返回"""
        replies = result.get("replies", [])
        images = []
        texts = []
        meta = None
        for r in replies:
            rtype = r.get("type")
            if rtype == "image":
                images.append(r.get("base64", ""))
            elif rtype == "text":
                texts.append(r.get("data", ""))
            elif rtype == "meta":
                meta = r.get("data")
        return meta, images, texts

    async def _handle_image_response(self, event: AstrMessageEvent, result: dict):
        """处理含图片的 replies 响应"""
        _, images, texts = self._parse_replies(result)
        for text in texts:
            yield event.plain_result(text)
        for b64 in images:
            async for r in self._send_image_from_base64(event, b64):
                yield r

    async def _handle_overstats_error(self, event: AstrMessageEvent, exc: Exception):
        """统一错误处理"""
        if isinstance(exc, DashenSearchMaintenanceError):
            yield event.plain_result("网易大神官方搜索接口暂时维护中，请等待恢复后重试。")
        elif isinstance(exc, httpx.TimeoutException):
            yield event.plain_result("请求超时，Overstats 服务繁忙，请稍后重试。")
        elif isinstance(exc, httpx.ConnectError):
            yield event.plain_result("无法连接 Overstats 服务，请确认服务已启动。")
        elif isinstance(exc, httpx.HTTPStatusError):
            try:
                err = exc.response.json()
                msg = err.get("message", "")
                hint = err.get("hint", "")
                details = err.get("details", {})
                error_code = err.get("error", "")

                # 从 details 中提取更具体的错误信息
                if not msg or msg == "Internal server error. See details.":
                    detail_msg = details.get("message", "")
                    detail_exc = details.get("exception", "")
                    if detail_msg:
                        msg = detail_msg
                    elif detail_exc:
                        msg = f"服务内部异常：{detail_exc}"

                # 已知错误码：替换英文消息为中文提示
                error_messages = {
                    "bnet_not_found": "未找到该玩家，请检查 BattleTag 是否正确（区分大小写，如 Player#12345）",
                    "dashen_search_maintenance": "网易大神官方搜索接口暂时维护中，请等待恢复后重试。",
                    "invalid_json": "请求格式错误",
                    "missing_target": "请提供 BattleTag 或先绑定",
                    "missing_match_selector": "请提供对局序号，如：ow 详情 1",
                    "missing_customer_token": "缺少玩家凭证，请先查询战绩获取",
                    "render_failed": "图片生成失败，请稍后重试",
                }
                if error_code in error_messages:
                    msg = error_messages[error_code]
                    hint = ""

                # 优先使用上游 hint，否则用内置 hint
                if not hint:
                    builtin_hints = {
                        "bnet_not_found": "已绑定用户可直接使用 ow 资料 查询",
                        "missing_target": "使用 ow 绑定 Player#12345 绑定后可省略玩家参数",
                    }
                    hint = builtin_hints.get(error_code, "")

                lines = [f"请求失败：{msg}"] if msg else ["请求失败"]
                if hint:
                    lines.append(hint)
                yield event.plain_result("\n".join(lines))
            except Exception:
                yield event.plain_result(f"请求失败：HTTP {exc.response.status_code}")
        else:
            yield event.plain_result(f"发生错误：{type(exc).__name__}: {exc}")

    # ======================== 命令组：ow ========================

    @filter.command("ow")
    async def ow_dispatch(self, event: AstrMessageEvent, subcmd: str = "", arg1: str = "", arg2: str = "", arg3: str = ""):
        """守望先锋数据查询"""
        # 显示帮助
        if not subcmd or subcmd in ("help", "帮助"):
            # 从消息中提取命令前缀
            msg = event.message_str.strip()
            prefix = msg.split()[0].rstrip("ow").rstrip("/") + "/" if "ow" in msg else "/"
            p = f"{prefix}ow"
            help_text = f"""OWStats 命令帮助

基础查询：
  {p} 资料 [玩家] - 玩家资料图
  {p} 战绩 [玩家] - 近期战绩图
  {p} 详情 [玩家] [序号] - 单场详情图
  {p} 详情 [玩家] [序号] 锐评 - 详情 + AI 锐评
  {p} 开庭 [玩家] [序号] - AI 开庭（审判视角）
  {p} 段位 [玩家] - 段位历史图
  {p} 今日/昨日/周 [玩家] - 总结图
  {p} 强度 [玩家] - 快速强度分析
  {p} 竞技强度 [玩家] - 竞技强度分析
  {p} 同玩 [玩家1] [玩家2] - 同玩查询

排行榜：
  {p} 排行 省榜 [省] [职责] - 省榜排名
  {p} 排行 英雄 [省] [英雄] - 英雄榜单
  {p} 排行 选取率 [模式] - 英雄选取率

绑定管理：
  {p} 绑定 [BattleTag] - 绑定你的 BattleTag
  {p} 解绑 - 解除绑定
  {p} 我的 - 查看绑定信息

其他：
  {p} 商店 - 当前商店商品
  {p} 赛事 - OWCS 赛事信息
  {p} 补丁 - 最新补丁说明

提示：已绑定用户可省略玩家参数，如 {p} 资料"""
            yield event.plain_result(help_text)
            return

        # 绑定管理
        if subcmd == "绑定":
            async for r in self._cmd_bind(event, arg1):
                yield r
            return
        if subcmd == "解绑":
            async for r in self._cmd_unbind(event):
                yield r
            return
        if subcmd == "我的":
            async for r in self._cmd_my_bind(event):
                yield r
            return
        if subcmd == "查绑":
            async for r in self._cmd_check_bind(event, arg1):
                yield r
            return

        # 其他功能
        if subcmd == "商店":
            async for r in self._cmd_shop(event):
                yield r
            return
        if subcmd == "赛事":
            async for r in self._cmd_esports(event):
                yield r
            return
        if subcmd == "补丁":
            async for r in self._cmd_patch_notes(event):
                yield r
            return

        # 排行榜
        if subcmd == "排行":
            async for r in self._cmd_leaderboard(event, arg1, arg2, arg3):
                yield r
            return

        # 基础查询
        if subcmd == "资料":
            async for r in self._cmd_profile(event, arg1):
                yield r
            return
        if subcmd == "战绩":
            async for r in self._cmd_match_list(event, arg1):
                yield r
            return
        if subcmd == "详情":
            analyze = arg3 == "锐评" or arg2 == "锐评"
            # 解析序号
            index_str = arg2 if arg3 == "锐评" else arg2
            player = arg1
            if arg2 == "锐评":
                index_str = ""
                player = arg1
            elif arg3 == "锐评":
                index_str = arg2
                player = arg1
            # 如果 arg1 是数字，说明省略了玩家
            if arg1 and arg1.isdigit():
                index_str = arg1
                player = ""
                if arg2 == "锐评":
                    analyze = True
            async for r in self._cmd_match_detail(event, player, index_str, analyze):
                yield r
            return
        if subcmd == "开庭":
            # ow 开庭 [玩家] [序号]
            index_str = arg2
            player = arg1
            if arg1 and arg1.isdigit():
                index_str = arg1
                player = ""
            async for r in self._cmd_court(event, player, index_str):
                yield r
            return
        if subcmd == "段位":
            async for r in self._cmd_rank_history(event, arg1):
                yield r
            return
        if subcmd == "今日":
            async for r in self._cmd_summary(event, arg1, "today"):
                yield r
            return
        if subcmd == "昨日":
            async for r in self._cmd_summary(event, arg1, "yesterday"):
                yield r
            return
        if subcmd == "周":
            async for r in self._cmd_summary(event, arg1, "week"):
                yield r
            return
        if subcmd == "强度":
            async for r in self._cmd_quick_strength(event, arg1):
                yield r
            return
        if subcmd == "竞技强度":
            async for r in self._cmd_competitive_strength(event, arg1):
                yield r
            return
        if subcmd == "同玩":
            async for r in self._cmd_sameplay(event, arg1, arg2):
                yield r
            return

    # ======================== 绑定命令 ========================

    async def _cmd_bind(self, event: AstrMessageEvent, bnet_id: str):
        if not bnet_id:
            yield event.plain_result("请提供 BattleTag，如：ow 绑定 Player#12345")
            return
        if "#" not in bnet_id:
            yield event.plain_result("BattleTag 格式错误，必须包含 #，如 Player#12345")
            return
        bind_key = f"bind:{self._get_user_key(event)}"
        await self.put_kv_data(bind_key, bnet_id)
        yield event.plain_result(f"已绑定 BattleTag：{bnet_id}")

    async def _cmd_unbind(self, event: AstrMessageEvent):
        bind_key = f"bind:{self._get_user_key(event)}"
        await self.delete_kv_data(bind_key)
        yield event.plain_result("已解除绑定。")

    async def _cmd_my_bind(self, event: AstrMessageEvent):
        bind_key = f"bind:{self._get_user_key(event)}"
        bound = await self.get_kv_data(bind_key, "")
        if bound:
            yield event.plain_result(f"当前绑定：{bound}")
        else:
            yield event.plain_result("未绑定 BattleTag。使用 ow 绑定 Player#12345 进行绑定。")

    async def _cmd_check_bind(self, event: AstrMessageEvent, target: str):
        # 管理员查看他人绑定
        yield event.plain_result("该功能暂未实现。")

    # ======================== 基础查询命令 ========================

    async def _cmd_profile(self, event: AstrMessageEvent, arg: str):
        bnet_id = await self._resolve_bnet_id(event, arg)
        if not bnet_id:
            yield event.plain_result("请提供 BattleTag 或先绑定。")
            return
        try:
            img = await self._call_overstats_image("/api/v2/dashen-profile/image", {"bnet_id": bnet_id})
            path = await self._save_temp_image(img)
            yield event.image_result(path)
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    async def _cmd_match_list(self, event: AstrMessageEvent, arg: str):
        bnet_id = await self._resolve_bnet_id(event, arg)
        if not bnet_id:
            yield event.plain_result("请提供 BattleTag 或先绑定。")
            return
        try:
            result = await self._call_overstats("/api/v2/dashen-match/replies", {"bnet_id": bnet_id})
            async for r in self._handle_image_response(event, result):
                yield r
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    async def _cmd_match_detail(self, event: AstrMessageEvent, arg: str, index_str: str, analyze: bool):
        bnet_id = await self._resolve_bnet_id(event, arg)
        if not bnet_id:
            yield event.plain_result("请提供 BattleTag 或先绑定。")
            return
        if not index_str:
            yield event.plain_result("请提供对局序号，如：ow 详情 1")
            return
        try:
            index = int(index_str)
        except ValueError:
            yield event.plain_result("序号必须是数字。")
            return
        if index < 1:
            yield event.plain_result("序号必须大于等于 1（1 表示最近一场）。")
            return
        # 转换为 0-based 索引给 Overstats
        index = index - 1

        if analyze:
            ok, remaining = await self._check_ai_cooldown(event)
            if not ok:
                yield event.plain_result(f"AI 锐评冷却中，请 {remaining} 秒后再试。")
                return

        try:
            if analyze:
                # 使用 Astrbot LLM 进行 AI 锐评
                yield event.plain_result("AI 锐评生成中，请稍候...")

                # 获取对局原始数据
                raw_data = await self._get_match_raw_data(bnet_id, index)
                if not raw_data or not raw_data.get("ok"):
                    yield event.plain_result("获取对局数据失败。")
                    return

                # 构建 prompt 并调用 LLM
                modules = self._import_overstats_modules()
                if not modules:
                    yield event.plain_result("Overstats 模块加载失败。")
                    return

                detail = raw_data.get("detail", {})
                match_data = modules["_extract_match_detail_data"](detail)
                target_id = raw_data.get("resolved", {}).get("full_id", bnet_id)

                prompt = self._build_analysis_prompt(match_data, target_id, mode="analysis")
                llm_response = await self._call_astrbot_llm(event, prompt)

                if not llm_response:
                    yield event.plain_result("AI 锐评生成失败：LLM 调用失败。")
                    return

                # 解析 JSON 响应
                parsed = self._parse_ai_json(llm_response)
                if not parsed:
                    yield event.plain_result("AI 锐评生成失败：无法解析 LLM 响应。")
                    return

                # 渲染图片
                import time as _time
                parsed["generated_at"] = _time.strftime("%Y-%m-%d %H:%M", _time.localtime())
                parsed["carry_index_data"] = modules["build_carry_index_data"](match_data)

                focus_player = match_data.get("heroList", [{}])[0] if match_data.get("heroList") else {}
                court_image = modules["render_analysis_report"](
                    parsed,
                    target_hero_images=modules["build_target_hero_icons"](match_data.get("heroList", []), size=40),
                    map_name=modules["map_name_for_match"](match_data),
                    map_icon_img=modules["map_icon_image_for_match"](match_data),
                    match_result="胜利" if match_data.get("matchRet") == 1 else "失败",
                    footer_source="AI锐评 (Astrbot LLM)",
                )
                path = await self._save_temp_image(court_image.content)
                yield event.image_result(path)
                await self._set_ai_cooldown(event)
            else:
                result = await self._call_overstats("/api/v2/dashen-match/detail/replies", {
                    "bnet_id": bnet_id,
                    "index": index,
                    "show_all_heroes": True,
                })
                async for r in self._handle_image_response(event, result):
                    yield r
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    async def _cmd_court(self, event: AstrMessageEvent, arg: str, index_str: str):
        bnet_id = await self._resolve_bnet_id(event, arg)
        if not bnet_id:
            yield event.plain_result("请提供 BattleTag 或先绑定。")
            return
        if not index_str:
            yield event.plain_result("请提供对局序号，如：ow 开庭 1")
            return
        try:
            index = int(index_str)
        except ValueError:
            yield event.plain_result("序号必须是数字。")
            return
        if index < 1:
            yield event.plain_result("序号必须大于等于 1（1 表示最近一场）。")
            return
        # 转换为 0-based 索引给 Overstats
        index = index - 1

        ok, remaining = await self._check_ai_cooldown(event)
        if not ok:
            yield event.plain_result(f"AI 开庭冷却中，请 {remaining} 秒后再试。")
            return

        try:
            # 使用 Astrbot LLM 进行 AI 开庭
            yield event.plain_result("AI 开庭生成中，请稍候...")

            # 获取对局原始数据
            raw_data = await self._get_match_raw_data(bnet_id, index)
            if not raw_data or not raw_data.get("ok"):
                yield event.plain_result("获取对局数据失败。")
                return

            # 构建 prompt 并调用 LLM
            modules = self._import_overstats_modules()
            if not modules:
                yield event.plain_result("Overstats 模块加载失败。")
                return

            detail = raw_data.get("detail", {})
            match_data = modules["_extract_match_detail_data"](detail)
            target_id = raw_data.get("resolved", {}).get("full_id", bnet_id)

            prompt = self._build_analysis_prompt(match_data, target_id, mode="court")
            llm_response = await self._call_astrbot_llm(event, prompt)

            if not llm_response:
                yield event.plain_result("AI 开庭生成失败：LLM 调用失败。")
                return

            # 解析 JSON 响应
            parsed = self._parse_ai_json(llm_response)
            if not parsed:
                yield event.plain_result("AI 开庭生成失败：无法解析 LLM 响应。")
                return

            # 渲染图片
            import time as _time
            parsed["generated_at"] = _time.strftime("%Y-%m-%d %H:%M", _time.localtime())
            parsed["carry_index_data"] = modules["build_carry_index_data"](match_data)

            court_image = modules["render_court_report"](
                parsed,
                target_hero_images=modules["build_target_hero_icons"](match_data.get("heroList", []), size=40),
                map_name=modules["map_name_for_match"](match_data),
                map_icon_img=modules["map_icon_image_for_match"](match_data),
                match_result="胜利" if match_data.get("matchRet") == 1 else "失败",
                footer_source="AI开庭 (Astrbot LLM)",
            )
            path = await self._save_temp_image(court_image.content)
            yield event.image_result(path)
            await self._set_ai_cooldown(event)
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    async def _cmd_rank_history(self, event: AstrMessageEvent, arg: str):
        bnet_id = await self._resolve_bnet_id(event, arg)
        if not bnet_id:
            yield event.plain_result("请提供 BattleTag 或先绑定。")
            return
        try:
            img = await self._call_overstats_image("/api/v2/dashen-rank-history/image", {"bnet_id": bnet_id})
            path = await self._save_temp_image(img)
            yield event.image_result(path)
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    async def _cmd_summary(self, event: AstrMessageEvent, arg: str, scope: str):
        bnet_id = await self._resolve_bnet_id(event, arg)
        if not bnet_id:
            yield event.plain_result("请提供 BattleTag 或先绑定。")
            return
        timeout = self.summary_timeout if scope == "week" else self.default_timeout
        try:
            img = await self._call_overstats_image(
                f"/api/v2/dashen-summary/{scope}/image",
                {"bnet_id": bnet_id},
                timeout=timeout,
            )
            path = await self._save_temp_image(img)
            yield event.image_result(path)
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    async def _cmd_quick_strength(self, event: AstrMessageEvent, arg: str):
        bnet_id = await self._resolve_bnet_id(event, arg)
        if not bnet_id:
            yield event.plain_result("请提供 BattleTag 或先绑定。")
            return
        try:
            img = await self._call_overstats_image("/api/v2/dashen-quick-strength/image", {"bnet_id": bnet_id})
            path = await self._save_temp_image(img)
            yield event.image_result(path)
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    async def _cmd_competitive_strength(self, event: AstrMessageEvent, arg: str):
        bnet_id = await self._resolve_bnet_id(event, arg)
        if not bnet_id:
            yield event.plain_result("请提供 BattleTag 或先绑定。")
            return
        try:
            img = await self._call_overstats_image("/api/v2/dashen-competitive-strength/image", {"bnet_id": bnet_id})
            path = await self._save_temp_image(img)
            yield event.image_result(path)
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    async def _cmd_sameplay(self, event: AstrMessageEvent, arg1: str, arg2: str):
        if not arg1 or not arg2:
            yield event.plain_result("请提供两个玩家，如：ow 同玩 Player1#1111 Player2#2222")
            return
        try:
            result = await self._call_overstats("/api/v2/dashen-sameplay/replies", {
                "player1_bnet_id": arg1,
                "player2_bnet_id": arg2,
            })
            async for r in self._handle_image_response(event, result):
                yield r
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    # ======================== 排行榜命令 ========================

    def _normalize_province(self, province: str) -> str:
        """规范化省份名称，去除省/市/自治区等后缀"""
        suffixes = ["省", "市", "自治区", "壮族自治区", "回族自治区", "维吾尔自治区", "特别行政区"]
        for suffix in suffixes:
            if province.endswith(suffix):
                return province[:-len(suffix)]
        return province

    async def _cmd_leaderboard(self, event: AstrMessageEvent, subcmd: str, arg1: str, arg2: str):
        if subcmd == "省榜":
            province = self._normalize_province(arg1) if arg1 else "北京"
            role = arg2 or "tank"
            try:
                img = await self._call_overstats_image("/api/v2/dashen-rank-leaderboard/image", {
                    "province": province,
                    "role": role,
                })
                path = await self._save_temp_image(img)
                yield event.image_result(path)
            except Exception as exc:
                async for r in self._handle_overstats_error(event, exc):
                    yield r
            return
        if subcmd == "英雄":
            province = self._normalize_province(arg1) if arg1 else "北京"
            hero = arg2 or "猎空"
            try:
                img = await self._call_overstats_image("/api/v2/dashen-hero-leaderboard/image", {
                    "province": province,
                    "hero": hero,
                })
                path = await self._save_temp_image(img)
                yield event.image_result(path)
            except Exception as exc:
                async for r in self._handle_overstats_error(event, exc):
                    yield r
            return
        if subcmd == "选取率":
            # 翻译中文模式名
            mode_map = {
                "竞技": "competitive",
                "排位": "competitive",
                "快速": "quick",
                "quick": "quick",
                "competitive": "competitive",
            }
            if not arg1:
                yield event.plain_result("用法：ow 排行 选取率 [模式]\n模式：竞技/排位、快速")
                return
            mode = mode_map.get(arg1)
            if not mode:
                yield event.plain_result(f"不支持的模式：{arg1}\n可用模式：竞技/排位、快速")
                return
            try:
                img = await self._call_overstats_image("/api/v2/ow-hero-pick-rate/image", {
                    "view": "ranking",
                    "game_mode": mode,
                })
                path = await self._save_temp_image(img)
                yield event.image_result(path)
            except Exception as exc:
                async for r in self._handle_overstats_error(event, exc):
                    yield r
            return
        yield event.plain_result("未知排行子命令。可选：省榜、英雄、选取率")

    # ======================== 其他功能命令 ========================

    async def _cmd_shop(self, event: AstrMessageEvent):
        if self.embedded_overstats and not self._overstats_server:
            yield event.plain_result("Overstats 服务正在启动中，请稍后再试...")
            return
        if not await self._check_overstats_health():
            yield event.plain_result("Overstats 服务未就绪，请稍后再试...")
            return
        try:
            yield event.plain_result("正在获取商店数据，请稍候...")
            img = await self._call_overstats_image("/api/v2/ow-shop/image", {}, timeout=self.shop_timeout)
            path = await self._save_temp_image(img)
            yield event.image_result(path)
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    async def _cmd_esports(self, event: AstrMessageEvent):
        try:
            img = await self._call_overstats_image("/api/v2/ow-esports/image", {})
            path = await self._save_temp_image(img)
            yield event.image_result(path)
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r

    async def _cmd_patch_notes(self, event: AstrMessageEvent):
        if self.embedded_overstats and not self._overstats_server:
            yield event.plain_result("Overstats 服务正在启动中，请稍后再试...")
            return
        if not await self._check_overstats_health():
            yield event.plain_result("Overstats 服务未就绪，请稍后再试...")
            return
        try:
            yield event.plain_result("正在获取补丁信息，请稍候...")
            img = await self._call_overstats_image("/api/v2/patch-notes/image", {}, timeout=self.shop_timeout)
            path = await self._save_temp_image(img)
            yield event.image_result(path)
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r
