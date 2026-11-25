import asyncio
import json

import pytest

from app.config import Settings
from app.knowledge import Chunk, Document, Knowledge, load_knowledge
from app.pipeline import Pipeline
from app.retrieval import Retriever, previous_question
from app.schemas import ChatRequest, Message
from app.verify import Draft, render_answer, source_passages, verify
from evaluation.run import citations_match

FIRST = "Для чего проводят Daily Scrum?"
FOLLOW = "А если команда распределённая?"
FACT = "Scrum Team — небольшая самоуправляемая кросс-функциональная команда без внутренней иерархии или подкоманд"
DEVELOPERS = "Developers создают пригодный к использованию Increment"
QUESTION = "За что отвечают участники Scrum Team? Как Developers работают с планом спринта?"


def test_follow_up_keeps_original_topic_and_latest_turn():
    history = [
        Message(role="user", content=FIRST),
        Message(role="assistant", content="Developers сверяют прогресс."),
        Message(role="user", content=FOLLOW),
        Message(role="assistant", content="Команда согласует формат."),
    ]
    assert previous_question("Тогда как поступить?", history).splitlines() == [FIRST, FOLLOW]


def test_short_follow_up_and_explicit_topic_reset():
    history = [Message(role="user", content=FIRST)]
    assert previous_question("А кто выбирает формат?", history) == FIRST
    assert previous_question("А что такое WIP?", history) == ""
    assert previous_question("Теперь о Git: как создать ветку?", history) == ""
    history.append(Message(role="user", content="Что такое Kanban?"))
    assert previous_question("А кто выбирает формат?", history) == "Что такое Kanban?"


def test_citation_offsets_point_to_the_selected_occurrence_in_utf16():
    quote = "В этой команде есть ёмкость."
    excerpt = f"🧩 {quote}\n\n  {quote}\n"
    chunk = Chunk("term", "doc", "Раздел", excerpt)
    document = Document("doc", "Материал", "Редакция", "1", "https://example.org/", "Описание")
    knowledge = Knowledge({"doc": document}, {"term": chunk}, "")
    draft = Draft.model_validate({
        "status": "answered",
        "blocks": [{"text": "Команда работает с ограничениями.", "evidence": [
            {"id": "S1.2", "quote": "в этой команде\nесть емкость."},
        ]}],
    })
    blocks = verify(draft, source_passages([chunk]))
    assert blocks is not None
    _, [source] = render_answer(blocks, knowledge)
    start = len(excerpt[:excerpt.rindex(quote)].encode("utf-16-le")) // 2
    assert source["evidence"] == [quote]
    assert source["evidence_spans"] == [{"start": start, "end": start + len(quote)}]
    assert citations_match(source, excerpt)
    assert not citations_match(dict(source, evidence_spans=[{"start": start - 1, "end": start + len(quote)}]), excerpt)


def test_evaluation_accepts_legacy_citations_and_rejects_empty_new_spans():
    excerpt = "В этой команде есть ёмкость."
    legacy = {"evidence": ["в этой команде\nесть емкость."]}
    assert citations_match(legacy, excerpt)
    assert not citations_match(dict(legacy, evidence=["Не найдено."]), excerpt)
    assert not citations_match(dict(legacy, evidence_spans=[]), excerpt)


def test_maximum_does_not_establish_an_exact_duration():
    quote = "Продолжительность события — не более 15 минут."
    chunk = Chunk("daily", "scrum", "Daily Scrum", quote)
    passages = source_passages([chunk])
    for claim, allowed in (("Событие длится ровно 15 минут.", False),
                           ("Событие длится максимум 15 минут.", True)):
        draft = Draft.model_validate({
            "status": "answered",
            "blocks": [{"text": claim, "evidence": [{"id": "S1.1", "quote": quote}]}],
        })
        assert (verify(draft, passages) is not None) == allowed


class Router:
    settings = Settings(embedding_model="")
    diagnostics = None

    def __init__(self, initial, expanded=None, *, initial_note="", expanded_note=""):
        self.initial = initial
        self.expanded = expanded
        self.initial_note = initial_note
        self.expanded_note = expanded_note
        self.calls = []

    async def chat(self, messages, *, timeout, stage="generation"):
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload)
        if "sources" not in payload:
            return '{"keep":[1]}'
        if self.expanded and len(self.calls) > 1:
            status, texts = self.expanded
            note = self.expanded_note
        else:
            status, texts = self.initial
            note = self.initial_note
        blocks = []
        for text in texts:
            part = next(part for part in payload["sources"] if text in part["text"])
            blocks.append({"text": text, "evidence": [{"id": part["id"], "quote": text}]})
        return json.dumps({"status": status, "blocks": blocks, "note": note}, ensure_ascii=False)


