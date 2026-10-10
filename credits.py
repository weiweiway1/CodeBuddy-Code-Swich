#!/usr/bin/env python3
"""cbs 的积分查询模块。

活跃套餐余额来自 /billing/meter/get-user-resource-summary（有余额无到期时间），
到期时间来自 /v2/billing/meter/get-user-resource（有到期时间但无余额），
两者按 PackageCode 关联。

接口规格取自 workbuddy-switch 的 credits.rs，已用本机三个真实账号实测验证。
"""

import json
import urllib.error
import urllib.request

TIMEOUT = 20

# 服务端会拒绝缺User-Agent 的请求（Python 默认 UA 被风控拦成 403）
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# summary 只给活跃套餐的额度，unit 固定为 credits
SUMMARY_PATH = "/billing/meter/get-user-resource-summary"
LEGACY_PATH = "/v2/billing/meter/get-user-resource"

# 按 account.domain 选 host；未知域名回落到国内站
HOST_CN = "https://www.codebuddy.cn"
HOST_AI = "https://www.codebuddy.ai"

DAY_MS = 86400_000
EXPIRING_SOON_MS = 7 * DAY_MS
# 超过两年的到期时间视为长期有效，不展示具体日期
LONG_TERM_MS = 730 * DAY_MS


def host_for(domain):
    text = (domain or "").lower()
    if ".ai" in text and "tencent" not in text and "codebuddy.cn" not in text:
        return HOST_AI
    return HOST_CN


class CreditsError(Exception):
    pass


def _post(host, path, token, uid, domain, payload):
    url = host + path
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer %s" % token,
            "X-User-Id": uid,
            "X-Domain": domain,
            "X-Client-Platform": "web",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": USER_AGENT,
            "Origin": host,
            "Referer": host + "/profile/plans-usage",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise CreditsError("HTTP %s %s" % (error.code, url))
    except urllib.error.URLError as error:
        raise CreditsError("网络错误 %s：%s" % (url, error.reason))
    except (ValueError, TimeoutError) as error:
        raise CreditsError("响应解析失败 %s：%s" % (url, error))


def _is_unauthorized(body):
    """服务端用 body里的 code 表达鉴权失败，不一定走 HTTP 401。"""
    if not isinstance(body, dict):
        return False
    code = body.get("code")
    if code in (401, 403):
        return True
    message = str(body.get("message") or body.get("msg") or "")
    return any(word in message.lower() for word in
               ("unauthorized", "登录", "失效", "token"))


def _check(body, what):
    if not isinstance(body, dict):
        raise CreditsError("%s 响应格式异常" % what)
    if body.get("code") not in (0, None):
        raise CreditsError("%s 返回 code=%s msg=%s"
                           % (what, body.get("code"), body.get("msg")))
    return body.get("data") or {}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _raw_expire(item):
    """原始到期时间（毫秒），不做「长期有效」截断，便于跨记录比较取最大值。"""
    raw = item.get("DeductionEndTime") or item.get("ExpiredTime") or \
        item.get("deductionEndTime") or item.get("expiredTime")
    expire = _num(raw) if raw is not None else 0
    if not expire:
        return 0
    # 秒级时间戳补齐到毫秒
    if abs(expire) < 10_000_000_000:
        expire *= 1000
    cycle = item.get("CycleEndTime")
    if cycle:
        try:
            import datetime
            cycle_ms = datetime.datetime.strptime(
                cycle, "%Y-%m-%d %H:%M:%S").timestamp() * 1000
            if expire - cycle_ms > 365 * DAY_MS:
                expire = cycle_ms
        except (ValueError, TypeError):
            pass
    return int(expire)


def _expire_ms(expire, now_ms):
    """把原始到期时间转成展示用值；超过两年的视为长期有效返回 None。"""
    if not expire:
        return None
    if expire - now_ms > LONG_TERM_MS:
        return None
    return int(expire)


