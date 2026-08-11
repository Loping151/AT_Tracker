# AT_Tracker

> 谁 at 我？—— 图文版

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

把群里别人 at 你的那段对话原貌还原成一张图发给你，而不是干巴巴一句「XX 在 X 时间 at 了你」。

支持 [gsuid_core](https://github.com/Genshin-bots/gsuid_core) 与 NoneBot2 两种宿主。

![example](./example.png)

## 工作原理

- **会话粒度，不是消息粒度**：a 连续 at b 视为同一次会话，期间 c at b 单独计一条
- **上下文还原**：每条记录覆盖 `a 首次发言 -1 条 ~ a 最后发言 +1 条` 的完整对话片段，结合前后 `-5/+10` 条群消息一起渲染
- **聊天图片随会话缓存**：保留期内可以正常打开，过期统一清理 —— 占用按 `RETENTION_DAYS` 和 `MAX_DISK_MB` 双重控制，撑到上限时自动从最早的记录开始淘汰
- **不记录 at 机器人自己**：@bot 的指令消息不会进记录；`@bot @某人` 这类混合 at 仍会为「某人」建记录

## 安装

### gsuid_core

把整个仓库 clone 到 `gsuid_core/gsuid_core/plugins/` 下：

```bash
cd <你的 gsuid_core 路径>/gsuid_core/plugins
git clone https://github.com/Loping151/core_plugin_at_tracker AT_Tracker
```

子目录 `nonebot_plugin_at_tracker/` 用不上，可以删，不删也不影响。

### NoneBot2

仓库内 `nonebot_plugin_at_tracker/` 即 nb 版本，按常规 NoneBot 插件方式装入。

## 用法

群里发：

```
谁at我        # 查询自己
谁艾特我
谁@我
谁at我 @某人  # 帮别人查（at 谁就查谁）
```

机器人会回一张图，按时间倒序列出所有命中会话。

## 配置

| Key | 说明 | 默认 |
|---|---|---|
| `RETENTION_DAYS` | AT 记录保留天数，超期删除 | 3 |
| `CACHE_SIZE` | 群组最近消息缓存条数 | 5 |
| `TRACKING_COUNT` | 检测到 AT 后继续追踪发送者后续消息条数 | 10 |
| `MAX_DISK_MB` | 记录缓存磁盘占用上限（MB），超限从最早记录起淘汰到 90% 水位，0 为不限制 | 1024 |
| `EnableAvatarCache` | 缓存用户头像（加速渲染，吃磁盘） | False |

## 致谢

源自作者本地的 NoneBot 插件，5 分钟改成的 gsuid 版本。移植欢迎，保留出处即可。
