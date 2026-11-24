import pytest
from pydantic import ValidationError

from app.knowledge import Chunk
from app.verify import Draft, source_passages, verify


TIME = "Событие ограничено 15 минутами."
FORMAT = "Developers выбирают формат встречи."
CHUNK = Chunk("scrum--daily-scrum", "scrum", "Daily Scrum", f"{TIME}\n\n{FORMAT}")


def block(text, source_id, quote):
    return {"text": text, "evidence": [{"id": source_id, "quote": quote}]}


def test_verifier_accepts_a_paraphrase_with_matching_quotes():
    draft = Draft(status="answered", blocks=[
        block("Формат встречи определяют Developers.", "S1.2", FORMAT),
        block("На событие отведено максимум 15 минут.", "S1.1", TIME),
    ])
    verified = verify(draft, source_passages([CHUNK]))
    assert verified is not None and len(verified) == 2


@pytest.mark.parametrize(("status", "blocks"), [
    ("answered", []),
    ("answered", [block(TIME, "S2.1", TIME)]),
    ("answered", [block(TIME, "S1.2", TIME)]),
    ("answered", [block(TIME, "S1.1", "Событие начинается в пятницу.")]),
    ("answered", [block(TIME, "S1.1", "Событие")]),
    ("insufficient", [block(TIME, "S1.1", TIME)]),
    ("unrelated", [block(TIME, "S1.1", TIME)]),
    ("clarify", [block(TIME, "S1.1", TIME)]),
])
def test_verifier_rejects_invalid_citations(status, blocks):
    assert verify(Draft(status=status, blocks=blocks), source_passages([CHUNK])) is None


@pytest.mark.parametrize("claim", [
    "Событие занимает 45 минут.", "Событие занимает 15 дней.", "Событие проходит до 15 ноября.",
])
def test_real_quote_does_not_license_an_unsupported_quantity_or_date(claim):
    draft = Draft(status="answered", blocks=[block(claim, "S1.1", TIME)])
    assert verify(draft, source_passages([CHUNK])) is None


def test_provider_cannot_append_free_text_outside_the_contract():
    with pytest.raises(ValidationError):
        Draft.model_validate({
            "status": "answered", "blocks": [block(TIME, "S1.1", TIME)], "answer": "Встреча длится 45 минут.",
        })


def test_authentic_quote_about_a_related_subject_does_not_define_the_requested_term():
    quote = "Scrum Team — небольшая самоуправляемая команда."
    chunk = Chunk("scrum--team", "scrum", "Scrum Team", quote)
    draft = Draft(status="answered", blocks=[block("Scrum Team — команда с самоуправлением.", "S1.1", quote)])
    assert verify(draft, source_passages([chunk]), definition_term="Scrum") is None
