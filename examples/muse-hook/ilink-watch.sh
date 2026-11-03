#!/usr/bin/env bash
# iLink 微信分身：长轮询收消息（hook 版）
# 每一轮脚本运行 = 一次 getupdates 长轮询（服务端 hold 最多 35s，有消息立刻返回）。
# 有新消息 -> 写入 pending 队列并 wake，由 worker agent 生成回复；
# 无消息/出错 -> silent。
# poll 是两段式的：ilink.py poll 只拉取、不推进游标不记去重 ID；本脚本把
# 批次原子落盘 pending 之后才调 ilink.py commit 提交。任何一步崩溃，
# 消息都会在下一轮被重新拉到——宁可重复，绝不丢失。
# 告警闭环（alert-state.json 状态机）：
#   - token 过期：首次立即 wake 报一次，之后每小时复报一次（payload 带 repeat/hours）
#   - 轮询连续失败：满 10 分钟才报（防抖动误报），之后每小时复报一次
#   - 恢复：曾告警过的异常恢复正常后 wake 一次 recovered（报好不只报坏）；
#     短暂抖动未告警的静默清除状态
#   - worker 卡住：processing 超 10 分钟被回收时记 stuck 起点，积压超 10 分钟
#     随 pending 的 wake 带 stuck_alert（每小时至多一次）
# 可靠性补强：
#   - 单实例锁（flock）：重叠触发时后到的实例安静退出，防两个实例在
#     查队列→poll→落盘→commit→唤醒全程互相覆盖
#   - pending 损坏隔离：队列文件不可解析时改名 .corrupt-<epoch> 保留
#     现场并在日志打 PENDING_CORRUPT 标记，绝不当成空队列覆盖或静默卡死
#   - commit 待提交账本：commit 前先把 payload 原子落盘到
#     ilink-commit-pending.json，成功才删；失败时下一轮开头先补交，
#     补交成功前不 poll——commit 失败只是延迟，不与重拉交叠出重复处理
# 存活心跳（2026-10-25）：
#   - 每轮拿锁成功后 touch ilink-heartbeat；安静轮次不写 poll.log，
#     唯独心跳能证明 hook 活着，供 ilink.py watchdog 看门
# 唤醒节流与认领协议（2026-11-03，生产一天 236 次 wake、34 个 worker
# 里 30 个白跑之后）：
#   - 一批只 wake 一次：有暖态 worker、或有未回复的新鲜认领（它发送前会
#     自己合并 pending）时不 wake；同一批无人认领满 ILINK_REWAKE_SECS
#     （默认 180 秒）才补一次。以前 pending 在就每轮 wake，wake 投递
#     排队积压一个多小时，真消息的 wake 排在一堆重复 wake 后面
#   - pending 里有批次、本轮又不用叫人时照常 poll，新消息持队列锁接到
#     pending 后面（以前 pending 在就不 poll，后到的消息晚十几秒）
#   - worker 用 ilink.py claim/renew/release 认领（与本脚本共用
#     ilink-queue.lock）；超时认领连同旧版 worker 自建的
#     ilink-processing-*.json 一起处理：已回复的直接删，未回复的并回
#     pending 队首，不再因为 pending 非空就跳过回收
#   - 合法空 pending 删掉后照常 poll（以前会让本脚本永远不再收消息）
#   - typing 后台限时点亮，不挡在 wake 前面
#   - 断档报告：touch 心跳前先读上次心跳，隔了 ILINK_GAP_SECS（默认
#     600 秒）以上就 wake hook_gap 报"断了多久"——cron 看门狗和 hook
#     一起停、hook 先恢复时，watchdog 只能看到新鲜心跳
#   - ilink-events.jsonl：wake/认领/合并/放下逐条记一行（只有 id、条数
#     和时间），一个批次从到达到放下的全过程可查
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# 部署自己的值（ILINK_DATA_DIR、ILINK_CLIENT、带 cryptography 的 venv 的
# PATH）写进脚本旁的 ilink-hook.env：hook 定义没有环境变量字段时只能靠它，
# 升级只换脚本、不碰它。文件里只放 export 行，先于下面的默认值生效。
HOOK_ENV=${ILINK_HOOK_ENV:-$(dirname "${BASH_SOURCE[0]}")/ilink-hook.env}
if [ -f "$HOOK_ENV" ]; then
  # shellcheck source=/dev/null
  source "$HOOK_ENV"
