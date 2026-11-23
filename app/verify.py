import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.knowledge import Chunk, Knowledge
from app.retrieval import institutional_request

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=700)]
Label = Annotated[str, StringConstraints(pattern=r"^S(?:[1-9]|1[0-2])$")]
NUMBER = re.compile(r"\d+(?:[.,–—-]\d+)*")
UNTRUSTED_MARKUP = re.compile(r"https?\s*:|www\.|\[S\s*\d+\]|\[[^\]]*\]\s*\(|<[^>]+>", re.I)


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(OutputModel):
    id: Label
    quote: Annotated[str, StringConstraints(strip_whitespace=True, min_length=12, max_length=280)]


class Fact(OutputModel):
    text: Text
    evidence: list[Evidence] = Field(min_length=1, max_length=3)


class Step(OutputModel):
    text: Text
    sources: list[Label] = Field(min_length=1, max_length=3)


class Draft(OutputModel):
    status: Literal["answered", "partial", "insufficient"]
    facts: list[Fact] = Field(max_length=6)
    steps: list[Step] = Field(max_length=6)
    limits: str = Field(default="", max_length=450)
    search_terms: str = Field(default="", max_length=240)
    reason: Literal["missing", "unrelated", ""] = ""


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().replace("ё", "е").split())


def numbers(text: str) -> set[str]:
    return {value.replace(",", ".").replace("–", "-").replace("—", "-") for value in NUMBER.findall(text)}


def safe_prose(text: str) -> bool:
    return not UNTRUSTED_MARKUP.search(text) and all(char in "\t\r\n" or not unicodedata.category(char).startswith("C") for char in text)


def verify(draft: Draft, choices: dict[str, Chunk], user_text: str, knowledge: Knowledge | None = None) -> list[str]:
    issues = []
    if draft.status == "insufficient":
        return issues
    if not draft.facts:
        issues.append("Нет ни одного утверждения с указанием доказательства.")
    if not safe_prose(draft.limits):
        issues.append("Убери разметку ссылок из поля limits.")
    user_numbers = numbers(user_text)
    for index, fact in enumerate(draft.facts, 1):
        supported = set()
        for evidence in fact.evidence:
            chunk = choices.get(evidence.id)
            quote = normalized(evidence.quote)
            if chunk is None or len(quote) < 12 or "[…]" in quote or quote not in normalized(chunk.text):
                issues.append(f"Утверждение {index}: укажи дословный фрагмент из поля text указанного источника.")
                continue
            supported |= numbers(evidence.quote) | numbers(chunk.section)
        if numbers(fact.text) - supported - user_numbers:
            issues.append(f"Утверждение {index}: число или номер отсутствует в цитатах, заголовках разделов и вопросе.")
        if knowledge and institutional_request(fact.text, ()) and not any(
            knowledge.documents[choices[item.id].document_id].scope == "institutional"
            for item in fact.evidence if item.id in choices
        ):
            issues.append(f"Утверждение {index}: для локальных требований УрФУ нужен официальный локальный документ.")
        if not safe_prose(fact.text):
            issues.append(f"Утверждение {index}: удали внешние ссылки, HTML и ручные сноски.")
    for index, step in enumerate(draft.steps, 1):
        if not all(label in choices for label in step.sources) or not safe_prose(step.text):
            issues.append(f"Шаг {index}: используй только идентификаторы выданных источников в поле sources.")
    return issues


def plain_line(text: str) -> str:
    # The server owns Markdown block structure and citation markers.
    return " ".join(text.split())


def render(draft: Draft, choices: dict[str, Chunk], knowledge: Knowledge) -> tuple[str, list[dict]]:
    used: dict[str, list[str]] = {}

    def cite(text: str, labels: list[str]) -> str:
        return f"{plain_line(text)} " + " ".join(f"[{label}]" for label in dict.fromkeys(labels))

    facts = []
    for fact in draft.facts:
        for evidence in fact.evidence:
            quotes = used.setdefault(evidence.id, [])
            if evidence.quote not in quotes:
                quotes.append(evidence.quote)
        facts.append(cite(fact.text, [item.id for item in fact.evidence]))

    lines = ["### Что говорят источники", "", "\n\n".join(facts)]
    if draft.steps:
        lines.extend(["", "### Как применить в вашем проекте", ""])
        for index, step in enumerate(draft.steps, 1):
            for label in step.sources:
                used.setdefault(label, [])
            lines.append(f"{index}. {cite(step.text, step.sources)}")
    if draft.limits.strip():
        lines.extend(["", "### Что уточнить", "", plain_line(draft.limits)])
    sources = [dict(knowledge.source(choices[label], label), evidence=quotes) for label, quotes in used.items()]
    return "\n".join(lines), sources
