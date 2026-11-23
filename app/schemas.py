from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

MAX_BODY_BYTES = 64 * 1024
MessageText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]


def valid_text(value: str) -> str:
    # JSON escape sequences can introduce control characters or lone UTF-16 surrogates.
    if any((ord(char) < 32 and char not in "\r\n\t") or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("Недопустимый символ в сообщении")
    return value


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: MessageText

    _valid_content = field_validator("content")(valid_text)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: MessageText
    history: list[Message] = Field(default_factory=list, max_length=8)

    _valid_message = field_validator("message")(valid_text)
