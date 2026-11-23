import pytest
from pydantic import ValidationError

from app.knowledge import Chunk
from app.verify import Draft, source_passages, verify


TEXT = "Событие ограничено 15 минутами.\n\nDevelopers выбирают формат встречи."
CHUNK = Chunk("scrum--daily-scrum", "scrum", "Daily Scrum", TEXT)


def test_verifier_resolves_only_passages_provided_to_model():
    passages = source_passages([CHUNK])
    draft = Draft(status="answered", passages=["S1.2", "S1.1"])
    assert [part.text for part in verify(draft, passages)] == [
        "Developers выбирают формат встречи.", "Событие ограничено 15 минутами.",
    ]


@pytest.mark.parametrize(("status", "ids"), [
    ("answered", []),
    ("answered", ["S2.1"]),
    ("answered", ["S1.1", "S1.1"]),
    ("insufficient", ["S1.1"]),
    ("unrelated", ["S1.1"]),
])
def test_verifier_rejects_invalid_selection(status, ids):
    assert verify(Draft(status=status, passages=ids), source_passages([CHUNK])) is None


def test_provider_cannot_include_its_own_claims_in_the_answer():
    with pytest.raises(ValidationError):
        Draft.model_validate({"status": "answered", "passages": ["S1.1"], "answer": "Встреча длится 45 минут."})
