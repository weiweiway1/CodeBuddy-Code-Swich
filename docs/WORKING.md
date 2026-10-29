# 工作原理 / How It Works

本文记录 `cbs` 的实现原理与关键结论。全部结论均在本机实测得出（Ubuntu 22.04 / WSL2、
CodeBuddy Code CLI 2.164.0），实测方法与证据一并给出，便于他人复核与扩展。

All conclusions below were verified empirically on Ubuntu 22.04 (WSL2) with
CodeBuddy Code CLI 2.164.0. The verification method is documented alongside each
finding so others can reproduce and extend it.

[中文](#中文) | [English](#english)

---

## 中文

### 1. 凭证存在哪里

CodeBuddy Code CLI 在 Linux 上的凭证是**明文 JSON**：

```
~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info
```

权限 600，结构：

```json
{
  "account": { "uid": "...", "nickname": "...", "uin": "...", "phoneNumber": "...",
               "loginChannels": ["openplatform"], "...": "..." },
  "auth":    { "accessToken": "<JWT>", "refreshToken": "<JWT>",
               "expiresAt": 1794058398162, "refreshExpiresAt": 1794231198162,
               "tokenType": "Bearer", "domain": "www.codebuddy.cn",
               "sessionState": "...", "scope": "...", "lastRefreshTime": 0 },
  "accounts": [ { "...": "..." } ]
}
```

**实测方法**：`strace -f -e trace=openat codebuddy -p "hi"`，可见 CLI 启动时
`openat` + `read` 该文件两次（每次 3403 字节）。

**关键点**

- 这是 CLI **自己**的约定，不是 VS Code 插件的遗留：干净 HOME 下运行一次 CLI，
  它就会创建 `CodeBuddyExtension/Data/Public/auth/` 目录
- 该文件被 **CLI 与 VS Code CodeBuddy 插件共享**——切换 CLI 账号会同时改变插件的登录账号
- 域名（`auth.domain`）决定档位：国内版 `www.codebuddy.cn`、国际版 `www.codebuddy.ai`

### 2. 切换账号 = 原子替换 5 个文件

单纯替换凭证文件不够。区域档位还涉及另外 4 处，必须同步：

| 文件 | 操作 | 国内版 | 国际版 |
|---|---|---|---|
| `…/Tencent-Cloud.coding-copilot.info` | 写 | — | — |
| `~/.codebuddy/settings.json` | 改 `env` | `CODEBUDDY_INTERNET_ENVIRONMENT=internal`，删除 `CODEBUDDY_BASE_URL` | `=public` + `CODEBUDDY_BASE_URL=https://www.codebuddy.ai/v2` |
| `~/.codebuddy/local_storage/entry_3bab….info` | 写 | `"internal"` | `"public"` |
| `~/.codebuddy/local_storage/entry_933d….info` | 写 | `"https://copilot.tencent.com"` | `"https://www.codebuddy.ai"` |
| `~/.codebuddy/local_storage/entry_604f….info` | **删** | 产品包缓存，按档位区分，必须丢弃 | 同左 |

三个 `entry_*.info` 的文件名是 `entry_<md5(key)>.info`，key 由 CLI 写死，因此文件名固定。

**`/v2` 后缀是必须的**（来自 workbuddy-switch 源码注释，实测印证）：
`CODEBUDDY_BASE_URL` 若写成门户根地址 `https://www.codebuddy.ai`，CLI 会 POST 到
`/chat/completions`，被 nginx 拒绝（405）。而 local_storage 缓存里的 endpoint
**不带** `/v2`（那是门户根地址，用途不同）。两处不能混淆。

**原子性**：所有写入走同目录临时文件 → `fchmod` → `write` → `fsync` → `rename` →
`fsync(dir)`。`rename` 是原子的，CLI 侧已打开的 fd 仍指向旧 inode，不存在写坏一半的中间态。

### 3. CLI 运行时的三个行为（最容易踩坑的部分）

CLI 日志（`~/.codebuddy/logs/<日期>/*.log`）的 `AuthenticationManager` 会记录每一次
账号变更：`userId changed: <旧uid> -> <新uid>`。实测记录揭示了三个行为：

**行为一：进程会跟随磁盘凭证变化。**
切换 `settings` 后，运行中的 CLI 会在下一次检查时切到新账号——日志里同一个 pid
连续出现 6 次 `userId changed`。所以 `cbs use` 是**即时生效**的，不需要重启进程，
也不会丢对话历史。

**行为二：每个进程内存里各持一份凭证副本，续期时写回磁盘。**
这是"切了又被顶回去"的根因：

```
t0    cbs use <账号B>        → 磁盘 = B
t0+ε  进程 A 尚未察觉磁盘变化，内存里还是 A 的 token
t1    进程 A 触发续期 → 用内存里的 A refreshToken 换新 token → 写回磁盘
      结果：磁盘 = A，刚做的切换被覆盖
```

实测观测到：切到某账号后约 6 分钟，磁盘凭证被改回另一个账号，`lastRefreshTime`
随之更新——典型的续期写回。

**应对**：切换前退出其它 CLI 进程，只留一个。`cbs use` 会在切换后 3 秒复核磁盘，
被改回就明确告警。

**行为三：额度耗尽不会触发换号，token 失效才会。**
额度型失败（quota exceeded）与鉴权失败（401）是两回事：前者 CLI 不会换账号，
后者会走恢复流程（试内存里的 refreshToken，不行则重读磁盘凭证）。这解释了一个
常见现象：切到新账号后，老会话在额度耗尽时依旧报错；而 token 恰好同时失效时，
老会话会"自己"恢复成磁盘上的账号。

### 4. Token 生命周期与轮换

| | 有效期 | 说明 |
|---|---|---|
| `accessToken` | 28–30 天 | JWT 里 `exp` |
| `refreshToken`（登录时） | 30 天 | |
| `refreshToken`（刷新后） | **60 天** | 实测刷新响应 `refreshExpiresIn=5183999` |

**refreshToken 是轮换的**：每次刷新都会换发新的并**作废旧的一族 token**（包括纸面
尚未过期的旧 accessToken）。这带来两个后果：

1. **纸面 `exp` 不可信**。服务端可以随时作废整族 token，JWT 上的日期仍在。
   实测有账号纸面显示"还有 8 天过期"，实际 accessToken 与 refreshToken 皆 401。
2. **快照式账号库存档会静默失效**。库存档存的是导入时刻的 token 副本；若该账号
   在别处被续期过，库存档里的副本即成死 token。

### 5. 保活：keepalive 原理

刷新接口（取自 CLI 二进制内嵌实现，实测验证）：

```
POST {host}/v2/plugin/auth/token/refresh
Headers: X-Refresh-Token: <refreshToken>
         X-Auth-Refresh-Source: plugin
         X-User-Id: <uid>
         X-Domain: <domain>
Body:   {}（空）
响应:   data 即新 auth（accessToken / refreshToken / expiresIn / refreshExpiresIn）
```

刷新响应只给 `expiresIn`/`refreshExpiresIn`，缺 `expiresAt`/`refreshExpiresAt`/
`lastRefreshTime`，需自行按 CLI 行为补全。

**因为 refreshToken 轮换，刷新后必须把新 refreshToken 存回库存档**，否则下一轮就
用不了了。存回后，该账号的有效期被重置为 30 天 access + 60 天 refresh——
只要每 60 天内至少刷一次，账号即可无限续命，不再需要重新登录。

**关键约束**：当前生效账号必须跳过。它由 CLI 自己续期（内存里持有新 token），
用库存档里的旧副本刷新反而会作废现场凭证的 token 族，导致 CLI 下次续期失败被登出。

实测还发现一个意外：accessToken 已被作废（积分接口 401）但 refreshToken 尚存的
账号，**可以靠 keepalive 救活，无需重新登录**。

### 6. 积分查询

两个服务端接口，实测可用：

| 接口 | 用途 |
|---|---|
| `POST /billing/meter/get-user-resource-summary` | 套餐聚合余额（无到期时间） |
| `POST /v2/billing/meter/get-user-resource` | **额度券明细**，每条自带余额与到期时间 |

`cbs` 以第二个接口为准。

**核心概念：每条记录是一张独立的额度券，不是套餐。** 同一账号可能持有十几张券
（`PackageCode` 相同，`DeductionEndTime` 各异），各有余额与到期日。若按
`PackageCode` 合并成"一个套餐"，会丢掉更早到期的券——实测中一个账号因此把
"最近到期 10-14"错报成"11-10"。正确做法是逐条处理，按到期时间升序排列，
"最近到期"取**余额 > 0 的未过期券**中最早的一张。

**过期券的余额不可用**。接口会返回已过期但仍挂着余额的券，须单独标注，不计入
可用额度。实测某账号服务端显示 251.25 credits，实际可用为 0——那笔余额挂在
早已过期的裂变包上。

**两个接口坑**：

1. **必须带浏览器 `User-Agent`**。缺失会被服务端风控拦成 403（所有国内账号全失败，
   错误信息只有 403，看不出原因）
2. **`X-Domain` 必须与账号 `domain` 一致**，否则网关按一致性拒绝

### 7. 用积分接口验活

因为积分查询本来就要发一次请求，它的返回状态可以直接用来判断 token 真伪：

| 积分接口返回 | 含义 |
|---|---|
| 200 | token 有效 |
| 401 / 403 | **token 已被服务端作废**，纸面 exp 再长也不可信 |

`cbs ls` 的 Token 列据此显示"有效 / 已失效(需重登)"，而不是纸面日期。
`cbs use` 与 `--dry-run` 也会先验活，避免切过去才发现登录不上。

### 8. 环境变量会覆盖配置

进程环境里的这两个变量优先级高于 `settings.json`，会导致切换"看起来没生效"：

- `CODEBUDDY_AUTH_TOKEN` —— 直接指定 token，覆盖整个账号库
- `CODEBUDDY_INTERNET_ENVIRONMENT` —— 覆盖区域档位

注意：CLI 启动后会把 `CODEBUDDY_INTERNET_ENVIRONMENT` 注入给子进程，所以在一个
codebuddy 会话里 `echo $CODEBUDDY_INTERNET_ENVIRONMENT` 会有值——那是父进程注入的，
不是用户设置的。新开的终端通常没有。

### 9. 并发与回滚

- **锁**：`~/.cbs/.lock` + `fcntl.flock(LOCK_EX|LOCK_NB)`，覆盖切换的关键区间，
  抢不到即退出（防止两个终端同时切换）
- **备份**：每次切换前全量备份上述 5 个文件 + `manifest.json`（记录哪些文件原本存在，
  便于回滚时判断该删还是该写），保留最近 10 份
- **回滚**：备份之后的任何异常（含 Ctrl-C）按 manifest 逆序恢复，原本不存在的文件
  则删除。原子写保证每个文件要么旧要么新，不存在半写状态

### 10. 关键路径速查

| 用途 | 路径 |
|---|---|
| 凭证（账号库） | `~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info` |
| CLI 配置 | `~/.codebuddy/settings.json` |
| 区域/endpoint/产品包缓存 | `~/.codebuddy/local_storage/entry_{3bab…,933d…,604f…}.info` |
| CLI 运行日志（含 `userId changed`） | `~/.codebuddy/logs/<日期>/*.log` |
| 账号库存档 | `~/.cbs/accounts.json`（含明文 token，600） |
| 切换前备份 | `~/.cbs/backups/<UTC时间戳>-<别名>/` |

---

## English

### 1. Where credentials live

On Linux, CodeBuddy Code CLI keeps credentials in **plaintext JSON**:

```
~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info
```

Mode 600. Shape: `{account: {...}, auth: {accessToken, refreshToken, expiresAt,
refreshExpiresAt, domain, sessionState, scope, lastRefreshTime}, accounts: [...]}`.

**How verified**: `strace -f -e trace=openat codebuddy -p "hi"` shows the CLI opening
and reading this file twice at startup (3403 bytes each).

**Key points**

- This is the CLI's **own** convention, not a VS Code plugin leftover: with a clean
  HOME, running the CLI once creates `CodeBuddyExtension/Data/Public/auth/`
- The file is **shared by the CLI and the VS Code plugin** — switching the CLI
  account also changes the plugin's login
- `auth.domain` selects the tier: `www.codebuddy.cn` (CN) or `www.codebuddy.ai` (intl)

### 2. Switching = atomically replacing 5 files

Replacing the credential file alone is not enough. The region tier lives in four more
places that must be synced:

| File | Operation | CN | Intl |
|---|---|---|---|
| `…/Tencent-Cloud.coding-copilot.info` | write | — | — |
| `~/.codebuddy/settings.json` | edit `env` | `CODEBUDDY_INTERNET_ENVIRONMENT=internal`, drop `CODEBUDDY_BASE_URL` | `=public` + `CODEBUDDY_BASE_URL=https://www.codebuddy.ai/v2` |
| `~/.codebuddy/local_storage/entry_3bab….info` | write | `"internal"` | `"public"` |
| `~/.codebuddy/local_storage/entry_933d….info` | write | `"https://copilot.tencent.com"` | `"https://www.codebuddy.ai"` |
| `~/.codebuddy/local_storage/entry_604f….info` | **delete** | product bundle cache, tier-specific | same |

The three `entry_*.info` filenames are `entry_<md5(key)>.info`; the keys are hard-coded
by the CLI, so the names are stable.

**The `/v2` suffix is mandatory** (from workbuddy-switch's source comments, confirmed
empirically): setting `CODEBUDDY_BASE_URL` to the bare portal root makes the CLI POST
to `/chat/completions`, which nginx rejects with 405. The local_storage endpoint cache
carries **no** `/v2` (that one is the portal root, used differently). Don't mix them up.

**Atomicity**: every write is same-directory temp file → `fchmod` → `write` → `fsync` →
`rename` → `fsync(dir)`. `rename` is atomic and an already-open fd keeps pointing at the
old inode, so there is never a half-written state.

### 3. Three runtime behaviours of the CLI (the tricky part)

The CLI log (`~/.codebuddy/logs/<date>/*.log`) records every account change in
`AuthenticationManager`: `userId changed: <old uid> -> <new uid>`. Empirical records
reveal three behaviours:

**Behaviour 1 — processes follow on-disk credential changes.**
After a switch, a running CLI picks up the new account on its next check — the same pid
logged 6 consecutive `userId changed` entries. So `cbs use` takes effect **immediately**;
no process restart, and no conversation history is lost.

**Behaviour 2 — each process holds its own credential copy and writes it back on refresh.**
This is why a switch can be silently undone:

```
t0    cbs use <accountB>     → disk = B
t0+ε  process A hasn't noticed yet; memory still holds A's token
t1    process A refreshes → swaps its in-memory refreshToken for a new one → writes disk
      result: disk = A, the switch is overwritten
```

Observed: about 6 minutes after switching to an account, the on-disk credential was
replaced by a different one with an updated `lastRefreshTime` — a refresh write-back.

**Mitigation**: exit other CLI processes before switching, leaving only one. `cbs use`
re-checks the disk 3 seconds after switching and warns if it was overwritten.

**Behaviour 3 — quota exhaustion does not switch accounts; token failure does.**
A quota error and an auth error are different: the CLI won't change accounts on quota
exhaustion, but on 401 it enters recovery (try the in-memory refreshToken, else re-read
the credential file). This explains a common observation: after switching, an old
session still errors on quota exhaustion — but if the token happened to be invalid too,
that old session "recovers" into whatever account is on disk.

### 4. Token lifetime and rotation

| | Lifetime | Note |
|---|---|---|
| `accessToken` | 28–30 days | JWT `exp` |
| `refreshToken` (at login) | 30 days | |
| `refreshToken` (after refresh) | **60 days** | measured `refreshExpiresIn=5183999` |

**refreshTokens rotate**: each refresh issues a new one and **invalidates the old
token family** (including an accessToken whose paper `exp` hasn't passed). Two
consequences:

1. **Paper `exp` is unreliable.** The server can invalidate a family at any time while
   the JWT date still looks fine. Observed: an account showing "8 days left" had both
   accessToken and refreshToken returning 401.
2. **Snapshot-style account stores fail silently.** A stored snapshot is a copy from
   import time; if the account was refreshed elsewhere, the snapshot is dead.

### 5. keepalive: how it works

Refresh endpoint (taken from the CLI's bundled implementation, verified empirically):

```
POST {host}/v2/plugin/auth/token/refresh
Headers: X-Refresh-Token: <refreshToken>
         X-Auth-Refresh-Source: plugin
         X-User-Id: <uid>
         X-Domain: <domain>
Body:   {} (empty)
Response: data holds the new auth (accessToken / refreshToken / expiresIn / refreshExpiresIn)
```

The response omits `expiresAt`/`refreshExpiresAt`/`lastRefreshTime`; they must be
recomputed the way the CLI does.

**Because refreshTokens rotate, the new refreshToken must be written back to the store**
or the next round fails. After that the account's lifetime resets to 30 days access +
60 days refresh — refreshing at least once every 60 days keeps an account alive
indefinitely, with no re-login.

**Critical constraint**: the currently active account must be skipped. The CLI refreshes
it on its own (holding the new token in memory); refreshing from a stale store copy
would invalidate the live token family and get the CLI logged out.

Empirically, a pleasant surprise: an account whose accessToken was already void
(credits API 401) but whose refreshToken still worked could be **rescued by keepalive
without re-login**.

### 6. Credits queries

Two server endpoints, both verified working:

| Endpoint | Purpose |
|---|---|
| `POST /billing/meter/get-user-resource-summary` | aggregated package balance (no expiry) |
| `POST /v2/billing/meter/get-user-resource` | **voucher details**, each with balance and expiry |

`cbs` relies on the second.

**Core concept: each record is an independent credit voucher, not a package.** One
account may hold a dozen vouchers (same `PackageCode`, differing `DeductionEndTime`),
each with its own balance and expiry. Collapsing them by `PackageCode` into "one
package" drops the earlier-expiring ones — this made one account report "expires
2026-11-10" when the true nearest expiry was 2026-10-14. Handle records individually,
sort by expiry, and take "nearest expiry" from **unexpired vouchers with a non-zero
balance**.

**Balances on expired vouchers are not usable.** The API returns expired vouchers that
still carry a balance; report them separately and exclude them from available credits.
One account showed 251.25 credits server-side while the usable amount was 0 — that
balance sat on a long-expired promo package.

**Two API gotchas**:

1. **A browser `User-Agent` is required** — without it the server's risk control
   returns 403 for every account, and the error message alone gives no clue
2. **`X-Domain` must match the account's `domain`** or the gateway rejects on consistency

### 7. Using the credits endpoint as a liveness probe

Since the credits query is sent anyway, its status doubles as a token truth check:

| Credits response | Meaning |
|---|---|
| 200 | token valid |
| 401 / 403 | **token voided by the server**, paper `exp` is meaningless |

`cbs ls` shows "valid / void (re-login needed)" in its Token column accordingly, rather
than a paper date. `cbs use` and `--dry-run` probe first, so you find out before the
switch instead of after.

### 8. Environment variables override config

These take precedence over `settings.json` and can make a switch look like it failed:

- `CODEBUDDY_AUTH_TOKEN` — pins the token, overriding the account store
- `CODEBUDDY_INTERNET_ENVIRONMENT` — overrides the region tier

Note: the CLI injects `CODEBUDDY_INTERNET_ENVIRONMENT` into child processes, so
`echo $CODEBUDDY_INTERNET_ENVIRONMENT` inside a codebuddy session shows a value the
user never set. A freshly opened terminal usually has none.

### 9. Concurrency and rollback

- **Lock**: `~/.cbs/.lock` + `fcntl.flock(LOCK_EX|LOCK_NB)` over the critical section of
  a switch; a second terminal exits instead of racing
- **Backup**: every switch backs up all 5 files plus a `manifest.json` recording which
  files existed (so restore knows what to delete vs. write); 10 snapshots are kept
- **Rollback**: any failure after the backup (including Ctrl-C) restores files in
  reverse manifest order, deleting files that didn't exist before. Atomic writes mean
  each file is either old or new — never half-written

### 10. Path reference

| Purpose | Path |
|---|---|
| Credential store | `~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info` |
| CLI config | `~/.codebuddy/settings.json` |
| Region/endpoint/product caches | `~/.codebuddy/local_storage/entry_{3bab…,933d…,604f…}.info` |
| CLI runtime log (has `userId changed`) | `~/.codebuddy/logs/<date>/*.log` |
| Account store | `~/.cbs/accounts.json` (plaintext tokens, mode 600) |
| Pre-switch backups | `~/.cbs/backups/<UTC timestamp>-<alias>/` |
