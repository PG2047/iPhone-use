# 设备租约约定（多个对话共用一台 iPhone）

同一台 Mac 上的多个 Codex 对话各自运行一个 iPhone Use MCP 进程，却共用一台手机的屏幕、前台 App 和输入焦点。本约定让它们像使用共享打印机一样轮流使用手机：空闲即用，占用即排队，连续操作期间保留控制权，用完归还。实现只依赖文件和 `flock`，没有常驻服务、socket 或数据库，任何语言写的工具（例如 KVM 控制器）都可以按同一约定加入队列。

实现文件：`server/device_lease.py`。本文描述磁盘上的约定，供其他工具实现兼容者。

## 目录

默认目录按物理设备划分：`~/.local/share/iphone-use/leases/<udid>/`，`udid` 来自 state 目录里 `config.json` 的 `udid` 字段。两个 state 目录指向同一台手机就共用一把锁；两台手机互不阻塞。环境变量 `IPHONE_USE_LEASE_DIR` 可以指定其他目录；尚未配置设备时使用 `<state_dir>/lease/`。

| 文件 | 作用 |
|---|---|
| `device.lock` | 租约本体。持有者进程对它执行 `flock(LOCK_EX)` 并保持文件打开；进程退出时内核自动释放。文件内容无意义。 |
| `device-owner.json` | 当前或最近一位持有者的信息，原子写入（先写临时文件再 `rename`）。 |
| `device-queue/<ns>-<pid>.json` | 排队票据，每个等待进程一张。文件名前 20 位是补零的纳秒时间戳，字典序即先来后到。 |

所有文件权限 `0600`，目录 `0700`。

## 持有者记录 `device-owner.json`

```json
{"format": 1, "pid": 12345, "label": "山姆购物车", "transport": "wda",
 "generation": 7, "acquired_ts": 1761480000.1, "last_call_ts": 1761480042.8,
 "last_tool": "pua_tap", "released_at": 1761480103.0, "release_reason": "idle"}
```

- `pid`：持有者进程。进程已死且没有 `released_at`，说明它崩溃了。
- `label`：给人看的任务标签，由持有者自报（iPhone Use 用 `pua_device(action="claim", label=...)`，否则取最近启动的 App）。
- `transport`：通道名。iPhone Use 写 `wda`；其他工具写自己的名字（如 `kvm`）。
- `generation`：每次成功获取加一，用来判断自己上次持有之后是否有别人用过手机。
- `acquired_ts` / `last_call_ts`：Unix 时间戳（秒）。`last_call_ts` 由持有者每次调用后更新，排队方据此显示「多久没动」。
- `released_at` / `release_reason`：正常归还时写入（`explicit` / `idle` / `closed`）。存在该字段 = 无人持有。

## 票据 `device-queue/<ns>-<pid>.json`

```json
{"format": 1, "pid": 23456, "label": "联通话费", "transport": "kvm",
 "enqueued_ts": 1761480050.0, "refreshed_ts": 1761480078.5}
```

票据有效的条件：`pid` 仍存活，且 `refreshed_ts` 距今不超过 TTL（默认 30 秒）。等待进程在轮询时每次刷新 `refreshed_ts`；暂时退出轮询但仍想要设备的进程，由后台线程继续刷新最多 180 秒。无效票据任何参与者都可以删除。

## 规则

1. **获取**：写入自己的票据；循环：删除无效票据 → 按文件名排序，只有队头可以尝试 `flock(LOCK_EX | LOCK_NB)` → 成功后删除自己的票据并写 `device-owner.json`（`generation` = 上一位 + 1）；失败则刷新票据、等待 0.25 秒再试。等待预算默认 40 秒（Codex 工具调用 60 秒超时之内），超时向调用方返回「排队中」错误，附持有者标签、空闲秒数和自己的位置，并保留票据。
2. **非阻塞尝试**（例如用户在预览面板上的点击）：队列非空时不插队，直接返回「被占用」。
3. **持有**：每次调用后更新 `last_call_ts`。持有者活跃时不会被抢占；公平性来自空闲归还、显式归还和可见性。
4. **归还**：显式归还、空闲超过 60 秒（可配）或进程退出。正常归还要先写 `released_at` 再解锁；进程崩溃时内核解锁但没有 `released_at`。
5. **换手**：新持有者发现上一位 `pid` 不是自己时，先要求一次「新观察」（读取屏幕）再允许动作；若上一位没有 `released_at`（崩溃），再多等 3 秒让可能在途的一条命令结束。不需要重启手机。
6. **感知**：任何参与者读取 `device-owner.json` 和 `device-queue/` 即可知道谁在用、在做什么、排了几个人。

## iPhone Use 的可配置项

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `IPHONE_USE_IDLE_RELEASE_SECONDS` | 60 | 无手机调用多久后自动归还 |
| `IPHONE_USE_WAIT_SECONDS` | 40 | 一次调用内最多排队等待多久 |
| `IPHONE_USE_HANDOVER_SETTLE_SECONDS` | 3 | 上一位崩溃时接手前的等待 |
| `IPHONE_USE_TICKET_TTL_SECONDS` | 30 | 票据不刷新多久算失效 |
| `IPHONE_USE_QUEUE_HOLD_SECONDS` | 180 | 返回「排队中」后替本进程继续刷新票据多久 |
| `IPHONE_USE_LEASE_DIR` | 按 udid | 锁目录 |

## 其他工具如何加入

用相同目录、相同文件名和字段：获取时写票据、按队头尝试 `flock`、成功后写持有者记录并把 `transport` 写成自己的名字；每次动作后更新 `last_call_ts`；结束时写 `released_at` 再解锁。同一个对话里从 iPhone Use 切到另一工具属于两个进程，先让 iPhone Use `pua_device(action="release")`，否则对方要等空闲归还。
