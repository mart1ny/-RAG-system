#!/usr/bin/env python3
"""
Minimal HTTP API that reuses the local Qdrant/Postgres stack to simulate a chat-style RAG assistant.
"""

from __future__ import annotations

import os
import textwrap
import uuid
from typing import Iterable, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from scripts.common import QDRANT_COLLECTION, fake_embed, get_pg_connection, get_qdrant_client

app = FastAPI(title="RAG Learning Tasks Assistant", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_CHUNK_LIMIT = int(os.getenv("CHAT_CHUNK_LIMIT", "6"))

EXAMPLE_PROMPTS = [
    "Как подготовиться к пайплайну RAG для курса?",
    "Что включает граф знаний для тем по машинному обучению?",
    "Как лучше объяснить студенту векторное хранилище?",
]


class SourceChunk(BaseModel):
    assignment_title: str
    topic: Optional[str]
    source: Optional[str]
    chunk_number: Optional[int]
    content: str
    score: float


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Текст вопроса пользователя")
    limit: int = Field(
        DEFAULT_CHUNK_LIMIT,
        ge=1,
        le=8,
        description="Сколько фрагментов контекста вернуть",
    )


class ChatResponse(BaseModel):
    answer: str
    sources: List[SourceChunk]


def _search_hits(query: str, limit: int) -> List:
    client = get_qdrant_client()
    vector = fake_embed(query)
    return client.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=vector,
        with_payload=True,
        limit=limit,
    )


def _hydrate_hits(hits: Iterable, limit: int) -> List[SourceChunk]:
    doc_id_map: dict[uuid.UUID, dict] = {}
    ordered_docs: list[uuid.UUID] = []

    for hit in hits:
        payload = hit.payload or {}
        doc_id_str = payload.get("document_id")
        if doc_id_str:
            try:
                doc_uuid = uuid.UUID(doc_id_str)
            except ValueError:
                continue
            ordered_docs.append(doc_uuid)

    if not ordered_docs:
        return []

    with get_pg_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.content, d.source, d.chunk_number, a.title, a.topic
            FROM documents d
            JOIN assignments a ON d.assignment_id = a.id
            WHERE d.id = ANY(%s)
            """,
            (ordered_docs,),
        )
        for row in cur.fetchall():
            doc_id_map[row[0]] = {
                "content": row[1],
                "source": row[2],
                "chunk_number": row[3],
                "assignment_title": row[4],
                "topic": row[5],
            }

    hydrated: List[SourceChunk] = []
    for hit in hits:
        payload = hit.payload or {}
        doc_id_str = payload.get("document_id")
        if not doc_id_str:
            continue
        try:
            doc_uuid = uuid.UUID(doc_id_str)
        except ValueError:
            continue

        meta = doc_id_map.get(doc_uuid)
        if not meta:
            continue

        hydrated.append(
            SourceChunk(
                assignment_title=meta["assignment_title"],
                topic=meta["topic"],
                source=meta["source"],
                chunk_number=meta["chunk_number"],
                content=meta["content"],
                score=hit.score,
            )
        )
        if len(hydrated) >= limit:
            break

    return hydrated


def _build_answer(prompt: str, sources: List[SourceChunk]) -> str:
    intro = f"### Короткий ответ на запрос «{prompt}»"

    highlights: list[str] = []
    seen: set[str] = set()
    for src in sources:
        for sentence in src.content.replace("…", ".").split("."):
            normalized = sentence.strip(" \n\t;-")
            if len(normalized) < 20:
                continue
            lower = normalized.lower()
            if lower in seen:
                continue
            seen.add(lower)
            highlights.append(normalized)
            if len(highlights) >= 5:
                break
        if len(highlights) >= 5:
            break

    summary_section = ["#### Главное", ""]
    if highlights:
        summary_section.extend(f"- {highlight}" for highlight in highlights)
    else:
        summary_section.append("- Материалы подтверждают базовые определения и шаги RAG.")

    detail_lines = ["#### Использованные фрагменты", ""]
    for idx, src in enumerate(sources, start=1):
        meta_parts = [src.assignment_title]
        if src.topic:
            meta_parts.append(src.topic)
        meta = " · ".join(meta_parts)
        source_hint = ""
        if src.source:
            source_hint = f" ({src.source}, chunk #{src.chunk_number})"
        bullet = textwrap.dedent(
            f"""
            {idx}. **{meta}**{source_hint}
               > {src.content.strip()}
            """
        ).strip()
        detail_lines.append(bullet)

    outro = (
        "#### Что дальше?\n"
        "Если нужен пошаговый план или хочется раскрыть конкретный шаг, задай уточняющий вопрос — "
        "я подберу дополнительные материалы."
    )

    sections = [intro, "", *summary_section, "", *detail_lines, "", outro]
    return "\n".join(sections)


@app.get("/healthz")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/examples")
def get_examples() -> dict[str, list[str]]:
    return {"examples": EXAMPLE_PROMPTS}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    hits = _search_hits(req.message, req.limit)
    if not hits:
        raise HTTPException(status_code=404, detail="Материалы по запросу не найдены.")

    sources = _hydrate_hits(hits, req.limit)
    if not sources:
        raise HTTPException(status_code=404, detail="Не удалось сопоставить документы в Postgres.")

    answer = _build_answer(req.message, sources)
    return ChatResponse(answer=answer, sources=sources)
