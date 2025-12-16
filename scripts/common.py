from __future__ import annotations

import hashlib
import itertools
import os
from typing import List

import psycopg
from qdrant_client import QdrantClient

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "384"))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "course_materials")


def fake_embed(text: str, dim: int = EMBEDDING_DIM) -> List[float]:
    hashed = hashlib.sha256(text.encode("utf-8")).digest()
    normalized = [byte / 255.0 for byte in hashed]
    return list(itertools.islice(itertools.cycle(normalized), dim))


def get_pg_connection() -> psycopg.Connection:
    return psycopg.connect(os.getenv("POSTGRES_DSN", "postgresql://rag:ragpass@localhost:5432/rag"), autocommit=True)


def get_qdrant_client() -> QdrantClient:
    return QdrantClient(host=os.getenv("QDRANT_HOST", "localhost"), port=int(os.getenv("QDRANT_PORT", "6333")))
