import hashlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    publisher: str
    version: str
    url: str
    description: str
    scope: str = "general"


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    section: str
    text: str
    url: str | None = None


@dataclass(frozen=True)
class Knowledge:
    documents: dict[str, Document]
    chunks: dict[str, Chunk]
    fingerprint: str

    def embedding_text(self, chunk: Chunk) -> str:
        document = self.documents[chunk.document_id]
        return f"{document.title}\n{chunk.section}\n{chunk.text}"

    def source(self, chunk: Chunk, label: str | None = None) -> dict:
        document = self.documents[chunk.document_id]
        result = {
            "id": chunk.id, "document_id": document.id, "title": document.title,
            "publisher": document.publisher, "version": document.version, "url": chunk.url or document.url,
            "section": chunk.section, "excerpt": chunk.text,
        }
        if label is not None:
            result["label"] = label
        return result


def load_knowledge(path: Path) -> Knowledge:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        raise ValueError("Неизвестный формат индекса")
    documents = {entry["id"]: Document(**entry) for entry in data["documents"]}
    chunks = {entry["id"]: Chunk(**entry) for entry in data["chunks"]}
    if not documents or not chunks or len(documents) != len(data["documents"]) or len(chunks) != len(data["chunks"]):
        raise ValueError("Индекс пуст или содержит повторяющиеся идентификаторы")
    for chunk in chunks.values():
        if chunk.document_id not in documents or not chunk.text.strip():
            raise ValueError("Неверный фрагмент в индексе")
    for url in [doc.url for doc in documents.values()] + [chunk.url for chunk in chunks.values() if chunk.url]:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("Неверная ссылка в индексе")
    # Hash embedding inputs, without depending on formatting or platform line endings.
    inputs = [[ch.id, documents[ch.document_id].title, ch.section, ch.text] for ch in chunks.values()]
    fingerprint = hashlib.sha256(json.dumps(inputs, ensure_ascii=False).encode("utf-8")).hexdigest()
    return Knowledge(documents, chunks, fingerprint)


def unit_vector(values) -> tuple[float, ...]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 8192:
        raise ValueError("Некорректный вектор")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("Некорректный вектор")
    length = math.hypot(*values)
    if not length or not math.isfinite(length):
        raise ValueError("Некорректный вектор")
    return tuple(value / length for value in values)


def load_vectors(path: Path, knowledge: Knowledge, model: str) -> dict[str, tuple[float, ...]] | None:
    if not model or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data["schema"], data["model"], data["fingerprint"]) != (1, model, knowledge.fingerprint):
            logger.info("Semantic index does not match the corpus or embedding model")
            return None
        vectors = {key: unit_vector(values) for key, values in data["vectors"].items()}
        if vectors.keys() != knowledge.chunks.keys() or len({len(v) for v in vectors.values()}) != 1:
            raise ValueError("Ошибочная размерность семантического индекса")
        return vectors
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        logger.warning("Semantic index could not be loaded; lexical retrieval is available")
        return None
