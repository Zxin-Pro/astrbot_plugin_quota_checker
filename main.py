import asyncio
import datetime
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

USER_AGENT = "AstrBot-QuotaChecker/1.0"


class LogPermissionError(Exception):
    """日志接口无权限（401/403，需管理员令牌）"""


class LogUnavailableError(Exception):
    """日志接口不可用（非权限原因）"""


REQUEST_TIMEOUT = 15          # 单请求超时（秒）
PAGE_SIZE = 100               # /api/log 分页大小
MAX_LOG_PAGES = 20            # 日志翻页上限，防止无限拉取


def _day_range_ts() -> Tuple[int, int]:
    """当日 00:00:00 ~ 次日 00:00:00 的 Unix 时间戳"""
    today = datetime.date.today()
    start = datetime.datetime(today.year, today.month, today.day)
    end = start + datetime.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _dig(data: Any, *names) -> Optional[Any]:
    """在嵌套 dict/list 中（递归、按顺序优先）查找第一个非空的目标字段"""
    if isinstance(data, dict):
        for n in names:
            v = data.get(n)
            if v is not None:
                return v
        for v in data.values():
            r = _dig(v, *names)
            if r is not None:
                return r
    elif isinstance(data, list):
        for item in data:
            r = _dig(item, *names)
            if r is not None:
                return r
    return None


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _fmt_money(raw_quota: Optional[int], divisor: int) -> str:
    if raw_quota is None:
        return "暂无数据"
    return f"${raw_quota / divisor:,.2f}"


def _fmt_int(v: Optional[int]) -> str:
    return f"{v:,}" if v is not None else "暂无数据"


def _usd(value: Any) -> str:
    """WorldCodes 金额已经是美元，保留小数，不使用 quota_divisor。"""
    try:
        amount = Decimal(str(value))
        if amount.is_finite():
            return f"${amount:,.2f}"
    except (InvalidOperation, ValueError, TypeError):
        pass
    return "暂无数据"