def query(record, now_ms=None):
    """查询单个账号的活跃套餐。返回 dict，失败时带 error 字段而不抛异常。"""
    now_ms = now_ms or _now_ms()
    auth = record.get("auth") or {}
    account = record.get("account") or {}
    token = auth.get("accessToken")
    uid = account.get("uid")
    domain = auth.get("domain")
    if not token or not uid:
        return {"ok": False, "error": "库存档缺少 accessToken 或 uid"}

    host = host_for(domain)
    try:
        # 每条 legacy 记录是一张独立的额度券，自带余额与到期时间，
        # 必须逐条处理——按PackageCode 合并会丢掉更早到期的券。
        legacy = _check(_post(host, LEGACY_PATH, token, uid, domain, {
            "PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": "2020-01-01 00:00:00",
            "PackageEndTimeRangeEnd": _far_future(),
        }), "resource")
        accounts = ((legacy.get("Response") or {}).get("Data") or {}).get("Accounts") or []

        plan_name = None
        try:
            summary = _check(_post(host, SUMMARY_PATH, token, uid, domain, {}),
                             "summary")
            plan_name = summary.get("SubscriptionPackageName")
        except CreditsError:
            pass

        vouchers = []
        for item in accounts:
            expire = _raw_expire(item)
            remaining = _num(item.get("CycleCapacityRemainPrecise")
                             or item.get("CapacityRemainPrecise"))
            capacity = _num(item.get("CycleCapacitySizePrecise")
                            or item.get("CapacitySizePrecise"))
            used = _num(item.get("CycleCapacityUsedPrecise")
                        or item.get("CapacityUsedPrecise"))
            is_expired = bool(expire and expire <= now_ms)
            vouchers.append({
                "id": item.get("ResourceId") or item.get("AccountId"),
                "package_code": item.get("PackageCode"),
                "name": item.get("PackageName") or plan_name or "额度券",
                "capacity": capacity,
                "remaining": remaining,
                "used": used,
                "unit": item.get("CapacityUnit") or "credits",
                "raw_expire": expire,
                "expire_at": _expire_ms(expire, now_ms),
                "expired": is_expired,
                "status": item.get("Status"),
            })

        # 未过期的按到期时间升序，过期的排在后面
        vouchers.sort(key=lambda v: (v["expired"], v["raw_expire"] or 1 << 62))

        usable = [v for v in vouchers if not v["expired"] and v["remaining"] > 0]
        expired = [v for v in vouchers if v["expired"] and v["remaining"] > 0]

        soonest = usable[0]["expire_at"] if usable else None
        expiring_soon = any(
            v["expire_at"] and v["expire_at"] - now_ms <= EXPIRING_SOON_MS
            for v in usable)

        return {
            "ok": True,
            "total_remaining": round(sum(v["remaining"] for v in vouchers), 2),
            "usable_remaining": round(sum(v["remaining"] for v in usable), 2),
            "expired_remaining": round(sum(v["remaining"] for v in expired), 2),
            "total_capacity": round(sum(v["capacity"] for v in vouchers), 2),
            "total_used": round(sum(v["used"] for v in vouchers), 2),
            "plan_name": plan_name,
            "vouchers": vouchers,
            "usable_count": len(usable),
            "soonest_expire_at": soonest,
            "expiring_soon": expiring_soon,
        }
    except CreditsError as error:
        return {"ok": False, "error": str(error)}
    except Exception as error:  # 网络栈异常不应让 cbs整体崩掉
        return {"ok": False, "error": "查询失败：%s" % error}


def query_all(store, only=None, now_ms=None):
    """查库存档（可指定单个别名）。单账号失败不影响其它账号。"""
    now_ms = now_ms or _now_ms()
    out = {}
    for alias, record in store.get("accounts", {}).items():
        if only and alias != only:
            continue
        out[alias] = query(record, now_ms)
    return out


def _now_ms():
    import time
    return int(time.time() * 1000)


def _far_future():
    import datetime
    return (datetime.datetime.now() + datetime.timedelta(days=3650)).strftime(
        "%Y-%m-%d %H:%M:%S")


def format_credits(value):
    """去掉无意义的小数尾巴：1525.74保留，500.0 显示 500。"""
    if value is None:
        return "-"
    if abs(value - round(value)) < 0.01:
        return str(int(round(value)))
    return ("%.2f" % value).rstrip("0").rstrip(".")


def format_expire(expire_ms, now_ms=None):
    """长期有效显示「长期有效」，否则显示日期。7 天内到期额外标红提示。"""
    if expire_ms is None:
        return "长期有效"
    import time
    now_ms = now_ms or _now_ms()
    text = time.strftime("%Y-%m-%d", time.localtime(expire_ms / 1000))
    if expire_ms <= now_ms:
        return text + " 已过期"
    if expire_ms - now_ms <= EXPIRING_SOON_MS:
        days = max(1, (expire_ms - now_ms) // DAY_MS)
        return "%s (%d天后)" % (text, days)
    return text