# astrbot_plugin_owstats777

Astrbot 插件，封装 Overstats 本地 API，为 QQ/Telegram 等平台提供守望先锋数据查询功能。

## 架构

```
astrbot_plugin_owstats777/
  main.py                  # 插件主入口，注册所有命令
  _conf_schema.json        # 配置项 schema（Overstats 地址、默认玩家等）
  metadata.yaml            # 插件元数据
  requirements.txt         # 依赖：httpx
  Overstats/               # Overstats 服务（独立运行，非插件代码）
```

插件本身 **不包含** Overstats 业务逻辑，只作为 HTTP 客户端调用本地运行的 Overstats 服务。

## 命令设计

### 基础查询（`ow` 命令组）

玩家参数 `[玩家]` 为可选，已绑定用户可直接使用 `ow 资料` 等命令查询自己。

| 命令 | 说明 | 示例 |
|------|------|------|
| `ow 资料 [玩家]` | 玩家资料图 | `ow 资料 Gulee#5667` 或 `ow 资料` |
| `ow 战绩 [玩家]` | 近期战绩图 | `ow 战绩 Gulee#5667` 或 `ow 战绩` |
| `ow 详情 [玩家] [序号]` | 单场详情图 | `ow 详情 Gulee#5667 1` 或 `ow 详情 1` |
| `ow 详情 [玩家] [序号] 锐评` | 单场详情 + AI 锐评（可选） | `ow 详情 Gulee#5667 1 锐评` |
| `ow 开庭 [玩家] [序号]` | 单场 AI 开庭（审判视角） | `ow 开庭 Gulee#5667 1` 或 `ow 开庭 1` |
| `ow 段位 [玩家]` | 段位历史图 | `ow 段位 Gulee#5667` 或 `ow 段位` |
| `ow 今日 [玩家]` | 今日总结图 | `ow 今日 Gulee#5667` 或 `ow 今日` |
| `ow 昨日 [玩家]` | 昨日总结图 | `ow 昨日 Gulee#5667` 或 `ow 昨日` |
| `ow 周 [玩家]` | 本周总结图 | `ow 周 Gulee#5667` 或 `ow 周` |
| `ow 强度 [玩家]` | 快速强度分析图 | `ow 强度 Gulee#5667` 或 `ow 强度` |
| `ow 竞技强度 [玩家]` | 竞技强度分析图 | `ow 竞技强度 Gulee#5667` 或 `ow 竞技强度` |
| `ow 同玩 [玩家1] [玩家2]` | 同玩查询 | `ow 同玩 Alpha#1111 Bravo#2222` |

### 排行榜（`ow 排行` 子命令组）

| 命令 | 说明 | 示例 |
|------|------|------|
| `ow 排行 省榜 [省] [职责]` | 省榜排名 | `ow 排行 省榜 北京 坦克` |
| `ow 排行 英雄 [省] [英雄]` | 英雄榜单 | `ow 排行 英雄 北京 猎空` |
| `ow 排行 选取率 [模式]` | 英雄选取率 | `ow 排行 选取率 竞技` |

### 绑定管理

| 命令 | 说明 | 示例 |
|------|------|------|
| `ow 绑定 [BattleTag]` | 绑定 BattleTag 到当前用户 | `ow 绑定 Gulee#5667` |
| `ow 解绑` | 解除绑定 | `ow 解绑` |
| `ow 我的` | 查看当前绑定信息 | `ow 我的` |
| `ow 查绑 [用户]` | 查看指定用户的绑定（管理员） | `ow 查绑 @某人` |

### 其他功能

| 命令 | 说明 | 示例 |
|------|------|------|
| `ow 商店` | 当前商店商品 | `ow 商店` |
| `ow 赛事` | OWCS 赛事信息 | `ow 赛事` |
| `ow 补丁` | 最新补丁说明 | `ow 补丁` |
| `ow help` 或 `ow 帮助` | 命令帮助 | `ow help` |

**绑定逻辑**：
- 使用 Astrbot KV 存储，key 格式：`bind:{platform}:{user_id}`，value 为 BattleTag
- 绑定后，所有查询命令省略玩家参数时自动使用绑定 ID
- 优先级：命令参数 > 用户绑定 > 插件配置默认值
- 解绑后恢复使用插件配置的 `default_bnet_id`（如有）
- BattleTag 格式校验：必须包含 `#`，如 `Player#12345`
- 支持管理员查看他人绑定，普通用户只能操作自己的绑定

