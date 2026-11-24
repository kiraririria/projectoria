"""Оценка поиска без API-ключа и проверка ответов запущенного сервиса."""

import argparse
import json
import re
from collections.abc import Iterator
from pathlib import Path
from statistics import mean
from time import perf_counter
from urllib.parse import quote

import httpx

from app.config import DEFAULT_EMBEDDING_MODEL, Settings
from app.knowledge import load_knowledge, load_vectors, unit_vector
from app.retrieval import Retriever, retrieval_query
from app.schemas import Message
from app.verify import normalized


CASES = Path(__file__).with_name("questions.jsonl")
CITATION = re.compile(r"\[(S[0-9]+)\]")
SOURCE_FIELDS = ("id", "document_id", "title", "publisher", "version", "url", "section", "excerpt")


def load_cases():
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]


def missing_groups(case, observed):
    return [group for group in case["evidence"] if not observed.intersection(group)]


def retrieval_results(cases, wide=False, *, query_vectors=None, settings=None):
    settings = settings or Settings()
    knowledge = load_knowledge(settings.index_path)
    vectors = None
    if query_vectors is not None:
        vectors = load_vectors(settings.vectors_path, knowledge, settings.embedding_model)
        if not vectors:
            raise ValueError("Нет семантического индекса, совместимого с моделью и текущей базой знаний")
    retriever = Retriever(knowledge, vectors)
    rows = []
    for case in cases:
        history = [Message(**message) for message in case.get("history", [])]
        vector = None
        if query_vectors is not None:
            if case["id"] not in query_vectors:
                raise ValueError(f"Не задан вектор запроса {case['id']}")
            vector = unit_vector(query_vectors[case["id"]])
            if len(vector) != retriever.vector_size:
                raise ValueError(f"Размерность запроса {case['id']} отличается от размерности индекса")
        ranked = retriever.rank(case["question"], history, wide=wide, vector=vector)
        selected = [chunk.id for chunk in retriever.select(ranked, wide=wide)]
        expected = set().union(*case["evidence"])
        rank = next((i for i, item in enumerate(selected, 1) if item in expected), None)
        rows.append({
            **case,
            "selected": selected,
            "mode": "hybrid" if vector is not None else "lexical",
            "missing_evidence_groups": missing_groups(case, set(selected)),
            "reciprocal_rank": 1 / rank if rank else 0.0,
        })
    return rows


def events(lines: Iterator[str]):
    """Собирает именованные события SSE, включая многострочные данные."""
    kind = "message"
    data = []
    for line in lines:
        if not line:
            if data:
                yield kind, json.loads("\n".join(data))
            kind, data = "message", []
            continue
        field, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.removeprefix(" ")
        if field == "event":
            kind = value
        elif field == "data":
            data.append(value)


def fetch_answer(client, case):
    body = {"message": case["question"], "history": case.get("history", [])}
    with client.stream("POST", "/api/chat", json=body) as response:
        response.raise_for_status()
        for kind, payload in events(response.iter_lines()):
            if kind == "error":
                raise ValueError(payload["message"])
            if kind == "final":
                return payload
    raise ValueError("Поток завершился без события final")


def check_answer(client, case, payload):
    sources = payload["sources"]
    labels = {source["label"] for source in sources}
    source_ids = {source["id"] for source in sources}
    answer = payload["answer"]
    answer_text = normalized(answer)
    expected_status = case.get("expected_status")
    issues = []
    if not answer.strip():
        issues.append("Пустой ответ")
    if len(labels) != len(sources) or len(source_ids) != len(sources):
        issues.append("Повторяются метки или идентификаторы источников")
    if set(CITATION.findall(answer)) != labels:
        issues.append("Внутренние ссылки и карточки источников расходятся")
    if case["evidence"] and not payload["grounded"]:
        issues.append("Вместо ответа по базе вернулись только связанные материалы или сообщение об ошибке")
    if not case["evidence"] and expected_status != "project_details" and sources:
        issues.append("Статус наличия подтверждений отличается от ожидаемого")
    if expected_status and payload.get("status") != expected_status:
        issues.append(f"Ожидался статус {expected_status}, получен {payload.get('status')}")
    for group in case.get("required", []):
        markers = [group] if isinstance(group, str) else group
        if not any(normalized(marker) in answer_text for marker in markers):
            issues.append(f"Нет ожидаемого фрагмента ответа: {', '.join(markers)}")
    if opening := case.get("opening_pattern"):
        if not re.match(opening, answer_text.replace("**", "")):
            issues.append("Ответ не начинается с прямого определения понятия")
    if (max_words := case.get("max_words")) is not None:
        if len(CITATION.sub("", answer).split()) > max_words:
            issues.append(f"Ответ длиннее {max_words} слов")
    for marker in case.get("forbidden", []):
        if normalized(marker) in answer_text:
            issues.append(f"В ответ попала контрольная строка {marker}")
    for source in sources:
        detail = client.get(f"/api/sources/{quote(source['id'], safe='')}")
        record = detail.json() if detail.is_success else {}
        if not all(record.get(key) == source[key] for key in SOURCE_FIELDS):
            issues.append(f"Карточка {source['id']} не совпадает с записью в базе")
        elif not source.get("evidence") or any(
            not normalized(snippet) or normalized(snippet) not in normalized(record["excerpt"])
            for snippet in source["evidence"]
        ):
            issues.append(f"Выдержка из {source['id']} отсутствует в базе")
    missing = missing_groups(case, source_ids)
    if missing:
        issues.append(f"Отсутствуют нужные фрагменты: {missing}")
    return issues