fi

# Paths are parameterized for this public example. In production these point
# at the bridge's data dir and the hook state dir.
BRIDGE=${ILINK_DATA_DIR:-$HOME/wechat-ilink-data}
# Canonical name/default match `ilink.py health` (ILINK_HOOK_STATE_DIR,
# ~/hooks/state) — the two MUST agree or health inspects an empty dir.
# ILINK_HOOK_STATE is the legacy name, still honored as an alias.
STATE_DIR=${ILINK_HOOK_STATE_DIR:-${ILINK_HOOK_STATE:-$HOME/hooks/state}}
mkdir -p "$STATE_DIR"
# 单实例锁：用 fd 持有 flock 到脚本结束（不要用 flock 命令包裹子命令，
# 那样会漏掉段落）。拿不到锁 = 已有实例在跑，安静退出即可，不是错误。
exec 9>"$STATE_DIR/ilink-watch.lock"
if ! flock -n 9; then
  exit 0
fi
# 共享驱动锁（daemon/hook 二选一落到锁上，不只靠文档）：
# daemon 常驻时会一直持有 $STATE_DIR/ilink-driver.lock；本脚本每轮
# 尝试非阻塞拿同一把锁，拿不到 = daemon 在跑，本轮安静退出，不
# touch 心跳、不 poll、不 wake。反过来，daemon 启动时会先看
# ilink-heartbeat 是否新鲜（本脚本每轮都 touch），新鲜就拒绝启动，
# 所以先跑起来的驱动赢，后到的安静让路。两把锁分工不同：
# ilink-watch.lock 防 hook 自己重叠，ilink-driver.lock 防 hook 与
# daemon 互抢。注意顺序：驱动锁检查必须在 touch 心跳之前，否则
# 一个被 daemon 挡住的 hook 轮次也会刷新心跳，误导 daemon 以为
# hook 还活着。
exec 8>"$STATE_DIR/ilink-driver.lock"
if ! flock -n 8; then
  exit 0
fi
# 存活心跳：每轮都 touch，看门狗凭它判断 hook 是不是还在跑。
# 拿不到锁的实例安静退出，不 touch。touch 之前先记下上一次心跳的
# 时间——那就是 hook 上次活着的时刻，断档检测靠它。
prev_hb=$(stat -c %Y "$STATE_DIR/ilink-heartbeat" 2>/dev/null || echo 0)
touch "$STATE_DIR/ilink-heartbeat"
PENDING=$STATE_DIR/ilink-pending.json
PROC=$STATE_DIR/ilink-processing.json
WARM=$STATE_DIR/ilink-warm.json
WOKEN=$STATE_DIR/ilink-woken.json
EVENTS=$STATE_DIR/ilink-events.jsonl
COMMIT_PENDING=$STATE_DIR/ilink-commit-pending.json
LEDGER=$BRIDGE/send_ledger.json
RESCAN=$BRIDGE/rescan_notified
POLLLOG=$BRIDGE/poll.log
ALERT=$BRIDGE/alert-state.json
DRY=${HATCH_HOOK_DRY_RUN:-0}
REWAKE_SECS=${ILINK_REWAKE_SECS:-180}
GAP_SECS=${ILINK_GAP_SECS:-600}
NOW=$(date +%s)

now() { date '+%Y-%m-%d %H:%M:%S'; }

# 带时间戳的一行日志进 poll.log（脚本平时落痕的地方；dry-run 不写）
log_line() {
  [ "$DRY" = "1" ] && return 0
  echo "[$(now)] $1" >> "$POLLLOG"
  trim_poll_log
}

# poll.log 上限：health 只读最后一行，但脚本只追加从不截断，文件会
# 无限增长。每次写日志后只保留最近 500 行（安静轮次不写日志，所以
# 截断本身很廉价；单实例锁保证截断时没有别的实例在写）。
trim_poll_log() {
  [ "$DRY" = "1" ] && return 0
  [ -f "$POLLLOG" ] || return 0
  local n
  n=$(wc -l < "$POLLLOG" 2>/dev/null || echo 0)
  if [ "$n" -gt 500 ] 2>/dev/null; then
    tail -n 500 "$POLLLOG" > "$POLLLOG.trim.$$" && mv "$POLLLOG.trim.$$" "$POLLLOG"
  fi
}

