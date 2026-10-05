from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

MAX_INT64 = 2**63 - 1
DEFAULT_MAX_API_BASE = "https://platform-api2.max.ru"
DEFAULT_MAX_UPDATE_TYPES = (
    "message_callback",
    "bot_started",
    "bot_added",
    "bot_removed",
    "bot_admin_permissions_changed",
)


class ConfigurationError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


def _get(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = env.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _required(env: Mapping[str, str], name: str) -> str:
    value = _get(env, name)
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is missing")
    if "\r" in value or "\n" in value:
        raise ConfigurationError(f"{name} must not contain line breaks")
    return value


def _parse_int(env: Mapping[str, str], name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(env, name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(env, name)
    if raw is None:
        return default
    normalized = raw.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be one of: 1, 0, true, false, yes, no, on, off")


def _csv(env: Mapping[str, str], name: str) -> frozenset[str]:
    raw = _get(env, name)
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _https_url(value: str, name: str) -> str:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"{name} contains an invalid port") from exc
    if port is not None and port < 1:
        raise ConfigurationError(f"{name} contains an invalid port")
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            f"{name} must be an absolute HTTPS URL without embedded credentials, query or fragment"
        )
    return value.rstrip("/")


def validate_max_webhook_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("MAX_WEBHOOK_URL contains an invalid port") from exc
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname or port not in {None, 443}:
        raise ConfigurationError("MAX_WEBHOOK_URL must use HTTPS on port 443")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError("MAX_WEBHOOK_URL must not contain credentials, query or fragment")
    return value.rstrip("/")


def _optional_api_url(value: str | None, name: str, *, allow_http: bool) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"{name} contains an invalid port") from exc
    if port is not None and port < 1:
        raise ConfigurationError(f"{name} contains an invalid port")
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        expected = "HTTP(S)" if allow_http else "HTTPS"
        raise ConfigurationError(f"{name} must be an absolute {expected} URL without credentials, query or fragment")
    if parsed.path.rstrip("/").casefold().endswith("/crmapi/v1"):
        raise ConfigurationError(f"{name} must not include the /crmapi/v1 suffix")
    return value.rstrip("/")


def _did_names(value: str | None) -> dict[str, str]:
    """Parse `DID=Name; DID2=Name2` (also accepts commas as separators)."""
    if not value:
        return {}
    result: dict[str, str] = {}
    for item in value.replace(",", ";").split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ConfigurationError("MEGAPBX_DID_NAMES must use DID=Name; DID2=Name2 format")
        did, name = item.split("=", 1)
        did, name = did.strip(), name.strip()
        if not did or not name:
            raise ConfigurationError("MEGAPBX_DID_NAMES contains an empty DID or name")
        result[did] = name
    return result


