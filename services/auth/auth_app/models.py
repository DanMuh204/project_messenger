from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(
        min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$", strict=True
    )
    password: str = Field(min_length=15, max_length=128, strict=True)

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        return value.lower()


class ResolveUsers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_ids: list[UUID] = Field(min_length=1, max_length=100)
