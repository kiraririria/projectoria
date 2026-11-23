import math
import re
from collections import Counter, defaultdict

import snowballstemmer

from app.knowledge import Chunk, Knowledge, unit_vector
from app.schemas import Message

TOKEN = re.compile(r"[a-zа-яё]+[0-9]*|[0-9]+(?:[.,-][0-9]+)*", re.I)
STOP = set("а и но или в во на за под от до из о об для по с со к при без через у над не это я мы вы он они нам "
           "надо нужно можно мой наш их его ее как какой какие какую какое каким кто что чем где если когда "
           "ли же то так есть будет мне быть между один два для меня дать объясни расскажи помоги пожалуйста".split())
ALIASES = (
    (r"\b(?:скрам\w*|scrum)\b", "scrum скрам"),
    (r"\b(?:канбан\w*|kanban|wip|незаверш[её]нн\w*)\b", "kanban канбан wip"),
    (r"\b(?:agile|аджайл\w*)\b", "agile манифест гибкая разработка"),
    (r"\b(?:тз|техзадани\w*)\b", "техническое задание"),
    (r"\b(?:ес пд|еспд)\b", "гост 19.201-78 программа"),
    (r"\b(?:ас|автоматизированн\w* систем\w*)\b", "автоматизированная система гост 34.602-2020"),
    (r"\b(?:спринт\w*|sprint\w*)\b", "sprint спринт"),
    (r"\b(?:б[еэ]клог\w*|backlog)\b", "backlog бэклог"),
    (r"\b(?:пул[- ]реквест\w*|pull requests?|pr)\b", "pull request github"),
    (r"\b(?:мерж[- ]реквест\w*|merge requests?|mr)\b", "merge request gitlab"),
    (r"\b(?:пайплайн\w*|pipeline\w*|ci/cd)\b", "pipeline пайплайн ci cd"),
    (r"\b(?:девопс\w*|devops)\b", "devops девопс"),
    (r"\b(?:гитхаб\w*|github)\b", "github"),
    (r"\b(?:гитлаб\w*|gitlab)\b", "gitlab"),
    (r"\b(?:гит\w{0,2}|git)\b", "git"),
    (r"\b(?:дод|dod|definition of done|критери\w* готовност\w*)\b", "definition of done критерии готовности"),
    (r"\b(?:с чего.{0,30}нача\w*|первые шаги|старт\w* проекта)\b", "планирование цель границы результат проекта"),
)
FOLLOW_UP = re.compile(
    r"^\s*(?:а\s+)?(?:если\b|тогда\b|в таком случае\b|покажи пример\b|что насч[её]т\b|"
    r"можешь уточнить\b|как (?:это|именно)\b|какие из них\b|кто за это\b|продолжи\b)",
    re.I,
)
REACTION = re.compile(
    r"[\s.!?…]+|(?:что|ч[её]|шо)\s+(?:это\s+)?за\s+(?:х[у*]йн[яю]|фигн[яю]|ерунд[ау]|бред|"
    r"дичь|хрень|хрен[ья])[\s.!?…]*|(?:не понял[аи]?|не понимаю|непонятно|"
    r"что это такое|что это было)[\s.!?…]*",
    re.I,
)
CAMPUS = r"\b(?:урфу|ирит(?:\s*[-–]\s*ртф)?|уральск\w* федеральн\w* университет\w*)\b"
INSTITUTION = re.compile(CAMPUS, re.I)
LOCAL_POLICY = re.compile(
    r"локальн\w* (?:правил|требовани|регламент|положени)|регламент|приказ|дедлайн|"
    r"(?:когда|срок\w*|формат\w*|дат[ауые]|время|времени|расписани\w*|"
    r"(?:до )?какого числа|на какой неделе) .{0,65}(?:сда|защит|отч[её]т)|"
    r"(?:обязательн\w*|правил\w*|требовани\w*|требу\w*|утвержд\w*)\s+(?:(?:в|для)\s+)?" + CAMPUS + "|" +
    CAMPUS + r"\s+(?:обязательн|требуе?т|установил|утвердил|предписыва)", re.I,
)
SCRUM_MASTER = re.compile(r"\b(?:scrum[\s\-‑–]+master|скрам[\s\-‑–]+мастер\w*)\b", re.I)
OBLIGATION = re.compile(r"\b(?:обязател(?:ен|ьн\w*)|нуж(?:ен|на|но|ны)|надо|требу\w*)\b", re.I)
SCRUM_GUIDE = re.compile(r"\b(?:scrum\s+guide|руководств\w*\s+по\s+(?:scrum|скраму?))\b", re.I)


