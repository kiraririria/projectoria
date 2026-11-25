import json
from pathlib import Path

import httpx
import pytest

from app.retrieval import dialog_context
from app.config import Settings
from app.knowledge import load_knowledge, load_vectors
from app.schemas import Message
from app.token_budget import estimate_input_tokens, fit_messages, messages_for
from test_api import browser_api, cited_payload, event_rows, model_prompt, TEAM_QUESTION
from test_diagnostics import diagnostic_records


CASES = json.loads(Path(__file__).with_name("dialog_cases.json").read_text(encoding="utf-8"))


def test_combined_knowledge_has_matching_vectors_for_both_additions():
    settings = Settings()
    knowledge = load_knowledge(settings.index_path)
    vectors = load_vectors(settings.vectors_path, knowledge, settings.embedding_model)
    assert vectors is not None and vectors.keys() == knowledge.chunks.keys()
    assert len(vectors) == 52
    assert "scrum--definition" in vectors
    assert "artifacts: true" in knowledge.chunks["gitlab--needs"].text


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_server_context_matches_shared_dialog_cases(case):
    history = [Message(role=role, content=text) for question, answer in case["turns"]
               for role, text in (("user", question), ("assistant", answer))]
    # Confirmation is handled before retrieval; its prompt has no follow-up context.
    expected = [] if case["id"] == "confirmation" else case["expected"]
    assert dialog_context(case["message"], history) == expected
    browser_history = [Message(**item) for item in case["expected"]]
    assert dialog_context(case["message"], browser_history) == expected


def test_budget_preserves_sources_for_verified_blocks_and_definition():
    payload = {"question": "Составной вопрос", "dialog_context": [], "definition_source": "S1.1",
               "sources": [{"id": f"S{i}.1", "text": "источник " * 100} for i in range(1, 5)],
               "verified_blocks": [{"text": "Проверенная часть", "evidence": [{"id": "S3.1", "quote": "источник"}]}]}
    required_payload = {**payload, "sources": [payload["sources"][0], payload["sources"][2]]}
    limit = estimate_input_tokens(messages_for("Правила", required_payload))
    fitted = fit_messages("Правила", payload, limit, required_source_ids={"S3"})
    assert estimate_input_tokens(fitted) <= limit
    assert [part["id"] for part in payload["sources"]] == ["S1.1", "S3.1"]
    assert payload["verified_blocks"] == required_payload["verified_blocks"]


@pytest.mark.anyio
async def test_repair_metrics_keep_actual_usage_separate_from_estimate(browser_api, diagnostic_records):
    calls = []

    def provider(request):
        prompt = model_prompt(request)
        calls.append(prompt)
        payload = cited_payload(prompt)
        if len(calls) == 1:
            payload["blocks"][0]["evidence"][0]["quote"] = "Несуществующая цитата о Product Owner."
        return httpx.Response(200, json={
            "model": "actual/provider-model",
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 17, "total_tokens": 140},
        })

    async with browser_api(provider) as client:
        kind, result = event_rows(await client.post("/api/chat", json={"message": TEAM_QUESTION}))
    assert kind == "final" and result["grounded"]
    rows = [json.loads(record.getMessage()) for record in diagnostic_records.records]
    provider_rows = [row for row in rows if row["event"] == "chat_provider_call"]
    assert [row["operation"] for row in provider_rows] == ["answer", "repair"]
    assert all(row["estimated_input_tokens"] > row["prompt_tokens"] == 123 for row in provider_rows)
    assert all(row["model"] == "actual/provider-model" and row["requested_model"] == "test/mentor"
               and row["total_tokens"] == 140 for row in provider_rows)
    assert rows[-1]["provider_calls"] == 2
    assert rows[-1]["prompt_tokens"] == 246 and rows[-1]["completion_tokens"] == 34
    assert len({row["request_id"] for row in rows}) == 1


@pytest.fixture
def anyio_backend():
    return "asyncio"