# 队列文件三态计数：空/不存在 -> 0；合法队列（对象 + .msgs 数组）-> 条数；
# 损坏或结构不对 -> 立即隔离改名保留现场、日志打 PENDING_CORRUPT
# 标记、本轮按 0 处理。绝不把损坏文件当成空队列：当 0 条会被上层
# 覆盖（丢消息）或误判成"队列已空"而静默卡死。
queue_count() {
  local path=$1 n iso
  [ -s "$path" ] || { echo 0; return; }
  # Strict shape check, not just "parses as JSON": a file like
  # {"msgs": "invalid"} parses fine but is not a queue. Counting it
  # as 0 used to send the script down the silent "pending file empty"
  # branch forever (the pending branch never reaches poll), a quiet
  # total stall. Anything misshapen quarantines like corrupt input.
  if ! jq -e 'type == "object" and (.msgs | type == "array")' "$path" >/dev/null 2>&1; then
    # $$ in the name: two quarantines in the same second must not
    # clobber each other (epoch seconds alone collide).
    iso="$path.corrupt-$(date +%s)-$$"
    if mv "$path" "$iso" 2>/dev/null; then
      log_line "PENDING_CORRUPT: $path unparseable, quarantined as $iso"
      log "PENDING_CORRUPT: quarantined $path as $iso"
    fi
    echo 0
    return
  fi
  n=$(jq '[.msgs[]?] | length' "$path" 2>/dev/null || echo 0)
  echo "${n:-0}"
}

# commit 待提交账本补交：上一轮 commit 失败时 payload 留在
# ilink-commit-pending.json。本轮 poll 之前先补交：成功删账本、继续；
# 失败打 COMMIT_RETRY_FAILED 并返回非 0，本轮跳过 poll（旧批次未提交
# 前拉新批次会交叠）。账本本身损坏则隔离丢弃并明确打标记——payload
# 丢了游标就停在原地，下轮 poll 会把这批消息原样重拉，不丢消息。
flush_commit_pending() {
  [ -f "$COMMIT_PENDING" ] || return 0
  [ "$DRY" = "1" ] && return 0
  local payload iso
  if ! jq -e 'type == "object"' "$COMMIT_PENDING" >/dev/null 2>&1; then
    iso="$COMMIT_PENDING.corrupt-$(date +%s)"
    mv "$COMMIT_PENDING" "$iso" 2>/dev/null || rm -f "$COMMIT_PENDING"
    log_line "COMMIT_PENDING_CORRUPT: $COMMIT_PENDING unparseable, quarantined as $iso; its batch will be re-fetched"
    log "COMMIT_PENDING_CORRUPT: quarantined $COMMIT_PENDING as $iso"
    return 0
  fi
  payload=$(jq -c '.' "$COMMIT_PENDING")
  if printf '%s' "$payload" | python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" commit >/dev/null 2>&1; then
    rm -f "$COMMIT_PENDING"
    log "retried pending commit OK"
    return 0
  fi
  log_line "COMMIT_RETRY_FAILED: $COMMIT_PENDING still uncommitted, poll skipped this round"
  log "commit retry failed, poll skipped this round"
  return 1
}

# 热态守候：worker 回复后不退出、由 ilink-warm-wait.sh 维护 ilink-warm.json
# 标记（心跳 + 总时长上限）。心跳新鲜（<=45s）且未过总上限时，说明已有一个
# 上下文就绪的 worker 在盯着队列——本脚本只管落盘/点亮输入，不再重复 wake；
# 标记过期即视为 worker 已死，自动恢复正常 wake（自愈）。
warm_alive() {
  [ -f "$WARM" ] || return 1
  local hb until_n
  hb=$(jq -r '.heartbeat // 0' "$WARM" 2>/dev/null || echo 0)
  until_n=$(jq -r '.until // 0' "$WARM" 2>/dev/null || echo 0)
  case "$hb" in ''|*[!0-9]*) return 1 ;; esac
  case "$until_n" in ''|*[!0-9]*) return 1 ;; esac
  [ $((NOW - hb)) -le 45 ] && [ "$until_n" -gt "$NOW" ]
}

