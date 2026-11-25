import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.knowledge import Chunk, Knowledge
from app.retrieval import INSTITUTION, tokens

PassageId = Annotated[str, StringConstraints(pattern=r"^S[1-9][0-9]*\.[1-9][0-9]*$")]
Status = Literal["answered", "partial", "project_details", "insufficient", "unrelated", "clarify"]
MACHINE_REFERENCE = re.compile(r"\[S\d+(?:\.\d+)?\]|https?://|\]\(", re.I)
LIST_NUMBER = re.compile(r"^\s*\d+[.)]\s+")
QUANTITY_UNITS = {
    text: family for family, variants in (
        ("секунд", "секунд секунда секунды секунду секундами секундное секундный"),
        ("минут", "минут минута минуты минуту минутами минутное минутный мин"),
        ("час", "час часа часов часами часовое часовой"),
        ("день", "день дня дней днем днями дневное дневный"),
        ("неделя", "неделя недели недель неделю неделями недельное недельный"),
        ("месяц", "месяц месяца месяцев месяцами месячное месячный"),
        ("год", "год года лет годами годовое годовой"),
        ("человек", "человек человека людьми участник участника участников"),
        ("%", "% процент процента процентов процентами"),
    ) for text in variants.split()
}
UNIT_PATTERN = "|".join(map(re.escape, QUANTITY_UNITS))
MONTH_PATTERN = ("января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря")
NUMBER = re.compile(
    rf"(?P<amount>\d+(?:[.,:/–—-]\d+)*)(?:\s*(?P<date>(?:{MONTH_PATTERN})\b(?:\s+\d{{4}}\b)?)"
    rf"|[ \t–—-]*(?P<unit>[‰%]|(?:{UNIT_PATTERN})(?!\w)))?"
)
EXACT_QUANTITY = re.compile(r"\bровно\s+" + NUMBER.pattern)
BOUNDED_QUANTITY = re.compile(r"\b(?:не более|максимум|не менее|минимум)\s+" + NUMBER.pattern)
PASSAGE_BREAK = re.compile(r"\n[ \t]*\n|\Z")
REQUIREMENT = re.compile(r"\b(?:треб\w*|обяз\w*|долж\w*|регламент\w*|приказ\w*)\b", re.I)
SUBJECT_ALIASES = {"agile": "аджайл", "scrum": "скрам", "kanban": "канбан"}
INSTRUCTION_LINE = re.compile(
    r"^\s*(?:[-*>]\s*)?(?:system|assistant|developer|система|ассистент|разработчик)\s*[:：]|"
    r"\b(?:игнорируй\w*|забудь\w*|ignore|forget|disregard|override)\b.{0,90}"
    r"\b(?:инструкц\w*|промпт\w*|instructions?|prompts?)\b|"
    r"\b(?:покажи\w*|выведи\w*|раскрой\w*|print|reveal|show)\b.{0,50}"
    r"\b(?:системн\w* (?:промпт\w*|инструкц\w*)|system prompt)\b", re.I,
)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: PassageId
    quote: str = Field(min_length=1, max_length=560)


class AnswerBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)]
    evidence: list[Evidence] = Field(min_length=1, max_length=4)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status
    blocks: list[AnswerBlock] = Field(max_length=4)
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=420)] = ""


@dataclass(frozen=True)
class Passage:
    chunk: Chunk
    text: str
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.text)


@dataclass(frozen=True)
class VerifiedBlock:
    text: str
    evidence: tuple[Passage, ...]


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).casefold().replace("ё", "е").split())


def source_passages(chunks: list[Chunk]) -> dict[str, Passage]:
    passages = {}
    for source_index, chunk in enumerate(chunks, 1):
        offset = 0
        for part_index, separator in enumerate(PASSAGE_BREAK.finditer(chunk.text), 1):
            block = chunk.text[offset:separator.start()]
            start = offset + len(block) - len(block.lstrip())
            offset = separator.end()
            # Keep full paragraphs and lists together so a condition is not cut off.
            if block.strip() and not any(INSTRUCTION_LINE.search(line) for line in block.splitlines()):
                passages[f"S{source_index}.{part_index}"] = Passage(chunk, block.strip(), start)
    return passages


def find_quote(text: str, quote: str) -> re.Match[str] | None:
    words = unicodedata.normalize("NFC", quote).split()
    if len(words) < 2 or len(" ".join(words)) < 12:
        return None
    pattern = r"\s+".join(re.sub("[её]", "[её]", re.escape(word), flags=re.I) for word in words)
    if words[0][0].isalnum():
        pattern = r"(?<!\w)" + pattern
    if words[-1][-1].isalnum():
        pattern += r"(?!\w)"
    return re.search(pattern, text, flags=re.I)


def values(text: str, *, include_bare: bool = False, pattern: re.Pattern[str] = NUMBER) -> set[tuple[str, str]]:
    result = set()
    for match in pattern.finditer(normalized(text)):
        amount = match["amount"].translate(str.maketrans("–—,", "--."))
        unit = match["date"] or QUANTITY_UNITS.get(match["unit"], match["unit"]) or ""
        result.add((amount, unit))
        if include_bare:
            result.add((amount, ""))
    return result


