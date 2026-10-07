# Changelog

## [0.1.0-beta8] - 2026-06-16

- Release version 0.1.0-beta8

## [0.1.0-beta9] - 2026-06-16

- Release version 0.1.0-beta9

## [0.2.0-beta1] - 2026-06-16

- Release version 0.2.0-beta1

## [0.2.0-beta2] - 2026-06-16

- Release version 0.2.0-beta2

## [0.2.1-beta] - 2026-06-17

- Release version 0.2.1-beta

## [0.2.1-beta1] - 2026-06-17

- Release version 0.2.1-beta1

## [0.2.1-beta2] - 2026-06-18

- Release version 0.2.1-beta2

## [0.2.1-beta3] - 2026-06-18

- Release version 0.2.1-beta3

## [0.2.1-beta4] - 2026-06-18

- Release version 0.2.1-beta4

## [0.2.1-beta5] - 2026-06-18

- Release version 0.2.1-beta5

## [0.2.2-beta] - 2026-06-20

- Release version 0.2.2-beta

## [0.2.2-beta1] - 2026-06-20

- Release version 0.2.2-beta1

## [0.2.2-beta2] - 2026-06-21

- Release version 0.2.2-beta2

## [0.2.2-beta3] - 2026-06-21

- Release version 0.2.2-beta3

## [0.2.2-beta4] - 2026-06-21

- Release version 0.2.2-beta4

## [0.2.2-beta5] - 2026-06-21

- Release version 0.2.2-beta5

## [0.2.2-beta6] - 2026-06-21

- Release version 0.2.2-beta6

## [0.2.2-beta7] - 2026-06-21

- Release version 0.2.2-beta7

## [0.2.2-beta8] - 2026-06-21

- Release version 0.2.2-beta8

## [0.2.3] - 2026-07-16

- Release version 0.2.3

## [0.3.0] - 2026-07-17

- Release version 0.3.0

## [0.3.1] - 2026-07-17

- 修复扫码凭证未随“删除持久化数据”清除的问题，并自动迁移旧版 KV 凭证

## [0.4.0] - 2026-08-13

- 同步 Overstats `85019e6`、`e476cfe`、`4a08252`
- 适配国服第四赛季、D.Mon 和翡翠段位
- 增加大神 403 原因日志与玩家禁赛状态标记

## [0.4.1] - 2026-09-08

- Release version 0.4.1

## [0.5.0] - 2026-10-07

- 更新大神第 25 赛季、国服英雄榜第 5 赛季及英雄别名。
- 修复赛季日期解析与快速强度重复分页，避免无效请求持续循环。
- 优化 token 查询的玩家身份识别，兼容战绩详情、同玩及 AI 开庭，避免同名玩家误判。
- 快速与竞技强度图新增双方段位分布，停用耗时的全库百分位计算。
- 优化历史段位图，使用中文职责名，清晰区分当前、最高段位与未定级状态。
- 保留压缩赛季图、合并字体及 AstrBot 缓存适配。