# 事件流水 ilink-events.jsonl（与 ilink.py claim 共用）：只记 id、条数、
# 时间，不记正文。$2 是附加字段的 JSON 对象。
hook_event() {
  [ "$DRY" = "1" ] && return 0
  local extra=${2:-}
  [ -n "$extra" ] || extra='{}'
  jq -nc --argjson ts "$NOW" --arg e "$1" --argjson x "$extra" \
     '{ts: $ts, event: $e, source: "hook"} + $x' >> "$EVENTS" 2>/dev/null || true
}

# 批次签名 = 首条 message_id，也就是 worker 回复时用的 --key
batch_sig() {
  jq -r '.msgs[0].message_id // empty | tostring' "$1" 2>/dev/null || true
}

# 这一批是否已回复：发送账本里它的 key（或首个气泡 key-1）已标记 sent。
# 已回复的认领只是没人放下，不算"有人在干活"，回收时也不该再叫人。
batch_answered() {
  local id
  id=$(batch_sig "$1")
  [ -n "$id" ] && [ -f "$LEDGER" ] || return 1
  jq -e --arg k "$id" \
     '[.[$k], .[$k + "-1"]] | any(type == "object" and (.status == "sent" or (.status == null and .result == "ok")))' \
     "$LEDGER" >/dev/null 2>&1
}

# 有人正在处理一批还没回复的消息：它发送前会自己合并 pending，不必再叫人
claim_active() {
  [ -f "$PROC" ] || return 1
  [ -z "$(find "$PROC" -cmin +10 2>/dev/null || true)" ] || return 1
  ! batch_answered "$PROC"
}

# 距上次为同一批 wake 过了几秒；没为这批 wake 过则失败
woken_ago() {
  [ -f "$WOKEN" ] || return 1
  local s at
  s=$(jq -r '.sig // empty' "$WOKEN" 2>/dev/null || true)
  at=$(jq -r '.at // 0' "$WOKEN" 2>/dev/null || echo 0)
  [ -n "$s" ] && [ "$s" = "$1" ] || return 1
  case "$at" in ''|*[!0-9]*) return 1 ;; esac
  echo $((NOW - at))
}

mark_woken() {
  [ "$DRY" = "1" ] && return 0
  local tmp="$WOKEN.tmp.$$"
  if jq -nc --arg s "$1" --argjson at "$NOW" '{sig: $s, at: $at}' > "$tmp" 2>/dev/null; then
    mv "$tmp" "$WOKEN" 2>/dev/null || rm -f "$tmp"
  else
    rm -f "$tmp"
  fi
}

