"""HTTP and message.created v1 contracts."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SEQ = 2**63 - 1


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageCreate(Contract):
    client_message_id: UUID
    text: str = Field(min_length=1, max_length=4000, strict=True)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text must contain non-whitespace characters")
        if "\x00" in value:
            raise ValueError("Text must not contain a null character")
        return value


class Message(Contract):
    message_id: UUID
    dialog_id: UUID
    sender_id: UUID
    seq: int = Field(ge=1, le=MAX_SEQ)
    text: str = Field(min_length=1, max_length=4000)
    created_at: AwareDatetime


class HistoryPage(Contract):
    items: list[Message]
    next_cursor: int | None = Field(default=None, ge=1, le=MAX_SEQ)
    has_more: bool


class MessageCreated(Contract):
    event_id: UUID
    type: Literal["message.created"]
    version: Literal[1]
    occurred_at: AwareDatetime
    participant_ids: tuple[UUID, UUID]
    message: Message

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value):
        if type(value) is not int:
            raise ValueError("Event version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_participants(self):
        if self.participant_ids[0] == self.participant_ids[1]:
            raise ValueError("A private dialog requires two different participants")
        if self.message.sender_id not in self.participant_ids:
            raise ValueError("Sender must belong to the dialog")
        return self
