import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.knowledge import Chunk, Knowledge

PassageId = Annotated[str, StringConstraints(pattern=r"^S[1-9][0-9]*\.[1-9][0-9]*$")]
Status = Literal["answered", "partial", "project_details", "insufficient", "unrelated"]
INSTRUCTION_LINE = re.compile(
    r"^\s*(?:[-*>]\s*)?(?:system|assistant|developer|система|ассистент|разработчик)\s*[:：]|"
    r"\b(?:игнорируй\w*|забудь\w*|ignore|forget|disregard|override)\b.{0,90}"
    r"\b(?:инструкц\w*|промпт\w*|instructions?|prompts?)\b|"
    r"\b(?:покажи\w*|выведи\w*|раскрой\w*|print|reveal|show)\b.{0,50}"
    r"\b(?:системн\w* (?:промпт\w*|инструкц\w*)|system prompt)\b", re.I,
)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status
    passages: list[PassageId] = Field(max_length=4)


@dataclass(frozen=True)
class Passage:
    chunk: Chunk
    text: str


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().replace("ё", "е").split())


def source_passages(chunks: list[Chunk]) -> dict[str, Passage]:
    passages = {}
    for source_index, chunk in enumerate(chunks, 1):
        for part_index, block in enumerate(re.split(r"\n[ \t]*\n", chunk.text), 1):
            # Keep full paragraphs and lists together so a condition is not cut off.
            if block.strip() and not any(INSTRUCTION_LINE.search(line) for line in block.splitlines()):
                passages[f"S{source_index}.{part_index}"] = Passage(chunk, block.strip())
    return passages


def verify(draft: Draft, passages: dict[str, Passage]) -> list[Passage] | None:
    if draft.status in {"answered", "partial"} and not draft.passages:
        return None
    if draft.status in {"insufficient", "unrelated"} and draft.passages:
        return None
    if len(set(draft.passages)) != len(draft.passages) or not all(key in passages for key in draft.passages):
        return None
    return [passages[key] for key in draft.passages]


def render(passages: list[Passage], knowledge: Knowledge, *, lead: str = "", note: str = "") -> tuple[str, list[dict]]:
    blocks = [lead] if lead else []
    sources = {}
    for passage in passages:
        chunk = passage.chunk
        if chunk.id not in sources:
            sources[chunk.id] = dict(knowledge.source(chunk, f"S{len(sources) + 1}"), evidence=[])
        source = sources[chunk.id]
        source["evidence"].append(passage.text)
        blocks.append(f"{passage.text} [{source['label']}]")
    if note:
        blocks.append(note)
    return "\n\n".join(blocks), list(sources.values())
