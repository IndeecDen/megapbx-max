from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

import httpx

from .config import Settings
from .max_api.models import User

logger = logging.getLogger(__name__)

PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{5,}\d")
DENY_KEYS_SUBSTR = (
    "token",
    "secret",
    "sign",
    "signature",
    "auth",
    "authorization",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
)
DENY_KEYS_EXACT = {
    "callid",
    "id",
    "start",
    "wait",
    "duration",
    "user",
    "ext",
    "telnum",
    "diversion",
    "grouprealname",
    "type",
    "status",
    "cmd",
}
CALL_FAIL_STATUSES = {
    "missed": "📵 Не взяли трубку",
    "busy": "☎️ Занято",
    "cancel": "🚫 Отменён",
    "notavailable": "📴 Недоступен",
    "notallowed": "⛔️ Направление запрещено",
    "notfound": "❓ Абонент не найден",
}


def now_local(offset_hours: int) -> datetime:
    return datetime.now(timezone(timedelta(hours=offset_hours)))


def format_time(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M")


def as_text(value: Any) -> str:
    return str(value or "").strip()


def html_text(value: Any, *, max_length: int | None = None) -> str:
    text = as_text(value)
    if max_length is not None and len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return escape(text, quote=False)


def fingerprint(value: Any) -> str:
    raw = as_text(value).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:12]


def safe_log_value(value: Any, max_len: int = 32) -> str:
    value = as_text(value)[:max_len]
    allowed = {
        "history",
        "event",
        "missed",
        "success",
        "busy",
        "cancel",
        "notavailable",
        "notallowed",
        "notfound",
        "accepted",
        "completed",
        "outgoing",
        "answered",
        "connected",
        "out",
        "in",
    }
    if value.casefold() not in allowed:
        return "other"
    return re.sub(r"[^A-Za-z0-9_.:-]", "?", value)


def is_phone(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    text = str(value).strip()
    digits = re.sub(r"[^0-9]", "", text)
    return 7 <= len(digits) <= 15 and bool(PHONE_RE.fullmatch(text))


def normalize_phone(number: str) -> str:
    digits = re.sub(r"[^0-9]", "", as_text(number))
    if len(digits) == 10:
        digits = f"7{digits}"
    elif len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    return f"+{digits}"


def _key_allowed(key: str) -> bool:
    normalized = key.casefold()
    return normalized not in DENY_KEYS_EXACT and not any(
        forbidden in normalized for forbidden in DENY_KEYS_SUBSTR
    )


def find_phone_anywhere(value: dict[str, Any]) -> str | None:
    stack: list[Any] = [value]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, dict):
            for key, nested in current.items():
                if not _key_allowed(str(key)):
                    continue
                if isinstance(nested, (dict, list, tuple)):
                    stack.append(nested)
                elif isinstance(nested, (str, int)) and is_phone(nested):
                    return str(nested).strip()
        elif isinstance(current, (list, tuple)):
            for nested in current:
                if isinstance(nested, (dict, list, tuple)):
                    stack.append(nested)
                elif isinstance(nested, (str, int)) and is_phone(nested):
                    return str(nested).strip()
        elif isinstance(current, (str, int)) and is_phone(current):
            return str(current).strip()
    return None


def is_missed_call(payload: dict[str, Any]) -> bool:
    return (
        as_text(payload.get("cmd")).casefold() == "history"
        and as_text(payload.get("status")).casefold() == "missed"
        and as_text(payload.get("type")).casefold() != "out"
    )


def is_allowed_destination(payload: dict[str, Any], settings: Settings) -> bool:
    if settings.megapbx_allow_all:
        return True
    group = as_text(payload.get("groupRealName"))
    telnum = as_text(payload.get("telnum"))
    diversion = as_text(payload.get("diversion"))
    if group and group in settings.megapbx_allowed_groups:
        return True
    return bool(
        (telnum and telnum in settings.megapbx_allowed_dids)
        or (diversion and diversion in settings.megapbx_allowed_dids)
    )


def extract_destination(payload: dict[str, Any], settings: Settings, directory: PbxDirectory) -> str:
    group = as_text(payload.get("groupRealName"))
    telnum = as_text(payload.get("telnum"))
    diversion = as_text(payload.get("diversion"))
    ext = as_text(payload.get("ext"))
    user = as_text(payload.get("user"))
    if not group:
        group = (
            directory.groups.get(diversion)
            or directory.groups.get(telnum)
            or settings.megapbx_did_names.get(diversion)
            or settings.megapbx_did_names.get(telnum)
            or ""
        )
    return html_text(next((item for item in (group, telnum, diversion, ext, user) if item), "неизвестно"), max_length=300)


def display_caller(payload: dict[str, Any]) -> tuple[str, str]:
    name = html_text(payload.get("contact_name") or payload.get("name"), max_length=200)
    number = payload.get("phone")
    raw_phone = ""
    if is_phone(number):
        raw_phone = normalize_phone(str(number))
    else:
        fallback = find_phone_anywhere(payload)
        if is_phone(fallback):
            raw_phone = normalize_phone(str(fallback or ""))
    phone_html = f"<code>{raw_phone}</code>" if raw_phone else ""
    if name and raw_phone:
        return f"{name} ({phone_html})", raw_phone
    if name:
        return name, raw_phone
    if raw_phone:
        return phone_html, raw_phone
    return "неизвестно", raw_phone


def user_full_name(user: User | dict[str, Any] | None) -> str:
    if user is None:
        return "пользователь"
    if isinstance(user, User):
        data = user.model_dump()
    else:
        data = user
    first = as_text(data.get("first_name"))
    last = as_text(data.get("last_name"))
    return ((f"{first} {last}").strip() or as_text(data.get("username")) or "пользователь")[:200]


