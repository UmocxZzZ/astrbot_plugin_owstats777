"""
Centralized Dashen season configuration.

Derives the current season, history start season, and rollover time
from config.py and query_tool.json instead of hardcoding them in
multiple requests.py files.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_dashen_season_time(date_str: str) -> Optional[dt.datetime]:
    """Parse date strings like '2026.4.15' or '2026/4/15' or '2026-04-15'."""
    normalized = str(date_str or "").strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def get_dashen_manual_season() -> int:
    """Read DASHEN_CURRENT_SEASON from config (default 23)."""
    return int(_config_value("DASHEN_CURRENT_SEASON", 23))


def get_dashen_history_start_season() -> int:
    """Read DASHEN_HISTORY_START_SEASON from config (default 15)."""
    return int(_config_value("DASHEN_HISTORY_START_SEASON", 15))


def _extract_auto_dashen_season() -> int:
    """Scan AIEvaluateConfig[].seasonIdList in query_tool.json for max season."""
    config = _read_query_tool_config()
    max_season = 0
    for entry in config.get("AIEvaluateConfig", []):
        for season_id in entry.get("seasonIdList", []):
            try:
                max_season = max(max_season, int(season_id))
            except (TypeError, ValueError):
                continue
    return max_season


def get_dashen_current_season() -> int:
    """Return max(manual, auto) season."""
    manual = get_dashen_manual_season()
    auto = _extract_auto_dashen_season()
    return max(manual, auto)


def get_dashen_season_rollover_at() -> dt.datetime:
    """Parse the rollover datetime from query_tool.json's seasonList."""
    config = _read_query_tool_config()
    season_list = config.get("seasonList", [])
    if season_list and isinstance(season_list, list) and len(season_list) > 0:
        # Use the latest season's startTime as rollover reference
        latest = season_list[-1] if isinstance(season_list[-1], dict) else {}
        start_time = latest.get("startTime", "")
        parsed = parse_dashen_season_time(start_time)
        if parsed is not None:
            return parsed
    # Fallback: use a reasonable default
    return dt.datetime(2026, 4, 15, 0, 0, 0)


def normalize_query_tool_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Fill in missing seasonList entries for seasons from start to current."""
    current = get_dashen_current_season()
    start = get_dashen_history_start_season()
    season_list = config.get("seasonList", [])

    existing_seasons = set()
    for entry in season_list:
        if isinstance(entry, dict):
            try:
                existing_seasons.add(int(entry.get("seasonId", 0)))
            except (TypeError, ValueError):
                continue

    for season_id in range(start, current + 1):
        if season_id not in existing_seasons:
            season_list.append({
                "seasonId": season_id,
                "startTime": f"2025.{(season_id - 15) % 12 + 1}.1",
            })

    config["seasonList"] = season_list
    return config
