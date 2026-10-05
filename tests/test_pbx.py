from __future__ import annotations

import httpx

from megapbx_max.config import Settings
from megapbx_max.pbx import (
    PbxDirectory,
    append_callback_status,
    build_missed_text,
    callback_status_text,
    display_caller,
    extract_destination,
    find_phone_anywhere,
    is_allowed_destination,
    is_missed_call,
    normalize_phone,
)


def settings() -> Settings:
    return Settings.from_env(
        {
            "MAX_BOT_TOKEN": "max-secret",
            "MAX_CHAT_ID": "42",
            "MEGAPBX_CRM_TOKEN": "crm-secret",
            "MEGAPBX_ALLOWED_GROUP": "Support",
            "MEGAPBX_ALLOWED_DID": "100,200",
            "MEGAPBX_DID_NAMES": "300=Sales,400=IVR",
        }
    )


def test_dynamic_values_are_html_escaped_and_phone_normalized() -> None:
    display, phone = display_caller(
        {
            "contact_name": "A & <B>",
            "phone": "5555550123",
        }
    )
    assert display == "A &amp; &lt;B&gt; (<code>+75555550123</code>)"
    assert phone == "+75555550123"
    assert normalize_phone("8 (495) 123-45-67") == "+74951234567"


def test_nested_phone_search_skips_secret_keys() -> None:
    payload = {
        "authorization": "+70000000000",
        "nested": [{"api_token": "+70000000001"}, {"customerPhone": "+70000000002"}]
    }
    assert find_phone_anywhere(payload) == "+70000000002"


def test_destination_filter_and_enrichment() -> None:
    configured = settings()
    directory = PbxDirectory(None, None)
    directory.groups["500"] = "Support IVR"

    assert is_allowed_destination({"groupRealName": "Support"}, configured)
    assert is_allowed_destination({"telnum": "100"}, configured)
    assert not is_allowed_destination({"telnum": "999"}, configured)
    allow_all = Settings.from_env(
        {
            "MAX_BOT_TOKEN": "max-secret",
            "MAX_CHAT_ID": "42",
            "MEGAPBX_CRM_TOKEN": "crm-secret",
            "MEGAPBX_ALLOWED_DID": "100",
            "MEGAPBX_ALLOW_ALL": "1",
        }
    )
    assert is_allowed_destination({"telnum": "999"}, allow_all)
    assert extract_destination({"diversion": "500"}, configured, directory) == "Support IVR"
    assert extract_destination({"telnum": "400"}, configured, directory) == "IVR"
    assert extract_destination({}, configured, directory) == "неизвестно"


def test_outgoing_missed_history_is_not_a_new_notification() -> None:
    assert is_missed_call({"cmd": "history", "status": "Missed"})
    assert not is_missed_call({"cmd": "history", "status": "Missed", "type": "out"})


def test_message_text_and_failed_status_formatting() -> None:
    text = build_missed_text(
        "Client (<code>+70000000000</code>)",
        "Support",
        wait="4",
        duration=0,
        today=2,
        total=5,
        offset_hours=3,
    )
    assert "ожидание: 4 с" in text
    assert "Пропущено (сегодня: 2, всего: 5)" in text

    updated = append_callback_status(text, "A & B", "Busy")
    updated = append_callback_status(updated, "A & B", "NotAvailable")
    assert "↩️ A &amp; B: 📴 Недоступен" in updated
    assert "Busy" not in updated
    assert callback_status_text("Unknown") == "❌ Unknown"


async def test_pbx_directory_paginates_and_prefers_ivr_timeout_group() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-API-KEY"] == "pbx-secret"
        if request.url.path.endswith("/users"):
            return httpx.Response(
                200,
                json={
                    "items": [{"login": "100", "name": "Operator One"}],
                    "info": {"total": 1},
                },
            )
        start = int(request.url.params["start"])
        if start == 0:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "telnum": "500",
                            "type": "ivr",
                            "ivr": {
                                "items": [
                                    {"button": "1", "group_name": "First"},
                                    {"button": "timeout", "group_name": "Timeout"},
                                ]
                            },
                        }
                    ],
                    "info": {"total": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [{"telnum": "600", "type": "number", "user_name": "Sales"}],
                "info": {"total": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        directory = PbxDirectory("https://pbx.example.com", "pbx-secret", http_client=http_client)
        await directory.refresh()
        assert directory.accounts == {"100": "Operator One"}
        assert directory.groups == {"500": "Timeout", "600": "Sales"}

    assert len(requests) == 3
