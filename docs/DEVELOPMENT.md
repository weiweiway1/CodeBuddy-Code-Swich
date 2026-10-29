# 开发记录 / Development Notes

本文记录 `cbs` 的开发过程、每个功能背后的实测依据，以及开发中踩过的坑——尤其是那些
**看起来正确但实际错误**的判断，避免后来者重蹈覆辙。

Records how `cbs` was built, the empirical basis for each feature, and the mistakes
made along the way — especially wrong assumptions that looked right.

[中文](#中文) | [English](#english)

---

## 中文

### v1 — 账号切换（基础）

**目标**：在 Linux 上切换 CodeBuddy Code CLI 的登录账号。

**关键突破**：CodeBuddy Code CLI 没有 `login` 子命令，凭证存在一个**明文 JSON** 文件里。
用 `strace` 抓出这个路径后，整个方案就简化了——不需要改 settings 注入 token
（workbuddy-switch 在 macOS/Linux 上的做法），直接原子替换文件即可。

**实现要点**

- `atomic_write()`：同目录临时文件 → `fchmod` → `write` → `fsync` → `rename` → `fsync(dir)`
- 区域档位同步 5 个文件（详见 [WORKING.md](./WORKING.md) 第 2 节）
- 切换前全量备份 + `manifest.json`，失败自动回滚
- `fcntl.flock` 防并发切换
- 切换时自动保存现场凭证——否则切走后，CLI 续期得到的新 token 会随现场文件一起被覆盖

**踩的坑**

- 最初以为凭证在 `~/.codebuddy/` 下，翻遍整个目录没找到。真相是它在
  `~/.local/share/CodeBuddyExtension/`——**CLI 与 VS Code 插件共享**的账号库，
  路径名里带 "Extension" 极具迷惑性
- 假账号测试时直接把伪造凭证写进了真实文件，覆盖掉用户的登录态。
  **教训：涉及用户凭证的验证一律先问，测试用隔离环境**
- `install.sh` 的 `CBS_HOME` 只覆盖了安装目录，软链路径写死 `~/.local/bin/cbs`，
  测试时把真实软链指到了临时目录。补上 `CBS_BIN_DIR` 后可完全隔离测试

### v2 — 积分查看

**目标**：显示各账号的可用积分与到期时间。

**接口发现**：CLI 二进制里没有积分接口（搜到的都是内部 daemon 路径）。真实接口在
服务端，从 workbuddy-switch 的 `credits.rs` 拿到规格，再用本机账号逐个验证。

**三个关键结论**（都是踩出来的）

1. **必须带浏览器 `User-Agent`**。Python 默认 UA 被风控拦成 403，四个账号全失败，
   错误信息只有 "HTTP 403" 毫无线索。加上真实 UA 立刻通了
2. **每条记录是一张独立额度券，不是套餐**。同一个 `PackageCode` 有 19 条记录、
   各自到期日不同。我最初按 `PackageCode` 合并成"一个套餐"并取第一条——第一条恰好
   是已过期的，于是把有效套餐标成过期。修了两次才对：先改成"取最晚到期的一条"，
   最后才想通**根本不该合并**，应该逐条展示
3. **过期券的余额不可用**。接口会返回已过期但仍挂余额的券，混进"可用积分"里严重
   误导（某账号服务端显示 251.25，实际可用 0）。必须单列标注

**踩的坑**

- 表格列宽用 `%-14s` 对齐，中文双宽字符导致错位。写了 `display_width()` 按东亚
  字符宽度计算
- `cbs ls` 逐行刷新（每查完一行刷一行）时行号随完成顺序漂移，覆写错行。改成
  整表重绘才稳定
- 重构两段式渲染时，把非终端（管道）分支的结果表输出弄丢了——管道里只剩表头。
  管道环境无法原地刷新，必须等查询完成后输出最终表

### v3 — 保活（keepalive）

**背景问题**：快照式账号库存档会静默失效。某账号纸面显示"还有 8 天过期"，
实际 accessToken 与 refreshToken 全被服务端作废，完全登录不上。

**根因**：refreshToken 是**轮换**的。每次刷新换发新 token 并作废旧的一族——
包括纸面尚未过期的 accessToken。所以库存档里的副本会随时间成为死 token。

**接口**：从 CLI 二进制内嵌实现里挖出（`strings` + 上下文分析）：

```
POST /v2/plugin/auth/token/refresh   Header: X-Refresh-Token
```

响应只给 `expiresIn`/`refreshExpiresIn`，缺 `expiresAt`/`refreshExpiresAt`，
需按 CLI 行为自行补全。

**实现**

- `cbs keepalive [--all|<别名>]`：刷新并把新凭证存回库存档
- **当前生效账号必须跳过**——它由 CLI 自己续期，用库存档旧副本刷新反而会作废
  现场 token 族导致 CLI 被登出
- 刷新后 refreshToken 寿命从 30 天变成 **60 天**（实测），所以每 60 天刷一次即可
  无限续命
- 意外收获：accessToken 已死但 refreshToken 尚存的账号，**能靠 keepalive 救活，
  无需重新登录**

### v4 — 用接口验活，取代纸面日期

**问题**：`cbs ls` 的 Token 列显示纸面 JWT `exp`，会骗人。改成复用积分查询的返回——
401/403 即判定 token 已被作废，显示"已失效(需重登)"。零额外请求。

**扩展**：`cbs use` 与 `--dry-run` 也先验活，避免切过去才发现登录不上。

**踩的坑（重要）**：我曾断言"切换后必须重启 CLI 进程才能生效"。日志里的
`userId changed` 记录直接推翻了它——**CLI 会跟随磁盘凭证变化，切换即时生效，
对话历史也不丢**。这个错误认知导致我给用户建议了多余的操作步骤。

更正后的认知：

| 说法 | 对错 |
|---|---|
| 切换后必须重开会话 | ❌ 即时生效 |
| 切换会丢对话 | ❌ 不会 |
| 老会话会烧旧账号积分 | ❌ 会跟随切换 |
| 磁盘放死 token 会 401 | ✅ 这才是真问题 |
| 老 token 续期回写会覆盖切换 | ⚠️ 仍存在 |

**另一个实测发现**：存活进程的续期回写会覆盖刚做完的切换（某账号切过去 6 分钟后
磁盘被改回另一个账号）。因此 `cbs use` 改为切换后 3 秒复核磁盘，被改回就告警。

### 工程教训

1. **先验证再下结论**。三次凭"合理推断"给出的解释（到期时间取哪条、老会话是否
   换 token、当前用的是谁的账号）都被实测推翻。日志和 strace 比推理可靠
2. **用户连续三次纠正同一个方向**（到期时间不对、最近到期不对、当前用谁的账号），
   根因是我没先搞清楚数据结构就动手写
3. **涉及用户凭证的操作一律先问**。测试用 dry-run 或隔离 HOME
4. **文档要写实测依据**。WORKING.md 里每个结论都附了验证方法，便于他人复核

### 已知限制

- 仅在 Ubuntu 22.04 (WSL2) + CodeBuddy Code CLI 2.164.0 上实测通过
- 积分接口的 401/403/404 错误路径未专门测试（所有测试账号均返回 200）
- 只管理 CLI，不管理 WorkBuddy 桌面端、VS Code 插件、JetBrains 插件
  （但切换会同时影响 VS Code 插件）
- 非 WSL 的原生 Linux 环境未验证

---

## English

### v1 — Account switching (foundation)

**Goal**: switch the CodeBuddy Code CLI login account on Linux.

**Key breakthrough**: the CLI has no `login` subcommand, and its credential sits in a
**plaintext JSON** file. Once `strace` revealed that path, the design simplified — no
need for settings-based token injection (workbuddy-switch's macOS/Linux approach);
just replace the file atomically.

**Implementation**

- `atomic_write()`: same-dir temp file → `fchmod` → `write` → `fsync` → `rename` → `fsync(dir)`
- Region tier synced across 5 files (see [WORKING.md](./WORKING.md) §2)
- Full backup + `manifest.json` before each switch, automatic rollback on failure
- `fcntl.flock` to prevent concurrent switches
- Auto-save the live credential on switch — otherwise the CLI's refreshed token is
  lost along with the overwritten file

**Mistakes**

- Assumed credentials lived under `~/.codebuddy/`; searched the whole tree with no
  luck. They live in `~/.local/share/CodeBuddyExtension/` — an account store
  **shared with the VS Code plugin**, and the "Extension" in the path is thoroughly
  misleading
- A fake-account test wrote fabricated credentials into the real file, wiping the
  user's login. **Lesson: always ask before touching user credentials; test in
  isolation**
- `install.sh`'s `CBS_HOME` only overrode the install dir while the symlink path was
  hardcoded to `~/.local/bin/cbs`, so the test repointed the real symlink at a temp
  directory. Adding `CBS_BIN_DIR` made isolated testing possible

### v2 — Credits display

**Goal**: show available credits and expiry per account.

**Interface discovery**: the CLI binary contains no credits endpoint (only internal
daemon paths). The real endpoints came from workbuddy-switch's `credits.rs`, then were
verified one by one against local accounts.

**Three key findings** (all earned the hard way)

1. **A browser `User-Agent` is mandatory** — Python's default UA gets 403 from risk
   control, all four accounts failed, and the error message alone gave nothing away
2. **Each record is an independent voucher, not a package** — one `PackageCode` had 19
   records with differing expiry dates. I initially merged them by `PackageCode` and
   took the first, which happened to be expired, marking a live package as dead. Took
   two attempts to fix: first "take the latest expiry", then the real insight —
   **never merge at all**
3. **Balances on expired vouchers are not usable** — the API returns them; folding
   them into "available credits" badly misled (one account: 251.25 shown, 0 usable).
   They must be reported separately

**Mistakes**

- Column widths via `%-14s` misaligned because CJK characters are double-width.
  Added `display_width()` with East Asian width handling
- Per-row refresh in `cbs ls` let line numbers drift as queries completed, overwriting
  the wrong rows. Whole-table redraw proved stable
- While restructuring the two-phase rendering, the result table was dropped from the
  non-TTY (pipe) branch — pipes showed only the header. Pipes can't redraw in place,
  so the final table must be printed after the queries finish

### v3 — keepalive

**Problem**: a snapshot-style account store fails silently. One account showed "8 days
left" while both its accessToken and refreshToken were void server-side.

**Root cause**: refreshTokens **rotate**. Each refresh issues a new one and invalidates
the old family — including accessTokens whose paper `exp` hasn't passed. Store
snapshots therefore decay into dead tokens.

**Endpoint**: recovered from the CLI's bundled implementation (`strings` plus context
analysis): `POST /v2/plugin/auth/token/refresh` with header `X-Refresh-Token`.

**Implementation**

- `cbs keepalive [--all|<alias>]`: refresh and write the new credential back
- **The active account must be skipped** — the CLI refreshes it itself, and refreshing
  from a stale store copy would invalidate the live token family and log the CLI out
- After a refresh the refreshToken lifetime becomes **60 days** (measured), so
  refreshing at least once every 60 days keeps an account alive indefinitely
- Pleasant surprise: accounts with a void accessToken but a working refreshToken can
  be **rescued by keepalive without re-login**

### v4 — Liveness probing replaces paper dates

**Problem**: the Token column showed the paper JWT `exp`, which lies. Reused the
credits query's result instead — 401/403 means voided, so show "void (re-login
needed)". Zero extra requests.

