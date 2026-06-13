from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
import random
import re
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import httpx

try:
    from overstats.config import config as app_config
    from overstats.src.constants import CHARA_NAME, iter_hero_alias_pairs
    from overstats.src.modules.errors import ModuleError
    from overstats.src.modules.query_tool import read_query_tool
except ModuleNotFoundError:
    from config import config as app_config
    from src.constants import CHARA_NAME, iter_hero_alias_pairs
    from src.modules.errors import ModuleError
    from src.modules.query_tool import read_query_tool


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CATALOG_ROOT = PROJECT_ROOT / "res" / "ow_guess"
DEFAULT_ASSET_ROOT_NAME = "ow_guess_assets"
LEGACY_ASSET_ROOT_NAME = "../ow_guess_assets"
DEFAULT_ASSET_ROOT_CANDIDATES = (
    (PROJECT_ROOT / DEFAULT_ASSET_ROOT_NAME).resolve(),
    (PROJECT_ROOT / LEGACY_ASSET_ROOT_NAME).resolve(),
)
DEFAULT_ASSET_ROOT_ALIASES = {
    "",
    DEFAULT_ASSET_ROOT_NAME,
    f"./{DEFAULT_ASSET_ROOT_NAME}",
    LEGACY_ASSET_ROOT_NAME,
}
DEFAULT_ASSET_ROOT_ALIAS_KEYS = {alias.lower() for alias in DEFAULT_ASSET_ROOT_ALIASES}
REMOTE_IMAGE_TIMEOUT = httpx.Timeout(20.0, connect=8.0, read=20.0, write=20.0, pool=8.0)
REMOTE_IMAGE_HEADERS = {
    "Accept": "image/*,*/*",
    "User-Agent": "overstats-ow-guess/1.0",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
REMOTE_QUERY_TOOL_TYPES = {"hero_icon", "map_image", "hero_silhouette"}


def _normalize_asset_root_value(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def _pick_default_asset_root(candidates: Sequence[Path] | None = None) -> Path:
    resolved_candidates = [Path(candidate).resolve() for candidate in (candidates or DEFAULT_ASSET_ROOT_CANDIDATES)]
    for candidate in resolved_candidates:
        if candidate.exists():
            return candidate
    return resolved_candidates[0]


def _resolve_asset_root(value: Any) -> Path:
    normalized = _normalize_asset_root_value(value)
    if normalized.lower() in DEFAULT_ASSET_ROOT_ALIAS_KEYS:
        return _pick_default_asset_root()
    candidate = Path(normalized)
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


DEFAULT_ASSET_ROOT = _pick_default_asset_root()


@dataclass(frozen=True)
class OWGuessTypeSpec:
    slug: str
    type_id: int
    label: str
    aliases: Sequence[str]
    media_kind: str
    recommended_wait_seconds: int
    supported: bool = True


@dataclass(frozen=True)
class OWGuessQuestionSelection:
    type_spec: OWGuessTypeSpec
    question_id: str
    difficulty: int
    prompt_text: str
    answer_canonical: str
    answer_aliases: Sequence[str]
    hint_steps: Sequence[Dict[str, Any]]
    payload: Mapping[str, Any]


QUESTION_TYPE_SPECS: Sequence[OWGuessTypeSpec] = (
    OWGuessTypeSpec(
        slug="map_music",
        type_id=1,
        label="地图音乐",
        aliases=("1", "map_music", "mapmusic", "地图音乐", "音乐", "地图bgm", "bgm"),
        media_kind="audio",
        recommended_wait_seconds=60,
    ),
    OWGuessTypeSpec(
        slug="hero_icon",
        type_id=2,
        label="英雄图标",
        aliases=("2", "hero_icon", "heroicon", "英雄图标", "图标", "头像"),
        media_kind="image",
        recommended_wait_seconds=30,
    ),
    OWGuessTypeSpec(
        slug="skill_icon_hero",
        type_id=3,
        label="技能图标猜英雄",
        aliases=("3", "skill_icon_hero", "skilliconhero", "技能图标", "技能图标猜英雄", "技能猜英雄"),
        media_kind="image",
        recommended_wait_seconds=30,
    ),
    OWGuessTypeSpec(
        slug="perk_icon_hero",
        type_id=4,
        label="威能图标猜英雄",
        aliases=("4", "perk_icon_hero", "perkiconhero", "威能图标", "威能", "perk", "perk图标", "威能猜英雄"),
        media_kind="image",
        recommended_wait_seconds=30,
    ),
    OWGuessTypeSpec(
        slug="map_image",
        type_id=5,
        label="地图图片",
        aliases=("5", "map_image", "mapimage", "地图图片", "地图截图", "地图"),
        media_kind="image",
        recommended_wait_seconds=30,
    ),
    OWGuessTypeSpec(
        slug="ult_voice",
        type_id=6,
        label="终极语音",
        aliases=("6", "ult_voice", "ultvoice", "终极语音", "大招语音", "语音", "ult", "大招"),
        media_kind="audio",
        recommended_wait_seconds=30,
    ),
    OWGuessTypeSpec(
        slug="hero_silhouette",
        type_id=8,
        label="猜猜我是谁",
        aliases=("8", "hero_silhouette", "herosilhouette", "猜猜我是谁", "我是谁", "剪影"),
        media_kind="image",
        recommended_wait_seconds=30,
    ),
    OWGuessTypeSpec(
        slug="hero_conversation",
        type_id=9,
        label="英雄对话",
        aliases=("9", "hero_conversation", "heroconversation", "英雄对话", "对话", "对话语音", "互动语音"),
        media_kind="audio",
        recommended_wait_seconds=30,
        supported=False,
    ),
    OWGuessTypeSpec(
        slug="skill_icon_name",
        type_id=10,
        label="技能图标猜技能名",
        aliases=("10", "skill_icon_name", "skilliconname", "技能名", "技能全称", "技能图标猜技能名", "技能图标猜技能"),
        media_kind="image",
        recommended_wait_seconds=30,
    ),
    OWGuessTypeSpec(
        slug="hero_description",
        type_id=11,
        label="描述猜英雄",
        aliases=("11", "hero_description", "herodescription", "描述猜英雄", "描述", "设定", "英雄描述"),
        media_kind="text",
        recommended_wait_seconds=60,
    ),
)


def _normalize_lookup_key(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return re.sub(r"[\s\-_/.:：，,!?！？]+", "", normalized)


def _strip_html_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"(?i)<div[^>]*>", "", text)
    text = re.sub(r"(?i)<img[^>]*>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    compact = "\n".join(line for line in lines if line)
    return compact.strip()


def _unique_strings(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


_TYPE_BY_KEY = {
    _normalize_lookup_key(alias): spec
    for spec in QUESTION_TYPE_SPECS
    for alias in (spec.slug, spec.label, *spec.aliases)
}
_HERO_ALIAS_TO_CANONICAL = {
    _normalize_lookup_key(alias): canonical
    for alias, canonical in iter_hero_alias_pairs()
}


def _resolve_hero_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    return _HERO_ALIAS_TO_CANONICAL.get(_normalize_lookup_key(text), text)


def _hero_aliases(hero_name: str) -> List[str]:
    canonical = _resolve_hero_name(hero_name)
    aliases = CHARA_NAME.get(canonical, [])
    return _unique_strings([canonical, *aliases])


def _guess_remote_extension(url: str, content_type: str) -> str:
    normalized_type = str(content_type or "").split(";", 1)[0].strip()
    guessed = mimetypes.guess_extension(normalized_type)
    if guessed:
        return guessed
    suffix = Path(httpx.URL(str(url or "")).path or "").suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return suffix
    return ".img"


class OWGuessCatalog:
    def __init__(
        self,
        *,
        resource_root: Path | str | None = None,
        catalog_root: Path | str | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        configured_asset_root = resource_root if resource_root is not None else getattr(app_config, "OW_GUESS_ASSET_ROOT", "")
        self.resource_root = _resolve_asset_root(configured_asset_root)
        self.catalog_root = Path(catalog_root or DEFAULT_CATALOG_ROOT)
        self.random = random_source or random.Random()
        self._entries_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._query_tool_cache: Dict[str, Any] | None = None

    def resolve_question_type(self, value: Any) -> OWGuessTypeSpec:
        normalized = _normalize_lookup_key(value)
        if not normalized:
            raise ModuleError(
                error="ow_guess_invalid_type",
                message="question_type is required.",
                status_code=400,
                hint='Example: {"question_type":"hero_icon"}',
                details={"question_type": value},
            )
        spec = _TYPE_BY_KEY.get(normalized)
        if spec is None:
            raise ModuleError(
                error="ow_guess_invalid_type",
                message=f"Unknown OW guess question type: {value}",
                status_code=400,
                details={"question_type": value},
            )
        if not spec.supported:
            raise ModuleError(
                error="ow_guess_type_unavailable",
                message=f"Question type is not available yet: {spec.slug}",
                status_code=400,
                details={"question_type": spec.slug},
            )
        return spec

    def load_entries(self, type_slug: str) -> List[Dict[str, Any]]:
        cached = self._entries_cache.get(type_slug)
        if cached is not None:
            return list(cached)

        if type_slug in REMOTE_QUERY_TOOL_TYPES:
            builder = getattr(self, f"_build_{type_slug}_entries", None)
            if builder is None:
                raise ModuleError(
                    error="ow_guess_resources_unavailable",
                    message=f"Question builder is missing for type: {type_slug}",
                    status_code=500,
                )
            entries = builder()
        else:
            path = self.catalog_root / type_slug / "questions.json"
            if not path.exists():
                raise ModuleError(
                    error="ow_guess_resources_unavailable",
                    message=f"Question catalog is missing for type: {type_slug}",
                    status_code=500,
                    details={"path": str(path)},
                )
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ModuleError(
                    error="ow_guess_resources_unavailable",
                    message=f"Question catalog is unreadable for type: {type_slug}",
                    status_code=500,
                    details={"path": str(path), "exception": type(exc).__name__, "message": str(exc)},
                ) from exc
            if not isinstance(payload, list):
                raise ModuleError(
                    error="ow_guess_resources_unavailable",
                    message=f"Question catalog is malformed for type: {type_slug}",
                    status_code=500,
                    details={"path": str(path)},
                )
            entries = [dict(item) for item in payload if isinstance(item, dict)]
            preparer = getattr(self, f"_prepare_{type_slug}_entries", None)
            if callable(preparer):
                entries = preparer(entries)

        self._entries_cache[type_slug] = entries
        return list(entries)

    def pick_question(self, type_spec: OWGuessTypeSpec) -> OWGuessQuestionSelection:
        entries = self.load_entries(type_spec.slug)
        if not entries:
            raise ModuleError(
                error="ow_guess_type_unavailable",
                message=f"No installed questions are available for type: {type_spec.slug}",
                status_code=400,
                details={"question_type": type_spec.slug, "reason": "empty_catalog"},
            )
        entry = dict(self.random.choice(entries))
        builder_name = f"_build_{type_spec.slug}_selection"
        builder = getattr(self, builder_name, None)
        if builder is None:
            raise ModuleError(
                error="ow_guess_resources_unavailable",
                message=f"Question builder is missing for type: {type_spec.slug}",
                status_code=500,
            )
        return builder(type_spec, entry)

    async def resolve_media_path(self, selection: OWGuessQuestionSelection) -> Optional[Path]:
        payload = dict(selection.payload)
        local_path = payload.get("local_path")
        if local_path:
            path = Path(str(local_path))
            if path.exists():
                return path
            raise FileNotFoundError(path)

        remote_url = str(payload.get("remote_url") or "").strip()
        if remote_url:
            return await self._cache_remote_asset(remote_url, selection.type_spec.slug)
        return None

    def hero_aliases_for(self, hero_name: str) -> List[str]:
        return _hero_aliases(hero_name)

    def _build_map_music_selection(self, type_spec: OWGuessTypeSpec, entry: Dict[str, Any]) -> OWGuessQuestionSelection:
        answer_pool = _unique_strings(entry.get("answer") or [])
        if not answer_pool:
            raise ModuleError(
                error="ow_guess_resources_unavailable",
                message="map_music question is missing answers.",
                status_code=500,
                details={"question_id": entry.get("question_id")},
            )
        local_path = self._resolve_asset_path("map_music", "assets", str(entry.get("audio_file") or "").strip())
        return OWGuessQuestionSelection(
            type_spec=type_spec,
            question_id=str(entry.get("question_id") or ""),
            difficulty=int(entry.get("difficulty") or 3),
            prompt_text=str(entry.get("prompt_text") or "请尝试猜出音乐对应的地图"),
            answer_canonical=answer_pool[0],
            answer_aliases=tuple(answer_pool),
            hint_steps=(),
            payload={"local_path": str(local_path)},
        )

    def _build_hero_icon_selection(self, type_spec: OWGuessTypeSpec, entry: Dict[str, Any]) -> OWGuessQuestionSelection:
        hero_name = _resolve_hero_name(entry.get("hero_name") or entry.get("name"))
        return OWGuessQuestionSelection(
            type_spec=type_spec,
            question_id=str(entry.get("question_id") or ""),
            difficulty=int(entry.get("difficulty") or 3),
            prompt_text="请尝试猜出英雄图标对应的英雄",
            answer_canonical=hero_name,
            answer_aliases=tuple(_hero_aliases(hero_name)),
            hint_steps=(),
            payload={"remote_url": str(entry.get("image_url") or "")},
        )

    def _build_skill_icon_hero_selection(self, type_spec: OWGuessTypeSpec, entry: Dict[str, Any]) -> OWGuessQuestionSelection:
        hero_name = _resolve_hero_name(entry.get("hero_name"))
        icon_path = self._pick_icon_from_pool(entry.get("pool_dir"))
        return OWGuessQuestionSelection(
            type_spec=type_spec,
            question_id=str(entry.get("question_id") or ""),
            difficulty=int(entry.get("difficulty") or 2),
            prompt_text="请尝试猜出技能图标对应的英雄",
            answer_canonical=hero_name,
            answer_aliases=tuple(_hero_aliases(hero_name)),
            hint_steps=(),
            payload={"local_path": str(icon_path)},
        )

    def _build_perk_icon_hero_selection(self, type_spec: OWGuessTypeSpec, entry: Dict[str, Any]) -> OWGuessQuestionSelection:
        hero_name = _resolve_hero_name(entry.get("hero_name"))
        icon_path = self._pick_icon_from_pool(entry.get("pool_dir"))
        return OWGuessQuestionSelection(
            type_spec=type_spec,
            question_id=str(entry.get("question_id") or ""),
            difficulty=int(entry.get("difficulty") or 3),
            prompt_text="请尝试猜出威能图标对应的英雄",
            answer_canonical=hero_name,
            answer_aliases=tuple(_hero_aliases(hero_name)),
            hint_steps=(),
            payload={"local_path": str(icon_path)},
        )

    def _build_map_image_selection(self, type_spec: OWGuessTypeSpec, entry: Dict[str, Any]) -> OWGuessQuestionSelection:
        map_name = str(entry.get("map_name") or entry.get("name") or "").strip()
        if not map_name:
            raise ModuleError(
                error="ow_guess_resources_unavailable",
                message="map_image question is missing map_name.",
                status_code=500,
                details={"question_id": entry.get("question_id")},
            )
        return OWGuessQuestionSelection(
            type_spec=type_spec,
            question_id=str(entry.get("question_id") or ""),
            difficulty=int(entry.get("difficulty") or 4),
            prompt_text="请尝试猜出此区域对应的地图",
            answer_canonical=map_name,
            answer_aliases=(map_name,),
            hint_steps=(),
            payload={"remote_url": str(entry.get("image_url") or "")},
        )

    def _build_ult_voice_selection(self, type_spec: OWGuessTypeSpec, entry: Dict[str, Any]) -> OWGuessQuestionSelection:
        hero_name = _resolve_hero_name(entry.get("hero_name"))
        local_path = self._resolve_asset_path("ult_voice", "assets", str(entry.get("audio_file") or "").strip())
        prompt_text = str(entry.get("prompt_text") or "").strip()
        if not prompt_text or set(prompt_text) == {"?"}:
            prompt_text = "请尝试猜出终极技能对应的英雄"
        return OWGuessQuestionSelection(
            type_spec=type_spec,
            question_id=str(entry.get("question_id") or ""),
            difficulty=int(entry.get("difficulty") or 1),
            prompt_text=prompt_text,
            answer_canonical=hero_name,
            answer_aliases=tuple(_hero_aliases(hero_name)),
            hint_steps=(),
            payload={"local_path": str(local_path)},
        )

    def _build_hero_silhouette_selection(self, type_spec: OWGuessTypeSpec, entry: Dict[str, Any]) -> OWGuessQuestionSelection:
        hero_name = _resolve_hero_name(entry.get("hero_name") or entry.get("name"))
        background_path = self._resolve_asset_path("hero_silhouette", "whois_bg.jpg")
        if not background_path.exists():
            raise ModuleError(
                error="ow_guess_type_unavailable",
                message=f"Question type requires the optional OW guess asset pack: {type_spec.slug}",
                status_code=400,
                details={
                    "question_type": type_spec.slug,
                    "reason": "local_asset_pack_missing",
                    "path": str(background_path),
                },
            )
        return OWGuessQuestionSelection(
            type_spec=type_spec,
            question_id=str(entry.get("question_id") or ""),
            difficulty=int(entry.get("difficulty") or 3),
            prompt_text="猜猜我是谁？",
            answer_canonical=hero_name,
            answer_aliases=tuple(_hero_aliases(hero_name)),
            hint_steps=(),
            payload={
                "remote_url": str(entry.get("image_url") or ""),
                "background_path": str(background_path),
            },
        )

    def _build_skill_icon_name_selection(self, type_spec: OWGuessTypeSpec, entry: Dict[str, Any]) -> OWGuessQuestionSelection:
        icon_path = self._pick_icon_from_pool(entry.get("pool_dir"))
        answer_name = icon_path.stem
        return OWGuessQuestionSelection(
            type_spec=type_spec,
            question_id=str(entry.get("question_id") or ""),
            difficulty=int(entry.get("difficulty") or 4),
            prompt_text="请尝试猜出技能图标的全称",
            answer_canonical=answer_name,
            answer_aliases=(answer_name,),
            hint_steps=(),
            payload={"local_path": str(icon_path)},
        )

    def _build_hero_description_selection(self, type_spec: OWGuessTypeSpec, entry: Dict[str, Any]) -> OWGuessQuestionSelection:
        hero_name = _resolve_hero_name(entry.get("hero_name"))
        hint_steps = self._build_hero_description_steps(entry, hero_name)
        return OWGuessQuestionSelection(
            type_spec=type_spec,
            question_id=str(entry.get("question_id") or ""),
            difficulty=int(entry.get("difficulty") or 1),
            prompt_text="请根据文字线索猜出对应的英雄",
            answer_canonical=hero_name,
            answer_aliases=tuple(_hero_aliases(hero_name)),
            hint_steps=tuple(hint_steps),
            payload={},
        )

    def _build_hero_description_steps(self, entry: Dict[str, Any], hero_name: str) -> List[Dict[str, Any]]:
        clue_pool: List[str] = []
        role_name = str(entry.get("role_name") or "").strip()
        if role_name:
            clue_pool.append(f"TA位于的职责是：{role_name}")
        location = str(entry.get("location") or "").strip()
        if location:
            clue_pool.append(f"TA出现过的地点是：{location}")
        birthday = str(entry.get("birthday") or "").strip()
        if birthday:
            clue_pool.append(f"TA的生日是：{birthday}")
        description = _strip_html_text(entry.get("description"))
        if description:
            clue_pool.append(f"TA的描述是：{description.replace(hero_name, 'TA')}")
        randomized = list(clue_pool)
        self.random.shuffle(randomized)

        skill_names = _unique_strings(entry.get("skill_names") or [])
        if skill_names:
            randomized.append(f"TA的其中一个技能是：{self.random.choice(skill_names)}")

        steps: List[Dict[str, Any]] = []
        for index, clue_text in enumerate(randomized, start=1):
            steps.append(
                {
                    "order": index,
                    "text": clue_text,
                    "recommended_delay_seconds": 10,
                }
            )
        return steps

    def _prepare_map_music_entries(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        available = []
        for entry in entries:
            audio_file = str(entry.get("audio_file") or "").strip()
            if not audio_file:
                continue
            if self._resolve_asset_path("map_music", "assets", audio_file).exists():
                available.append(entry)
        return self._require_local_entries("map_music", available, hint_path=self._resolve_asset_path("map_music", "assets"))

    def _prepare_ult_voice_entries(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        available = []
        for entry in entries:
            audio_file = str(entry.get("audio_file") or "").strip()
            if not audio_file:
                continue
            if self._resolve_asset_path("ult_voice", "assets", audio_file).exists():
                available.append(entry)
        return self._require_local_entries("ult_voice", available, hint_path=self._resolve_asset_path("ult_voice", "assets"))

    def _prepare_skill_icon_hero_entries(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        available = [entry for entry in entries if self._pool_has_icons(entry.get("pool_dir"))]
        return self._require_local_entries("skill_icon_hero", available, hint_path=self._resolve_asset_reference("../shared/hero_icons"))

    def _prepare_perk_icon_hero_entries(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        available = [entry for entry in entries if self._pool_has_icons(entry.get("pool_dir"))]
        return self._require_local_entries("perk_icon_hero", available, hint_path=self._resolve_asset_reference("../shared/hero_icons"))

    def _prepare_skill_icon_name_entries(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        available = [entry for entry in entries if self._pool_has_icons(entry.get("pool_dir"))]
        return self._require_local_entries("skill_icon_name", available, hint_path=self._resolve_asset_reference("../shared/hero_icons"))

    def _build_hero_icon_entries(self) -> List[Dict[str, Any]]:
        hero_list = self._load_query_tool_list("heroList", type_slug="hero_icon")
        entries: List[Dict[str, Any]] = []
        for item in hero_list:
            hero_name = _resolve_hero_name(item.get("name"))
            image_url = str(item.get("smallIconUrl") or item.get("icon") or "").strip()
            if not hero_name or not image_url:
                continue
            entries.append(
                {
                    "question_id": str(item.get("id") or hero_name),
                    "difficulty": 3,
                    "hero_name": hero_name,
                    "image_url": image_url,
                }
            )
        return self._require_dynamic_entries("hero_icon", entries, reason="query_tool_missing_hero_list")

    def _build_map_image_entries(self) -> List[Dict[str, Any]]:
        map_list = self._load_query_tool_list("mapList", type_slug="map_image")
        entries: List[Dict[str, Any]] = []
        for item in map_list:
            map_name = str(item.get("name") or "").strip()
            image_url = str(item.get("icon") or "").strip()
            if not map_name or not image_url:
                continue
            entries.append(
                {
                    "question_id": str(item.get("id") or map_name),
                    "difficulty": 4,
                    "map_name": map_name,
                    "image_url": image_url,
                }
            )
        return self._require_dynamic_entries("map_image", entries, reason="query_tool_missing_map_list")

    def _build_hero_silhouette_entries(self) -> List[Dict[str, Any]]:
        hero_list = self._load_query_tool_list("heroList", type_slug="hero_silhouette")
        entries: List[Dict[str, Any]] = []
        for item in hero_list:
            hero_name = _resolve_hero_name(item.get("name"))
            image_url = str(item.get("smallIconUrl") or item.get("icon") or "").strip()
            if not hero_name or not image_url:
                continue
            entries.append(
                {
                    "question_id": str(item.get("id") or hero_name),
                    "difficulty": 3,
                    "hero_name": hero_name,
                    "image_url": image_url,
                }
            )
        return self._require_dynamic_entries("hero_silhouette", entries, reason="query_tool_missing_hero_list")

    def _load_query_tool_payload(self) -> Dict[str, Any]:
        if self._query_tool_cache is not None:
            return dict(self._query_tool_cache)
        payload = read_query_tool(default={})
        self._query_tool_cache = payload if isinstance(payload, dict) else {}
        return dict(self._query_tool_cache)

    def _load_query_tool_list(self, section_name: str, *, type_slug: str) -> List[Dict[str, Any]]:
        payload = self._load_query_tool_payload()
        section = payload.get(section_name)
        if not isinstance(section, list):
            raise ModuleError(
                error="ow_guess_type_unavailable",
                message=f"Question type currently has no query-tool config data: {type_slug}",
                status_code=400,
                details={"question_type": type_slug, "reason": "query_tool_section_missing", "section": section_name},
            )
        return [dict(item) for item in section if isinstance(item, dict)]

    def _require_dynamic_entries(self, type_slug: str, entries: List[Dict[str, Any]], *, reason: str) -> List[Dict[str, Any]]:
        if entries:
            return entries
        raise ModuleError(
            error="ow_guess_type_unavailable",
            message=f"Question type currently has no available dynamic entries: {type_slug}",
            status_code=400,
            details={"question_type": type_slug, "reason": reason},
        )

    def _pick_icon_from_pool(self, pool_dir_value: Any) -> Path:
        pool_dir = self._resolve_asset_reference(pool_dir_value)
        if not pool_dir.exists():
            raise FileNotFoundError(pool_dir)
        candidates = [
            path
            for path in pool_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() in IMAGE_EXTENSIONS
            and not path.stem.startswith("职责：")
        ]
        if not candidates:
            raise FileNotFoundError(pool_dir)
        return Path(self.random.choice(candidates))

    def _pool_has_icons(self, pool_dir_value: Any) -> bool:
        pool_dir = self._resolve_asset_reference(pool_dir_value)
        if not pool_dir.exists():
            return False
        for path in pool_dir.iterdir():
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS and not path.stem.startswith("职责："):
                return True
        return False

    def _require_local_entries(self, type_slug: str, entries: List[Dict[str, Any]], *, hint_path: Path) -> List[Dict[str, Any]]:
        if entries:
            return entries
        raise ModuleError(
            error="ow_guess_type_unavailable",
            message=f"Question type requires the optional OW guess asset pack: {type_slug}",
            status_code=400,
            details={
                "question_type": type_slug,
                "reason": "local_asset_pack_missing",
                "path": str(hint_path),
            },
        )

    def _resolve_asset_reference(self, relative_value: Any) -> Path:
        raw = str(relative_value or "").strip().replace("\\", "/")
        if not raw:
            return self.resource_root
        normalized = raw
        while normalized.startswith("../"):
            normalized = normalized[3:]
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            return self.resource_root
        return self.resource_root.joinpath(*[part for part in normalized.split("/") if part])

    def _resolve_asset_path(self, *parts: str) -> Path:
        clean_parts = [str(part).strip() for part in parts if str(part).strip()]
        return self.resource_root.joinpath(*clean_parts)

    async def _cache_remote_asset(self, url: str, type_slug: str) -> Path:
        normalized = str(url or "").strip()
        if not normalized:
            raise FileNotFoundError(url)
        asset_dir = self._resolve_asset_path(type_slug, "assets")
        asset_dir.mkdir(parents=True, exist_ok=True)
        stem = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        cached = next((path for path in asset_dir.glob(f"{stem}.*") if path.is_file()), None)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(headers=REMOTE_IMAGE_HEADERS, timeout=REMOTE_IMAGE_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(normalized)
            response.raise_for_status()
            content = response.content
            suffix = _guess_remote_extension(normalized, response.headers.get("Content-Type", ""))

        fd, temp_path = tempfile.mkstemp(prefix=f"{type_slug}.", suffix=".tmp", dir=str(asset_dir))
        target_path = asset_dir / f"{stem}{suffix}"
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(content)
            Path(temp_path).replace(target_path)
        except Exception:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return target_path
