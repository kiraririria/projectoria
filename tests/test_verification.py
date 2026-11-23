import pytest

from app.knowledge import Chunk
from app.verify import Draft, verify


QUOTE = "Событие ограничено 15 минутами."
CHOICES = {"S1": Chunk("scrum--daily-scrum", "scrum", "Daily Scrum", QUOTE)}


def draft(fact="Ежедневная встреча длится до 15 минут.", identifier="S1", quote=QUOTE, steps=()):
    return Draft.model_validate({
        "status": "answered",
        "facts": [{"text": fact, "evidence": [{"id": identifier, "quote": quote}]}],
        "steps": list(steps),
    })


def test_verifier_accepts_real_quote_and_supports_numeric_recommendation():
    proposal = {"text": "Начните с 2 вопросов о движении к цели.", "sources": ["S1"]}
    assert not verify(draft(steps=[proposal]), CHOICES, "Как провести встречу?")


@pytest.mark.parametrize("overrides", [
    {"identifier": "S12"},
    {"quote": "По пятницам нужна другая встреча."},
    {"fact": "Ежедневная встреча длится 45 минут."},
    {"fact": "Ссылка на пример https://example.invalid"},
    {"fact": "Подставная сноска [S1]"},
    {"fact": "``` Фрагмент без видимой сноски"},
    {"fact": "### Заголовок, созданный моделью"},
])
def test_verifier_detects_unsupported_locator_quote_number_or_link(overrides):
    assert verify(draft(**overrides), CHOICES, "Как провести встречу?")


def test_local_policy_claims_cannot_be_presented_as_steps_or_limitations():
    assert verify(draft(steps=[{"text": "УрФУ требует проводить встречу.", "sources": ["S1"]}]), CHOICES, "Как провести встречу?")
    candidate = draft().model_copy(update={"limits": "В УрФУ обязательно проводить встречу."})
    assert verify(candidate, CHOICES, "Как провести встречу?")
    clarification = draft().model_copy(update={"limits": "Уточните у преподавателя, требует ли УрФУ проводить встречу."})
    assert not verify(clarification, CHOICES, "Как провести встречу?")
