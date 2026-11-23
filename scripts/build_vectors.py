"""Build the read-only dense index through OpenRouter or the matching local BGE-M3 weights."""

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.config import DEFAULT_EMBEDDING_MODEL, Settings
from app.knowledge import load_knowledge, unit_vector
from app.openrouter import OpenRouter, ProviderError


async def embed_remote(texts: list[str], settings: Settings) -> list:
    router = OpenRouter(settings)
    vectors = []
    try:
        for start in range(0, len(texts), 16):
            vectors.extend(await router.embed(texts[start:start + 16], timeout=45))
    finally:
        await router.http.aclose()
    return vectors


def embed_onnx(texts: list[str], model_dir: Path) -> list:
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=1024)
    # BGE-M3 uses XLM-R's <pad> with id 1. Dense pooling uses the first token.
    tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    session = ort.InferenceSession(str(model_dir / "model_int8.onnx"), options, providers=["CPUExecutionProvider"])
    input_names = {item.name for item in session.get_inputs()}
    vectors = []
    for start in range(0, len(texts), 4):
        encoded = tokenizer.encode_batch(texts[start:start + 4])
        inputs = {
            "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
            "attention_mask": np.asarray([item.attention_mask for item in encoded], dtype=np.int64),
            "token_type_ids": np.asarray([item.type_ids for item in encoded], dtype=np.int64),
        }
        hidden = session.run(["last_hidden_state"], {key: value for key, value in inputs.items() if key in input_names})[0]
        vectors.extend(unit_vector(row.tolist()) for row in hidden[:, 0, :])
    return vectors


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Build knowledge/vectors.json before deployment")
    parser.add_argument("--onnx-dir", type=Path, help="Local BGE-M3 INT8 model and tokenizer (offline)")
    args = parser.parse_args()
    if args.onnx_dir and settings.embedding_model != DEFAULT_EMBEDDING_MODEL:
        parser.error(f"--onnx-dir requires OPENROUTER_EMBEDDING_MODEL={DEFAULT_EMBEDDING_MODEL}")
    if not args.onnx_dir and (not settings.api_key or not settings.embedding_model):
        parser.error("Set OPENROUTER_API_KEY and OPENROUTER_EMBEDDING_MODEL or specify --onnx-dir")

    corpus = load_knowledge(settings.index_path)
    texts = [corpus.embedding_text(chunk) for chunk in corpus.chunks.values()]
    try:
        vectors = embed_onnx(texts, args.onnx_dir) if args.onnx_dir else asyncio.run(embed_remote(texts, settings))
    except ProviderError as exc:
        parser.exit(1, f"{exc}\n")

    if len(vectors) != len(texts) or len({len(vector) for vector in vectors}) != 1:
        raise ValueError("Incomplete vector index")
    if settings.embedding_model == DEFAULT_EMBEDDING_MODEL and len(vectors[0]) != 1024:
        raise ValueError("BGE-M3 dense vectors must have 1024 dimensions")

    data = {
        "schema": 1, "model": settings.embedding_model, "fingerprint": corpus.fingerprint,
        "origin": "bge-m3-onnx-int8" if args.onnx_dir else "openrouter",
        "vectors": {key: [round(value, 7) for value in vector]
                    for key, vector in zip(corpus.chunks, vectors, strict=True)},
    }
    with NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=settings.vectors_path.parent,
                            delete=False) as pending:
        temporary_path = Path(pending.name)
        json.dump(data, pending, ensure_ascii=False, separators=(",", ":"))
        pending.write("\n")
    temporary_path.replace(settings.vectors_path)
    print(f"{len(vectors)} x {len(vectors[0])} vectors: {settings.vectors_path}")


if __name__ == "__main__":
    main()