def live_results(cases, url):
    rows = []
    with httpx.Client(base_url=url, timeout=120.0) as client:
        for case in cases:
            started = perf_counter()
            row = {**case, "issues": []}
            try:
                payload = fetch_answer(client, case)
                row.update(payload)
                row["issues"] = check_answer(client, case, payload)
            except (httpx.HTTPStatusError, httpx.RequestError, ValueError, KeyError, TypeError) as error:
                row["issues"] = [str(error)]
            row["elapsed_seconds"] = round(perf_counter() - started, 2)
            rows.append(row)
            verdict = "ПРОВЕРИТЬ" if row["issues"] else "OK"
            print(f"{case['id']:27} {verdict:10} {row['elapsed_seconds']:6.2f} с", flush=True)
            for issue in row["issues"]:
                print(f"  {issue}")
    return rows


def print_retrieval(rows):
    if rows:
        mode = "гибридный" if rows[0]["mode"] == "hybrid" else "лексический"
        print(f"Поиск: {mode}")
    evaluated = [row for row in rows if row["evidence"]]
    complete = sum(not row["missing_evidence_groups"] for row in evaluated)
    groups = sum(len(row["evidence"]) for row in evaluated)
    missed = sum(len(row["missing_evidence_groups"]) for row in evaluated)
    for row in rows:
        verdict = "—"
        if row["evidence"]:
            verdict = "ПРОПУСК" if row["missing_evidence_groups"] else "OK"
        print(f"{row['id']:27} {verdict:8} {', '.join(row['selected'])}")
        for group in row["missing_evidence_groups"]:
            print(f"  Требуется один из фрагментов: {', '.join(group)}")
    if evaluated:
        mrr = mean(row["reciprocal_rank"] for row in evaluated)
        print(f"Все группы: {complete}/{len(evaluated)}; покрытие групп: {groups - missed}/{groups}; MRR: {mrr:.3f}")
    return complete == len(evaluated)


def encode_cases(cases, onnx_dir):
    from scripts.build_vectors import embed_onnx

    for file in ("model_int8.onnx", "tokenizer.json"):
        if not (onnx_dir / file).is_file():
            raise ValueError(f"В папке BGE-M3 отсутствует {file}")
    queries = [retrieval_query(case["question"], [Message(**message) for message in case.get("history", [])])
               for case in cases]
    return {case["id"]: vector for case, vector in zip(cases, embed_onnx(queries, onnx_dir), strict=True)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Адрес запущенного API для проверки реальных ответов")
    parser.add_argument("--output", type=Path, help="Сохранить результаты и критерии ручной оценки в JSON")
    parser.add_argument("--case", action="append", help="Один или несколько ID вопросов")
    parser.add_argument("--wide", action="store_true", help="Проверить расширенный контекст поиска")
    semantic = parser.add_mutually_exclusive_group()
    semantic.add_argument("--query-vectors", type=Path, help="JSON с векторами поисковых запросов по ID вопросов")
    semantic.add_argument("--onnx-dir", type=Path, help="Папка BGE-M3 ONNX для расчёта поисковых запросов")
    args = parser.parse_args()
    if args.url and (args.wide or args.query_vectors or args.onnx_dir):
        parser.error("--wide, --query-vectors и --onnx-dir применяются к офлайн-поиску")
    cases = load_cases()
    if args.case:
        unknown = set(args.case) - {case["id"] for case in cases}
        if unknown:
            parser.error(f"Неизвестные ID: {', '.join(sorted(unknown))}")
        cases = [case for case in cases if case["id"] in args.case]
    if args.url:
        rows = live_results(cases, args.url)
        passed_count = sum(not row["issues"] for row in rows)
        print(f"Автоматические проверки: {passed_count}/{len(rows)}. Смысл ответа оценивается отдельно.")
        passed = passed_count == len(rows)
    else:
        settings = Settings.from_env()
        query_vectors = None
        try:
            if args.query_vectors:
                query_vectors = json.loads(args.query_vectors.read_text(encoding="utf-8"))
                if not isinstance(query_vectors, dict):
                    raise ValueError("Векторы запросов должны быть JSON-объектом, где ключи — ID вопросов")
            if args.onnx_dir:
                if settings.embedding_model != DEFAULT_EMBEDDING_MODEL:
                    raise ValueError(f"--onnx-dir требует OPENROUTER_EMBEDDING_MODEL={DEFAULT_EMBEDDING_MODEL}")
                query_vectors = encode_cases(cases, args.onnx_dir)
            rows = retrieval_results(cases, wide=args.wide, query_vectors=query_vectors, settings=settings)
        except (ImportError, OSError, ValueError) as error:
            parser.exit(1, f"Ошибка оценки поиска: {error}\n")
        passed = print_retrieval(rows)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
