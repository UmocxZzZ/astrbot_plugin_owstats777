"""Offline regressions for the selected Overstats upstream sync.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from io import BytesIO
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ["OVERSTATS_ENABLE_DATABASE_WRITE"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Overstats"))

from PIL import Image
from src.modules import season_config as seasons
from src.modules.dashen_match import render as match_render
from src.modules.dashen_match import service as match_service
from src.modules.dashen_match.requests import DashenMatchDetail, DashenMatchQuery
from src.modules.dashen_quick_strength import render as strength_render
from src.modules.dashen_quick_strength.engine import DashenQuickStrengthEngine
from src.modules.dashen_quick_strength.requests import DashenQuickStrengthRequests, DashenQuickStrengthQuery
from src.modules.dashen_quick_strength.service import DashenQuickStrengthModule
from src.modules.dashen_competitive_strength.engine import DashenCompetitiveStrengthEngine
from src.modules.dashen_competitive_strength.requests import DashenCompetitiveStrengthQuery
from src.modules.dashen_competitive_strength.service import DashenCompetitiveStrengthModule
from src.modules.dashen_rank_history import render as history_render
from src.modules.query_tool import service as query_tool_service


def setUpModule():
    global _patches
    _patches = ExitStack()
    _patches.enter_context(patch.object(query_tool_service, "_CONFIG_CACHE", {}))
    _patches.enter_context(patch("httpx.AsyncClient.request", side_effect=AssertionError("tests must stay offline")))
    _patches.enter_context(patch("httpx.Client.request", side_effect=AssertionError("tests must stay offline")))


def tearDownModule():
    _patches.close()


class SeasonTests(unittest.TestCase):
    def test_current_season_uses_manual_and_auto_max(self):
        self.assertEqual(seasons.get_dashen_current_season({}), 25)
        self.assertEqual(seasons.get_dashen_current_season({
            "AIEvaluateConfig": [None, {"seasonIdList": ["bad", 24, "26"]}],
        }), 26)

    def test_rollover_uses_current_mapping_entry_not_last_entry(self):
        config = {"seasonList": {
            "25": {"startTime": "2026.10.6"},
            "24": {"startTime": "2026.8.1"},
        }}
        self.assertEqual(seasons.get_dashen_season_rollover_at(config), seasons.dt.datetime(2026, 10, 6))

    def test_rollover_falls_back_to_previous_end(self):
        config = {"seasonList": {"24": {"endTime": "2026/10/6"}}}
        self.assertEqual(seasons.get_dashen_season_rollover_at(config), seasons.dt.datetime(2026, 10, 6))

    def test_missing_rollover_is_not_a_fabricated_date(self):
        self.assertIsNone(seasons.get_dashen_season_rollover_at({}))
        self.assertIsNone(seasons.get_dashen_season_rollover_at({"seasonList": {"24": {"startTime": "2026.8.1"}}}))

    def test_date_formats(self):
        for value in ("2026.10.6", "2026/10/6", "2026-10-06", "2026-10-06T00:00:00"):
            with self.subTest(value=value):
                self.assertEqual(seasons.parse_dashen_season_time(value), seasons.dt.datetime(2026, 10, 6))
        self.assertEqual(seasons.parse_dashen_season_time("2026.10"), seasons.dt.datetime(2026, 10, 1))
        self.assertIsNone(seasons.parse_dashen_season_time("2026.13.40"))

    def test_normalize_preserves_mapping_and_input(self):
        config = {"seasonList": {"24": {"endTime": "2026.10.6"}}}
        normalized = seasons.normalize_query_tool_config(config)
        self.assertNotIn("25", config["seasonList"])
        self.assertEqual(normalized["seasonList"]["25"]["startTime"], "2026.10.6")
        self.assertEqual(normalized["seasonList"]["23"]["startTime"], "")


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_pages_stop_and_deduplicate(self):
        api = SimpleNamespace(query_match_list=AsyncMock(return_value={
            "code": 0, "data": {"matchList": [{"matchId": "one", "beginTs": 10}]},
        }))
        result = await asyncio.wait_for(DashenQuickStrengthRequests(api).list_recent_quick_matches(
            "test-token", limit=12, include_previous_season=False,
        ), timeout=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(api.query_match_list.await_count, 2)

    async def test_batch_duplicates_and_previous_season_fill(self):
        async def fetch(_token, _mode, *, page, season):
            match_id = "current" if season is None else "previous"
            return {"code": 0, "data": {"matchList": [{"matchId": match_id, "beginTs": 10}]}}
        api = SimpleNamespace(query_match_list=AsyncMock(side_effect=fetch))
        result = await asyncio.wait_for(DashenQuickStrengthRequests(api).list_recent_quick_matches(
            "test-token", limit=12, include_previous_season=True, pages_per_batch=2,
        ), timeout=1)
        self.assertEqual({row["matchId"] for row in result}, {"current", "previous"})
        self.assertEqual({row["_dashenSeason"] for row in result}, {24, 25})
        self.assertEqual(api.query_match_list.await_count, 8)

    async def test_explicit_current_season_fallback(self):
        async def fetch(_token, _mode, *, page, season):
            entries = [{"matchId": "one"}] if season == 25 else []
            return {"code": 0, "data": {"matchList": entries}}
        api = SimpleNamespace(query_match_list=AsyncMock(side_effect=fetch))
        result = await DashenQuickStrengthRequests(api).list_recent_quick_matches(
            "test-token", limit=1, include_previous_season=False,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(api.query_match_list.await_count, 2)


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_resolves_card_and_preserves_query_options(self):
        module = match_service.DashenMatchModule()
        module._fetch_cached_player_card = AsyncMock(return_value={"data": {"name": "测试#1234", "bnetId": "42"}})
        query = DashenMatchQuery(customer_token=" test-token ", seasons=[24], target_count=7, include_fight=False)
        resolved_query, resolved = await module._resolve_query(query)
        self.assertEqual(resolved_query.customer_token, "test-token")
        self.assertEqual(resolved_query.bnet_id, "测试#1234")
        self.assertEqual(resolved_query.seasons, [24])
        self.assertEqual(resolved_query.target_count, 7)
        self.assertFalse(resolved_query.include_fight)
        self.assertNotIn("customer_token", match_service._resolved_payload(resolved))

    async def test_failed_card_never_uses_token_as_display_label(self):
        module = match_service.DashenMatchModule()
        module._fetch_cached_player_card = AsyncMock(side_effect=RuntimeError("offline"))
        query, resolved = await module._resolve_query(DashenMatchQuery(customer_token="test-token", bnet_id="token:test-token"))
        self.assertEqual(module._display_identity(query, resolved), "未知玩家")

    async def test_strength_reuses_card_for_identity_and_profile(self):
        for module_type, query_type in (
            (DashenQuickStrengthModule, DashenQuickStrengthQuery),
            (DashenCompetitiveStrengthModule, DashenCompetitiveStrengthQuery),
        ):
            with self.subTest(module=module_type.__name__):
                api = SimpleNamespace(query_card=AsyncMock(return_value={"data": {"name": "测试#1234", "bnetId": "42"}}))
                module = module_type(api_client=api)
                query, identity = await module._resolve_query(query_type(customer_token=" test-token "))
                self.assertEqual(query.bnet_id, "测试#1234")
                await module._try_fetch_profile_assets(identity, query.customer_token)
                api.query_card.assert_awaited_once_with("test-token")

    async def test_court_direct_match_uses_customer_identity(self):
        module = match_service.DashenMatchModule()
        module._fetch_cached_player_card = AsyncMock(return_value={"data": {"name": "测试#1234", "bnetId": "42"}})
        module._get_match_detail_direct = AsyncMock(return_value=DashenMatchDetail(
            match_id="match", match_kind="fight", payload={"data": {}}, source_match={},
        ))
        rendered = match_render.RenderedImage(content=b"test-image")
        with patch.object(match_service, "render_match_detail", return_value=rendered) as render, patch.object(
            match_service, "decorate_rendered_image_header", side_effect=lambda image, *args, **kwargs: image,
        ):
            await module.query_match_detail_court(query=DashenMatchQuery(), customer_token="test-token", match_id="match")
        self.assertEqual(render.call_args.kwargs["query_customer_token"], "test-token")
        self.assertEqual(render.call_args.kwargs["query_full_id"], "测试#1234")

    def test_strong_identity_wins_over_earlier_same_name(self):
        module = match_service.DashenMatchModule()
        target = {"name": "测试#2222", "customerToken": "target"}
        detail = {"teammateList": [{"name": "测试"}, target]}
        self.assertEqual(module._find_focus_player(detail, query_full_id="测试", query_bnet_id="", query_customer_token="target"), target)

    def test_conflicting_token_or_full_battletag_does_not_highlight(self):
        for player in ({"name": "测试#2222", "customerToken": "other"}, {"name": "测试#1111"}):
            self.assertFalse(match_render._is_query_player(player, query_full_id="测试#2222", query_bnet_id="", query_customer_token="target"))

    def test_battletag_fallback_when_card_id_is_unavailable(self):
        self.assertTrue(match_render._is_query_player(
            {"name": "测试#2222", "bnetId": "42"}, query_full_id="测试#2222", query_bnet_id="测试#2222",
        ))


class StrengthEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_compatible_percentile_fields_without_database_work(self):
        for engine_type, request_method in (
            (DashenQuickStrengthEngine, "list_recent_quick_matches"),
            (DashenCompetitiveStrengthEngine, "list_recent_competitive_matches"),
        ):
            with self.subTest(engine=engine_type.__name__):
                requests = SimpleNamespace(**{request_method: AsyncMock(return_value=[{"matchId": "match"}])})
                engine = engine_type(requests)
                engine._build_match_point = AsyncMock(return_value={"avg_score": 2500})
                with patch("asyncio.to_thread", side_effect=AssertionError("unexpected population database work")):
                    result = await engine.build(customer_token="test-token", limit=12, include_previous_season=True, config={})
                self.assertEqual(result["summary"]["match_count"], 1)
                self.assertEqual(result["summary"]["overall_avg_score"], 2500)
                self.assertIsNone(result["summary"]["personal_data_exceeded_percent"])
                self.assertEqual(result["summary"]["personal_data_metric_count"], 0)


class RenderTests(unittest.TestCase):
    def test_distribution_counts_both_teams_and_skips_unranked(self):
        counts = strength_render._rank_distribution_counts([
            {"team_scores": [500, 500, 2500, None], "enemy_scores": [0, "bad", 4900]},
        ])
        self.assertEqual(sum(counts), 4)
        self.assertEqual(counts[0], 2)
        self.assertEqual(counts[20], 1)
        self.assertEqual(counts[-1], 1)

    def test_strength_render_dimensions(self):
        with patch.object(strength_render, "_draw_background"):
            rendered = strength_render.render_quick_strength(player_name="测试#1234", bnet_id="42", summary={}, matches=[])
        self.assertEqual(Image.open(BytesIO(rendered.content)).size, (1500, 1076))

    def test_history_empty_and_ranked_cards_render(self):
        rows = [{"roleType": role, "lastRankInfo": {}, "maxRankInfo": {"rankName": "Emerald", "rankSubTier": 3}, "matchSum": 12, "winRate": 50}
                for role in ("tank", "dps", "support", "open")]
        fixtures = [{"season": 25}, {"season": 24, "sport_payload": {"data": {"guideCountData": rows}}, "fight_payload": {"data": {"roleTypeCountData": rows}}}]
        for fixture in fixtures:
            with self.subTest(season=fixture["season"]):
                card = history_render._render_season_card(fixture, {"seasonList": {"25": {"desc": "测试赛季"}}})
                self.assertEqual(card.size, (500, 1000))

    def test_compressed_season_banner_is_supported(self):
        # Existing compressed assets must remain usable after the upstream layout update.
        banners = list(history_render.SEASON_LOGO_DIR.glob("s*.jpg"))
        self.assertTrue(banners)
        season = int(banners[0].stem[1:])
        self.assertEqual(history_render._find_season_banner_path(season), banners[0])
        self.assertNotIn(f"overstats/res/season_logo/s{season}.png", history_render.collect_missing_assets([{"season": season}]))


if __name__ == "__main__":
    unittest.main()