def tokens(text: str) -> list[str]:
    stemmer = snowballstemmer.stemmer("russian")
    words = TOKEN.findall(text.casefold().replace("ё", "е"))
    return stemmer.stemWords([word for word in words if word not in STOP and len(word) > 1])


def expansions(text: str) -> list[str]:
    return tokens(" ".join(extra for pattern, extra in ALIASES if re.search(pattern, text, re.I)))


def previous_question(message: str, history: list[Message] | tuple[Message, ...]) -> str:
    if FOLLOW_UP.search(message) and not REACTION.fullmatch(message):
        return next((msg.content[:700] for msg in reversed(history)
                     if msg.role == "user" and not REACTION.fullmatch(msg.content)), "")
    return ""


def retrieval_query(message: str, history: list[Message] | tuple[Message, ...] = ()) -> str:
    previous = previous_question(message, history)
    return f"{message[:2200]}\n{previous}" if previous else message[:2200]


def institutional_request(message: str, history: list[Message] | tuple[Message, ...]) -> bool:
    if not INSTITUTION.search(retrieval_query(message, history)):
        return False
    if LOCAL_POLICY.search(message):
        return True
    return bool(SCRUM_MASTER.search(message) and OBLIGATION.search(message) and not SCRUM_GUIDE.search(message))


class Retriever:
    def __init__(self, knowledge: Knowledge, vectors: dict | None = None):
        self.knowledge = knowledge
        self.vectors = vectors
        self.vector_size = len(next(iter(vectors.values()))) if vectors else 0
        self.lengths: dict[str, int] = {}
        self.postings: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for chunk in knowledge.chunks.values():
            title = knowledge.documents[chunk.document_id].title
            words = tokens(f"{title} {chunk.section} {chunk.section} {chunk.text}")
            self.lengths[chunk.id] = len(words)
            for term, frequency in Counter(words).items():
                self.postings[term].append((chunk.id, frequency))
        self.average_length = sum(self.lengths.values()) / max(len(self.lengths), 1) or 1

    def rank(self, message: str, history=(), *, wide=False, vector=None, extra="") -> list[Chunk]:
        query = {term: 1.0 for term in tokens(message)}
        previous = previous_question(message, history)
        for text, weight in ((previous, 0.7), (extra, 0.6)):
            for term in tokens(text):
                query[term] = max(query.get(term, 0), weight)
        for term in expansions(f"{message} {previous} {extra}"):
            query.setdefault(term, 0.45)

        lexical: dict[str, float] = defaultdict(float)
        for term, weight in query.items():
            postings = self.postings.get(term, ())
            if not postings:
                continue
            rarity = math.log1p((len(self.lengths) - len(postings) + 0.5) / (len(postings) + 0.5))
            for key, frequency in postings:
                length_factor = 1.4 * (0.25 + 0.75 * self.lengths[key] / self.average_length)
                lexical[key] += weight * rarity * frequency * 2.4 / (frequency + length_factor)

        top = 40 if wide else 22
        lexical_rank = sorted(lexical, key=lambda key: (-lexical[key], key))[:top]
        if self.vectors and vector is not None and len(vector) == self.vector_size:
            normalized = unit_vector(vector)
            cosine = {key: math.fsum(a * b for a, b in zip(normalized, values))
                      for key, values in self.vectors.items()}
            dense_rank = sorted(cosine, key=lambda key: (-cosine[key], key))[:top]
            fused: dict[str, float] = defaultdict(float)
            for candidates, weight in ((lexical_rank, 1.1), (dense_rank, 1.0)):
                for rank, key in enumerate(candidates, start=1):
                    fused[key] += weight / (32 + rank)
            lexical_rank = sorted(fused, key=lambda key: (-fused[key], key))[:top]
        return [self.knowledge.chunks[key] for key in lexical_rank]

    def select(self, ranked: list[Chunk], wide: bool = False) -> list[Chunk]:
        # Keep multiple sections of a relevant document while leaving room for other sources.
        budget = 13_500 if wide else 9_000
        chunk_limit = 12 if wide else 7
        per_document = Counter()
        selected = []
        for chunk in ranked:
            cost = len(self.knowledge.embedding_text(chunk))
            if cost > budget or per_document[chunk.document_id] >= (6 if wide else 4):
                continue
            selected.append(chunk)
            per_document[chunk.document_id] += 1
            budget -= cost
            if len(selected) == chunk_limit:
                break
        return selected