**管理员功能**：
- `ow 查绑 @某人`：查看指定用户的绑定 BattleTag（需 ADMIN 权限）

### AI 锐评使用限制

AI 锐评（`ow 详情 ... 锐评`）和 AI 开庭（`ow 开庭`）受使用频率限制：

| 用户类型 | 限制 |
|----------|------|
| 普通用户 | 每 5 分钟最多 1 次 |
| 白名单用户 | 无限制 |

**实现方式**：
- 使用 KV 存储记录用户上次使用时间，key：`ai_cooldown:{platform}:{user_id}`
- 普通用户调用时检查时间间隔，不足 5 分钟返回提示
- 白名单用户跳过检查
- 白名单通过配置项 `ai_whitelist` 设置（用户 ID 列表）
- AI 开庭与 AI 锐评共享同一冷却计时器

## Overstats API 端点映射

所有调用优先使用 `/replies` 端点（返回结构化回复 + 图片 base64），无 `/replies` 时用 `/image`，最后回退到 JSON。

| 插件功能 | Overstats 端点 | 超时建议 |
|----------|---------------|---------|
| 资料 | `POST /api/v2/dashen-profile/image` | 30s |
| 战绩 | `POST /api/v2/dashen-match/replies` | 30s |
| 详情 | `POST /api/v2/dashen-match/detail/image` | 30s |
| 详情+锐评 | `POST /api/v2/dashen-match/detail/replies` (analyze=true) | 30s |
| AI 开庭 | `POST /api/v2/dashen-match/detail/court` | 30s |
| 段位 | `POST /api/v2/dashen-rank-history/image` | 30s |
| 今日总结 | `POST /api/v2/dashen-summary/today/image` | 30s |
| 昨日总结 | `POST /api/v2/dashen-summary/yesterday/image` | 45s |
| 周总结 | `POST /api/v2/dashen-summary/week/image` | 90s |
| 快速强度 | `POST /api/v2/dashen-quick-strength/image` | 30s |
| 竞技强度 | `POST /api/v2/dashen-competitive-strength/image` | 30s |
| 同玩 | `POST /api/v2/dashen-sameplay/replies` | 30s |
| 省榜 | `POST /api/v2/dashen-rank-leaderboard/image` | 30s |
| 英雄榜 | `POST /api/v2/dashen-hero-leaderboard/image` | 30s |
| 选取率 | `POST /api/v2/ow-hero-pick-rate/image` | 30s |
| 商店 | `POST /api/v2/ow-shop/image` | 30s |
| 赛事 | `POST /api/v2/ow-esports/image` | 30s |
| 补丁 | `POST /api/v2/patch-notes/image` | 30s |

## 关键实现细节

### 用户绑定存储
- 使用 Astrbot KV 存储（`put_kv_data` / `get_kv_data`）
- Key 格式：`bind:{platform}:{user_id}`（如 `bind:aiocqhttp:123456789`）
- Value：BattleTag 字符串（如 `Gulee#5667`）
- 解析优先级：命令参数 > 用户绑定 > 插件配置 `default_bnet_id`
- BattleTag 格式校验：必须包含 `#` 且长度合理

### 图片处理
- Overstats `/image` 端点直接返回 PNG 二进制流
- `/replies` 端点返回 JSON，图片为 base64 编码
- 插件需要将 base64 解码为临时文件，再通过 `event.image_result()` 发送
- 使用 `tempfile` 管理临时图片，发送后清理

### HTTP 客户端
- 使用 `httpx.AsyncClient`，在 `__init__` 中创建，`terminate()` 中关闭
- 所有请求设置合理超时（30s 默认，周总结 90s）
- 错误处理：超时、连接失败、上游 4xx/5xx 均返回友好中文提示

### 配置项（`_conf_schema.json`）
- `overstats_url`：Overstats 服务地址，默认 `http://127.0.0.1:18080`
- `default_bnet_id`：默认 BattleTag（可选）
- `default_timeout`：默认请求超时秒数，默认 30
- `ai_whitelist`：AI 锐评/开庭白名单用户 ID 列表（无冷却限制）
- `ai_cooldown_seconds`：普通用户 AI 锐评冷却时间，默认 300（5分钟）

### 错误码处理
- `bnet_not_found`：提示检查 BattleTag 格式
- `invalid_json`：提示请求格式错误
- 超时：提示服务繁忙，请稍后重试
- 连接失败：提示 Overstats 服务未启动