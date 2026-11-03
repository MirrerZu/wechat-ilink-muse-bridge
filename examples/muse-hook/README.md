# Muse hook 变体：把“长轮询”塞进 Muse 的 hook 机制里

[中文](README.md) | [English](README.en.md)

`daemon.py` 是给普通环境用的常驻长轮询循环。但 Muse 运行时里已经有
现成的 agent 调度，不需要再养一个调 LLM 的进程——本目录是生产环境
实际在用的另一种接法：

## 结构

```
微信用户 ──消息──> iLink 服务器
                      │  getupdates（长轮询，服务端 hold ≤35s）
                      ▼
              ilink-watch.sh        ← Muse hook 脚本，每 5s 跑一轮，
                      │               每轮就是一次长轮询
             有新消息 │  先落盘，再 wake
                      ▼
        ilink-pending.json          ← 待处理队列（原子写入，断电不丢）
                      │  worker 用 ilink.py claim 认领 → ilink-processing.json
                      ▼
              Muse worker agent     ← 发送前复查队列、合并连发，只回一条；
                                      超过 10 分钟没处理完的批次会被
                                      脚本退回 pending 重新唤醒
```

## 为什么这么绕

- **不丢消息**：hook 脚本先把消息落盘再唤醒 agent。agent 崩溃/超时，
  消息还在队列里，下一轮脚本会再次唤醒。
- **一批只叫一次**：同一批消息只 wake 一次。有暖态 worker、或有未回复
  的新鲜认领时根本不 wake（那个 worker 发送前会自己合并 pending）；
  一直没人认领的，满 `ILINK_REWAKE_SECS`（默认 180 秒）才补一次。
  以前 pending 在就每轮 wake，一批消息被叫了约 95 次，wake 投递
  积压一个多小时。
- **不重复回复**：认领用 `ilink.py claim`（加锁，与本脚本共用
  `ilink-queue.lock`），第二个 worker 拿到 `CLAIM_BUSY`，不读任何
  上下文就退出。冷启动的 worker 用 `claim --recent --with <今天的日志>`
  一次拿齐批次、最近 24 小时往来原文（取自引用缓存）和日志末尾，第一轮
  就能写回复。发送前复查由第一个气泡的 `send --lease <租约>` 顺带完成，
  把新到的 pending 并进自己的认领、别人就不会再回一遍（有新消息时不发、
  返回 `SEND_DEFERRED`）；回复发完 `release <租约>` 放下。
  处理期间约每 60 秒 `renew <租约>` 续租：超过 10 分钟没续租的认领会被
  下一个 worker 接管。不要再用 `mv` 认领——它会覆盖别人的认领。
- **发送幂等键**：回复某批次时，把该批次首条消息的 `message_id`
  作 `--key` 传给 send（例：`ilink.py send <to> <ctx> - --key <message_id>`）；
  同一 key 的重复调用直接回 `SEND_ALREADY_SENT` 而不重发——这拦的是
  "两次独立调用各带新 client_id"这类服务端去重拦不住的重复。拆成多
  气泡时每个气泡的 key 要加气泡序号后缀（如 `<message_id>-1`、
  `<message_id>-2`），否则后几个气泡会被账本当成重复拦住。
- **连发合并**：用户连发几条时 iLink 会拆成多个响应。worker 不做固定
  等待，而是**发送前复查**：回复组织好、真正发出之前再看一眼队列，
  有新消息就合并进来、按最新上下文重新组织回复，最后只回一条。
- **"正在输入"秒亮**：消息落盘那一刻，脚本核对发信人是主人后立即点亮
  typing 指示，不等 worker 冷启动；worker 侧的点亮只作续期。
