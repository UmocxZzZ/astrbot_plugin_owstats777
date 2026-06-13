from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
import json
from pathlib import Path
from typing import Optional

from .registry import get_http_ui_bootstrap_payload, get_http_ui_module_specs


ASSET_DIR = Path(__file__).resolve().parent / "assets"


@dataclass(frozen=True)
class HTTPUIAssetResponse:
    status: HTTPStatus
    content_type: str
    body: bytes


def _read_text_asset(name: str) -> str:
    return (ASSET_DIR / name).read_text(encoding="utf-8")


def _read_binary_asset(name: str) -> bytes:
    return (ASSET_DIR / name).read_bytes()


def resolve_http_ui_asset(path: str) -> Optional[HTTPUIAssetResponse]:
    normalized = str(path or "").strip() or "/"
    if normalized == "/":
        return HTTPUIAssetResponse(
            status=HTTPStatus.OK,
            content_type="text/html; charset=utf-8",
            body=_render_index_html(),
        )
    if normalized == "/ui/app.css":
        return HTTPUIAssetResponse(
            status=HTTPStatus.OK,
            content_type="text/css; charset=utf-8",
            body=_read_binary_asset("app.css"),
        )
    if normalized == "/ui/app.js":
        return HTTPUIAssetResponse(
            status=HTTPStatus.OK,
            content_type="application/javascript; charset=utf-8",
            body=_read_binary_asset("app.js"),
        )
    if normalized == "/ui/healthz":
        payload = {
            "ok": True,
            "service": "overstats-http-ui",
            "module_count": len(get_http_ui_module_specs()),
            "root_path": "/",
        }
        return HTTPUIAssetResponse(
            status=HTTPStatus.OK,
            content_type="application/json; charset=utf-8",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    return None


def _render_index_html() -> bytes:
    bootstrap_json = json.dumps(get_http_ui_bootstrap_payload(), ensure_ascii=False).replace("<", "\\u003c")
    bootstrap_script = (
        "<script>"
        "window.__OVERSTATS_UI_BOOTSTRAP__ = "
        f"{bootstrap_json};"
        "</script>"
    )
    html_text = _read_text_asset("index.html").replace("__OVERSTATS_UI_BOOTSTRAP__", bootstrap_script)
    return html_text.encode("utf-8")