def _obj(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tpl_money(value: Any, unit: str) -> str:
    try:
        amount = Decimal(str(value))
        if amount.is_finite():
            u = (unit or "USD").upper()
            return f"${amount:,.2f}" if u == "USD" else f"{amount:,.2f} {u}"
    except (InvalidOperation, ValueError, TypeError):
        pass
    return "暂无数据"


def _render_template(tpl: str, data: Dict[str, Any]) -> str:
    """自定义模板渲染，支持 {balance} {total_cost} 等占位符"""
    unit = str(data.get("unit") or "USD")
    usage = _obj(data.get("usage"))
    total, today = _obj(usage.get("total")), _obj(usage.get("today"))
    quota = _obj(data.get("quota"))

    balance = data.get("balance")
    if balance is None:
        r = data.get("remaining")
        balance = "无限制" if r == -1 else r
    balance_text = balance if isinstance(balance, str) else _tpl_money(balance, unit)

    remaining = data.get("remaining")
    if remaining == -1:
        remaining_text = "无限制"
    elif remaining is None:
        remaining_text = "暂无数据"
    else:
        remaining_text = _tpl_money(remaining, unit)

    mapping = {
        "balance": balance_text,
        "remaining": remaining_text,
        "key_remaining": _tpl_money(quota.get("remaining"), unit),
        "plan_name": str(data.get("planName") or "暂无数据"),
        "unit": unit,
        "mode": str(data.get("mode") or "暂无数据"),
        "status": str(data.get("status") or "暂无数据"),
        "total_cost": _tpl_money(total.get("actual_cost"), unit),
        "total_tokens": _fmt_int(_to_int(total.get("total_tokens"))),
        "total_requests": _fmt_int(_to_int(total.get("requests"))),
        "today_cost": _tpl_money(today.get("actual_cost"), unit),
        "today_tokens": _fmt_int(_to_int(today.get("total_tokens"))),
        "today_requests": _fmt_int(_to_int(today.get("requests"))),
    }
    out = tpl
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _usage_report(data: Dict[str, Any], show_usage: bool = True) -> str:
    """按明确字段解析 /v1/usage，避免将 Key 消费误标为全账号消费。"""
    mode = data.get("mode")
    if mode not in ("unrestricted", "quota_limited"):
        return "无法识别 /v1/usage 响应，请检查站点和接口配置"
    if data.get("isValid") is False:
        return "API Key 不可用，请检查 Key 状态"
    lines = ["📊 额度与用量", "━━━━━━━━━━━━"]
    if mode == "quota_limited":
        quota = _obj(data.get("quota"))
        if quota:
            lines.append(f"✅ 当前 Key 剩余额度：{_usd(quota.get('remaining'))}")
        if show_usage:
            for limit in data.get("rate_limits") or []:
                if isinstance(limit, dict):
                    lines.append(f"⏳ 当前 Key {limit.get('window', '')} 剩余额度：{_usd(limit.get('remaining'))}")
            lines.append("账号钱包余额：此 Key 的接口响应未提供")
    elif "balance" in data:
        lines.append(f"✅ 账号钱包余额：{_usd(data['balance'])}")
    else:
        remaining = data.get("remaining")
        label = "无限制" if remaining == -1 else _usd(remaining)
        lines.append(f"✅ 订阅剩余额度：{label}")
    if show_usage:
        if data.get("status"):
            lines.append(f"Key 状态：{data['status']}")
        usage = _obj(data.get("usage"))
        total, today = _obj(usage.get("total")), _obj(usage.get("today"))
        lines.extend([
            "━━━━━━━━━━━━",
            f"📉 当前 Key 累计消费：{_usd(total.get('actual_cost'))}",
            f"🪙 当前 Key 累计 Token：{_fmt_int(_to_int(total.get('total_tokens')))}",
            f"📅 当前 Key 今日消费：{_usd(today.get('actual_cost'))}",
            f"🪙 当前 Key 今日 Token：{_fmt_int(_to_int(today.get('total_tokens')))}",
            "金额单位：USD；今日按站点统计口径",
        ])
    return "\n".join(lines)


@register(
    "astrbot_plugin_quota_checker",
    "Zxin-Pro",
    "查询 AI 中转站（One-API / New-API 等）的额度与 Token 消耗统计",
    "v1.3.0",
    "https://github.com/Zxin-Pro/astrbot_plugin_quota_checker",
)
class QuotaCheckerPlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        self.config = config or {}

    # ---------- 配置辅助 ----------

    def _cfg(self, key: str, default: Any = None) -> Any:
        v = self.config.get(key, default)
        return default if v is None else v

    def _base_url(self) -> str:
        base = str(self._cfg("base_url", "") or "").strip().rstrip("/")
        if base and not base.startswith(("http://", "https://")):
            base = "https://" + base
        return base

    def _headers(self, api_key: Optional[str] = None) -> Dict[str, str]:
        key = str(api_key or self._cfg("api_key", "") or "").strip()
        headers = {
            "Authorization": f"Bearer {key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        user_id = str(self._cfg("api_user_id", "") or "").strip()
        if user_id:
            headers["New-Api-User"] = user_id
            headers["Veloera-User"] = user_id  # 不同中转站头名不同，多带无副作用
        extra = self._cfg("extra_headers", "") or ""
        if isinstance(extra, str):
            try:
                extra = json.loads(extra) if extra.strip() else {}
            except json.JSONDecodeError:
                logger.warning(f"[quota_checker] extra_headers 不是合法 JSON，已忽略: {extra}")
                extra = {}
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    # ---------- HTTP ----------

    async def _get(
        self,
        http: aiohttp.ClientSession,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        url = self._base_url() + path
        try:
            async with http.get(url, params=params, headers=self._headers(api_key)) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                logger.info(f"[quota_checker] GET {url} -> {status}")
                if status >= 400:
                    logger.warning(f"[quota_checker] 响应异常: {url} status={status}")
                return status, data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"[quota_checker] 请求失败: {url} ({type(e).__name__}: {e})")
            raise

    # ---------- 数据解析 ----------

    def _sum_log_items(self, items: List[Dict[str, Any]]) -> Tuple[int, int]:
        """累加当日日志的 quota 与 token（type=2 为消费记录，非消费不计入）"""
        quota_sum = token_sum = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            t = it.get("type")
            if t is not None and _to_int(t) not in (None, 2):
                continue
            quota_sum += _to_int(it.get("quota")) or 0
            pt = _to_int(it.get("prompt_tokens")) or 0
            ct = _to_int(it.get("completion_tokens")) or 0
            tk = _to_int(it.get("token_used"))  # 兼容自定义字段
            token_sum += (pt + ct) if (pt or ct) else (tk or 0)
        return quota_sum, token_sum

    def _extract_log_items(self, data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """兼容 {data:[...]}（One-API）与 {data:{items:[...]}}（New-API）两种结构"""
        if not isinstance(data, dict):
            return []
        d = data.get("data")
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d["items"]
        if isinstance(d, list):
            return d
        if isinstance(data.get("items"), list):
            return data["items"]
        return []

    async def _fetch_daily_stats(self, http: aiohttp.ClientSession) -> Tuple[int, int]:
        """遍历当日日志，返回（当日消耗 quota, 当日 token）；无权限时抛 LogPermissionError"""
        start_ts, end_ts = _day_range_ts()
        quota_sum = token_sum = 0
        for page in range(1, MAX_LOG_PAGES + 1):
            st, data = await self._get(
                http,
                str(self._cfg("api_path_log", "/api/log")),
                params={
                    "start_timestamp": start_ts,
                    "end_timestamp": end_ts,
                    "p": page,
                    "page_size": PAGE_SIZE,
                },
            )
            if st in (401, 403):
                raise LogPermissionError()
            if st != 200:
                raise LogUnavailableError(f"status={st}")
            items = self._extract_log_items(data)
            q, t = self._sum_log_items(items)
            quota_sum += q
            token_sum += t
            if len(items) < PAGE_SIZE:
                break
        return quota_sum, token_sum

    # ---------- 多 Key 查询 ----------

    def _fmt_key_usage(self, label: str, data: Dict[str, Any]) -> str:
        """单个 Key 的用量块（Token + 消费）"""
        unit = str(data.get("unit") or "USD")
        usage = _obj(data.get("usage"))
        total, today = _obj(usage.get("total")), _obj(usage.get("today"))
        return "\n".join([
            f"📋 {label}",
            f"🪙 累计 Token：{_fmt_int(_to_int(total.get('total_tokens')))} ｜ 今日：{_fmt_int(_to_int(today.get('total_tokens')))}",
            f"📉 累计消费：{_tpl_money(total.get('actual_cost'), unit)} ｜ 今日：{_tpl_money(today.get('actual_cost'), unit)}",
        ])

    async def _multi_key_report(self, http: aiohttp.ClientSession, entries: List[str]) -> str:
        """余额取第一个 Key，全部 Key 逐个查用量后汇总"""
        path = str(self._cfg("api_path_v1_usage", "/v1/usage")) or "/v1/usage"
        tpl = str(self._cfg("template", "") or "").strip()
        balance_block: Optional[str] = None
        usage_blocks: List[str] = []
        sum_total = sum_today = 0
        has_tok = False
        for i, entry in enumerate(entries, 1):
            if ":" in entry and not entry.lstrip().lower().startswith("sk-"):
                label, key = entry.split(":", 1)
                label, key = label.strip(), key.strip()
            else:
                label, key = "", entry
            if not label:
                label = f"Key {i}（*{key[-4:]}）" if len(key) >= 4 else f"Key {i}"
            try:
                st, data = await self._get(http, path, api_key=key)
                if st == 200 and isinstance(data, dict) and data.get("isValid") is not False:
                    if i == 1:  # 余额固定用第一个 Key
                        balance_block = _render_template(tpl, data) if tpl else _usage_report(data, show_usage=False)
                    usage_blocks.append(self._fmt_key_usage(label, data))
                    usage = _obj(data.get("usage"))
                    t = _to_int(_obj(usage.get("total")).get("total_tokens"))
                    d = _to_int(_obj(usage.get("today")).get("total_tokens"))
                    if t is not None or d is not None:
                        sum_total += t or 0
                        sum_today += d or 0
                        has_tok = True
                elif st in (401, 403):
                    usage_blocks.append(f"📋 {label}\n❌ Token 无效或已过期，或权限不足")
                elif st == 404:
                    usage_blocks.append(f"📋 {label}\n❌ 接口路径错误，请检查配置")
                else:
                    usage_blocks.append(f"📋 {label}\n❌ 查询失败（HTTP {st}）")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                usage_blocks.append(f"📋 {label}\n❌ 无法连接到中转站")
            if i < len(entries):
                await asyncio.sleep(0.3)  # 轻微间隔，防限流
        if balance_block is None:
            balance_block = "❌ 余额查询失败（第一个 Key 无效或网络异常）"
        blocks = [balance_block] + usage_blocks
        if len(entries) > 1 and has_tok:
            blocks.append(f"🪙 合计 Token：{_fmt_int(sum_total)} ｜ 今日：{_fmt_int(sum_today)}")
        return "\n\n".join(blocks)

    # ---------- 命令 ----------

    @filter.command("额度")
    async def quota(self, event: AstrMessageEvent):
        """查询 AI 中转站的额度与 Token 消耗统计"""
        # 群白名单
        gid = event.get_group_id()
        if gid:
            allowed = [str(g) for g in (self._cfg("allowed_groups", []) or [])]
            if allowed and str(gid) not in allowed:
                return

        if not self._base_url() or (
            not str(self._cfg("api_key", "") or "").strip() and not (self._cfg("api_keys", []) or [])
        ):
            yield event.plain_result("请先在插件配置中填写中转站地址和令牌")
            return

        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                # 多 Key 模式：配置了 api_keys 时逐个查询 /v1/usage（WorldCodes/Sub2API 协议）
                keys_cfg = [str(k).strip() for k in (self._cfg("api_keys", []) or []) if str(k).strip()]
                if keys_cfg:
                    yield event.plain_result(await self._multi_key_report(http, keys_cfg))
                    return

                # 用户信息路径设为 /v1/usage 时，使用 WorldCodes/Sub2API 协议。
                if str(self._cfg("api_path_user", "")).strip() == "/v1/usage":
                    st, data = await self._get(http, "/v1/usage")
                    if st in (401, 403):
                        message = f"额度查询被拒绝（HTTP {st}），请检查 Key、权限或站点访问限制"
                    elif st == 404:
                        message = "接口路径错误（HTTP 404），请使用站点根地址和 /v1/usage"
                    elif st != 200 or not isinstance(data, dict):
                        message = f"额度接口返回异常（HTTP {st}），请检查插件日志和站点响应"
                    else:
                        tpl = str(self._cfg("template", "") or "").strip()
                        if tpl and data.get("isValid") is not False:
                            message = _render_template(tpl, data)
                        elif data.get("isValid") is False:
                            message = "API Key 不可用，请检查 Key 状态"
                        else:
                            message = _usage_report(data, show_usage=bool(self._cfg("show_usage", False)))
                    yield event.plain_result(message)
                    return

                divisor = _to_int(self._cfg("quota_divisor", 500000)) or 500000

                # 1) /api/status（可选）：自动检测 quota_per_unit
                try:
                    st, data = await self._get(http, "/api/status")
                    if st == 200 and isinstance(data, dict):
                        qpu = _dig(data, "quota_per_unit")
                        qpu = _to_int(qpu)
                        if qpu and qpu > 0:
                            divisor = qpu
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass  # 可选接口，失败不阻塞

                # 2) 账户额度（必需）
                st, data = await self._get(http, str(self._cfg("api_path_user", "/api/user/self")))
                if st in (401, 403):
                    yield event.plain_result("Token 无效或已过期，或权限不足")
                    return
                if st == 404:
                    yield event.plain_result("接口路径错误，请检查配置")
                    return
                if st != 200 or not isinstance(data, dict):
                    yield event.plain_result("无法解析数据，响应格式可能已变更")
                    return
                used_quota = _to_int(_dig(data, "used_quota"))
                remain_quota = _to_int(_dig(data, "quota"))
                if used_quota is None and remain_quota is None:
                    yield event.plain_result("无法解析数据，响应格式可能已变更")
                    return

                # 3) Token 用量（可选，失败不阻塞）
                total_used = total_available = None
                try:
                    st2, data2 = await self._get(http, str(self._cfg("api_path_usage", "/api/usage/token")))
                    if st2 == 200 and isinstance(data2, dict):
                        total_used = _to_int(_dig(data2, "total_used"))
                        total_available = _to_int(_dig(data2, "total_available"))
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass

                # 4) 当日消耗（需管理员权限，403 时优雅降级）
                day_quota = day_tokens = log_note = None
                try:
                    day_quota, day_tokens = await self._fetch_daily_stats(http)
                except LogPermissionError:
                    log_note = "不可用（查询日志需要管理员权限的令牌）"
                except LogUnavailableError as e:
                    logger.warning(f"[quota_checker] 日志接口不可用: {e}")
                    log_note = "不可用（日志接口返回异常）"

            lines = ["📊 账户统计", "━━━━━━━━━━━━"]
            lines.append(f"📉 总消耗额度：{_fmt_money(used_quota, divisor)}")
            lines.append(f"🪙 总 Token：{_fmt_int(total_used)}")
            lines.append("━━━━━━━━━━━━")
            if day_quota is not None:
                lines.append(f"📅 当日消耗额度：{_fmt_money(day_quota, divisor)}")
                lines.append(f"🪙 当日 Token：{_fmt_int(day_tokens)}")
            else:
                lines.append(f"📅 当日消耗：{log_note or '暂无数据'}")
            lines.append("━━━━━━━━━━━━")
            lines.append(f"✅ 剩余额度：{_fmt_money(remain_quota, divisor)}")
            yield event.plain_result("\n".join(lines))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            yield event.plain_result("无法连接到中转站，请检查网络或地址")
        except Exception as e:
            logger.error(f"[quota_checker] 未知错误: {type(e).__name__}: {e}")
            yield event.plain_result("查询失败，请稍后重试或查看日志")