# "正在输入"：后台点亮、限时 45 秒（仅主人）。同步调用时票据刷新加
# 重试最坏要等 4 次 20 秒超时，wake 跟着晚一分多钟。
light_typing() {
  [ "$DRY" = "1" ] && return 0
  local owner sender
  owner=$(jq -r '.owner_id // empty' "$BRIDGE/owner.json" 2>/dev/null || true)
  sender=$(jq -r '.msgs[0].from_user_id // empty' "$1" 2>/dev/null || true)
  if [ -n "$owner" ] && [ "$sender" = "$owner" ]; then
    # 关掉锁 fd 再放进后台：flock 跟着打开的文件走，typing 进程带着
    # 9/8 活多久，watch 锁和驱动锁就被占多久，下一轮拿不到锁、整轮
    # 跳过——生产上每批消息之后都丢一轮（2026-11-03，约 10 秒）。
    (timeout 45 python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" typing "$sender" start >/dev/null 2>&1 7>&- 8>&- 9>&- &) || true
  fi
}

# 告警状态读写（jq，原子写；dry-run 不写状态，保持纯净）
astate() { jq -r ".$1 // 0" "$ALERT" 2>/dev/null || echo 0; }
aset() {
  [ "$DRY" = "1" ] && return 0
  local tmp="$ALERT.tmp.$$"
  if [ -f "$ALERT" ]; then
    jq --argjson v "$2" ".$1 = \$v" "$ALERT" > "$tmp" 2>/dev/null || echo '{}' > "$tmp"
  else
    jq -n --argjson v "$2" "{\"$1\": \$v}" > "$tmp" 2>/dev/null || echo '{}' > "$tmp"
  fi
  mv "$tmp" "$ALERT" 2>/dev/null || true
}

# 断档报告：上次心跳距今超过 GAP_SECS，说明中间整段 hook 没跑过（VM
# 替换、运行时暂停、驱动被停）。本轮只报这一件事：wake hook_gap 后结束，
# 服务端保留的消息下一轮照常拉。维护开关 ilink-watchdog-off 在时是
# 主动停桥，不报。
if [ "$prev_hb" -gt 0 ] && [ $((NOW - prev_hb)) -ge "$GAP_SECS" ] \
   && [ ! -e "$STATE_DIR/ilink-watchdog-off" ]; then
  gap=$((NOW - prev_hb))
  log_line "HOOK_GAP: no hook run for $((gap / 60))m ($(date -d "@$prev_hb" '+%Y-%m-%d %H:%M:%S') -> $(now))"
  hook_event gap "{\"secs\":$gap,\"from\":$prev_hb}"
  if [ "$DRY" != "1" ]; then
    wake "hook_gap" "{\"minutes\":$((gap / 60)),\"from\":$prev_hb,\"to\":$NOW}"
  else
    silent "dry-run: would wake hook_gap ($((gap / 60))m)"
  fi
  exit 0
fi

# 回收超时认领。与 ilink.py claim/renew/release 共用 ilink-queue.lock，
# 两边不会同时改队列。超过 10 分钟没续租的认领文件——ilink-processing.json，
# 以及旧版 worker 临场自建的 ilink-processing-*.json——逐个处理：
#   - 已回复过（见 batch_answered）：只是没人放下，直接删，不再叫人。
#     旧契约没写"回复完放下"，这类回收以前每次都白白叫起一个核查 worker
#   - 没回复过：并回 pending 队首（整条去重）。以前 pending 非空就不回收，
#     新 worker 再用 mv 认领时会把这批死认领覆盖掉
# 必须用 -cmin（inode 变更时间）而非 -mmin（内容修改时间）：mv 认领保留
# 消息落盘时的 mtime，积压超过 10 分钟的批次一认领就会被误判过期；
# rename 和 touch 都刷新 ctime，ctime 才是真正的认领/续租时间。
exec 7>"$STATE_DIR/ilink-queue.lock"
if ! flock -w 30 7; then
  silent "queue lock busy, round skipped"
  exit 0
fi
pending_msgs=$(queue_count "$PENDING")
for claim in "$PROC" "$STATE_DIR"/ilink-processing-*.json; do
  [ -f "$claim" ] || continue
  [ -n "$(find "$claim" -cmin +10 2>/dev/null || true)" ] || continue
  name=$(basename "$claim")
  if ! jq -e 'type == "object" and (.msgs | type == "array")' "$claim" >/dev/null 2>&1; then
    iso="$claim.corrupt-$(date +%s)-$$"
    mv "$claim" "$iso" 2>/dev/null || true
    log_line "CLAIM_CORRUPT: stale $name unparseable, quarantined as $iso"
    continue
  fi
  if batch_answered "$claim"; then
    rm -f "$claim"
    log "dropped stale $name: already answered, never released"
    hook_event drop_answered "{\"file\":\"$name\"}"
    continue
  fi
  tmp="$PENDING.tmp.$$"
  if [ "${pending_msgs:-0}" -gt 0 ]; then
    merge='{msgs: (reduce (.[0].msgs + .[1].msgs)[] as $m ([]; if any(.[]; . == $m) then . else . + [$m] end))}'
    jq -c -s "$merge" "$claim" "$PENDING" > "$tmp" 2>/dev/null || { rm -f "$tmp"; continue; }
  else
    jq -c '{msgs}' "$claim" > "$tmp" 2>/dev/null || { rm -f "$tmp"; continue; }
  fi
  mv "$tmp" "$PENDING"
  rm -f "$claim"
  pending_msgs=$(queue_count "$PENDING")
  log "recovered stale $name into pending"
  hook_event recover "{\"file\":\"$name\",\"count\":${pending_msgs:-0}}"
  # 卡住计时：能被回收说明这一轮处理至少已卡了 10 分钟，起点回拨 600 秒
  if [ "$(astate stuck_since)" -eq 0 ]; then
    aset stuck_since $((NOW - 600))
  fi
done
# 合法但为空的 pending（{"msgs":[]}）：删掉，本轮照常 poll。以前它让本
# 脚本每轮都停在"pending file empty"，再也不收新消息。
if [ -s "$PENDING" ] && [ "${pending_msgs:-0}" -eq 0 ]; then
  rm -f "$PENDING"
  log "removed empty pending file"
fi
exec 7>&-

# 有未处理的消息（上一轮拉到、还没人认领）：先决定要不要叫人。要叫就
# 叫，本轮到此为止；不用叫（守候的 worker、开着的认领、刚叫起的 worker
# 都会把 pending 收走）就照常 poll，新消息接到 pending 后面。以前这里
# 一律不 poll：worker 生成回复的几十秒里后到的消息全压在服务端，等它
# 并走 pending 才拉下来，常常又逼出一次 SEND_DEFERRED 重写（2026-11-03，
# 一句文字因此晚到 17 秒）。
held=""
if [ "${pending_msgs:-0}" -gt 0 ]; then
  cnt=$pending_msgs
  sig=$(batch_sig "$PENDING")
  if warm_alive; then
    held="warm worker on duty"
  elif claim_active; then
    held="worker holds an open claim, it merges pending before sending"
  elif ago=$(woken_ago "$sig") && [ "$ago" -lt "$REWAKE_SECS" ]; then
    held="woken ${ago}s ago for this batch, waiting for a claim"
  else
    light_typing "$PENDING"
    # 卡住告警：积压起点距今 >=10 分钟且距上次告警 >=1 小时，随 wake 带出
    extra=""
    stuck_since=$(astate stuck_since)
    stuck_last=$(astate stuck_last)
    if [ "$stuck_since" -gt 0 ] && [ $((NOW - stuck_since)) -ge 600 ] \
       && [ $((NOW - stuck_last)) -ge 3600 ]; then
      aset stuck_last "$NOW"
      extra=",\"stuck_alert\":true,\"stuck_minutes\":$(( (NOW - stuck_since) / 60 ))"
    fi
    mark_woken "$sig"
    hook_event wake "{\"reason\":\"pending_wechat_messages\",\"sig\":\"$sig\",\"count\":$cnt}"
    wake "pending_wechat_messages" "{\"count\":$cnt,\"pending_file\":\"$PENDING\"$extra}"
    exit 0  # wake 是本轮最后一步
  fi
fi
if ! flush_commit_pending; then
  # 上一轮的 commit 还没补交成功：本轮不 poll，避免新批次与未提交的
  # 旧批次交叠（补交成功后旧批次的游标才推进，届时正常拉新消息）
  silent "commit retry pending, poll skipped${held:+; $held}"
else
  # 长轮询一轮（服务端 hold 住最多 35s）。timeout 120 封顶：poll 本身
  # 是纯拉取（不推进游标、不记去重），超时只是本轮无结果、下轮重拉，
  # 但能防止病态媒体批次把队列拖住数十分钟（单实例锁会让后续轮次
  # 安静退出，队列实际停滞）。
  poll_t0=$(date +%s)
  out=$(timeout 120 python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" poll 2>/dev/null || true)
  # NOW 是本轮开头取的，长轮询可能已 hold 了 35 秒：之后的事件时间、
  # 热态心跳判断和告警计时都按 poll 返回的时刻算
  NOW=$(date +%s)

  if [[ "$out" == TOKEN_STALE* ]]; then
    # -14 退避窗口内：token 可能自愈，不告警、不记 poll 故障，下轮再看
    silent "token stale, backoff in progress"
  elif [[ "$out" == NO_TOKEN* ]] || [[ "$out" == TOKEN_EXPIRED* ]]; then
    exp_since=$(astate exp_since)
    exp_last=$(astate exp_last)
    if [ "$exp_since" -eq 0 ]; then
      # 首次过期：立即报一次（rescan_notified 标记保留作兼容，成功轮询时清除）
      aset exp_since "$NOW"
      aset exp_last "$NOW"
      if [ "$DRY" != "1" ]; then
        touch "$RESCAN"
        wake "token_expired" "{}"
      else
        silent "dry-run: would set expiry state and wake token_expired"
      fi
    elif [ $((NOW - exp_last)) -ge 3600 ]; then
      # 长时间未恢复：每小时复报一次
      aset exp_last "$NOW"
      if [ "$DRY" != "1" ]; then
        wake "token_expired" "{\"repeat\":true,\"hours\":$(( (NOW - exp_since) / 3600 ))}"
      else
        silent "dry-run: would repeat token_expired alert"
      fi
    else
      silent "token expired, repeat alert throttled"
    fi
  elif [[ "$out" == POLL_ERROR* ]] || ! printf '%s' "$out" | jq -e 'type == "object" and has("msgs")' >/dev/null 2>&1; then
    # POLL_ERROR，或 poll 进程硬崩溃（空输出/非法 JSON/缺 msgs 字段）。
    # 崩溃若不进本分支，会被误判成"没有新消息"、故障永远不告警。
    if [ "$DRY" != "1" ]; then
      echo "[$(now)] $(printf '%s' "${out:-POLL_CRASH:empty-or-invalid-output}" | head -c 200)" >> "$POLLLOG"
      trim_poll_log
    fi
    err_since=$(astate err_since)
    if [ "$err_since" -eq 0 ]; then
      aset err_since "$NOW"
      err_since=$NOW
    fi
    err_last=$(astate err_last)
    err_dur=$((NOW - err_since))
    if [ "$err_dur" -ge 600 ] && { [ "$err_last" -eq 0 ] || [ $((NOW - err_last)) -ge 3600 ]; }; then
      # 连续失败满 10 分钟才报（抖动通常几分钟内自愈），之后每小时复报
      aset err_last "$NOW"
      if [ "$DRY" != "1" ]; then
        wake "poll_failing" "{\"minutes\":$((err_dur / 60))}"
      else
        silent "dry-run: would wake poll_failing"
      fi
    else
      silent "poll error"
    fi
  else
    # 轮询成功：先做恢复记账（曾告警过的异常恢复了要报平安）
    recovered=""
    exp_since=$(astate exp_since)
    err_since=$(astate err_since)
    # 卡住事件收尾：队列已排空（本轮开始时没有 pending）且无人处理中，清状态
    if [ "${pending_msgs:-0}" -eq 0 ] && [ ! -f "$PROC" ] \
       && [ "$(astate stuck_since)" -gt 0 ]; then
      aset stuck_since 0
      aset stuck_last 0
    fi
    if [ "$exp_since" -gt 0 ]; then
      recovered="{\"kind\":\"token\",\"minutes\":$(( (NOW - exp_since) / 60 ))}"
      aset exp_since 0
      aset exp_last 0
      if [ "$DRY" != "1" ]; then rm -f "$RESCAN"; fi
    elif [ "$err_since" -gt 0 ]; then
      if [ "$(astate err_last)" -gt 0 ]; then
        recovered="{\"kind\":\"poll_error\",\"minutes\":$(( (NOW - err_since) / 60 ))}"
      fi
      aset err_since 0
      aset err_last 0
    fi
    cnt=$(printf '%s' "$out" | jq '[.msgs[]?] | length' 2>/dev/null || echo 0)
    commit_json=$(printf '%s' "$out" | jq -c '.commit // empty' 2>/dev/null || true)
    # 提交本轮 poll（推进游标 + 记 seen）：只能在批次可靠落盘之后调用。
    # payload 先原子落盘成待提交账本再调 commit、成功才删账本：进程在
    # 前后任一步崩溃，账本都还在，下轮开头先补交（见 flush_commit_pending）。
    # 失败不中断本轮（批次已在队列里、worker 照常处理），COMMIT_FAILED
    # 记入 poll.log 并注明账本位置——失败只是延迟，不与重拉交叠。
    do_commit() {
      if [ "$DRY" != "1" ] && [ -n "$commit_json" ]; then
        local ctmp="$COMMIT_PENDING.tmp.$$"
        printf '%s' "$commit_json" > "$ctmp" && mv "$ctmp" "$COMMIT_PENDING"
        if printf '%s' "$commit_json" | python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" commit >/dev/null 2>&1; then
          rm -f "$COMMIT_PENDING"
        else
          echo "[$(now)] COMMIT_FAILED: payload kept in $COMMIT_PENDING, retried next round" >> "$POLLLOG"
          trim_poll_log
        fi
      fi
    }
    if [ "${cnt:-0}" -gt 0 ] 2>/dev/null; then
      if [ "$DRY" = "1" ]; then
        # dry 不落盘、不提交：消息留给正式轮询去拉，避免重复入队
        silent "dry-run: $cnt messages fetched, left uncommitted for live poll"
      else
        # 原子写入 pending（只写 msgs，保持队列文件既有格式）。pending 里
        # 可能还有本轮带着 poll 的上一批：持队列锁把新消息接在后面，与
        # claim 互斥——不持锁时 worker 恰好认领，旧批会被写回 pending 再
        # 答一遍。拿不到锁就不 commit，这批下一轮重拉。
        exec 7>"$STATE_DIR/ilink-queue.lock"
        if ! flock -w 30 7; then
          silent "queue lock busy, $cnt messages left uncommitted for the next poll"
          exit 0
        fi
        tmp="$PENDING.tmp.$$"
        if [ -s "$PENDING" ]; then
          printf '%s' "$out" | jq -c --slurpfile p "$PENDING" '{msgs: ($p[0].msgs + .msgs)}' > "$tmp"
        else
          printf '%s' "$out" | jq -c '{msgs}' > "$tmp"
        fi
        mv "$tmp" "$PENDING"
        total=$(queue_count "$PENDING")
        exec 7>&-
        log "received $cnt messages"
        # Light the "typing..." indicator the moment the batch lands (owner
        # only) instead of waiting for the worker agent to boot — the worker's
        # own typing start then just acts as a keep-alive refresh.
        light_typing "$PENDING"
        do_commit
        rec_extra=""
        if [ -n "$recovered" ]; then rec_extra=",\"recovered\":$recovered"; fi
        sig=$(batch_sig "$PENDING")
        # 收件延迟：poll_secs 是这次长轮询 hold 了多久，lag_secs 是最早
        # 一条从主人发出到落盘过了多久。lag 远大于 poll 说明消息在本轮
        # 开始前就在服务端等着：各轮首尾相接，多半是上一轮长轮询没把它
        # 带回来；两者都大且接近，说明本轮服务端有消息也没提前返回。
        # create_time_ms 用的是服务端时钟，实测比本机快 1~2 秒
        lag=$(printf '%s' "$out" | jq --argjson now "$NOW" \
          '[.msgs[]?.create_time_ms | numbers | select(. > 0)]
           | if length > 0 then [$now - (min / 1000 | floor), 0] | max else empty end' \
          2>/dev/null || true)
        lag_field=""
        case "$lag" in ''|*[!0-9]*) ;; *) lag_field=",\"lag_secs\":$lag" ;; esac
        # arrive 记本轮新消息的首条 id（pending 可能还挂着上一批，队首是它的）
        arr_sig=$(printf '%s' "$out" | jq -r '.msgs[0].message_id // empty | tostring' 2>/dev/null || true)
        hook_event arrive "{\"sig\":\"$arr_sig\",\"count\":$cnt,\"poll_secs\":$((NOW - poll_t0))$lag_field}"
        if [ -z "$recovered" ] && warm_alive; then
          # 守候中的 worker 会在数秒内自己认领；带 recovered 时照常 wake，
          # 免得恢复通知被吞掉（多叫的 worker 由 claim 返回 CLAIM_BUSY 退出）
          silent "warm worker on duty"
        elif [ -z "$recovered" ] && claim_active; then
          silent "worker holds an open claim, it merges the new batch before sending"
        elif [ -z "$recovered" ] && ago=$(woken_ago "$sig") \
             && [ "$ago" -lt "$REWAKE_SECS" ]; then
          # 接在刚叫起、还没认领的那批后面：签名是首条 id，不变，不再叫一个
          silent "woken ${ago}s ago for this batch, waiting for a claim"
        else
          mark_woken "$sig"
          hook_event wake "{\"reason\":\"new_wechat_messages\",\"sig\":\"$sig\",\"count\":$total}"
          wake "new_wechat_messages" "{\"count\":$total,\"pending_file\":\"$PENDING\"$rec_extra}"
        fi
      fi
    elif [ -n "$recovered" ]; then
      do_commit
      if [ "$DRY" != "1" ]; then
        wake "recovered" "$recovered"
      else
        silent "dry-run: would wake recovered"
      fi
    else
      do_commit
      silent "no new messages${held:+; $held}"
    fi
  fi
fi
