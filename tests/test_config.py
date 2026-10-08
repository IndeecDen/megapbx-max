from __future__ import annotations

import pytest

from megapbx_max.config import ConfigurationError, Settings


def valid_env() -> dict[str, str]:
    return {
        "MAX_BOT_TOKEN": "max-secret",
        "MAX_CHAT_ID": "42",
        "MEGAPBX_CRM_TOKEN": "crm-secret",
        "MEGAPBX_ALLOWED_DID": "78005553535, 78005553536",
        "MAX_WEBHOOK_SECRET": "webhook_secret-1",
        "MAX_WEBHOOK_URL": "https://bot.example.com/max/webhook",
    }


def test_settings_from_env() -> None:
    settings = Settings.from_env(valid_env())

    assert settings.max_chat_id == 42
    assert settings.megapbx_allowed_dids == frozenset({"78005553535", "78005553536"})
    assert settings.max_webhook_update_types[0] == "message_callback"
    assert settings.max_api_base == "https://platform-api2.max.ru"


@pytest.mark.parametrize("chat_id", ["-12345678901234", "-9223372036854775808", "9223372036854775807"])
def test_signed_chat_ids_are_accepted(chat_id: str) -> None:
    env = valid_env()
    env["MAX_CHAT_ID"] = chat_id
    assert Settings.from_env(env).max_chat_id == int(chat_id)


@pytest.mark.parametrize("chat_id", ["0", "-0", "9223372036854775808", "-9223372036854775809"])
def test_zero_and_out_of_range_chat_ids_are_rejected(chat_id: str) -> None:
    env = valid_env()
    env["MAX_CHAT_ID"] = chat_id
    with pytest.raises(ConfigurationError, match="MAX_CHAT_ID"):
        Settings.from_env(env)


@pytest.mark.parametrize("variable", ["MAX_BOT_TOKEN", "MAX_CHAT_ID", "MEGAPBX_CRM_TOKEN"])
def test_required_values_are_rejected_when_missing(variable: str) -> None:
    env = valid_env()
    del env[variable]

    with pytest.raises(ConfigurationError, match=variable):
        Settings.from_env(env)


def test_empty_direction_allowlist_is_rejected() -> None:
    env = valid_env()
    del env["MEGAPBX_ALLOWED_DID"]

    with pytest.raises(ConfigurationError, match="MEGAPBX_ALLOWED"):
        Settings.from_env(env)


def test_explicit_allow_all_is_accepted() -> None:
    env = valid_env()
    del env["MEGAPBX_ALLOWED_DID"]
    env["MEGAPBX_ALLOW_ALL"] = "1"

    assert Settings.from_env(env).megapbx_allow_all is True


def test_webhook_url_rejects_non_443_port_and_query() -> None:
    env = valid_env()
    env["MAX_WEBHOOK_URL"] = "https://bot.example.com:8443/max/webhook"
    with pytest.raises(ConfigurationError, match="port 443"):
        Settings.from_env(env)
    env["MAX_WEBHOOK_URL"] = "https://bot.example.com/max/webhook?token=leak"
    with pytest.raises(ConfigurationError, match="query"):
        Settings.from_env(env)


def test_retry_settings_reject_non_finite_values() -> None:
    env = valid_env()
    env["MAX_API_RETRY_BASE_SEC"] = "nan"
    with pytest.raises(ConfigurationError, match="finite"):
        Settings.from_env(env)


def test_api_urls_reject_query_credentials_and_fragments() -> None:
    env = valid_env()
    env["MAX_API_BASE"] = "https://api.example.com/?access_token=leak"
    with pytest.raises(ConfigurationError, match="query"):
        Settings.from_env(env)

    env = valid_env()
    env["MEGAPBX_API_BASE"] = "https://pbx.example.com/api#fragment"
    env["MEGAPBX_API_TOKEN"] = "pbx-token"
    with pytest.raises(ConfigurationError, match="fragment"):
        Settings.from_env(env)

    env = valid_env()
    env["MEGAPBX_API_BASE"] = "https://pbx.example.com/crmapi/v1"
    env["MEGAPBX_API_TOKEN"] = "pbx-token"
    with pytest.raises(ConfigurationError, match="suffix"):
        Settings.from_env(env)


def test_webhook_requires_https_and_secret() -> None:
    env = valid_env()
    env["MAX_WEBHOOK_URL"] = "http://bot.example.com/max/webhook"
    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.from_env(env)

    env = valid_env()
    del env["MAX_WEBHOOK_SECRET"]
    with pytest.raises(ConfigurationError, match="MAX_WEBHOOK_SECRET"):
        Settings.from_env(env)


def test_state_database_cannot_be_in_memory() -> None:
    env = valid_env()
    env["STATE_DB_PATH"] = ":memory:"
    with pytest.raises(ConfigurationError, match="persistent"):
        Settings.from_env(env)


def test_callback_update_type_must_be_present_in_subscription() -> None:
    env = valid_env()
    env["MAX_WEBHOOK_UPDATE_TYPES"] = "bot_started"

    with pytest.raises(ConfigurationError, match="message_callback"):
        Settings.from_env(env)
