
from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star


class OWStatsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.overstats_url: str = config.get("overstats_url", "http://127.0.0.1:18080").rstrip("/")
        self.default_timeout: int = config.get("default_timeout", 60)
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

            # 注入插件配置的大神账号
            dashen_role_id = self.config.get("dashen_role_id", "")
            dashen_token = self.config.get("dashen_token", "")
            logger.info(f"大神配置: role_id={dashen_role_id}, token={'***' if dashen_token else '未配置'}")
            if dashen_role_id and dashen_token:
                # 直接修改 Overstats 配置文件，确保所有导入路径都能读到
                config_file = Path(overstats_dir) / "config" / "config.py"
                config_content = config_file.read_text(encoding="utf-8")
                # 替换 DASHEN_ACCOUNTS
                import re
                new_accounts = f'''DASHEN_ACCOUNTS = [
    {{
        "name": "plugin-account",
        "role_id": {dashen_role_id},
        "token": "{dashen_token}",
    }},
]'''
                config_content = re.sub(
                    r'DASHEN_ACCOUNTS\s*=\s*\[.*?\]',
                    new_accounts,
                    config_content,
                    flags=re.DOTALL
                )
                config_file.write_text(config_content, encoding="utf-8")
                logger.info("已注入大神账号配置到配置文件")
            else:
                logger.warning("请在 Astrbot WebUI 插件配置中填写 dashen_role_id 和 dashen_token，否则查询功能将无法使用")

            # 清除模块缓存，确保读取到修改后的配置文件
            for mod_name in list(sys.modules.keys()):
                if mod_name.startswith("config") or mod_name.startswith("overstats"):
                    del sys.modules[mod_name]

            # 导入 config.config 模块（不是 config 包）
            from config import config as overstats_config

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

    async def terminate(self):
        """插件卸载时停止服务"""
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
