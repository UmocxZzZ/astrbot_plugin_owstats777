# OWStats 插件排障指南

## 常见问题

### 1. 请求超时

**症状**：执行命令后返回"请求超时，Overstats 服务繁忙，请稍后重试。"

**原因**：插件配置中的 `default_timeout` 值太小。

**解决**：
1. 进入 Astrbot WebUI → 插件管理 → astrbot_plugin_owstats777 配置
2. 将 `default_timeout` 改为 `120`（秒）
3. 重启插件

**说明**：Overstats 部分接口（如强度分析、周总结）需要较长时间处理，默认 30 秒可能不够。

---

### 2. 大神账号认证失败（401 错误）

**症状**：执行查询命令返回"Could not resolve customerToken from bnet_id"

**原因**：大神 token 无效或过期。

**解决**：
1. 打开大神主页 https://ds.163.com/
2. F12 打开控制台
3. 运行 `Faststart.md` 中的脚本获取新 token
4. 在 Astrbot WebUI 插件配置中更新 `dashen_role_id` 和 `dashen_token`
5. 重启插件

---

### 3. Overstats 内置服务启动失败

**症状**：日志显示"Overstats 内置服务启动失败"

**原因**：通常是配置问题。

**排查步骤**：
1. 查看 Astrbot 日志中的详细错误信息
2. 确认 `dashen_role_id` 和 `dashen_token` 已正确填写
3. 确认 Python 环境满足要求（Python 3.11+）

---

### 4. AI 锐评/开庭无响应

**症状**：执行 `ow 详情 ... 锐评` 或 `ow 开庭` 后无反应

**原因**：
- 未配置 Astrbot LLM Provider
- 用户处于冷却时间内（普通用户 5 分钟限制）

**解决**：
1. 确认 Astrbot 已配置 LLM Provider
2. 检查是否在冷却时间内，白名单用户无限制

---

### 5. 图片显示"未知地图"

**症状**：AI 锐评/开庭生成的图片中地图名称显示为"未知地图"

**原因**：Overstats 静态资源未下载完成。

**解决**：
1. 首次启动时 Overstats 会自动下载约 300MB 静态资源
2. 等待下载完成后再使用
3. 查看日志中的下载进度

---

### 6. BattleTag 绑定失败

**症状**：执行 `ow 绑定` 后提示格式错误

**原因**：BattleTag 必须包含 `#` 号。

**解决**：
- 正确格式：`Player#12345`
- 错误格式：`Player12345`

---

## 配置项说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `overstats_url` | `http://127.0.0.1:18080` | Overstats 服务地址 |
| `overstats_port` | `18080` | 内置服务端口 |
| `embedded_overstats` | `true` | 是否启用内置服务 |
| `dashen_role_id` | 空 | 大神 role_id |
| `dashen_token` | 空 | 大神 token |
| `default_timeout` | `120` | 默认请求超时（秒） |
| `summary_timeout` | `90` | 周总结超时（秒） |
| `ai_timeout` | `180` | AI 锐评/开庭超时（秒） |
| `ai_whitelist` | `[]` | AI 白名单用户列表 |
| `ai_cooldown_seconds` | `300` | AI 冷却时间（秒） |

---

## 日志查看

```bash
# 查看 Astrbot 日志
journalctl -u astrbot -f

# 或在 Astrbot WebUI 查看实时日志
```

关键日志标识：
- `Overstats 内置服务已启动` - 服务启动成功
- `已注入大神账号配置` - 配置注入成功
- `Overstats 请求: URL, 超时: Xs` - 请求发出
- `请求超时` - 超时错误
- `bnet_not_found` - BattleTag 解析失败
