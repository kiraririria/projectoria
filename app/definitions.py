import re

from app.knowledge import Knowledge
from app.schemas import Message
from app.verify import Passage, normalized, source_passages

DEFINITION_QUESTION = re.compile(
    r"(?:а\s+)?(?:что\s+такое|что\s+означает|дай(?:те)?\s+определение)\s+"
    r"(?P<term>[«\"]?[\w+#/.-]+(?:[ \t]+[\w+#/.-]+){0,4}?[»\"]?)"
    r"(?:(?:,?\s+)(?:простыми словами|своими словами|в двух словах|кратко|пожалуйста))*[.!?…]*",
    re.I,
)
DEFINITION_LINE = re.compile(
    r"^(?P<term>[\w+#/.-]+(?:[ \t]+[\w+#/.-]+){0,4})"
    r"(?:\s*\((?P<alias>[^()\n]+)\))?\s+(?:[—–-]|означает|представляет собой)\s+\S",
    re.I,
)
SCRUM_SPELLING = "Вы имели в виду Scrum?"
CONFIRMATION = re.compile(r"да(?:\s*,?\s*(?:scrum|скрам))?[.!?…]*", re.I)
ACRONYM = re.compile(r"[A-ZА-ЯЁ]{2,8}")
ALIASES = {"скрам": "scrum"}
METHODOLOGY_CONTEXT = re.compile(r"\s+в\s+(?:scrum|скраме|agile|kanban|канбане|проекте|разработке)$", re.I)


def requested_term(message: str) -> str:
    match = DEFINITION_QUESTION.fullmatch(message)
    if not match:
        return ""
    term = match["term"].strip('«»"').rstrip(".")
    if re.search(r"\b(?:и|или)\s+(?:как|чем|кто|где|почему|что|когда|зачем|можно|нужно)\b", term, re.I):
        return ""
    return METHODOLOGY_CONTEXT.sub("", term)


def confirmed_scrum(message: str, history: list[Message]) -> bool:
    if len(history) < 2 or not CONFIRMATION.fullmatch(message):
        return False
    question, reply = history[-2:]
    return (question.role == "user" and normalized(requested_term(question.content)) == "srum"
            and reply.role == "assistant" and reply.content == SCRUM_SPELLING)


class Definitions:
    def __init__(self, knowledge: Knowledge):
        self.passages: dict[str, Passage] = {}
        self.corpus = normalized("\n".join(knowledge.embedding_text(chunk) for chunk in knowledge.chunks.values()))
        for passage in source_passages(list(knowledge.chunks.values())).values():
            match = DEFINITION_LINE.match(passage.text)
            if match:
                for term in (match["term"], match["alias"]):
                    if term:
                        self.passages.setdefault(normalized(term), passage)

    def lookup(self, term: str) -> Passage | None:
        key = normalized(term)
        return self.passages.get(ALIASES.get(key, key))

    def missing_message(self, term: str) -> str:
        key = normalized(term)
        if ALIASES.get(key, key) == "scrum":
            return "В базе знаний нет определения Scrum. В ней описаны команда Scrum, события спринта и артефакты."
        if ACRONYM.fullmatch(term) and not re.search(rf"\b{re.escape(key)}\b", self.corpus):
            return f"В базе знаний нет определения «{term}»."
        return ""
