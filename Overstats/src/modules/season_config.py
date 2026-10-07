"""
Centralized Dashen season configuration.

Derives the current season, history start season, and rollover time
from config.py and query_tool.json instead of hardcoding them in
multiple requests.py files.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

try:
    from overstats.config import config as overstats_config
except ModuleNotFoundError:
    try:
        from config import config as overstats_config
    except ModuleNotFoundError:
        overstats_config = None  # type: ignore[assignment]


_QUERY_TOOL_PATH_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "res" / "query_tool.json",
    Path(__file__).resolve().parents[3] / "Overstats" / "res" / "query_tool.json",
)


def _config_value(name: str, default: Any) -> Any:
    if overstats_config is None:
        return default
    return getattr(overstats_config, name, default)


def _find_query_tool_path() -> Optional[Path]:
    for candidate in _QUERY_TOOL_PATH_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _read_query_tool_config() -> Dict[str, Any]:
    path = _find_query_tool_path()
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def parse_dashen_season_time(date_str: str) -> Optional[dt.datetime]:
    """Parse date strings like '2026.4.15' or '2026/4/15' or '2026-04-15'."""
    normalized = str(date_str or "").strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m"):
        try:
            return dt.datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def get_dashen_manual_season() -> int:
    """Read DASHEN_CURRENT_SEASON from config (default 25)."""
    return int(_config_value("DASHEN_CURRENT_SEASON", 25))


def get_dashen_history_start_season() -> int:
    """Read DASHEN_HISTORY_START_SEASON from config (default 15)."""
    return int(_config_value("DASHEN_HISTORY_START_SEASON", 15))


def _extract_auto_dashen_season(config: Mapping[str, Any]) -> int:
    """Scan AIEvaluateConfig[].seasonIdList in query_tool.json for max season."""
    max_season = 0
    entries = config.get("AIEvaluateConfig")
    if not isinstance(entries, list):
        return max_season
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("seasonIdList"), list):
            continue
        for season_id in entry["seasonIdList"]:
            try:
                max_season = max(max_season, int(season_id))
            except (TypeError, ValueError):
                continue
    return max_season


def get_dashen_current_season(query_tool_config: Optional[Mapping[str, Any]] = None) -> int:
    """Return max(manual, auto) season."""
    manual = get_dashen_manual_season()
    config = query_tool_config if query_tool_config is not None else _read_query_tool_config()
    auto = _extract_auto_dashen_season(config)
    return max(manual, auto)


def get_dashen_season_rollover_at(query_tool_config: Optional[Mapping[str, Any]] = None) -> Optional[dt.datetime]:
    """Parse the rollover datetime from query_tool.json's seasonList."""
    config = query_tool_config if query_tool_config is not None else _read_query_tool_config()
    current = get_dashen_current_season(config)
    season_list = config.get("seasonList")
    if not isinstance(season_list, Mapping):
        return None
    current_info = season_list.get(str(current))
    if isinstance(current_info, Mapping):
        parsed = parse_dashen_season_time(current_info.get("startTime"))
        if parsed is not None:
            return parsed
    previous_info = season_list.get(str(current - 1))
    if isinstance(previous_info, Mapping):
        return parse_dashen_season_time(previous_info.get("endTime"))
    return None


def normalize_query_tool_config(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Fill in missing seasonList entries for seasons from start to current."""
    normalized = dict(config or {})
    current = get_dashen_current_season(normalized)
    start = min(get_dashen_history_start_season(), current)
    raw_season_list = normalized.get("seasonList")
    season_list = dict(raw_season_list) if isinstance(raw_season_list, Mapping) else {}
    for season_id in range(start, current + 1):
        key = str(season_id)
        if isinstance(season_list.get(key), Mapping):
            continue
        previous = season_list.get(str(season_id - 1))
        previous = previous if isinstance(previous, Mapping) else {}
        season_list[key] = {
            "startTime": str(previous.get("endTime") or "").strip(),
            "endTime": "",
            "desc": f"S{season_id}",
        }
    normalized["seasonList"] = season_list
    return normalized