def build_missed_text(
    from_text: str,
    to_text: str,
    *,
    wait: Any = None,
    duration: Any = None,
    today: int = 0,
    total: int = 0,
    offset_hours: int = 3,
) -> str:
    wait_value = _positive_int(wait)
    duration_value = _positive_int(duration)
    parts = [
        "📵 <b>Пропущенный звонок</b>",
        f"🕐 {format_time(now_local(offset_hours))}",
        f"От: {from_text}",
        f"Кому: {to_text}",
    ]
    timing: list[str] = []
    if wait_value:
        timing.append(f"ожидание: {wait_value} с")
    if duration_value:
        timing.append(f"длительность: {duration_value} с")
    if timing:
        parts.append("⏱ Время: " + ", ".join(timing))
    if today > 1 or total > 1:
        counter: list[str] = []
        if today > 1:
            counter.append(f"сегодня: {today}")
        if total > today:
            counter.append(f"всего: {total}")
        parts.append("🔁 Пропущено (" + ", ".join(counter) + ")")
    return "\n".join(parts)[:4000]


def callback_status_text(status: str) -> str:
    return CALL_FAIL_STATUSES.get(status.casefold(), f"❌ {html_text(status, max_length=100)}")


def append_callback_status(text: str, who: str, status: str) -> str:
    lines = [line for line in text.split("\n") if not line.startswith("↩️")]
    lines.append(f"↩️ {html_text(who, max_length=200)}: {callback_status_text(status)}")
    return "\n".join(lines)[:4000]


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


class PbxDirectory:
    def __init__(
        self,
        base_url: str | None,
        token: str | None,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_sec: float = 20.0,
        verify: str | bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.token = token
        self._owns_client = http_client is None
        self._client = http_client or (
            httpx.AsyncClient(timeout=timeout_sec, verify=verify) if self.enabled else None
        )
        self.accounts: dict[str, str] = {}
        self.groups: dict[str, str] = {}
        self._refresh_lock = asyncio.Lock()
        self._accounts_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def resolve_user(self, user: str) -> str:
        login = as_text(user)
        if not login:
            return "сотрудник"
        if not self.accounts:
            async with self._accounts_lock:
                if not self.accounts:
                    fresh = await self._fetch_accounts(raise_errors=False)
                    if fresh:
                        self.accounts = fresh
        return self.accounts.get(login, login)

    async def refresh(self) -> None:
        if not self.enabled:
            return
        async with self._refresh_lock:
            try:
                accounts = await self._fetch_accounts(raise_errors=False)
                groups = await self._fetch_groups(raise_errors=False)
                if accounts:
                    self.accounts = accounts
                    logger.info("PBX accounts refreshed: count=%d", len(accounts))
                if groups:
                    self.groups = groups
                    logger.info("PBX groups refreshed: count=%d", len(groups))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("PBX directory refresh failed: %s", type(exc).__name__)

    async def run_forever(self, interval_sec: int) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(interval_sec)

    async def _fetch_accounts(self, *, raise_errors: bool) -> dict[str, str]:
        if not self.enabled:
            return {}
        result: dict[str, str] = {}
        start = 0
        try:
            while True:
                data = await self._get_page("/crmapi/v1/users", start)
                items = data.get("items", [])
                if not isinstance(items, list):
                    break
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    login = as_text(item.get("login"))
                    name = as_text(item.get("name"))
                    if login:
                        result[login] = name or login
                start += len(items)
                total = _page_total(data)
                if not items or start >= total:
                    break
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("PBX accounts fetch failed: %s", type(exc).__name__)
            if raise_errors:
                raise
            return {}
        return result

    async def _fetch_groups(self, *, raise_errors: bool) -> dict[str, str]:
        if not self.enabled:
            return {}
        result: dict[str, str] = {}
        start = 0
        try:
            while True:
                data = await self._get_page("/crmapi/v1/telnums", start)
                items = data.get("items", [])
                if not isinstance(items, list):
                    break
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    telnum = as_text(item.get("telnum"))
                    if not telnum:
                        continue
                    if as_text(item.get("type")) == "ivr":
                        name = _ivr_group_name(item.get("ivr"))
                    else:
                        name = as_text(
                            item.get("group_name") or item.get("user_name") or item.get("name")
                        )
                    if name:
                        result[telnum] = name
                start += len(items)
                total = _page_total(data)
                if not items or start >= total:
                    break
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("PBX groups fetch failed: %s", type(exc).__name__)
            if raise_errors:
                raise
            return {}
        return result

    async def _get_page(self, path: str, start: int) -> dict[str, Any]:
        if not self.base_url or not self.token or self._client is None:
            return {}
        response = await self._client.get(
            f"{self.base_url}{path}",
            headers={"X-API-KEY": self.token},
            params={"start": start, "limit": 100},
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("PBX API response must be an object")
        return value


def _ivr_group_name(ivr: Any) -> str:
    if not isinstance(ivr, dict):
        return ""
    items = ivr.get("items")
    if not isinstance(items, list):
        return ""
    first_group = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        group_name = as_text(item.get("group_name"))
        if item.get("button") == "timeout" and group_name:
            return group_name
        if not first_group and group_name:
            first_group = group_name
    return first_group


def _page_total(data: dict[str, Any]) -> int:
    info = data.get("info")
    if not isinstance(info, dict):
        return 0
    try:
        return max(0, int(info.get("total", 0)))
    except (TypeError, ValueError):
        return 0
