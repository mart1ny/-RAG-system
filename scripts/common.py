from __future__ import annotations

import hashlib
import itertools
import os
from typing import List, Optional, Sequence, TypedDict

import psycopg
from dotenv import load_dotenv
from llama_cpp import Llama
from neo4j import Driver, GraphDatabase
from qdrant_client import QdrantClient

load_dotenv(os.getenv("ENV_FILE", ".env"))

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "384"))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "course_materials")
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "huggingface").lower()
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "local").lower()
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4jpass")

print(f"[init] EMBEDDING_PROVIDER={EMBEDDING_PROVIDER} (dim={EMBEDDING_DIM})")
print(f"[init] LLM_PROVIDER={LLM_PROVIDER}")

_hf_model = None
_embed_fallback_logged: dict[str, bool] = {}
_llama_client: Optional[Llama] = None
_chat_fallback_logged = False
_hf_loaded_logged = False
_llama_loaded_logged = False
_neo4j_driver: Optional[Driver] = None


class ChatMessage(TypedDict):
    role: str
    content: str


def fake_embed(text: str, dim: int = EMBEDDING_DIM) -> List[float]:
    hashed = hashlib.sha256(text.encode("utf-8")).digest()
    normalized = [byte / 255.0 for byte in hashed]
    return list(itertools.islice(itertools.cycle(normalized), dim))


def embed_text(text: str) -> List[float]:
    if EMBEDDING_PROVIDER in {"huggingface", "hf"}:
        try:
            return _embed_huggingface(text)
        except Exception as exc:  # pragma: no cover
            _log_embed_warning("Hugging Face", exc)
    return fake_embed(text)


def _get_huggingface_model():
    global _hf_model
    global _hf_loaded_logged
    if _hf_model is not None:
        return _hf_model

    model_name = os.getenv("HUGGINGFACE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    from sentence_transformers import SentenceTransformer  # type: ignore

    _hf_model = SentenceTransformer(model_name)
    if not _hf_loaded_logged:
        print(f"[info] SentenceTransformer model loaded: {model_name}")
        _hf_loaded_logged = True

    dimension = getattr(_hf_model, "get_sentence_embedding_dimension", lambda: None)()
    if dimension and dimension != EMBEDDING_DIM:
        print(
            f"[warning] SentenceTransformer embedding dim {dimension} differs from EMBEDDING_DIM={EMBEDDING_DIM}. "
            "Ensure Qdrant collection uses the same size."
        )
    return _hf_model


def _embed_huggingface(text: str) -> List[float]:
    model = _get_huggingface_model()
    vector = model.encode([text], convert_to_numpy=True)[0]
    return vector.tolist()


def chat_completion(messages: Sequence[ChatMessage], temperature: Optional[float] = None) -> Optional[str]:
    if LLM_PROVIDER != "llama":
        return None

    try:
        client = _get_llama_client()
    except RuntimeError as exc:
        _log_chat_warning(exc)
        return None

    temp = temperature if temperature is not None else float(os.getenv("LLAMA_TEMPERATURE", "0.2"))
    max_tokens = int(os.getenv("LLAMA_MAX_TOKENS", "512"))

    try:
        response = client.create_chat_completion(
            messages=[{"role": msg["role"], "content": msg["content"]} for msg in messages],
            temperature=temp,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # pragma: no cover
        _log_chat_warning(exc)
        return None

    choice = response["choices"][0]["message"]["content"]
    return choice.strip() if choice else None


def _get_llama_client() -> Llama:
    global _llama_client
    global _llama_loaded_logged
    if _llama_client is not None:
        return _llama_client

    model_path = os.getenv("LLAMA_MODEL_PATH")
    if not model_path:
        raise RuntimeError("LLAMA_MODEL_PATH is not set. Provide path to a GGUF model.")

    ctx = int(os.getenv("LLAMA_CTX_SIZE", "4096"))
    n_threads = int(os.getenv("LLAMA_THREADS", str(os.cpu_count() or 4)))
    n_gpu_layers = int(os.getenv("LLAMA_GPU_LAYERS", "0"))

    _llama_client = Llama(
        model_path=model_path,
        n_ctx=ctx,
        n_threads=n_threads,
        n_gpu_layers=n_gpu_layers,
        use_mmap=True,
    )
    if not _llama_loaded_logged:
        print(f"[info] Llama model loaded from {model_path} (ctx={ctx}, threads={n_threads}, gpu_layers={n_gpu_layers})")
        _llama_loaded_logged = True
    return _llama_client


def get_pg_connection() -> psycopg.Connection:
    return psycopg.connect(os.getenv("POSTGRES_DSN", "postgresql://rag:ragpass@localhost:5432/rag"), autocommit=True)


def get_qdrant_client() -> QdrantClient:
    return QdrantClient(host=os.getenv("QDRANT_HOST", "localhost"), port=int(os.getenv("QDRANT_PORT", "6333")))


def get_neo4j_driver() -> Driver:
    global _neo4j_driver
    if _neo4j_driver:
        return _neo4j_driver
    _neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _neo4j_driver


def _log_embed_warning(provider: str, exc: Exception) -> None:
    if not _embed_fallback_logged.get(provider):
        print(f"[warning] {provider} embeddings failed ({exc}). Falling back to deterministic embeddings.")
        _embed_fallback_logged[provider] = True


def _log_chat_warning(exc: Exception) -> None:
    global _chat_fallback_logged
    if not _chat_fallback_logged:
        print(f"[warning] Local LLM generation failed ({exc}). Falling back to Markdown summary.")
        _chat_fallback_logged = True
