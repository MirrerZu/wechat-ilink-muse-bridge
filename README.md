# wechat-ilink-muse-bridge

[![CI](https://github.com/MirrerZu/wechat-ilink-muse-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/MirrerZu/wechat-ilink-muse-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)

[中文](README.md) | [English](README.en.md)

**把 Muse 接进微信。**

## 项目介绍

基于腾讯官方 iLink 协议（`ilinkai.weixin.qq.com`）。扫码授权后，你给机器人发
微信，Muse 的 worker 接住、回复；Muse 也能主动给你推送。文字、图片、文件、视频
都能收发，语音能收；只有白名单里的主人能唤醒它。

生产驱动是 Muse hook（`examples/muse-hook/`）：

```
微信 ──> iLink 服务器 ──长轮询──> ilink-watch.sh（Muse hook，每 5 秒一轮）
                                      │ 消息落盘 ilink-pending.json
                                      ▼
                              wake worker ──> ilink.py claim
                                      │       批次 + 近 24 小时对话 + 今天的日志
                                      ▼
                              ilink.py send --lease ──> 微信
                                      │       发送前并入新到的消息
                                      ▼
                              release ──> ilink-warm-wait.sh 守候追问
```

`ilink.py` 是不依赖框架的 iLink 客户端：收发消息、认领队列、媒体加解密、自检。
本仓库 fork 自上游 wechat-ilink-muse-bridge，2026-11-03 起按生产数据重做了可靠性
和回复延迟。

## 优化点

### 一批消息只叫一个 worker

- hook 对同一批只 wake 一次。有守候中的 worker、或有开着的认领时不 wake；没人认领
  的批次 180 秒后才补叫一次。
- 认领改用 `ilink.py claim / renew / release`：加锁、带租约，取代原来的 `mv`。重复
  启动的 worker 拿到 `CLAIM_BUSY` 直接退出；10 分钟没续租的认领被接管，已经回复过的
  直接丢弃，不再叫人核查。
- 发送带幂等 key（批次首条 `message_id`），同一个 key 不会发两次；网络错误用同一个
  `client_id` 退避重试。
- 去掉了自更新。升级按 [`AGENTS.md`](AGENTS.md) 第 5 节原地手动做，留备份，可回滚。

### 少调几轮模型

- `claim --recent --with <今天的日志>`：冷启动的 worker 一次调用就拿到批次、近 24
  小时双方的原话、今天日志的尾部，不用再分几轮读记忆。
- `send --lease`：发送前复查并进发送本身。期间有新消息就不发，打印 `SEND_DEFERRED`
  和合并后的批次，worker 按最新上下文重写。
- `ilink-warm-wait.sh` 守候时替 worker 认领，追问直接带着批次交给 worker。
- `send --lease` 和 `release` 都会立起守候标记：worker 写日志的那十几秒里来的追问，
  不会再叫起第二个冷启动的 worker。

### 收件更快

- 同一轮里的多张图、多个文件并行下载（最多 4 路）。
- 后台点亮"正在输入"的子进程不再占着 hook 的锁。以前每批消息之后，下一轮都被跳过。
- 队列里有批次、本轮又不需要叫人时照常 poll，新消息在队列锁里接到后面。以前 worker
  写回复期间补发的消息要压在服务端等它写完。
- hook 间隔定为 5 秒。

### 部署和观测

- hook 脚本先读同目录的 `ilink-hook.env`：hook 定义没有环境变量字段的运行时（比如要
  用 venv 里的 python）也能原样装新脚本，升级不覆盖它。
- `ilink-events.jsonl` 给每批消息记下到达、唤醒、认领、合并、放下（只有 id、条数和
  时间，不含正文）。到达事件带 `poll_secs` 和 `lag_secs`；`ilink.py stats` 汇总当天
  的收件延迟和认领等待。

## 优化后的效果

2026-11-03 在生产上实测：

| 指标 | 优化前 | 优化后 |
|---|---|---|
| 唤醒 | 一天 236 次 wake，一批最多被叫约 95 次；34 个 worker 里 30 个在核查已经做完的事 | 每批 1 次，不出现第二个 worker |
| 冷启动回复（消息到达 → 第一个气泡） | 一个回合约 36 秒（桥的工具只占 0.4 秒，其余是一轮 3–9 秒的模型调用） | 16 秒 |
| 守候中的追问 | — | 13 秒 |
| 多张图进队列 | 4 张图串行下载 9 秒 | 10 张图（2.4 MB）并行 4.3 秒 |
| 回复期间补发的消息 | 一句文字晚到 17 秒，还多一次重写 | 0–9 秒 |
| 收件延迟 / 认领等待（中位数） | — | 0 秒 / 1 秒 |

还在观察的事项（冷启动第一轮时快时慢、连发多图时先写一版再重写等）见
[`docs/latency-followups.md`](docs/latency-followups.md)。

## 使用说明

### 新部署

把这句话发给你的 Muse：

```
帮我用 wechat-ilink-muse-bridge 把微信接入你：https://github.com/MirrerZu/wechat-ilink-muse-bridge
```

它会按 [`AGENTS.md`](AGENTS.md) 自己完成部署，你只需要做两件事：

1. 用微信扫它给你的二维码。
2. 它提示后，**10 分钟内给机器人发任意一条消息**完成主人绑定。第一个发信的账号成为
   Owner，会收到三条欢迎消息；超时没发，它会再提醒你、重开绑定窗口。

### 升级已有部署

在 Muse 的**主聊天**里说：

```
请按 https://github.com/MirrerZu/wechat-ilink-muse-bridge 的 AGENTS.md 第 5 节，把微信桥原地升级到 main 最新版。
```

不用重新扫码，也不用重新绑定，数据目录（登录态、主人、发送记录）原样保留。从上游
仓库装的旧桥也这样升级。升级只能从主聊天发起，微信消息不能触发。

### 日常使用

- **聊天**：直接给机器人发微信。连发几条会合并成一次回复；引用一条消息再提问，它能
  看到被引的原文。
- **主动推送**：`python3 ilink.py proactive <user_id> -`，正文走 stdin。
- **放行新发信人**：名单外的人（比如你的另一个微信号）发来的消息不会被理会，ID 记进
  `data/unknown_senders.json`。想放行时在 Muse 主聊天里说"把待批名单里的加进白名单"，
  它会列出来让你确认。只认主聊天，微信里说什么都不算。
- **重新扫码**：登录过期时 Muse 会提醒你一次，之后每小时复报，直到你重扫。
- **出问题时**：先让 Muse 跑 `ilink.py health`。

### 命令速查

```bash
python3 ilink.py health [--offline]       # 自检：登录、队列、最近一次轮询（退出码 0/1/2）
python3 ilink.py stats                    # 本地快照：今日收件、收件延迟、认领等待、发送
python3 ilink.py smoke-test [--no-send]   # 端到端自检，会给主人发一条测试文字和一张测试图
python3 ilink.py watchdog                 # hook 心跳看门狗（cron 每 15 分钟）
python3 ilink.py proactive <user_id> -    # 主动推送文字
python3 ilink.py proactive-image <user_id> <图片路径>   # 主动推送图片
python3 ilink.py rollback [--hook-dir <dir>]            # 从最新的 .bak-* 备份恢复
python3 ilink.py prune-media              # 清理媒体（默认保留 30 天、500 MB）
python3 ilink.py preflight                # 登录前环境预检
```

数据目录默认 `./data/`，用环境变量 `ILINK_DATA_DIR` 改。完整命令见 `python3 ilink.py`。

### 注意

- `data/session.json` 是**登录凭证**，等同于密码：只留在本地，不要提交、不要分享。
- 机器人只能收到发给它的消息，看不到你主号的聊天、通讯录和朋友圈。
- iLink 通道不投递机器人发出的语音条，想让它"出声"可以把音频当文件发。

## 致谢与参考

拟人化回复的打磨参考了以下项目的思路（只借思路，本仓库没有直接复用
它们的代码；未采纳的部分也一并注明，免得后人误以为是照搬）：

- [ElmerAlan-an/human_chat_operit](https://github.com/ElmerAlan-an/human_chat_operit) — 短句口语、自检清单、反重复的思路；未采纳其斜杠命令体系与人格扮演设定
- [oaa529/zhichi](https://github.com/oaa529/zhichi) — 拟真引擎的分条、打字节奏、情绪连续性思路；未采纳故意错别字、说漏嘴撤回等表演项
- [huihuihenqiang/WeClone-llm](https://github.com/huihuihenqiang/WeClone-llm) — 逐句回复的思路；未采纳按标点硬切并删标点的做法
- [Napus-BackendDev/GenieAi-ChatBOT](https://github.com/napus-backenddev/genieai-chatbot/blob/HEAD/CLAUDE.md) — 分隔符分气泡 + 按长度停顿的 dispatcher 思路；停顿档位尚未实现，仅作参考
- [freestylefly/openclaw-wechat](https://deepwiki.com/freestylefly/openclaw-wechat/4.5-reply-dispatcher) — Reply Dispatcher 的分段与发送时序思路
- [molang163/chinese-natural-reply](https://github.com/molang163/chinese-natural-reply) — 先给结论、说大白话、去防御性套话
- [speak-human-tw](https://www.typingmind.com/skills/raymondhou0917-speak-human-tw) — 先保事实、再去 AI 味、最后加人味
- [Treasure-hub-agent/ai-with-u](https://github.com/Treasure-hub-agent/ai-with-u) — 时间差感知：隔夜隔天自然接续
- [kroxchan/xinyi](https://github.com/kroxchan/xinyi) — 情绪状态机（情绪别跳变、负面消退慢、过渡自然）与真实性自检五维；未采纳其学人/解密聊天记录的整套路线

协议本身来自腾讯官方 iLink / openclaw-weixin 生态。微信解密、桌面自动化、
聊天记录微调克隆等路线均未采用。

## License

MIT
