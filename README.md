# cbs — CodeBuddy Code CLI 账号切换与积分查看工具

> 适用于 Linux / WSL。补充 [workbuddy-switch](https://github.com/changexbc/workbuddy-switch) 在 Linux CLI 端的缺失。
> For Linux / WSL. Fills the gap that [workbuddy-switch](https://github.com/changexbc/workbuddy-switch) leaves on Linux CLI.

[English](#english) | [中文](#中文)

> **需要协助安装或排查？** 把 [`AGENTS.md`](./AGENTS.md) 交给你的 AI Agent（Claude Code、
> CodeBuddy Code、Cursor 等），它包含环境确认、安装流程、常见问题排查和代码结构说明。
>
> **Need help installing or troubleshooting?** Hand [`AGENTS.md`](./AGENTS.md) to your
> AI agent — it has the full setup flow, common failure modes, and code structure notes.
>
> ⚠️ **Status / 状态**：目前仅在 **Ubuntu 22.04 (WSL2) + CodeBuddy Code CLI 2.164.0** 上实测通过。
> 原生 Linux、其他发行版、CLI 其他版本均未验证，遇到问题请先跑 `cbs doctor` 并反馈。
> Currently verified on Ubuntu 22.04 (WSL2) with CLI 2.164.0 only.

---

## 中文

`cbs`（CodeBuddy Switch）在 Linux 上管理 CodeBuddy Code CLI 的登录账号，查看各账号的可用积分与到期时间。

### 为什么需要它

workbuddy-switch 提供了完整的账号切换桌面应用，但**没有 Linux 预编译包能跑**——它的 Linux 构建要求 glibc 2.38+（Ubuntu 24.04），而 Ubuntu 22.04 只有 2.35。`cbs` 直接操作凭证文件，不依赖任何 GUI 工具。

### 前置条件

- Linux 或 WSL，Python 3.7+
- 已安装 CodeBuddy Code CLI（`codebuddy` 命令）
- 至少一个已登录的 CodeBuddy 账号

### 安装

```bash
git clone https://github.com/weiweiway1/CodeBuddy-Code-Swich.git
cd CodeBuddy-Code-Swich
./install.sh
```

安装脚本会把 `cbs.py` 与 `credits.py` 放到 `~/.cbs/`，并软链到 `~/.local/bin/cbs`。

若 `~/.local/bin` 不在 PATH 中，脚本会提示你添加：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 快速开始

```bash
cbs ls              # 列出所有账号、当前生效账号、可用积分
cbs credits         # 查看每个账号的详细额度券与到期时间
cbs use work# 切换账号
cbs current         # 当前生效账号详情
```

### 添加账号

`cbs` 的账号库存档是本地文件。添加新账号的顺序是**先登录，再导入**：

```bash
# 1. 启动 CLI 并在浏览器完成登录（新增账号时）
codebuddy

# 2. 确认登录的是预期账号
cbs current

# 3. 存入库存档，起个名字
cbs import --as work
```

`cbs import` 只读取当前凭证，不会改动登录状态。

### 命令参考

| 命令 | 说明 | 说明（English） |
|---|---|---|
| `cbs ls` | 列出账号，含可用积分（并发查询，先显示账号信息再刷新积分） | List accounts with credits |
| `cbs ls --json` | JSON 输出 | JSON output |
| `cbs ls --no-credits` | 跳过积分查询，不发网络请求（瞬时返回） | Skip network calls |
| `cbs credits [别名]` | 详细额度券列表与到期时间 | Detailed voucher breakdown |
| `cbs current` | 当前生效账号详情 | Current account details |
| `cbs import --as <别名>` | 把当前登录账号存入库存档 | Import current account |
| `cbs sync <别名>` | 把现场凭证回写库存档（CLI 续期后用） | Refresh snapshot after renewal |
| `cbs use <别名>` | 切换账号 | Switch account |
| `cbs rm <别名>` | 从库存档删除（不影响现场凭证） | Remove from store |
| `cbs doctor` | 环境预检 | Environment check |
| `cbs backup list\|restore\|prune` | 备份管理 | Backup management |

### 切换账号

```bash
cbs use work
```

`cbs use` 的执行顺序：

1. **预检** — `settings.json` 必须是合法 JSON，否则**一个字节都不写**
2. **进程确认** — 列出正在运行的 codebuddy 进程。默认**不杀**，回车继续；加 `--kill` 才会关闭
3. **备份** — 5 个相关文件全量备份到 `~/.cbs/backups/`，保留最近 10 份
4. **写凭证** — 替换共享账号库中的凭证文件
5. **同步区域** — `settings.json` 的 `env` 与三个 local_storage 缓存
6. **验证** — 回读并断言 uid 与区域一致
7. **失败回滚** — 第 3 步之后任何异常（含 Ctrl-C）自动回滚

常用选项：

```bash
cbs use work --dry-run  # 只打印将执行的动作，不落盘
cbs use work --yes      # 跳过进程确认
cbs use work --kill     # 切换前关闭运行中的 CLI
```

### 切换后必须重开终端

CLI 在**启动时**读取凭证到内存。之后磁盘怎么改，已运行的会话都不知道。

```
cbs use work# 改磁盘凭证
# 你当前的 codebuddy 会话：仍是旧账号
# 新起终端 → 新起 codebuddy → 读到新账号
```

### 环境变量会覆盖配置

如果 shell 里有这些变量，它们会覆盖 `settings.json`，导致切换"看起来没生效"：

```bash
echo $CODEBUDDY_AUTH_TOKEN            # 应为空
echo $CODEBUDDY_INTERNET_ENVIRONMENT  # 国内版 internal / 国际版 public
```

有输出就 `unset` 后重开终端。

### 积分说明

积分数据来自 CodeBuddy 服务端接口，实测可用的有两条：

- `/billing/meter/get-user-resource-summary` — 套餐聚合余额
- `/v2/billing/meter/get-user-resource` — **额度券明细，每条自带余额与到期时间**

`cbs` 以第二条为准。

**关键概念：每条记录是一张独立的额度券，不是套餐。** 同一账号可能有十几张券，各有到期日。`cbs` 逐条展示，按到期时间升序，「最近到期」取的是**余额 > 0 的未过期券**中最早的那张。

> 接口会返回已过期但仍挂着余额的券。这部分余额**不可使用**，`cbs` 会单独标注，不计入可用积分。

Token 有效期与积分到期是两回事：token 过期 `cbs ls` 的 Token 列显示，积分到期看「可用积分」列。

### 已知限制

- **仅在 Ubuntu 22.04 (WSL2) + CLI 2.164.0 上实测通过**。原生 Linux、其他发行版、其他
  CLI 版本未验证，遇到问题先跑 `cbs doctor`
- 仅支持 CodeBuddy Code CLI，不管理 WorkBuddy桌面端、VS Code 插件、JetBrains 插件
- 但**切换会同时影响 VS Code CodeBuddy 插件**——两者共享同一个账号库
- 积分查询是实时请求，无缓存。账号多时 `cbs ls` 会变慢，用 `--no-credits` 跳过
- 国内版与国际版需分别查询，接口 host 按账号 `domain` 自动选择
- 积分接口的 401/403/404 错误路径未验证（所有测试账号均返回 200）

### 工作原理

CodeBuddy Code CLI 在 Linux 上的凭证是**明文 JSON**：

```
~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info
```

结构为 `{account: {...}, auth: {accessToken, refreshToken, expiresAt, ...}}`，权限 600。

区域档位还涉及 `~/.codebuddy/settings.json` 的 `env` 字段，以及 `~/.codebuddy/local_storage/` 下三个缓存文件（文件名是 `entry_<md5(key)>.info`，key 由 CLI 写死）。切换档位时三者必须同步，否则 CLI 仍打旧站点。

所有写入都是**原子替换**（同目录临时文件 + `rename` + `fsync`），不存在写坏一半的中间态。

### 安全

- 凭证文件保持 600 权限
- `~/.cbs/accounts.json` 存有明文 token，权限 600，目录 700
- `cbs` 不会把 token 打印到stdout 或日志
- **切勿提交 `accounts.json` 到版本库**（本仓库已`.gitignore` 排除）

---

## English

`cbs` (CodeBuddy Switch) manages CodeBuddy Code CLI login accounts on Linux and shows each
account's available credits and expiry dates.

### Why

[workbuddy-switch](https://github.com/changexbc/workbuddy-switch) is a full-featured desktop
switcher, but **its Linux build won't run** — it requires glibc 2.38+ (Ubuntu 24.04) while
Ubuntu 22.04 ships 2.35. `cbs` manipulates the credential file directly and needs no GUI tool.

### Requirements

- Linux or WSL (verified on Ubuntu 22.04 / WSL2), Python 3.7+
- CodeBuddy Code CLI installed (`codebuddy` command available)
- At least one logged-in CodeBuddy account

### Install

```bash
git clone https://github.com/weiweiway1/CodeBuddy-Code-Swich.git
cd CodeBuddy-Code-Swich
./install.sh
```

The script copies `cbs.py` and `credits.py` into `~/.cbs/` and symlinks `~/.local/bin/cbs`.

If `~/.local/bin` is not in PATH, add:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Quick start

```bash
cbs ls              # list accounts, current account, available credits
cbs credits         # detailed vouchers and expiry dates
cbs use work# switch account
cbs current         # current account details
```

### Adding an account

The account store is a local file. To add a new account, **log in first, then import**:

```bash
# 1. launch CLI and complete browser login
codebuddy

# 2. confirm you are logged in as expected
cbs current

# 3. save into the store under a name
cbs import --as work
```

`cbs import` only reads the current credential — it never modifies login state.

### Commands

| Command | Description |
|---|---|
| `cbs ls` | List accounts with credits (queries concurrently: account info shows first, credits refresh in) |
| `cbs ls --json` | JSON output |
| `cbs ls --no-credits` | Skip credit queries entirely (instant) |
| `cbs credits [alias]` | Detailed voucher breakdown and expiry dates |
| `cbs current` | Current account details |
| `cbs import --as <alias>` | Import the currently logged-in account |
| `cbs sync <alias>` | Refresh the stored snapshot after CLI renews its token |
| `cbs use <alias>` | Switch accounts |
| `cbs rm <alias>` | Remove from the store (live credential untouched) |
| `cbs doctor` | Environment check |
| `cbs backup list\|restore\|prune` | Backup management |

### Switching accounts

```bash
cbs use work
```

Execution order:

1. **Preflight** — `settings.json` must be valid JSON, otherwise **nothing is written**
2. **Process check** — lists running codebuddy processes. Default is **not** to kill them; press
   Enter to continue, or pass `--kill` to close them
3. **Backup** — all 5 affected files into `~/.cbs/backups/`, keeping the latest 10
4. **Write credential** — replace the credential in the shared account store
5. **Sync region** — `env` in `settings.json` plus three local_storage caches
6. **Verify** — re-read and assert uid and region match
7. **Rollback** — any failure after step 3 (including Ctrl-C) restores the previous state

Options:

```bash
cbs use work --dry-run  # print planned actions without touching disk
cbs use work --yes      # skip the process confirmation prompt
cbs use work --kill     # close running CLI sessions before switching
```

### You must restart the terminal after switching

The CLI reads credentials **at startup**. Afterwards, on-disk changes are invisible to running
sessions.

```
cbs use work          # changes on-disk credential
# your current codebuddy session: still the old account
# new terminal → new codebuddy → picks up the new account
```

### Environment variables override config

If these exist in your shell they override `settings.json`, making switches appear to fail:

```bash
echo $CODEBUDDY_AUTH_TOKEN            # should be empty
echo $CODEBUDDY_INTERNET_ENVIRONMENT  # internal (CN) / public (intl)
```

If either is set, `unset` it and open a new terminal.

### About credits

Credit data comes from two CodeBuddy server endpoints, both verified working:

- `/billing/meter/get-user-resource-summary` — aggregated package balance
- `/v2/billing/meter/get-user-resource` — **voucher details, each with its own balance and expiry**

`cbs` relies on the second one.

**Key concept: each record is an independent voucher, not a package.** A single account may hold
a dozen vouchers with different expiry dates. `cbs` lists them individually, sorted by expiry, and
"nearest expiry" is the earliest one among vouchers that are **unexpired with a non-zero balance**.

> The API returns expired vouchers that still carry a balance. That balance is **not usable**, so
> `cbs` reports it separately and excludes it from available credits.

Token lifetime and credit expiry are unrelated: token expiry appears in the `Token` column, credit
expiry in the `可用积分` column.

### Known limitations

- **Verified on Ubuntu 22.04 (WSL2) with CLI 2.164.0 only.** Native Linux, other distros, and
  other CLI versions are untested — run `cbs doctor` first when reporting problems
- Covers CodeBuddy Code CLI only — not the WorkBuddy desktop app, VS Code plugin, or JetBrains plugin
- However, **switching also affects the VS Code CodeBuddy plugin** — both share one account store
- Credit queries are live with no caching. With many accounts `cbs ls` gets slow; use `--no-credits`
- CN and intl accounts are queried separately; the host is chosen from each account's `domain`
- Error paths (401/403/404) of the credits API are untested — every test account returned 200

### How it works

On Linux, CodeBuddy Code CLI keeps credentials in **plaintext JSON**:

```
~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info
```

Shape: `{account: {...}, auth: {accessToken, refreshToken, expiresAt, ...}}`, mode 600.

Region switching also involves the `env` field in `~/.codebuddy/settings.json` and three cache
files under `~/.codebuddy/local_storage/` (named `entry_<md5(key)>.info`, where the key is
hard-coded by the CLI). All three must be synced together or the CLI keeps hitting the old site.

Every write is an **atomic replace** (temp file in the same directory + `rename` + `fsync`), so
there is never a half-written state.

### Security

- Credential file stays at mode 600
- `~/.cbs/accounts.json` contains plaintext tokens; mode 600 inside a mode 700 directory
- `cbs` never prints tokens to stdout or logs
- **Never commit `accounts.json`** (already excluded by `.gitignore`)

---

## License

MIT