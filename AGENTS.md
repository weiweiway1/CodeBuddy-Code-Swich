# AGENTS.md

给协助用户安装与排查 `cbs` 的 AI Agent 使用的说明文件。
Documentation for AI agents assisting users with installing and troubleshooting `cbs`.

## 这个工具是什么

`cbs` 是 CodeBuddy Code CLI（命令行工具，命令名 `codebuddy`）在 Linux/WSL 上的
账号切换与积分查看工具。

- 不是什么 GUI 桌面应用（那是 workbuddy-switch，但它的 Linux 包跑不起来，见下）
- 不处理 WorkBuddy 桌面端、VS Code 插件、JetBrains 插件的账号
- 但切换 CLI 账号会**同时影响 VS Code CodeBuddy 插件**，因为两者共享同一个凭证文件

## 先读这个：为什么不用 workbuddy-switch

用户很可能已经试过 https://github.com/changexbc/workbuddy-switch 并失败。
它的 Linux 构建要求 **glibc 2.38+**（Ubuntu 24.04 构建），Ubuntu 22.04 只有 2.35。
报错形如：

```
wb-switch-rust: /lib/x86_64-linux-gnu/libc.so.6: weak version `GLIBC_2.39' not found
libwebkit2gtk-4.1.so.0 => not found
```

**不要建议用户去编译它或改用它的 AppImage** —— 会浪费时间，`cbs` 就是为了绕开这个问题。

## 环境确认（开始前必做）

```bash
cat /etc/os-release | head -2      # 发行版与版本
ldd --version | head -1            # glibc 版本
python3 --version                  # 需要 3.7+
which codebuddy && codebuddy --version
echo $DISPLAY                      # 有值说明有图形环境（WSLg）
```

判断分支：

- **glibc < 2.38** → workbuddy-switch 一定跑不了，用 `cbs`（正常情况，多数用户）
- **glibc >= 2.38** → workbuddy-switch 可用，但 `cbs` 更轻（无GUI、终端直接用），仍可推荐 cbs
- **没有 `codebuddy`** → 先让用户装CodeBuddy Code CLI，本工具依附于它
- **`python3` 不存在** → `sudo apt install python3`（这一步需要用户自己输密码）

## 安装（标准流程）

```bash
git clone https://github.com/weiweiway1/CodeBuddy-Code-Swich.git
cd CodeBuddy-Code-Swich
./install.sh
```

脚本会：

1. 检查 python3
2. 把 `cbs.py`、`credits.py` 装到 `~/.cbs/`
3. 软链到 `~/.local/bin/cbs`
4. 语法自检

**注意**：`cbs.py` 和 `credits.py` 必须同目录，`cbs.py` 用普通 `import credits` 导入它。
只拷一个文件会报 `ModuleNotFoundError`。

若 `~/.local/bin` 不在 PATH，提示用户：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 装完必须让用户做的第一件事

```bash
cbs doctor
```

`doctor` 输出决定后续怎么走，**不要跳过**。可能的结论与应对：

| doctor 输出 | 含义 | 该做什么 |
|---|---|---|
| 凭证文件可解析、有昵称 | 正常 | 继续下一步|
| `凭证文件 不存在` | 用户还没登录过 CLI | 让用户跑 `codebuddy` 完成浏览器登录，再重试 |
| `settings 不是合法 JSON` | 配置损坏 | 见下方「settings.json 损坏」 |
| `权限过宽` | 凭证文件权限不安全 | `chmod 600 <路径>` |
| `环境变量 ... 会覆盖 settings.json` | 有环境变量干扰 | 见下方「环境变量覆盖」 |

## 核心工作流

### 首次添加账号

顺序不能颠倒——**先登录，再导入**：

```bash
codebuddy                              # 浏览器登录，登录完退出
cbs current# 确认登录的是预期账号
cbs import --as work                   # 存入库存档
cbs ls                                 # 确认已入库
```

`cbs import` 只读凭证，不改登录态，可以放心执行。

### 切换账号

```bash
cbs use work
```

执行顺序（Agent 需要理解，不要绕过）：

1. 预检 `settings.json` 合法性 —— 不合法则**一个字节都不写**，退出码 4
2. 列出运行中的 codebuddy 进程，等用户确认。**默认不杀**
3. 备份 5 个文件到 `~/.cbs/backups/`
4. 写凭证文件（原子替换）
5. 同步 `settings.json` 的 `env` 与三个 local_storage 缓存
6. 回读验证
7. 第 3 步之后任何失败（含 Ctrl-C）自动回滚

常用参数：`--dry-run` 只看不改、`--yes` 跳过确认、`--kill` 关闭运行中的 CLI。

### 切换与运行中的会话（重要，勿用旧认知）

CLI **会跟随磁盘凭证变化**：`cbs use` 之后，正在运行的会话在下一次请求时自动
切到新账号——**即时生效，不丢对话历史，不需要重启进程**。不要建议用户重开终端，
那不是必需的步骤。

真正要提防的是**多进程续期回写**：每个 CLI 进程内存里各持一份凭证副本，续期时
写回磁盘，可能覆盖用户刚做的切换。用户报告"切了又变回去"时，先想到这个：

```
cbs use <别名>     → 磁盘 = 新账号
某个旧账号的进程续期 → 写回磁盘 = 旧账号   ← 切换被覆盖
```

处理：退出其它 CLI 进程，只留一个，然后重新 `cbs use`。诊断可看 CLI 日志
`~/.codebuddy/logs/<日期>/*.log` 里的 `userId changed` 记录（带 pid 和时间戳）。

另外两个相关事实：

- **额度耗尽不会触发换号**，token 失效才会——CLI 在 401 时走恢复（重读磁盘）
- `cbs use` 会在切换后 3 秒复核磁盘，被覆盖会直接告警

## 常见问题排查

### 切换后看起来没生效

按可能性排序：

1. **切换被其它进程的续期回写覆盖** —— 最常见，见上文；`cbs current` 看
   磁盘上现在是谁，日志 `userId changed` 看谁改的
2. **环境变量覆盖** —— 检查 `CODEBUDDY_AUTH_TOKEN` 和 `CODEBUDDY_INTERNET_ENVIRONMENT`：
   ```bash
   echo $CODEBUDDY_AUTH_TOKEN
   echo $CODEBUDDY_INTERNET_ENVIRONMENT
   ```
   有输出就 `unset` 然后重开终端。注意：这些变量可能是 CLI 注入给子进程的，
   换新终端通常就没有了
3. **切到的是死 token** —— `cbs use` 现在会先验活（401/403 直接提示失效），
   若已切过去则表现为 CLI 报 401 要求 `/login`。需重新登录救活：
   `cbs use <别名>` → CLI 内 `/login` → `cbs import --as <别名> --force`
4. **区域档位没同步** —— `cbs ls` 看区域列是否与目标账号一致：
   ```bash
   python3 -c "import json;print(json.load(open('$HOME/.codebuddy/settings.json')).get('env'))"
   cat ~/.codebuddy/local_storage/entry_3bab*.info   # 应为 "internal" 或 "public"
   ```

### 积分显示为 0 或不准

- 「可用 0」但服务端显示有余额 → 那些余额挂在**已过期**额度券上，不可用。
  `cbs credits <别名>` 会分别列出可用券与过期券
- 显示 `查询失败` → 多半是网络或 token 问题，`cbs credits <别名>` 看具体错误
- 有人反馈"到期时间不对" → **先自己查 legacy 接口的真实数据**再下结论。
  每条记录是一张独立额度券，各有余额和到期日，按`PackageCode` 合并会丢掉更早到期的券。
  这是一个已经踩过的坑，不要重复

### settings.json 损坏

`cbs use` 会拒绝执行（退出码 4）且不写任何文件，这是正确行为。

```bash
cp ~/.codebuddy/settings.json{,.broken}    # 先备份
# 修好 JSON 后重试
```

如果损坏到无法修复，可以从备份恢复：

```bash
cbs backup list
cbs backup restore <时间戳>
```

### 切换后 CLI 报错 401 / 403

token 已被服务端作废（纸面 exp 不可信）。**先试 keepalive 救活**，多数情况无需重新登录：

```bash
cbs keepalive <别名>    # accessToken 死但 refreshToken 活 → 直接救活
cbs credits <别名>     # 查询成功即救活成功
```

keepalive 也失败（refreshToken 同样作废）才需要重新登录：

```bash
cbs use <别名>               # 写入旧 token（会提示已失效）
codebuddy                    # 起会话，报 401 时在会话内 /login
cbs import --as <别名> --force# 更新库存档快照
cbs keepalive <别名>         # 确认之后可持续保活
```

**日常保活**：建议用户配 cron，彻底免维护：

```bash
0 9 1 * * ~/.local/bin/cbs keepalive --all >> /tmp/cbs-keepalive.log 2>&1
```

注意：
- 当前生效账号会被 keepalive 自动跳过（它由 CLI 自己续期，用库存档副本刷新
  反而会把它登出）——这是正常行为，不是故障
- 网页端登录领积分**不会**作废 CLI token，用户可放心在网页端登录各账号
- `cbs sync <别名>` 可以在 CLI 自动续期后刷新库存档快照（现场凭证还是该账号时）

## 代码结构（Agent 需要读源码时）

单文件 Python，无第三方依赖：

- `cbs.py` — 主程序。凭证/配置路径常量在文件顶部；`atomic_write()` 是所有写入的
  基础（临时文件 + rename + fsync）；`StoreLock` 防并发；子命令各自 `cmd_*` 函数
- `credits.py` — 积分查询。两个服务端接口，需要浏览器 `User-Agent`
  （缺失会被风控拦成 403，这是踩过的坑）

**改代码时注意**：

- 所有文件写入必须走 `atomic_write()`，不要用裸 `open(...,'w')`
- 不要在输出里打印 token
- `credits.py` 里每条 legacy 记录是独立的额度券，**不要按PackageCode 合并**
- `CODEBUDDY_BASE_URL` 必须带 `/v2` 后缀，写成门户根地址会被 nginx 拒绝（405）

## 安全红线

- `~/.cbs/accounts.json` 含**明文 access token**，权限 600，目录 700
- 绝不要把 `accounts.json`、`backups/` 提交到任何仓库（`.gitignore` 已排除）
- 不要在日志、输出、错误信息里回显 token
- 未经用户明确同意，不要执行 `git push` 等影响远程仓库的操作

## 已验证 / 未验证

Agent 在给建议时应如实告知用户以下区别：

**已实测**：Ubuntu 22.04 (WSL2)、Python 3.10、CodeBuddy Code CLI 2.164.0、
国内版与国际版各一个账号、切换/回滚/备份恢复全流程

**未验证**：原生 Linux（非WSL）、其他发行版、CLI 2.164 以外的版本、
积分接口的 401/403/404 错误路径、非 WSL 的 GUI 环境

如果用户在未验证的环境遇到问题，先建议跑 `cbs doctor` 收集信息，
而不是直接下结论说"不支持"。