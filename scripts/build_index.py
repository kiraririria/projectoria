"""Prepare the read-only corpus used by the web application."""

import argparse
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"
CHUNK_SIZE = 1600
SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
SECTION = re.compile(rf"^## ({SLUG}) \| (\S.*)$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
DOCUMENT_FIELDS = ("id", "title", "publisher", "version", "url", "description")


def validate_url(url: str) -> None:
    if not isinstance(url, str) or re.search(r"[\s\x00-\x1f\x7f]", url):
        raise ValueError("Нужна абсолютная ссылка HTTPS на первоисточник")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"Некорректная ссылка HTTPS: {url}")


def read_sections(source: Path):
    """Yield explicit sections. A heading inside a code fence is plain text."""
    key, heading = "", ""
    body = []
    fence = ""
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        marker = FENCE.fullmatch(line)
        section = SECTION.fullmatch(line) if not fence else None
        if section:
            if key:
                yield key, heading, body
            key, heading = section.groups()
            body = []
            continue
        if not fence and line.startswith("## "):
            raise ValueError(f"{source.name}: ожидается заголовок ## id | Название")
        if key:
            body.append(line)
        if marker:
            delimiter, suffix = marker.groups()
            if not fence:
                fence = delimiter
            elif not suffix.strip() and delimiter[0] == fence[0] and len(delimiter) >= len(fence):
                fence = ""
    if fence:
        raise ValueError(f"{source.name}: незакрытый блок кода")
    if key:
        yield key, heading, body


def split_blocks(lines: list[str], locator: str):
    """Pack paragraphs, lists and fenced code without cutting inside a block."""
    pending = []
    block = []
    fence = ""
    for line in [*lines, ""]:
        marker = FENCE.fullmatch(line)
        if marker:
            delimiter, suffix = marker.groups()
            if not fence:
                fence = delimiter
            elif not suffix.strip() and delimiter[0] == fence[0] and len(delimiter) >= len(fence):
                fence = ""
        if line.strip() or fence:
            block.append(line.rstrip())
            continue
        if block:
            paragraph = "\n".join(block).strip()
            if len(paragraph) > CHUNK_SIZE:
                raise ValueError(f"{locator}: абзац или блок больше {CHUNK_SIZE} символов")
            combined = "\n\n".join([*pending, paragraph])
            if len(combined) > CHUNK_SIZE:
                yield "\n\n".join(pending)
                pending = []
            pending.append(paragraph)
            block = []
    if pending:
        yield "\n\n".join(pending)


def build_index(knowledge_dir: Path = KNOWLEDGE_DIR) -> dict:
    manifest = json.loads((knowledge_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("Манифест должен содержать список документов")
    documents = []
    chunks = []
    ids = set()
    sources = knowledge_dir.joinpath("sources").resolve()
    for entry in manifest:
        if not isinstance(entry, dict):
            raise ValueError("Запись манифеста должна быть объектом")
        document = {}
        for key in DOCUMENT_FIELDS:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Пустое или некорректное поле {key}: {entry.get('id')}")
            document[key] = value.strip()
        doc_id = document["id"]
        if not re.fullmatch(SLUG, doc_id) or doc_id in ids:
            raise ValueError(f"Некорректный или повторный ID документа: {doc_id}")
        ids.add(doc_id)
        validate_url(document["url"])
        if "scope" in entry:
            if entry["scope"] != "institutional":
                raise ValueError(f"{doc_id}: допустимая область scope — institutional")
            document["scope"] = entry["scope"]
        file_name = entry.get("file")
        if not isinstance(file_name, str) or not file_name:
            raise ValueError(f"{doc_id}: укажите имя файла")
        source = sources.joinpath(file_name).resolve()
        if not source.is_relative_to(sources) or source.suffix.lower() not in {".md", ".txt"}:
            raise ValueError(f"{doc_id}: нужен .md или .txt внутри knowledge/sources/")
        urls = entry.get("section_urls", {})
        if not isinstance(urls, dict):
            raise ValueError(f"{doc_id}: section_urls должен быть объектом")
        sections = set()
        for key, heading, lines in read_sections(source):
            if key in sections:
                raise ValueError(f"{doc_id}: повторный ID раздела {key}")
            sections.add(key)
            locator = f"{doc_id}--{key}"
            paragraphs = list(split_blocks(lines, locator))
            if not paragraphs:
                raise ValueError(f"Пустой раздел: {locator}")
            url = urls.get(key, document["url"])
            validate_url(url)
            for number, paragraph in enumerate(paragraphs, 1):
                chunk_id = locator if number == 1 else f"{locator}--{number}"
                chunks.append({
                    "id": chunk_id,
                    "document_id": doc_id,
                    "section": heading if len(paragraphs) == 1 else f"{heading} ({number}/{len(paragraphs)})",
                    "text": paragraph,
                    "url": url,
                })
        if not sections:
            raise ValueError(f"{source.name}: отсутствуют разделы ## id | Название")
        if unknown := urls.keys() - sections:
            raise ValueError(f"{doc_id}: ссылки для неизвестных разделов {', '.join(sorted(unknown))}")
        documents.append(document)
    return {"schema": 1, "documents": documents, "chunks": chunks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="сверить опубликованный индекс с исходниками")
    args = parser.parse_args()
    target = KNOWLEDGE_DIR / "index.json"
    try:
        data = build_index()
        rendered = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if args.check:
            if not target.is_file() or target.read_bytes() != rendered:
                parser.exit(1, "Индекс устарел. Выполните python scripts/build_index.py\n")
        else:
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as handle:
                handle.write(rendered)
            temp_file = Path(handle.name)
            try:
                temp_file.replace(target)
            finally:
                temp_file.unlink(missing_ok=True)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Ошибка сборки индекса: {error}\n")
    print(f"Документов: {len(data['documents'])}, фрагментов: {len(data['chunks'])}")


if __name__ == "__main__":
    main()
