from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class MaxModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class User(MaxModel):
    user_id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    is_bot: bool = False
    last_activity_time: int | None = None


class Recipient(MaxModel):
    chat_id: int | None = None
    chat_type: str
    user_id: int | None = None
    post_id: str | None = None


class MessageBody(MaxModel):
    mid: str
    seq: int
    text: str | None = None
    attachments: list[dict[str, Any]] | None = None
    markup: list[dict[str, Any]] | None = None


class Message(MaxModel):
    sender: User | None = None
    recipient: Recipient
    timestamp: int
    body: MessageBody | None = None
    link: dict[str, Any] | None = None
    stat: dict[str, Any] | None = None
    url: str | None = None


class SendMessageResult(MaxModel):
    message: Message


class Callback(MaxModel):
    timestamp: int
    callback_id: str = Field(min_length=1)
    payload: str | None = None
    user: User


class Update(MaxModel):
    update_type: str
    timestamp: int
    chat_id: int | None = None
    user: User | None = None
    message: Message | None = None
    callback: Callback | None = None
    payload: str | None = None
    is_channel: bool | None = None
    user_locale: str | None = None

    @model_validator(mode="after")
    def validate_known_update(self) -> Update:
        if self.update_type == "message_callback" and self.callback is None:
            raise ValueError("message_callback update must contain callback")
        return self


class UpdateList(MaxModel):
    updates: list[Update] = Field(...)
    marker: int | None = None


class BotInfo(MaxModel):
    user_id: int
    name: str | None = None
    first_name: str
    username: str | None = None
    is_bot: bool = True
    commands: list[dict[str, Any]] | None = None


class SimpleResult(MaxModel):
    success: StrictBool
    message: str | None = None


class CallbackPayload(MaxModel):
    """Versioned application payload carried by a MAX callback button."""

    version: Literal[1] = Field(default=1, alias="v")
    action: Literal["call_back", "call_back_done"]
    record_id: str = Field(min_length=1, max_length=64)
