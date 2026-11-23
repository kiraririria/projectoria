import json

import pytest

from app.config import Settings
from evaluation.run import load_cases, retrieval_results
from scripts.build_index import build_index


@pytest.fixture(scope="module")
def corpus():
    return json.loads(Settings().index_path.read_text(encoding="utf-8"))


def test_initial_index_reproduces_from_sources(corpus):
    assert build_index() == corpus
    document_ids = {document["id"] for document in corpus["documents"]}
    chunk_ids = {chunk["id"] for chunk in corpus["chunks"]}
    assert len(corpus["chunks"]) == len(chunk_ids)
    assert all(chunk["document_id"] in document_ids for chunk in corpus["chunks"])


def test_evaluation_case_sources_exist_and_types_are_covered(corpus):
    cases = load_cases()
    assert len(cases) >= 24
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["kind"] for case in cases} >= {
        "method", "practice", "provision", "standard", "follow-up", "misconception", "unknown", "combined", "injection",
    }
    known = {chunk["id"] for chunk in corpus["chunks"]}
    for case in cases:
        assert case["question"].strip() and case["review"].strip()
        for alternatives in case["evidence"]:
            assert alternatives and set(alternatives) <= known, case["id"]


@pytest.mark.parametrize("case_id", [
    "scrum-cancel", "kanban-metrics", "gitlab-artifacts", "gost-scope", "context-frontend", "injection-scrum",
])
def test_lexical_retrieval_smoke(case_id):
    case = next(row for row in load_cases() if row["id"] == case_id)
    [result] = retrieval_results([case])
    assert not result["missing_evidence_groups"], result
