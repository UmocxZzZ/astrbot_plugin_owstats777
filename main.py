
from __future__ import annotations

import base64
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star


class OWStatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.overstats_url: str = config.get("overstats_url", "http://127.0.0.1:18080").rstrip("/")
        self.default_bnet_id: str = config.get("default_bnet_id", "")
        self.default_timeout: int = config.get("default_timeout", 30)
        self.summary_timeout: int = config.get("summary_timeout", 90)
        self.ai_timeout: int = config.get("ai_timeout", 180)
        self.ai_whitelist: list = config.get("ai_whitelist", [])
        self.ai_cooldown: int = config.get("ai_cooldown_seconds", 300)
        self.embedded_overstats: bool = config.get("embedded_overstats", True)
        self.overstats_port: int = config.get("overstats_port", 18080)
        self.client = httpx.AsyncClient(timeout=self.default_timeout)
        self._overstats_server = None
        self._overstats_thread = None

        # 启动内置 Overstats 服务
        if self.embedded_overstats:
            self._start_embedded_overstats()

    def _start_embedded_overstats(self):
        """启动内置的 Overstats HTTP 服务"""
        try:
            # 添加 Overstats 目录到 Python 路径
            overstats_dir = str(Path(__file__).parent / "Overstats")
            if overstats_dir not in sys.path:
                sys.path.insert(0, overstats_dir)

            from config import get_api_config
            from src import create_server

            config = get_api_config()
            config.port = self.overstats_port
            self._overstats_server = create_server(config)

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
            logger.info("将使用外部 Overstats 服务")

    async def terminate(self):
        """插件卸载时停止服务"""
        await self.client.aclose()

        # 停止内置 Overstats 服务
        if self._overstats_server:
            try:
                self._overstats_server.shutdown()
                logger.info("Overstats 内置服务已停止")
            except Exception as e:
                logger.error(f"停止 Overstats 服务失败: {e}")

    # ======================== 工具方法 ========================

    def _get_user_key(self, event: AstrMessageEvent) -> str:
        """获取平台用户唯一标识，如 aiocqhttp:123456789"""
        return f"{event.get_platform_name()}:{event.get_sender_id()}"

    async def _resolve_bnet_id(self, event: AstrMessageEvent, arg: Optional[str] = None) -> str:
        """解析玩家 ID：命令参数 > 用户绑定 > 默认值"""
        if arg:
            return arg
        # 查用户绑定
        bind_key = f"bind:{self._get_user_key(event)}"
        bound = await self.get_kv_data(bind_key, "")
        if bound:
            return bound
        if self.default_bnet_id:
            return self.default_bnet_id
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

    async def _call_overstats(self, endpoint: str, payload: dict, timeout: Optional[int] = None) -> dict:
        """调用 Overstats API"""
        url = f"{self.overstats_url}{endpoint}"
        resp = await self.client.post(url, json=payload, timeout=timeout or self.default_timeout)
        resp.raise_for_status()
        return resp.json()

    async def _call_overstats_image(self, endpoint: str, payload: dict, timeout: Optional[int] = None) -> bytes:
        """调用 Overstats 图片 API，返回 PNG 二进制"""
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
        if isinstance(exc, httpx.TimeoutException):
            yield event.plain_result("请求超时，Overstats 服务繁忙，请稍后重试。")
        elif isinstance(exc, httpx.ConnectError):
            yield event.plain_result("无法连接 Overstats 服务，请确认服务已启动。")
        elif isinstance(exc, httpx.HTTPStatusError):
            try:
                err = exc.response.json()
                msg = err.get("message", str(exc))
                hint = err.get("hint", "")
                yield event.plain_result(f"请求失败：{msg}\n{hint}" if hint else f"请求失败：{msg}")
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
            help_text = """OWStats 命令帮助

基础查询：
  ow 资料 [玩家] - 玩家资料图
  ow 战绩 [玩家] - 近期战绩图
  ow 详情 [玩家] [序号] - 单场详情图
  ow 详情 [玩家] [序号] 锐评 - 详情 + AI 锐评
  ow 开庭 [玩家] [序号] - AI 开庭（审判视角）
  ow 段位 [玩家] - 段位历史图
  ow 今日/昨日/周 [玩家] - 总结图
  ow 强度 [玩家] - 快速强度分析
  ow 竞技强度 [玩家] - 竞技强度分析
  ow 同玩 [玩家1] [玩家2] - 同玩查询

排行榜：
  ow 排行 省榜 [省] [职责] - 省榜排名
  ow 排行 英雄 [省] [英雄] - 英雄榜单
  ow 排行 选取率 [模式] - 英雄选取率

绑定管理：
  ow 绑定 [BattleTag] - 绑定你的 BattleTag
  ow 解绑 - 解除绑定
  ow 我的 - 查看绑定信息

其他：
  ow 商店 - 当前商店商品
  ow 赛事 - OWCS 赛事信息
  ow 补丁 - 最新补丁说明

提示：已绑定用户可省略玩家参数，如 ow 资料"""
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

        if analyze:
            ok, remaining = await self._check_ai_cooldown(event)
            if not ok:
                yield event.plain_result(f"AI 锐评冷却中，请 {remaining} 秒后再试。")
                return

        try:
            payload = {"bnet_id": bnet_id, "index": index, "show_all_heroes": True}
            if analyze:
                payload["analyze"] = True
                result = await self._call_overstats("/api/v2/dashen-match/detail/replies", payload, timeout=self.ai_timeout)
                async for r in self._handle_image_response(event, result):
                    yield r
                await self._set_ai_cooldown(event)
            else:
                result = await self._call_overstats("/api/v2/dashen-match/detail/replies", payload)
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

        ok, remaining = await self._check_ai_cooldown(event)
        if not ok:
            yield event.plain_result(f"AI 开庭冷却中，请 {remaining} 秒后再试。")
            return

        try:
            result = await self._call_overstats("/api/v2/dashen-match/detail/court", {
                "bnet_id": bnet_id,
                "index": index,
            }, timeout=self.ai_timeout)
            async for r in self._handle_image_response(event, result):
                yield r
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

    async def _cmd_leaderboard(self, event: AstrMessageEvent, subcmd: str, arg1: str, arg2: str):
        if subcmd == "省榜":
            province = arg1 or "北京"
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
            province = arg1 or "北京"
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
            mode = arg1 or "competitive"
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
        try:
            img = await self._call_overstats_image("/api/v2/ow-shop/image", {})
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
        try:
            img = await self._call_overstats_image("/api/v2/patch-notes/image", {})
            path = await self._save_temp_image(img)
            yield event.image_result(path)
        except Exception as exc:
            async for r in self._handle_overstats_error(event, exc):
                yield r