def subject_forms(term: str) -> list[str]:
    name = normalized(term)
    alias = SUBJECT_ALIASES.get(name) or next(
        (key for key, value in SUBJECT_ALIASES.items() if value == name), "",
    )
    return [name, alias] if alias else [name]


def opens_with_definition(text: str, term: str) -> bool:
    lead = LIST_NUMBER.sub("", normalized(text)).lstrip("-* ")
    lead = lead.replace("**", "").replace("__", "").replace("`", "")
    names = "|".join(map(re.escape, subject_forms(term)))
    name = rf"[«\"]?(?:{names})[»\"]?"
    intro = (r"(?:(?:[—–:-]\s*)|(?:это|означает|называют|подразумевает|представляет собой|"
             r"расшифровывается как)\s+)")
    return bool(re.match(
        rf"(?:{name}(?:\s*\([^)]*\))?\s*{intro}|под\s+{name}\s+(?:понимают|имеют в виду)\s+)\S", lead,
    ))


def evidence_has_subject(evidence: tuple[Passage, ...], term: str) -> bool:
    if not evidence:
        return False
    for part in evidence:
        # Definitions can be synthesized from a passage dedicated to that subject.
        # Check its heading as well as the verified quotation, so supporting details can be brief.
        source = normalized(f"{part.chunk.section}\n{part.text}")
        stemmed = " ".join(tokens(source))
        for form in subject_forms(term):
            if re.search(rf"(?<!\w){re.escape(form)}(?!\w)", source):
                return True
            stem = " ".join(tokens(form))
            if stem and re.search(rf"(?<!\w){re.escape(stem)}(?!\w)", stemmed):
                return True
    return False


def verify(
    draft: Draft, passages: dict[str, Passage], *, question: str = "", definition_term: str = "",
    definition_passage: Passage | None = None,
) -> list[VerifiedBlock] | None:
    if draft.status in {"answered", "partial"} and not draft.blocks:
        return None
    if draft.status in {"insufficient", "unrelated", "clarify"} and draft.blocks:
        return None
    if re.search(r"[\r\n\d]", draft.note) or MACHINE_REFERENCE.search(draft.note):
        return None
    if (definition_term and draft.blocks and draft.status in {"answered", "partial"}
            and not opens_with_definition(draft.blocks[0].text, definition_term)):
        return None

    user_values = values(question, include_bare=True)
    verified = []
    for block in draft.blocks:
        if re.search(r"[\r\n]", block.text) or MACHINE_REFERENCE.search(block.text):
            return None
        evidence = []
        for ref in block.evidence:
            part = passages.get(ref.id)
            if part is None:
                return None
            quote = find_quote(part.text, ref.quote)
            if quote is None:
                return None
            verified_quote = Passage(part.chunk, quote.group(), part.start + quote.start())
            if verified_quote not in evidence:
                evidence.append(verified_quote)
        quotes = " ".join(part.text for part in evidence)
        if values(LIST_NUMBER.sub("", block.text)) - (values(quotes, include_bare=True) | user_values):
            return None
        exact_claims = values(block.text, pattern=EXACT_QUANTITY)
        if exact_claims:
            stated_bounds = values(quotes, pattern=BOUNDED_QUANTITY)
            stated_exact = values(quotes, pattern=EXACT_QUANTITY)
            if (exact_claims & stated_bounds) - stated_exact:
                return None
        if INSTITUTION.search(block.text) and REQUIREMENT.search(block.text) and not INSTITUTION.search(quotes):
            return None
        verified.append(VerifiedBlock(block.text, tuple(evidence)))
    if definition_term and verified and draft.status in {"answered", "partial"}:
        definition_evidence = verified[0].evidence
        if definition_passage is None:
            if not evidence_has_subject(definition_evidence, definition_term):
                return None
        elif not any(
            part.chunk.id == definition_passage.chunk.id
            and definition_passage.start <= part.start and part.end <= definition_passage.end
            for part in definition_evidence
        ):
            return None
    return verified


def render_answer(
    blocks: list[VerifiedBlock], knowledge: Knowledge, *, lead: str = "", note: str = "",
) -> tuple[str, list[dict]]:
    paragraphs = [lead] if lead else []
    sources = {}
    for block in blocks:
        labels = []
        for passage in block.evidence:
            chunk = passage.chunk
            if chunk.id not in sources:
                sources[chunk.id] = dict(
                    knowledge.source(chunk, f"S{len(sources) + 1}"), evidence=[], evidence_spans=[],
                )
            source = sources[chunk.id]
            if passage.text not in source["evidence"]:
                source["evidence"].append(passage.text)
            # JavaScript string indices count UTF-16 code units.
            start = len(chunk.text[:passage.start].encode("utf-16-le")) // 2
            end = start + len(passage.text.encode("utf-16-le")) // 2
            span = {"start": start, "end": end}
            if span not in source["evidence_spans"]:
                source["evidence_spans"].append(span)
            label = f"[{source['label']}]"
            if label not in labels:
                labels.append(label)
        paragraphs.append(f"{block.text} {' '.join(labels)}")
    if note:
        paragraphs.append(note)
    for source in sources.values():
        source["evidence_spans"].sort(key=lambda span: (span["start"], span["end"]))
    return "\n\n".join(paragraphs), list(sources.values())