**Extended**: `cbs use` and `--dry-run` probe first, so you learn before switching
instead of after.

**The significant mistake**: I asserted that "after switching you must restart the CLI
process". The log's `userId changed` records overturned it — **the CLI follows on-disk
credential changes; switches take effect immediately and conversation history is
preserved**. That wrong belief made me give the user unnecessary steps.

Corrected understanding:

| Claim | Verdict |
|---|---|
| Must restart the session after switching | ❌ takes effect immediately |
| Switching loses the conversation | ❌ it doesn't |
| An old session burns the old account's credits | ❌ it follows the switch |
| A dead token on disk causes 401 | ✅ this is the real failure mode |
| A live process refreshing overwrites your switch | ⚠️ still real |

**Another empirical finding**: a live process's refresh write-back can overwrite a
freshly made switch (one account was replaced ~6 minutes later). `cbs use` now
re-checks the disk 3 seconds after switching and warns if it was overwritten.

### Engineering lessons

1. **Verify before concluding**. Three conclusions I gave from "reasonable inference"
   (which expiry record to take, whether old sessions switch tokens, whose account is
   in use) were all overturned by measurement. Logs and strace beat reasoning
2. **The user corrected me three times in the same direction** (wrong expiry, wrong
   nearest expiry, wrong "whose account is this") — all because I coded before
   understanding the data shape
3. **Always ask before touching user credentials**. Test with dry-run or an isolated HOME
4. **Document the evidence**. Every finding in WORKING.md includes its verification
   method so others can reproduce it

### Known limitations

- Verified only on Ubuntu 22.04 (WSL2) with CodeBuddy Code CLI 2.164.0
- The credits API's 401/403/404 paths weren't specifically tested (all test accounts
  returned 200)
- Covers the CLI only — not the WorkBuddy desktop app, VS Code plugin, or JetBrains
  plugin (though switching does affect the VS Code plugin)
- Native (non-WSL) Linux is unverified