- **回复后守候（连聊免冷启动）**：worker 回复完不立刻退出，而是运行
  `ilink-warm-wait.sh` 守候（默认空闲 180 秒、连聊总上限 1800 秒）：它
  维护热态标记 `ilink-warm.json`（心跳），每秒看一次队列，新消息一到就
  替 worker 认领、把批次直接交给它，worker 不再为认领多想一轮，也跳过
  上下文重读直接回复；脚本侧看到心跳新鲜的标记就不再重复 wake。回复
  一发出（`send --lease`）、放下批次时（`release`）就先立起这个标记，worker
  写日志、还没进守候的那十来秒里主人追问，也交给它，不再叫第二个冷
  启动 worker。标记心跳过期（worker 已死，45 秒）脚本自动恢复正常
  wake，自愈。空闲满 180 秒无人说话，worker 才安静退出。
- **token 过期只通知一次**：脚本发现 `TOKEN_EXPIRED` 时创建
  `rescan_notified` 标记并唤醒 agent 通知用户重扫；标记在就不再打扰。
- **告警闭环**（`alert-state.json` 状态机）：过期超过 1 小时每小时复报
  一次；轮询连续失败满 10 分钟才告警（防网络抖动误报）、之后每小时
  复报；曾告警的异常恢复后唤醒 agent 报一句平安（短暂抖动未告警的
  静默清除）；worker 处理超时被回收后，积压的 wake 会带 `stuck_alert`
  字段；hook 自己整段没跑满 `ILINK_GAP_SECS`（默认 600 秒）时，恢复
  后的第一轮 wake `hook_gap` 报断了多久——和 hook 一起睡过去的 cron
  看门狗醒来只能看到刷新过的心跳。恢复（重新扫码/网络恢复）时自动清除过期标记——旧版标记永不
  清除会导致下一次过期不再通知，已修复。
- **交付语义：至少一次（at-least-once）**。poll 是两段式的：`ilink.py
  poll` 只拉取、不推进游标不记去重 ID；脚本把批次原子落盘后才调
  `ilink.py commit` 提交。任何一步崩溃，消息都会在下一轮被重新拉到
  ——可能重复回复，但绝不静默丢失。配套的防线：超过 10 分钟没续租的
  认领，已回复的（发送账本里有记录）直接删，没回复的并回待处理队列
  队首，旧版 worker 自建的 `ilink-processing-*.json` 一并处理，绝不
  覆盖新批次；合法空队列删掉后照常收消息；poll 输出为空/非法 JSON、
  或业务码非零，一律按轮询故障处理（进告警状态机，而不是被当成
  "没有新消息"）。
- **事件流水**：wake、认领、合并、放下逐条记进 `ilink-events.jsonl`
  （只有 id、条数和时间，不记正文），一批消息从到达到放下全程可查。
  到达（arrive）按 poll 返回的时刻记，带 `poll_secs`（这次长轮询
  hold 了多久）和 `lag_secs`（最早一条从主人发出到落盘过了多久）；
  `ilink.py stats` 汇总当天的收件延迟和认领等待。

- **共享驱动锁**：本脚本每轮在单实例锁之外，还会非阻塞尝试状态目录下的 `ilink-driver.lock`；拿不到说明 `daemon.py` 在跑，本轮安静退出（不 touch 心跳、不 poll、不 wake）。反过来，`daemon.py` 启动时若本脚本的心跳新鲜也会拒绝启动。先跑起来的驱动赢，后到的让路——“hook 与 daemon 二选一”不再只是一句文档。
- **两把锁分工不同**：`ilink-watch.lock` 防 hook 自己重叠，`ilink-driver.lock` 防 hook 与 daemon 互抢；两个驱动必须指向同一个状态目录，锁才共享。

脚本里的路径已参数化（`ILINK_DATA_DIR`、`ILINK_HOOK_STATE_DIR`、`ILINK_CLIENT`；
状态目录旧变量名 `ILINK_HOOK_STATE` 仍兼容，默认值与 `ilink.py health` 一致），
`$HATCH_HOOK_RUNTIME` 是 Muse hook 运行时提供的环境，非 Muse 环境请用
仓库根目录的 `daemon.py`。