async def outcome(router):
    pipeline = Pipeline(Retriever(load_knowledge(Settings().index_path)))
    deadline = asyncio.get_running_loop().time() + 45
    rows = [event async for event in pipeline.events(ChatRequest(message=QUESTION), router, deadline)]
    return rows[-1][1]


def test_multiblock_answer_retains_verified_parts_without_a_second_model_call():
    router = Router(("answered", [FACT, DEVELOPERS]))
    result = asyncio.run(outcome(router))
    assert FACT in result["answer"] and DEVELOPERS in result["answer"]
    assert len(router.calls) == 1


@pytest.mark.parametrize("expanded_status", ["answered", "partial"])
def test_wider_retry_cannot_replace_a_verified_partial_with_different_coverage(expanded_status):
    router = Router(("partial", [FACT]), (expanded_status, [DEVELOPERS]))
    result = asyncio.run(outcome(router))
    assert len(router.calls) == 2
    assert result["status"] == "partial"
    assert FACT in result["answer"] and DEVELOPERS not in result["answer"]


def test_wider_retry_can_complete_an_answer_while_preserving_verified_parts():
    router = Router(("partial", [FACT]), ("answered", [FACT, DEVELOPERS]))
    result = asyncio.run(outcome(router))
    assert result["status"] == "answered"
    assert FACT in result["answer"] and DEVELOPERS in result["answer"]
    assert len(router.calls) == 2
    assert router.calls[1]["verified_blocks"][0]["text"] == FACT


@pytest.mark.parametrize(("expanded_note", "expected_note"), [
    ("Не хватает сведений о формате встречи.", "В базе нашлись сведения для части вопроса."),
    (
        "Не хватает сведений о формате встречи и управлении сроками.",
        "Не хватает сведений о формате встречи и управлении сроками.",
    ),
], ids=["changed-note", "unchanged-note"])
def test_wider_partial_keeps_a_caution_when_the_unsupported_parts_are_uncertain(expanded_note, expected_note):
    initial_note = "Не хватает сведений о формате встречи и управлении сроками."
    router = Router(
        ("partial", [FACT]), ("partial", [FACT, DEVELOPERS]),
        initial_note=initial_note, expanded_note=expanded_note,
    )
    result = asyncio.run(outcome(router))
    assert result["status"] == "partial"
    assert FACT in result["answer"] and DEVELOPERS in result["answer"]
    assert result["answer"].endswith(expected_note)


@pytest.mark.parametrize("mention", [
    "способ контроля незавершённой работы (WIP), правила переходов и прогноз SLE",
    "The Kanban Guide не задаёт одинаковый WIP-лимит для всех команд или для каждой колонки.",
], ids=["other-section", "same-section"])
def test_known_term_cites_the_definition_passage_not_a_nearby_mention(mention):
    question = "Что такое WIP?"

    class WrongSourceRouter(Router):
        async def chat(self, messages, *, timeout, stage="generation"):
            payload = json.loads(messages[-1]["content"])
            self.calls.append(payload)
            assert payload["definition_term"] == "WIP"
            part = next(part for part in payload["sources"] if mention in part["text"])
            return json.dumps({"status": "answered", "blocks": [{
                "text": "WIP — ограничение, которое управляет переходом задач.",
                "evidence": [{"id": part["id"], "quote": mention}],
            }]})

    async def ask():
        pipeline = Pipeline(Retriever(load_knowledge(Settings().index_path)))
        deadline = asyncio.get_running_loop().time() + 45
        rows = [event async for event in pipeline.events(ChatRequest(message=question), router, deadline)]
        return rows[-1][1]

    router = WrongSourceRouter(None)
    result = asyncio.run(ask())
    assert result["status"] == "unavailable"
    assert result["sources"] == []


def test_wider_retry_cannot_relabel_an_unchanged_partial_answer():
    router = Router(("partial", [FACT]), ("answered", [FACT]))
    result = asyncio.run(outcome(router))
    assert len(router.calls) == 2
    assert result["status"] == "partial"
    assert FACT in result["answer"]
