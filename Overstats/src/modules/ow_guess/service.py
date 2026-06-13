from __future__ import annotations

import base64
from dataclasses import dataclass
import datetime as dt
import mimetypes
from pathlib import Path
import random
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import httpx

from .catalog import OWGuessCatalog, OWGuessQuestionSelection
from .render import RenderedImage, render_guess_image

try:
    from overstats.src.modules.errors import ModuleError
except ModuleNotFoundError:
    from src.modules.errors import ModuleError


def _text_reply(text: str) -> Dict[str, Any]:
    return {"type": "text", "data": str(text or "")}


def _image_reply(rendered: RenderedImage) -> Dict[str, Any]:
    return {
        "type": "image",
        "media_type": rendered.media_type,
        "base64": base64.b64encode(rendered.content).decode("ascii"),
    }


def _audio_reply(content: bytes, media_type: str) -> Dict[str, Any]:
    return {
        "type": "audio",
        "media_type": media_type,
        "base64": base64.b64encode(content).decode("ascii"),
    }


@dataclass(frozen=True)
class OWGuessQuery:
    question_type: str = ""


@dataclass(frozen=True)
class OWGuessOutput:
    generated_at: str
    question_type: str
    question_type_id: int
    question_type_label: str
    question_id: str
    difficulty: int
    recommended_wait_seconds: int
    question: Mapping[str, Any]
    answer: Mapping[str, Any]
    replies: Sequence[Mapping[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "generated_at": self.generated_at,
            "question_type": self.question_type,
            "question_type_id": int(self.question_type_id),
            "question_type_label": self.question_type_label,
            "question_id": self.question_id,
            "difficulty": int(self.difficulty),
            "recommended_wait_seconds": int(self.recommended_wait_seconds),
            "question": {
                "prompt_text": str(self.question.get("prompt_text") or ""),
                "media_kind": str(self.question.get("media_kind") or ""),
                "hint_steps": list(self.question.get("hint_steps") or []),
            },
            "answer": {
                "canonical": str(self.answer.get("canonical") or ""),
                "aliases": [str(item) for item in list(self.answer.get("aliases") or [])],
            },
            "replies": [dict(reply) for reply in self.replies],
        }


class OWGuessModule:
    def __init__(
        self,
        *,
        catalog: OWGuessCatalog | None = None,
        image_renderer: Optional[Callable[[Mapping[str, Any], Path], RenderedImage]] = None,
        time_provider: Optional[Callable[[], dt.datetime]] = None,
        random_source: random.Random | None = None,
    ) -> None:
        self.random = random_source or random.Random()
        self.catalog = catalog or OWGuessCatalog(random_source=self.random)
        self.image_renderer = image_renderer or (lambda selection, media_path: render_guess_image(selection, media_path, rng=self.random))
        self.time_provider = time_provider or dt.datetime.now

    async def query_guess_replies(self, query: OWGuessQuery) -> OWGuessOutput:
        type_spec = self.catalog.resolve_question_type(query.question_type)
        last_exc: Optional[BaseException] = None

        for _ in range(3):
            selection = self.catalog.pick_question(type_spec)
            try:
                replies = await self._build_replies(selection)
            except ModuleError as exc:
                if exc.error == "render_dependency_missing":
                    raise
                last_exc = exc
                continue
            except (FileNotFoundError, OSError, httpx.HTTPError) as exc:
                last_exc = exc
                continue

            return OWGuessOutput(
                generated_at=self._format_generated_at(self.time_provider()),
                question_type=type_spec.slug,
                question_type_id=type_spec.type_id,
                question_type_label=type_spec.label,
                question_id=selection.question_id,
                difficulty=int(selection.difficulty),
                recommended_wait_seconds=int(type_spec.recommended_wait_seconds),
                question={
                    "prompt_text": selection.prompt_text,
                    "media_kind": type_spec.media_kind,
                    "hint_steps": list(selection.hint_steps),
                },
                answer={
                    "canonical": selection.answer_canonical,
                    "aliases": list(selection.answer_aliases),
                },
                replies=replies,
            )

        raise ModuleError(
            error="ow_guess_resources_unavailable",
            message=f"Question resources are temporarily unavailable for type: {type_spec.slug}",
            status_code=503,
            details={
                "question_type": type_spec.slug,
                "exception": type(last_exc).__name__ if last_exc else "",
                "message": str(last_exc) if last_exc else "",
            },
        )

    async def _build_replies(self, selection: OWGuessQuestionSelection) -> List[Dict[str, Any]]:
        type_spec = selection.type_spec
        replies: List[Dict[str, Any]] = [_text_reply(selection.prompt_text)]
        if type_spec.media_kind == "text":
            return replies

        media_path = await self.catalog.resolve_media_path(selection)
        if media_path is None:
            raise FileNotFoundError(selection.question_id)

        if type_spec.media_kind == "audio":
            content = media_path.read_bytes()
            media_type = mimetypes.guess_type(media_path.name)[0] or "application/octet-stream"
            replies.append(_audio_reply(content, media_type))
            return replies

        if type_spec.media_kind != "image":
            return replies

        selection_payload = dict(selection.payload)
        selection_payload["question_type"] = type_spec.slug
        try:
            rendered = self.image_renderer(selection_payload, media_path)
        except RuntimeError as exc:
            raise ModuleError(
                error="render_dependency_missing",
                message=str(exc),
                status_code=500,
                hint="Install Pillow in the runtime environment to enable OW guess image rendering.",
            ) from exc
        replies.append(_image_reply(rendered))
        return replies

    def _format_generated_at(self, value: dt.datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")


ow_guess_module = OWGuessModule()