@dataclass(frozen=True, slots=True)
class Settings:
    max_bot_token: str
    max_chat_id: int
    max_api_base: str = DEFAULT_MAX_API_BASE
    max_webhook_secret: str | None = None
    max_webhook_url: str | None = None
    max_webhook_update_types: tuple[str, ...] = DEFAULT_MAX_UPDATE_TYPES
    max_api_max_retries: int = 2
    max_api_retry_base_sec: float = 0.5
    max_api_retry_max_sec: float = 8.0
    max_api_timeout_sec: float = 8.0
    max_webhook_body_bytes: int = 1_048_576
    state_db_path: str = "data/state.sqlite3"

    megapbx_crm_token: str = ""
    megapbx_allowed_groups: frozenset[str] = field(default_factory=frozenset)
    megapbx_allowed_dids: frozenset[str] = field(default_factory=frozenset)
    megapbx_did_names: dict[str, str] = field(default_factory=dict)
    megapbx_api_base: str | None = None
    megapbx_api_token: str | None = None
    megapbx_allow_all: bool = False
    megapbx_allow_query_token: bool = False
    megapbx_allow_http_api: bool = False
    megapbx_webhook_body_bytes: int = 1_048_576
    megapbx_enrichment_refresh_sec: int = 600

    tz_offset_hours: int = 3
    missed_max_age_sec: int = 3600
    missed_cleanup_interval_sec: int = 3600
    missed_dedup_ttl_sec: int = 86_400
    job_max_attempts: int = 20
    job_retry_base_sec: float = 1.0
    job_retry_max_sec: float = 300.0
    job_lease_sec: float = 300.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        max_chat_id = _parse_int(values, "MAX_CHAT_ID", 0, minimum=1)
        if max_chat_id > MAX_INT64:
            raise ConfigurationError("MAX_CHAT_ID must fit into a signed 64-bit integer")

        max_webhook_secret = _get(values, "MAX_WEBHOOK_SECRET")
        if max_webhook_secret is not None and not 5 <= len(max_webhook_secret) <= 256:
            raise ConfigurationError("MAX_WEBHOOK_SECRET must contain 5 to 256 characters")
        if max_webhook_secret is not None and not all(
            char.isascii() and (char.isalnum() or char in {"_", "-"}) for char in max_webhook_secret
        ):
            raise ConfigurationError("MAX_WEBHOOK_SECRET may contain only A-Z, a-z, 0-9, '_' and '-'")

        max_webhook_url_raw = _get(values, "MAX_WEBHOOK_URL")
        max_webhook_url = validate_max_webhook_url(max_webhook_url_raw) if max_webhook_url_raw else None
        if max_webhook_url is not None and max_webhook_secret is None:
            raise ConfigurationError("MAX_WEBHOOK_SECRET is required when MAX_WEBHOOK_URL is set")

        update_types_raw = _get(values, "MAX_WEBHOOK_UPDATE_TYPES")
        update_types = (
            tuple(item.strip() for item in update_types_raw.split(",") if item.strip())
            if update_types_raw
            else DEFAULT_MAX_UPDATE_TYPES
        )
        if "message_callback" not in update_types:
            raise ConfigurationError("MAX_WEBHOOK_UPDATE_TYPES must include message_callback")
        if len(set(update_types)) != len(update_types):
            raise ConfigurationError("MAX_WEBHOOK_UPDATE_TYPES contains duplicates")

        max_api_base = _https_url(
            _get(values, "MAX_API_BASE", DEFAULT_MAX_API_BASE) or DEFAULT_MAX_API_BASE,
            "MAX_API_BASE",
        )
        allow_http_api = _parse_bool(values, "MEGAPBX_ALLOW_HTTP_API", False)
        megapbx_api_base = _optional_api_url(
            _get(values, "MEGAPBX_API_BASE"),
            "MEGAPBX_API_BASE",
            allow_http=allow_http_api,
        )
        megapbx_api_token = _get(values, "MEGAPBX_API_TOKEN")
        if megapbx_api_token and ("\r" in megapbx_api_token or "\n" in megapbx_api_token):
            raise ConfigurationError("MEGAPBX_API_TOKEN must not contain line breaks")
        if (megapbx_api_base is None) != (megapbx_api_token is None):
            raise ConfigurationError("MEGAPBX_API_BASE and MEGAPBX_API_TOKEN must be configured together")
        allowed_groups = _csv(values, "MEGAPBX_ALLOWED_GROUP")
        allowed_dids = _csv(values, "MEGAPBX_ALLOWED_DID")
        allow_all = _parse_bool(values, "MEGAPBX_ALLOW_ALL", False)
        if not allowed_groups and not allowed_dids and not allow_all:
            raise ConfigurationError(
                "Configure MEGAPBX_ALLOWED_GROUP and/or MEGAPBX_ALLOWED_DID, "
                "or explicitly set MEGAPBX_ALLOW_ALL=1"
            )

        tz_offset_hours = _parse_int(values, "TZ_OFFSET_HOURS", 3, minimum=-12)
        if tz_offset_hours > 14:
            raise ConfigurationError("TZ_OFFSET_HOURS must be between -12 and 14")

        state_db_path = _get(values, "STATE_DB_PATH", "data/state.sqlite3") or "data/state.sqlite3"
        if state_db_path == ":memory:" or "\x00" in state_db_path or "\n" in state_db_path or "\r" in state_db_path:
            raise ConfigurationError("STATE_DB_PATH must be a persistent filesystem path")

        return cls(
            max_bot_token=_required(values, "MAX_BOT_TOKEN"),
            max_chat_id=max_chat_id,
            max_api_base=max_api_base,
            max_webhook_secret=max_webhook_secret,
            max_webhook_url=max_webhook_url,
            max_webhook_update_types=update_types,
            max_api_max_retries=_parse_int(values, "MAX_API_MAX_RETRIES", 2, minimum=0),
            max_api_retry_base_sec=_parse_float(values, "MAX_API_RETRY_BASE_SEC", 0.5, minimum=0.0),
            max_api_retry_max_sec=_parse_float(values, "MAX_API_RETRY_MAX_SEC", 8.0, minimum=0.0),
            max_api_timeout_sec=_parse_float(values, "MAX_API_TIMEOUT_SEC", 8.0, minimum=1.0),
            max_webhook_body_bytes=_parse_int(values, "MAX_WEBHOOK_BODY_BYTES", 1_048_576, minimum=1),
            state_db_path=state_db_path,
            megapbx_crm_token=_required(values, "MEGAPBX_CRM_TOKEN"),
            megapbx_allowed_groups=allowed_groups,
            megapbx_allowed_dids=allowed_dids,
            megapbx_did_names=_did_names(_get(values, "MEGAPBX_DID_NAMES")),
            megapbx_api_base=megapbx_api_base,
            megapbx_api_token=megapbx_api_token,
            megapbx_allow_all=allow_all,
            megapbx_allow_query_token=_parse_bool(values, "MEGAPBX_ALLOW_QUERY_TOKEN", False),
            megapbx_allow_http_api=allow_http_api,
            megapbx_webhook_body_bytes=_parse_int(
                values,
                "MEGAPBX_WEBHOOK_BODY_BYTES",
                1_048_576,
                minimum=1,
            ),
            megapbx_enrichment_refresh_sec=_parse_int(
                values,
                "MEGAPBX_ENRICHMENT_REFRESH_SEC",
                600,
                minimum=1,
            ),
            tz_offset_hours=tz_offset_hours,
            missed_max_age_sec=_parse_int(values, "MISSED_MAX_AGE_SEC", 3600, minimum=0),
            missed_cleanup_interval_sec=_parse_int(
                values, "MISSED_CLEANUP_INTERVAL_SEC", 3600, minimum=1
            ),
            missed_dedup_ttl_sec=_parse_int(values, "MISSED_DEDUP_TTL_SEC", 86_400, minimum=0),
            job_max_attempts=_parse_int(values, "JOB_MAX_ATTEMPTS", 20, minimum=1),
            job_retry_base_sec=_parse_float(values, "JOB_RETRY_BASE_SEC", 1.0, minimum=0.0),
            job_retry_max_sec=_parse_float(values, "JOB_RETRY_MAX_SEC", 300.0, minimum=0.0),
            job_lease_sec=_parse_float(values, "JOB_LEASE_SEC", 300.0, minimum=30.0),
        )


def _parse_float(env: Mapping[str, str], name: str, default: float, *, minimum: float) -> float:
    raw = _get(env, name)
    if raw is None:
        value = default
    else:
        try:
            value = float(raw)
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum:
        raise ConfigurationError(f"{name} must be a finite number at least {minimum}")
    return value
