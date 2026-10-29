#!/usr/bin/env python3
"""cbs — CodeBuddy CLI 账号切换工具（Linux）

CodeBuddy Code CLI 的凭证以明文 JSON 存放于
~/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info
该文件由 CodeBuddy CLI 与 VS Code CodeBuddy 插件共享。

切换账号需要同步写入凭证文件、~/.codebuddy/settings.json 的 env 区域字段，
以及 ~/.codebuddy/local_storage 下三个区域/endpoint 缓存文件。
"""

import argparse
import base64
import errno
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request

import credits as credits_mod

HOME = os.path.expanduser("~")


def _discover_cred_file():
    """定位 CLI 凭证文件。

    正常路径是 CLI 自己建的CodeBuddyExtension 目录，但不同安装方式
    （deb/ AppImage / snap / 手动）可能落在别处，所以额外探测几处。
    """
    candidates = [
        os.path.join(HOME, ".local/share/CodeBuddyExtension/Data/Public/auth",
                     "Tencent-Cloud.coding-copilot.info"),
        os.path.join(HOME, ".config/CodeBuddyExtension/Data/Public/auth",
                     "Tencent-Cloud.coding-copilot.info"),
        os.path.join(HOME, ".codebuddy/auth",
                     "Tencent-Cloud.coding-copilot.info"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


# 凭证文件：CLI 与 VS Code 插件共享的账号库
CRED_FILE = _discover_cred_file()

SETTINGS_FILE = os.path.join(HOME, ".codebuddy/settings.json")
STORAGE_DIR = os.path.join(HOME, ".codebuddy/local_storage")

# local_storage 缓存文件名固定：entry_<md5(key)>.info，key 由 CLI 写死
CACHE_ENV = os.path.join(STORAGE_DIR, "entry_3bab4ce61838088127d444e4cc042d6d.info")
CACHE_ENDPOINT = os.path.join(STORAGE_DIR, "entry_933d5543e80177622c17a73869c0fad7.info")
CACHE_PRODUCT = os.path.join(STORAGE_DIR, "entry_604f48c944053e01d9546675443286c1.info")

CBS_DIR = os.path.join(HOME, ".cbs")
ACCOUNTS_FILE = os.path.join(CBS_DIR, "accounts.json")
BACKUP_DIR = os.path.join(CBS_DIR, "backups")
LOCK_FILE = os.path.join(CBS_DIR, ".lock")

ENV_KEY = "CODEBUDDY_INTERNET_ENVIRONMENT"
BASE_URL_KEY = "CODEBUDDY_BASE_URL"

# 区域档位。BASE_URL 必须带 /v2：写成门户根地址会让 CLI POST 到
# /chat/completions 被 nginx 拒绝(405)。local_storage 的 endpoint 不带 /v2，
# 那是门户根地址，用途不同。
REGION_INTERNAL = "internal"
REGION_PUBLIC = "public"
REGION_ENV = {REGION_INTERNAL: REGION_INTERNAL, REGION_PUBLIC: REGION_PUBLIC}
REGION_ENDPOINT = {
    REGION_INTERNAL: "https://copilot.tencent.com",
    REGION_PUBLIC: "https://www.codebuddy.ai",
}
REGION_BASE_URL = {
    REGION_INTERNAL: None,  # 国内版删除该键
    REGION_PUBLIC: "https://www.codebuddy.ai/v2",
}

DAY_MS = 86400_000
KEEP_BACKUPS = 10

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_CORRUPT = 4
EXIT_LOCKED = 5

SHARED_HINT = (
    "注意：CodeBuddy CLI 与 VS Code CodeBuddy 插件共用此账号库，"
    "本次切换会同时改变 VS Code 插件的登录账号。"
)


class CbsError(Exception):
    def __init__(self, message, code=EXIT_FAIL):
        super().__init__(message)
        self.code = code


# ---------- 原子写 ----------

def atomic_write(path, data, mode=0o600):
    """临时文件建在目标同目录（同文件系统 rename 才原子），成功后 fsync 目录。"""
    directory = os.path.dirname(path)
    if os.path.islink(path):
        raise CbsError(f"拒绝写入符号链接：{path}", EXIT_CORRUPT)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cbs-tmp-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_json(path, value, mode=0o600):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode)


def existing_mode(path, default=0o644):
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return default


def read_json(path, what, missing_ok=False):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise CbsError(f"{what}不存在：{path}", EXIT_CORRUPT)
    except json.JSONDecodeError as error:
        raise CbsError(f"{what}不是合法 JSON：{path}\n  {error}", EXIT_CORRUPT)


# ---------- 库存档 ----------

def load_store():
    store = read_json(ACCOUNTS_FILE, "账号库存档", missing_ok=True)
    if store is None:
        store = {"version": 1, "accounts": {}}
    store.setdefault("version", 1)
    store.setdefault("accounts", {})
    return store


def save_store(store):
    write_json(ACCOUNTS_FILE, store)


class StoreLock:
    """覆盖 use 的第 2-7 步，防止两个终端并发切换。"""

    def __enter__(self):
        os.makedirs(CBS_DIR, mode=0o700, exist_ok=True)
        self.fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise CbsError("另一个 cbs 正在运行，请稍后重试", EXIT_LOCKED)
        return self

    def __exit__(self, *_):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        return False


# ---------- 区域与 token ----------

def region_of(domain):
    """按凭证里的 domain 判断国内版/国际版。"""
    text = (domain or "").lower()
    if ".ai" in text and "tencent" not in text and "codebuddy.cn" not in text:
        return REGION_PUBLIC
    return REGION_INTERNAL


def decode_jwt_exp(token):
    """只解 payload 取 exp，不验签——仅用于展示 token 状态。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return int(exp) * 1000 if exp else None
    except Exception:
        return None


def token_status(auth, now_ms=None):
    """返回 (档位, 人类可读说明)。档位：ok / soon / access-expired / dead / unknown"""
    now_ms = now_ms or int(time.time() * 1000)
    access_exp = auth.get("expiresAt") or decode_jwt_exp(auth.get("accessToken", ""))
    refresh_exp = auth.get("refreshExpiresAt")

    if refresh_exp and refresh_exp < now_ms:
        return "dead", "refreshToken 已过期，需重新登录"
    if not access_exp:
        return "unknown", "无有效期信息"
    if access_exp < now_ms:
        if refresh_exp and refresh_exp > now_ms:
            return "access-expired", "accessToken 已过期，CLI 将用 refreshToken 自动续期"
        return "dead", "accessToken 已过期且无 refreshToken"
    if access_exp - now_ms < DAY_MS:
        return "soon", "%s 后过期" % humanize_delta(access_exp - now_ms)
    return "ok", "%s 后过期" % humanize_delta(access_exp - now_ms)


def display_width(text):
    """中文/全角字符占两列，%-*s 才能对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text, width):
    return text + " " * max(0, width - display_width(text))


