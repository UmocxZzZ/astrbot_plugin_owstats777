from __future__ import annotations

# ======================= Core Service ====================== #
API_HOST = "127.0.0.1"
API_PORT = 18080
USE_STREAM_RESPONSE = True
ENABLE_DATABASE_WRITE = True

# ======================= Dashen Upstream ====================== #
# Configure at least one account.
DASHEN_ACCOUNTS = [
    {
        "name": "account-1",
        "role_id": 123456789,
        "token": "replace-with-your-token",
    },
]

DASHEN_DTS = 2026
DASHEN_SERVER = 1
DASHEN_ACCOUNT_MAX_REQUESTS_PER_SECOND = 5
DASHEN_ACCOUNT_RATE_LIMIT_WINDOW_SECONDS = 1.0
DASHEN_CLIENT_TYPE = "60"
DASHEN_ORIGIN = "https://act.ds.163.com"
DASHEN_REFERER = "https://act.ds.163.com/"
DASHEN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36 "
    "app/df_client dfVersion/100111"
)
DASHEN_ACCOUNT_FAILURE_COOLDOWN_SECONDS = 60
DASHEN_MAX_CONCURRENT_REQUESTS = 2
DASHEN_MAX_ACCEPTED_REQUESTS = max(len(DASHEN_ACCOUNTS) * 4, 1)

# Optional proxy settings.
DASHEN_INTERNATIONAL_PROXY = ""
DASHEN_NETEASE_PROXIES = [None]

# OW esports PandaScore API key.
OW_ESPORTS_API_KEY = ""

# Optional external OW guess asset pack root.
OW_GUESS_ASSET_ROOT = "ow_guess_assets"

# ======================= Dashen Season ====================== #
# Effective Dashen season = max(DASHEN_CURRENT_SEASON, max(AIEvaluateConfig[*].seasonIdList)).
DASHEN_CURRENT_SEASON = 23
DASHEN_HISTORY_START_SEASON = 15

# ======================= OW Hero Leaderboard ====================== #
OW_HERO_LEADERBOARD_CN_SEASON = 3

# ======================= Match Analysis ====================== #
# OpenAI-compatible base URL
ANALYSIS_BASE_URL = ""
ANALYSIS_API_KEY = ""
ANALYSIS_PROXY = ""
ANALYSIS_OPENAI_MODEL = ""

# Optional external patch-note fetch proxy.
PATCH_NOTES_USE_INTERNATIONAL_PROXY = False
PATCH_NOTES_INTERNATIONAL_PROXY = ""

# Only put AI persona/tone here.
ANALYSIS_PERSONA_PROMPT = """
【核心原则】
请保持绝对客观中立，拒绝阿谀奉承！不要因为查询指令的是焦点玩家就一味夸奖，如果焦点玩家表现平庸或拉垮请直接批评。

【人格设定】
你的说话人设是科比·布莱恩特，风格包含 [man! what can i say，mamba out] 等 meme。
""".strip()

# AI 开庭：绝对中立的审判视角，聚焦 MVP 与最差玩家
ANALYSIS_PERSONA_COURT = """
【核心原则】
你是绝对中立的法官，不偏袒任何一方。数据就是证据，表现就是判决依据。好的要表扬，差的必须严厉批判。

【人格设定】
你是电竞法庭的主审法官，风格严肃、犀利、不留情面。你的判决必须基于数据事实，不允许任何主观臆测。
""".strip()