def humanize_delta(ms):
    seconds = max(0, ms // 1000)
    days = seconds // 86400
    hours = seconds % 86400 // 3600
    if days:
        return "%d 天 %d 小时" % (days, hours)
    minutes = seconds % 3600 // 60
    if hours:
        return "%d 小时 %d 分" % (hours, minutes)
    return "%d 分钟" % max(minutes, 0)


def fmt_time(ms):
    if not ms:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


# ---------- 现场凭证 ----------

def read_live_credential():
    return read_json(CRED_FILE, "凭证文件")


def record_region(record):
    """区域始终由 domain 推导，不信任库存档里缓存的 region——
    凭证续期后 domain 可能变，用缓存值会切错档位。"""
    return region_of((record.get("auth") or {}).get("domain") or record.get("domain"))


def snapshot(live, alias=None):
    account = live.get("account") or {}
    auth = live.get("auth") or {}
    uid = account.get("uid")
    if not uid:
        raise CbsError("凭证文件缺少 account.uid，无法入库", EXIT_CORRUPT)
    now_ms = int(time.time() * 1000)
    return {
        "alias": alias,
        "uid": uid,
        "nickname": account.get("nickname"),
        "uin": account.get("uin"),
        "phone": account.get("phoneNumber"),
        "domain": auth.get("domain"),
        "region": region_of(auth.get("domain")),
        "account": account,
        "auth": auth,
        "accounts": live.get("accounts") or [],
        "imported_at": now_ms,
        "last_used_at": None,
    }


def live_uid():
    """当前生效账号的 uid；凭证文件不可读时返回 None。"""
    try:
        live = read_live_credential()
    except CbsError:
        return None
    return (live.get("account") or {}).get("uid")


def snapshot_is_stale(record, now_ms=None):
    """CLI 续期后会回写现场凭证，库存档快照随即变旧。"""
    now_ms = now_ms or int(time.time() * 1000)
    auth = record.get("auth") or {}
    imported = record.get("imported_at") or 0
    return (auth.get("lastRefreshTime") or 0) > imported


# ---------- 进程 ----------

def running_cli_pids():
    """正在运行的 codebuddy CLI 进程，排除 cbs 自身与父 shell。"""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,etime=,args="],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    me = os.getpid()
    found = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == me:
            continue
        args = parts[2]
        if "cbs" in args or "cbs.py" in args:
            continue
        if "codebuddy" in args.lower() and "bash" not in args:
            found.append((pid, parts[1], args[:100]))
    return found


def kill_pids(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline:
        alive = [p for p in pids if os.path.exists("/proc/%d" % p)]
        if not alive:
            return True
        time.sleep(0.2)
    for pid in [p for p in pids if os.path.exists("/proc/%d" % p)]:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.5)
    return not any(os.path.exists("/proc/%d" % p) for p in pids)


# ---------- 备份 ----------

def backup_targets():
    return [CRED_FILE, SETTINGS_FILE, CACHE_ENV, CACHE_ENDPOINT, CACHE_PRODUCT]


def make_backup(alias):
    """全量备份 + manifest（记录哪些文件原本存在，restore 时据此决定删还是写）。"""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = os.path.join(BACKUP_DIR, "%s-%s" % (stamp, alias))
    os.makedirs(dest, mode=0o700, exist_ok=True)
    manifest = {"created_at": int(time.time() * 1000), "alias": alias, "files": {}}
    for index, path in enumerate(backup_targets()):
        entry = {"index": index, "existed": os.path.exists(path)}
        if entry["existed"]:
            name = "%d-%s" % (index, os.path.basename(path))
            shutil.copy2(path, os.path.join(dest, name))
            entry["backup"] = name
            entry["mode"] = existing_mode(path)
        manifest["files"][path] = entry
    with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return dest


def restore_backup(dest):
    manifest = read_json(os.path.join(dest, "manifest.json"), "备份 manifest")
    for path, entry in sorted(
        manifest["files"].items(), key=lambda kv: -kv[1]["index"]
    ):
        if entry["existed"]:
            atomic_write(path, open(os.path.join(dest, entry["backup"]), "rb").read(),
                        entry.get("mode", 0o600))
        elif os.path.exists(path):
            os.unlink(path)


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    return sorted(
        (name for name in os.listdir(BACKUP_DIR)
         if os.path.isdir(os.path.join(BACKUP_DIR, name))),
        reverse=True,
    )


def prune_backups(keep):
    for name in list_backups()[keep:]:
        shutil.rmtree(os.path.join(BACKUP_DIR, name), ignore_errors=True)


def confirm(prompt):
    """询问确认，y / Y / yes 均视为同意，其余一律取消。"""
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


# ---------- 环境变量覆盖检查 ----------

def env_overrides():
    """进程环境里这两个变量会覆盖 settings.json，导致切换看起来没生效。"""
    found = []
    for key in ("CODEBUDDY_AUTH_TOKEN", ENV_KEY):
        value = os.environ.get(key)
        if value and value.strip():
            shown = "<已设置，%d 字符>" % len(value) if len(value) > 12 else value
            found.append((key, shown))
    return found


def check_env_conflict(region, quiet=False):
    conflicts = []
    token = os.environ.get("CODEBUDDY_AUTH_TOKEN")
    if token and token.strip():
        conflicts.append(
            "CODEBUDDY_AUTH_TOKEN 已设置，它会覆盖 settings.json，切换不生效；"
            "请 unset 后重开终端"
        )
    current = os.environ.get(ENV_KEY, "").strip()
    if current:
        normalized = "internal" if current.lower() in ("internal", "ioa") else current.lower()
        if normalized != region:
            conflicts.append(
                "%s=%s 与目标档位 %s 冲突，它会覆盖 settings.json；"
                "请 unset 后重开终端" % (ENV_KEY, current, region)
            )
    if conflicts and not quiet:
        for item in conflicts:
            print("  [警告] " + item)
    return conflicts


# ---------- doctor ----------

def cmd_doctor(_args):
    problems = 0
    print("cbs doctor")
    print("  凭证文件   %s" % CRED_FILE)
    if os.path.exists(CRED_FILE):
        try:
            live = read_live_credential()
            account = live.get("account") or {}
            print("  可解析     是（uid=%s nickname=%s）"
                  % (account.get("uid"), account.get("nickname")))
        except CbsError as error:
            print("  [问题] %s" % error)
            problems += 1
    else:
        print("  [问题] 不存在")
        print("         你还没有登录过 CodeBuddy CLI，或凭证路径与预期不同。")
        print("         请先运行 `codebuddy` 完成一次浏览器登录，然后重试。")
        print("         若已登录，可在 ~/.local/share/CodeBuddyExtension/Data/Public/auth/")
        print("         与 ~/.config/CodeBuddyExtension/Data/Public/auth/ 下查找")
        print("         Tencent-Cloud.coding-copilot.info 后手动调整 cbs.py 顶部的 CRED_FILE。")
        problems += 1

    mode = existing_mode(CRED_FILE, 0)
    if mode & 0o077:
        print("  [问题] 权限过宽：%o，应为 600" % mode)
        problems += 1
    elif os.path.exists(CRED_FILE):
        print("  权限       %o" % mode)

    if os.path.exists(SETTINGS_FILE):
        try:
            read_json(SETTINGS_FILE, "settings.json")
            print("  settings   合法 JSON")
        except CbsError as error:
            print("  [问题] %s" % error)
            problems += 1

    store = load_store()
    print("  库存档     %d 个账号" % len(store["accounts"]))
    pids = running_cli_pids()
    if pids:
        print("  运行中CLI  %d 个（cbs use 会列出它们并等待确认）" % len(pids))
    else:
        print("  运行中CLI  无")
    check_env_conflict(None, quiet=True)
    for key, shown in env_overrides():
        print("  [警告] 环境变量 %s=%s 会覆盖 settings.json" % (key, shown))

    print("结果：%s" % ("全部正常" if not problems else "%d 项问题" % problems))
    return EXIT_OK if not problems else EXIT_FAIL


# ---------- ls / current ----------

def render_credits_line(alias, record, data, now_ms, credits_off=False):
    """把积分摘要拼成表格里的一行。"""
    if data is None:
        return "-" if credits_off else "..."
    if not data.get("ok"):
        error = data.get("error", "")
        if "HTTP 401" in error or "HTTP 403" in error:
            return "-"  # token 已被服务端作废，积分查不了；Token 列会标"已失效"
        return "查询失败"
    usable = credits_mod.format_credits(data.get("usable_remaining"))
    parts = [usable]
    expired = data.get("expired_remaining") or 0
    if expired > 0:
        parts.append("(+%s已过期)" % credits_mod.format_credits(expired))
    text = " ".join(parts)
    expire = data.get("soonest_expire_at")
    if expire:
        # 显示可用积分中最近的一个到期时间，7 天内额外提示
        text += " 最近到期" + credits_mod.format_expire(expire, now_ms)
    if data.get("expiring_soon"):
        text += " ⚠"
    return text


def cmd_credits(args):
    store = load_store()
    if args.alias:
        if args.alias not in store["accounts"]:
            raise CbsError("未知别名 %s。用 `cbs ls` 查看。" % args.alias, EXIT_USAGE)
        targets = [args.alias]
    else:
        targets = sorted(store["accounts"])

    now_ms = int(time.time() * 1000)
    current = live_uid()

    # 并发查询，缩短多账号时的等待
    import threading
    results = {}

    def worker(alias):
        results[alias] = credits_mod.query(store["accounts"][alias], now_ms)

    threads = [threading.Thread(target=worker, args=(alias,)) for alias in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for alias in targets:
        record = store["accounts"][alias]
        data = results.get(alias) or {}
        mark = "*" if record.get("uid") == current else " "
        print("%s %-12s %s" % (mark, alias, record.get("nickname") or "-"))
        if not data.get("ok"):
            print("    查询失败：%s" % data.get("error"))
            print()
            continue
        unit = "credits"
        vouchers = data.get("vouchers") or []
        if vouchers:
            unit = vouchers[0].get("unit") or unit
        print("    可用 %s %s / 总额 %s / 已用 %s   [%s]"
              % (credits_mod.format_credits(data["usable_remaining"]), unit,
                 credits_mod.format_credits(data["total_capacity"]),
                 credits_mod.format_credits(data["total_used"]),
                 data.get("plan_name") or "-"))
        expire = data.get("soonest_expire_at")
        if expire:
            print("    可用积分最近到期：%s（%d 张可用额度券）"
                  % (credits_mod.format_expire(expire, now_ms),
                     data.get("usable_count") or 0))
        elif vouchers:
            print("    可用积分最近到期：无有效期限制")
        else:
            print("    可用积分最近到期：无可用额度")
        if data.get("expired_remaining"):
            print("    注意：另有 %s %s 挂在已过期额度券上，不可使用"
                  % (credits_mod.format_credits(data["expired_remaining"]), unit))
        for item in vouchers:
            if item["remaining"] <= 0 and item["expired"]:
                continue  # 过期且已用完的券无参考价值
            flag = " [已过期]" if item["expired"] else ""
            mark2 = "  ← 最近到期" if (item["expire_at"]
                                    and item["expire_at"] == expire) else ""
            print("      %-26s 剩余 %-10s 到期 %s%s%s"
                  % (item["name"][:26],
                     credits_mod.format_credits(item["remaining"]),
                     credits_mod.format_expire(item["expire_at"], now_ms),
                     flag, mark2))
        if data.get("expiring_soon"):
            print("    ⚠ 有可用套餐将在 7 天内到期，建议优先使用")
        print()
    return EXIT_OK


def cmd_ls(args):
    store = load_store()
    current = live_uid()
    now_ms = int(time.time() * 1000)
    rows = []
    for alias, record in sorted(store["accounts"].items()):
        status, note = token_status(record.get("auth") or {}, now_ms)
        rows.append({
            "alias": alias,
            "nickname": record.get("nickname"),
            "uid": record.get("uid"),
            "region": record_region(record),
            "status": status,
            "note": note,
            "stale": snapshot_is_stale(record, now_ms),
            "current": record.get("uid") == current,
            "expires_at": (record.get("auth") or {}).get("expiresAt"),
        })
    if args.json:
        if not args.no_credits:
            for row in rows:
                row["credits"] = credits_mod.query(
                    store["accounts"][row["alias"]], now_ms)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not rows:
        print("库存档为空。用 `cbs import --as <别名>` 导入当前账号。")
        return EXIT_OK

    credit_map = {}
    mark = {"ok": "有效", "soon": "将过期", "access-expired": "已过期",
            "dead": "需重登", "unknown": "未知"}
    has_credit_col = not args.no_credits

    def token_cell(row, credit_data):
        """Token 列以服务端真实状态为准：积分接口 401/403 = 凭证已被
        服务端作废，纸面 exp 再长也不可信。网络错误等退回纸面判断。"""
        status = mark.get(row["status"], row["status"])
        if has_credit_col and credit_data and not credit_data.get("ok"):
            error = credit_data.get("error", "")
            if "HTTP 401" in error or "HTTP 403" in error:
                return "已失效(需重登)"
        return status

    headers = ["别名", "昵称", "区域"]
    if has_credit_col:
        headers.append("可用积分")
    headers += ["Token", "uid", "备注"]
    body = []
    for row in rows:
        flags = []
        if row["current"]:
            flags.append("当前")
        if row["stale"]:
            flags.append("快照已旧")
        if row["expires_at"]:
            flags.append(fmt_time(row["expires_at"]) + " 到期")
        cells = [
            ("* " if row["current"] else "") + row["alias"],
            row["nickname"] or "-",
            row["region"] or "-",
        ]
        if has_credit_col:
            cells.append("...")
        cells += [
            token_cell(row, credit_map.get(row["alias"])),
            (row["uid"] or "")[:8],
            row["note"] + ("  [" + " / ".join(flags) + "]" if flags else ""),
        ]
        body.append(cells)

    def draw(credit_map=None):
        table = []
        for index, cells in enumerate(body):
            cells = list(cells)
            if has_credit_col:
                alias = rows[index]["alias"]
                data = credit_map.get(alias) if credit_map else None
                cells[3] = render_credits_line(
                    alias, store["accounts"][alias], data, now_ms)
                cells[4] = token_cell(rows[index], data)
            table.append(cells)
        widths = [max([display_width(headers[i])]
                      + [display_width(r[i]) for r in table])
                  for i in range(len(headers))]
        out = ["  ".join(pad(table[r][i], widths[i])
                         for i in range(len(headers))).rstrip()
               for r in range(len(table))]
        return "  ".join(pad(headers[i], widths[i])
                         for i in range(len(headers))).rstrip() + "\n" + "\n".join(out)

    if not has_credit_col:
        print(draw())
        print("\n" + SHARED_HINT)
        return EXIT_OK

    # 先立刻打印账号信息，积分并发查询，到齐后重绘积分列
    import threading
    results = {}

    def worker(alias, record):
        results[alias] = credits_mod.query(record, now_ms)

    threads = [threading.Thread(target=worker, args=(row["alias"],
                                                     store["accounts"][row["alias"]]))
               for row in rows]
    for thread in threads:
        thread.start()

    if sys.stdout.isatty():
        # 先秒出账号表（积分列占位 ...），并发查询，全部完成后原地重绘整表。
        # 逐行刷新会让行号随完成顺序漂移，覆写错行；整表重绘稳定且实测正确。
        sys.stdout.write(draw() + "\n")
        sys.stdout.flush()
        for thread in threads:
            thread.join()
        # 回到表头上方，清掉占位行后重绘
        sys.stdout.write("\033[%dA\033[J" % (len(body) + 1))
        sys.stdout.write(draw(results) + "\n")
    else:
        # 管道/重定向：无法原地刷新，等查询完成后一次输出最终表
        for thread in threads:
            thread.join()
        sys.stdout.write(draw(results) + "\n")

    sys.stdout.write("\n" + SHARED_HINT + "\n")
    sys.stdout.flush()
    return EXIT_OK


def cmd_current(_args):
    live = read_live_credential()
    account = live.get("account") or {}
    auth = live.get("auth") or {}
    status, note = token_status(auth)
    print("当前账号")
    print("  昵称       %s" % (account.get("nickname") or "-"))
    print("  uid        %s" % (account.get("uid") or "-"))
    print("  手机号     %s" % (account.get("phoneNumber") or "-"))
    print("  domain     %s" % (auth.get("domain") or "-"))
    print("  区域档位   %s" % region_of(auth.get("domain")))
    print("  Token      %s（%s）" % (status, note))
    print("  access 到期 %s" % fmt_time(auth.get("expiresAt")))
    print("  refresh 到期 %s" % fmt_time(auth.get("refreshExpiresAt")))
    print("\n" + SHARED_HINT)
    for key, shown in env_overrides():
        print("[警告] 环境变量 %s=%s 会覆盖 settings.json" % (key, shown))
    return EXIT_OK


# ---------- import ----------

def cmd_import(args):
    live = read_live_credential()
    store = load_store()
    record = snapshot(live, alias=args.as_name)
    uid = record["uid"]

    existing = [a for a, r in store["accounts"].items() if r.get("uid") == uid]
    if existing and args.as_name in store["accounts"]:
        if not args.force:
            raise CbsError(
                "别名 %s 已存在。用 --force 覆盖。" % args.as_name, EXIT_USAGE
            )
    if existing and not args.force:
        raise CbsError(
            "该 uid 已存在于别名 %s。换 --as 指定其它别名，或对已有别名用 --force。"
            % "、".join(existing),
            EXIT_USAGE,
        )
    if not args.as_name:
        raise CbsError("请用 --as <别名> 指定名称", EXIT_USAGE)

    store["accounts"][args.as_name] = record
    save_store(store)
    print("已导入 %s（%s，%s）到库存档"
          % (args.as_name, record["nickname"], record["region"]))
    print("  uid %s" % uid)
    return EXIT_OK


# ---------- use ----------

def refresh_auth(record):
    """用 refreshToken 调服务端刷新接口，返回补全到期时间的新 auth dict。

    接口规格（取自 CLI 二进制内嵌实现并实测）：
      POST {host}/v2/plugin/auth/token/refresh
      请求头 X-Refresh-Token / X-Auth-Refresh-Source: plugin / X-User-Id / X-Domain
      请求体 {}，响应 data 即新 auth。
    refreshToken 是轮换的：每次刷新作废旧值，必须把新值存回。
    """
    auth = record.get("auth") or {}
    account = record.get("account") or {}
    refresh_token = auth.get("refreshToken")
    uid = account.get("uid")
    domain = auth.get("domain")
    if not refresh_token or not uid:
        raise CbsError("缺少 refreshToken 或 uid", EXIT_CORRUPT)

    host = "https://www.codebuddy.ai" if region_of(domain) == REGION_PUBLIC \
        else "https://www.codebuddy.cn"
    request = urllib.request.Request(
        host + "/v2/plugin/auth/token/refresh",
        data=b"{}",
        headers={
            "X-Refresh-Token": refresh_token,
            "X-Auth-Refresh-Source": "plugin",
            "X-User-Id": uid,
            "X-Domain": domain or "",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": credits_mod.USER_AGENT,
            "Origin": host,
            "Referer": host + "/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=credits_mod.TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise CbsError("HTTP %s（refreshToken 很可能已作废，需重新登录）"
                       % error.code)
    except urllib.error.URLError as error:
        raise CbsError("网络错误：%s" % error.reason)

    new_auth = body.get("data")
    if body.get("code") not in (0, None) or not isinstance(new_auth, dict) \
            or not new_auth.get("accessToken"):
        raise CbsError("刷新被拒：code=%s msg=%s"
                       % (body.get("code"), body.get("msg")))

    # 刷新响应只有 expiresIn/refreshExpiresIn，到期时间按 CLI 行为自己补全
    now_ms = int(time.time() * 1000)
    new_auth["expiresAt"] = now_ms + _num(new_auth.get("expiresIn")) * 1000
    new_auth["refreshExpiresAt"] = now_ms + _num(new_auth.get("refreshExpiresIn")) * 1000
    new_auth["lastRefreshTime"] = now_ms
    return new_auth


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def cmd_keepalive(args):
    """用库存档的 refreshToken 刷新账号，实现长期保活。

    每次刷新作废旧 refreshToken 并换发新的（30 天 access + 60 天 refresh），
    只要每 60 天内至少刷一次，账号即可无限续命，不再需要重新登录。
    """
    store = load_store()
    if args.all:
        targets = sorted(store["accounts"])
    elif args.alias:
        if args.alias not in store["accounts"]:
            raise CbsError("未知别名 %s" % args.alias, EXIT_USAGE)
        targets = [args.alias]
    else:
        raise CbsError("指定别名或 --all", EXIT_USAGE)

    current = live_uid()
    now_ms = int(time.time() * 1000)
    ok = skipped = failed = 0
    for alias in targets:
        record = store["accounts"][alias]
        # 当前生效账号跳过：CLI 自己会续期，用库存档快照刷新反而会作废
        # 现场凭证的 token 族，导致 CLI 下次续期失败被登出。
        if record.get("uid") == current:
            print("  跳过 %s（当前生效账号，由 CLI 自动续期）" % alias)
            skipped += 1
            continue
        try:
            new_auth = refresh_auth(record)
        except CbsError as error:
            print("  失败 %s：%s" % (alias, error))
            failed += 1
            continue
        record["auth"] = new_auth
        record["imported_at"] = now_ms
        record["domain"] = new_auth.get("domain")
        store["accounts"][alias] = record
        ok += 1
        print("  续命 %s：access 至 %s，refresh 至 %s"
              % (alias,
                 fmt_time(new_auth.get("expiresAt")),
                 fmt_time(new_auth.get("refreshExpiresAt"))))

    if ok:
        save_store(store)
    print("\n完成：续命 %d，跳过 %d，失败 %d" % (ok, skipped, failed))
    if failed:
        print("失败的账号需重新登录救活：cbs use <别名> → codebuddy 内 /login → "
              "cbs import --as <别名> --force")
    return EXIT_OK if not failed else EXIT_FAIL


def sync_settings_region(region):
    settings = read_json(SETTINGS_FILE, "settings.json", missing_ok=True) or {}
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
    env[ENV_KEY] = REGION_ENV[region]
    base_url = REGION_BASE_URL[region]
    if base_url:
        env[BASE_URL_KEY] = base_url
    else:
        env.pop(BASE_URL_KEY, None)
    settings["env"] = env
    write_json(SETTINGS_FILE, settings, existing_mode(SETTINGS_FILE))
    return settings


def sync_storage_region(region):
    os.makedirs(STORAGE_DIR, exist_ok=True)
    write_json(CACHE_ENV, REGION_ENV[region], existing_mode(CACHE_ENV))
    write_json(CACHE_ENDPOINT, REGION_ENDPOINT[region], existing_mode(CACHE_ENDPOINT))
    # 产品包缓存按档位区分，切档必须丢掉，否则 CLI 仍打旧站点
    if os.path.exists(CACHE_PRODUCT):
        os.unlink(CACHE_PRODUCT)


def write_live_credential(record):
    payload = {
        "account": record["account"],
        "auth": record["auth"],
        "accounts": record.get("accounts") or [record["account"]],
    }
    payload["account"] = dict(payload["account"], lastLogin=True)
    write_json(CRED_FILE, payload, 0o600)


def verify_switch(record, region):
    live = read_live_credential()
    uid = (live.get("account") or {}).get("uid")
    if uid != record["uid"]:
        raise CbsError("验证失败：凭证 uid 为 %s，期望 %s" % (uid, record["uid"]),
                       EXIT_CORRUPT)
    settings = read_json(SETTINGS_FILE, "settings.json")
    got = (settings.get("env") or {}).get(ENV_KEY)
    if got != REGION_ENV[region]:
        raise CbsError("验证失败：settings.json env.%s 为 %r，期望 %r"
                       % (ENV_KEY, got, REGION_ENV[region]), EXIT_CORRUPT)
    base = (settings.get("env") or {}).get(BASE_URL_KEY)
    if base != REGION_BASE_URL[region]:
        raise CbsError("验证失败：settings.json env.%s 为 %r，期望 %r"
                       % (BASE_URL_KEY, base, REGION_BASE_URL[region]), EXIT_CORRUPT)
    if read_json(CACHE_ENV, "区域缓存", missing_ok=True) != REGION_ENV[region]:
        raise CbsError("验证失败：区域缓存未同步", EXIT_CORRUPT)
    if read_json(CACHE_ENDPOINT, "endpoint 缓存", missing_ok=True) != REGION_ENDPOINT[region]:
        raise CbsError("验证失败：endpoint 缓存未同步", EXIT_CORRUPT)
    if os.path.exists(CACHE_PRODUCT):
        raise CbsError("验证失败：产品包缓存未删除", EXIT_CORRUPT)


def cmd_use(args):
    store = load_store()
    if args.alias not in store["accounts"]:
        raise CbsError("未知别名 %s。用 `cbs ls` 查看。" % args.alias, EXIT_USAGE)
    record = store["accounts"][args.alias]
    region = record_region(record)

    # 0 预检：一个字节都还没写
    read_json(SETTINGS_FILE, "settings.json", missing_ok=True)
    status, note = token_status(record.get("auth") or {})
    if status == "dead":
        raise CbsError(
            "账号 %s 的 refreshToken 已过期，切换后无法使用。\n"
            "  需重新登录：cbs use %s → 运行 codebuddy 完成登录 → "
            "cbs import --as %s --force" % (args.alias, args.alias, args.alias),
            EXIT_REFUSED,
        )

    print("目标：%s（%s，%s）" % (args.alias, record.get("nickname"), region))
    print("  Token %s：%s" % (status, note))

    if args.dry_run:
        print("\n--dry-run：将执行以下动作但不落盘")
        print("  1. 备份 5 个文件到 %s" % BACKUP_DIR)
        print("  2. 写凭证 %s" % CRED_FILE)
        print("  3. settings.json env.%s=%s，env.%s=%s"
              % (ENV_KEY, REGION_ENV[region], BASE_URL_KEY,
                 REGION_BASE_URL[region] or "(删除)"))
        print("  4. 同步 local_storage 区域/endpoint 缓存，删除产品包缓存")
        print("  5. 回读验证")
        check_env_conflict(region)
        return EXIT_OK

    with StoreLock():
        # 0.5 保存现场：把当前生效账号的最新凭证（CLI 可能已续期过）回写库存档，
        # 否则切走时现场凭证被覆盖，该账号的续期后 token 从此丢失，
        # 之后切回来只能拿到导入时的旧 token（大概率已 401）。
        try:
            live = read_live_credential()
            live_account = live.get("account") or {}
            live_uid = live_account.get("uid")
            if live_uid:
                holder = [a for a, r in store["accounts"].items()
                          if r.get("uid") == live_uid]
                if holder:
                    alias0 = holder[0]
                    old = store["accounts"][alias0]
                    updated = snapshot(live, alias=alias0)
                    updated["last_used_at"] = old.get("last_used_at")
                    store["accounts"][alias0] = updated
                    print("已保存现场凭证到库存档：%s（uid %s）"
                          % (alias0, live_uid[:8]))
                else:
                    print("当前账号（%s）不在库存档中，未保存。"
                          "如需保留请先 `cbs import --as <别名>`。"
                          % (live_account.get("nickname") or live_uid[:8]))
        except CbsError:
            print("  [警告] 读取现场凭证失败，跳过保存当前账号")

        # 1 进程确认：默认绝不杀
        pids = running_cli_pids()
        if pids:
            print("\n正在运行的 CodeBuddy 进程：")
            for pid, etime, args_text in pids:
                print("  PID %-7s 运行 %-10s %s" % (pid, etime, args_text))
            if args.kill:
                print("\n--kill：正在关闭这些进程")
                if not kill_pids([p[0] for p in pids]):
                    print("  [警告] 部分进程未能关闭")
            elif not args.yes:
                print("\n切换后需重启这些进程才会生效。")
                if not confirm("输入 y 继续切换（不关闭进程），其它键取消: "):
                    print("已取消。")
                    return EXIT_REFUSED

        # 2 备份
        backup = make_backup(args.alias)
        print("\n已备份到 %s" % backup)

        # 3-6 写入，任一步失败即回滚
        try:
            write_live_credential(record)
            print("已写凭证（uid=%s）" % record["uid"])
            sync_settings_region(region)
            print("已同步 settings.json env（%s）" % REGION_ENV[region])
            sync_storage_region(region)
            print("已同步 local_storage 缓存，删除产品包缓存")
            verify_switch(record, region)
            print("验证通过")
        except BaseException as error:
            print("\n切换失败：%s" % error)
            try:
                restore_backup(backup)
                print("已回滚到切换前状态。")
                return EXIT_FAIL
            except Exception as rollback_error:
                raise CbsError(
                    "回滚失败：%s\n请手动从备份恢复：%s" % (rollback_error, backup),
                    EXIT_CORRUPT,
                )

        # 7 收尾
        record["last_used_at"] = int(time.time() * 1000)
        save_store(store)
        prune_backups(KEEP_BACKUPS)

    print("\n已切换到 %s。新启动的 codebuddy 立即生效。" % args.alias)
    if status in ("soon", "access-expired"):
        print("提示：CLI 首次请求会用 refreshToken 续期并回写凭证，"
              "之后请跑 `cbs sync %s` 更新库存档快照。" % args.alias)
    print(SHARED_HINT)
    return EXIT_OK


# ---------- sync / rm / backup ----------

def cmd_sync(args):
    store = load_store()
    record = store["accounts"].get(args.alias)
    if record is None:
        raise CbsError("未知别名 %s。用 `cbs ls` 查看。" % args.alias, EXIT_USAGE)
    live = read_live_credential()
    live_account_uid = (live.get("account") or {}).get("uid")
    if live_account_uid != record["uid"]:
        raise CbsError(
            "现场凭证 uid 为 %s，与 %s（%s）不符。请先 `cbs use %s`。"
            % (live_account_uid, args.alias, record["uid"], args.alias),
            EXIT_REFUSED,
        )
    store["accounts"][args.alias] = snapshot(live, alias=args.alias)
    store["accounts"][args.alias]["last_used_at"] = record.get("last_used_at")
    save_store(store)
    print("已把现场凭证回写库存档：%s" % args.alias)
    return EXIT_OK


def cmd_rm(args):
    store = load_store()
    if args.alias not in store["accounts"]:
        raise CbsError("未知别名 %s。" % args.alias, EXIT_USAGE)
    if store["accounts"][args.alias].get("uid") == live_uid():
        raise CbsError(
            "%s 是当前生效账号，不能删除。请先 `cbs use <其它别名>`。" % args.alias,
            EXIT_REFUSED,
        )
    if not args.force:
        if not confirm("确认删除库存档中的 %s？输入 y 继续: " % args.alias):
            print("已取消。")
            return EXIT_REFUSED
    del store["accounts"][args.alias]
    save_store(store)
    print("已从库存档删除 %s（现场凭证未改动）" % args.alias)
    return EXIT_OK


def cmd_backup(args):
    if args.action == "list":
        names = list_backups()
        if not names:
            print("暂无备份。")
            return EXIT_OK
        for name in names:
            print("  %s" % name)
        print("\n共 %d 份，cbs backup prune 可清理。" % len(names))
        return EXIT_OK
    if args.action == "restore":
        dest = args.target
        if not os.path.isdir(os.path.join(BACKUP_DIR, dest)):
            raise CbsError("备份不存在：%s" % dest, EXIT_USAGE)
        with StoreLock():
            pids = running_cli_pids()
            if pids and not args.yes:
                print("正在运行的 CodeBuddy 进程 %d 个，恢复后需重启。" % len(pids))
                if not confirm("输入 y 继续恢复: "):
                    print("已取消。")
                    return EXIT_REFUSED
            restore_backup(os.path.join(BACKUP_DIR, dest))
        print("已恢复备份 %s" % dest)
        return EXIT_OK
    if args.action == "prune":
        prune_backups(args.keep)
        print("已保留最近 %d 份备份。" % args.keep)
        return EXIT_OK
    raise CbsError("未知子命令 %s" % args.action, EXIT_USAGE)


# ---------- 入口 ----------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="cbs",
        description="CodeBuddy CLI 账号切换工具（Linux）",
    )
    sub = parser.add_subparsers(dest="command")

    p_ls = sub.add_parser("ls", help="列出库存档中的账号（含可用积分）")
    p_ls.add_argument("--json", action="store_true", help="输出 JSON")
    p_ls.add_argument("--no-credits", action="store_true",
                      help="跳过积分查询，不发网络请求")
    p_ls.set_defaults(func=cmd_ls)

    p_cred = sub.add_parser("credits", help="查询积分余额与到期时间")
    p_cred.add_argument("alias", nargs="?", help="只查指定别名，默认全部")
    p_cred.set_defaults(func=cmd_credits)

    p_cur = sub.add_parser("current", help="显示当前生效账号")
    p_cur.set_defaults(func=cmd_current)

    p_imp = sub.add_parser("import", help="把现场凭证导入库存档")
    p_imp.add_argument("--as", dest="as_name", help="账号别名")
    p_imp.add_argument("--force", action="store_true", help="覆盖已有别名")
    p_imp.set_defaults(func=cmd_import)

    p_use = sub.add_parser("use", help="切换到指定账号")
    p_use.add_argument("alias", help="账号别名")
    p_use.add_argument("--yes", action="store_true", help="跳过进程确认")
    p_use.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    p_use.add_argument("--kill", action="store_true", help="切换前关闭运行中的 CLI")
    p_use.set_defaults(func=cmd_use)

    p_sync = sub.add_parser("sync", help="把现场凭证回写库存档（CLI 续期后用）")
    p_sync.add_argument("alias")
    p_sync.set_defaults(func=cmd_sync)

    p_ka = sub.add_parser("keepalive", help="用 refreshToken 刷新账号（长期保活）")
    p_ka.add_argument("alias", nargs="?")
    p_ka.add_argument("--all", action="store_true", help="刷新全部可刷新账号")
    p_ka.set_defaults(func=cmd_keepalive)

    p_rm = sub.add_parser("rm", help="从库存档删除账号")
    p_rm.add_argument("alias")
    p_rm.add_argument("--force", action="store_true")
    p_rm.set_defaults(func=cmd_rm)

    p_doc = sub.add_parser("doctor", help="环境预检")
    p_doc.set_defaults(func=cmd_doctor)

    p_bak = sub.add_parser("backup", help="备份管理")
    p_bak.add_argument("action", choices=["list", "restore", "prune"])
    p_bak.add_argument("target", nargs="?")
    p_bak.add_argument("--keep", type=int, default=KEEP_BACKUPS)
    p_bak.add_argument("--yes", action="store_true")
    p_bak.set_defaults(func=cmd_backup)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except CbsError as error:
        print("错误：%s" % error, file=sys.stderr)
        return error.code
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())