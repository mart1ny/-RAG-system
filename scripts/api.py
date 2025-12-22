#!/usr/bin/env python3
"""
Minimal HTTP API that reuses the local Qdrant/Postgres stack to simulate a chat-style RAG assistant.
"""

from __future__ import annotations

import os
import textwrap
import uuid
from typing import Iterable, List, Optional, Set, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from scripts.common import (
    QDRANT_COLLECTION,
    chat_completion,
    embed_text,
    get_pg_connection,
    get_qdrant_client,
    get_neo4j_driver,
)

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

SYSTEM_PROMPT = (
    "Ты — RAG-ассистент для учебного курса. Отвечай структурированно, с шагами и ссылками на источники, "
    "используя только предоставленный контекст. В конце добавляй короткое приглашение задать уточняющие вопросы."
)

FEW_SHOT_EXAMPLES = [
    {
        "question": "Как объяснить студенту пайплайн RAG?",
        "context": "- intro_01.md: RAG совмещает поиск и генерацию.\n- pipeline.md: этапы ingestion → embeddings → retrieval → генерация.",
        "answer": (
            "1. Напоминаем, что RAG = поиск + LLM, поэтому заранее готовим базу знаний.\n"
            "2. Подчёркиваем необходимость пайплайна ingestion (сбор, чистка, чанкинг) — иначе вектора будут шумными.\n"
            "3. После векторизации в Qdrant отвечаем за быстрый поиск и добавляем контекст в промпт перед генерацией."
        ),
    },
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
    graph: Optional["GraphContext"] = None


class GraphNode(BaseModel):
    topic: str
    label: str
    assignments: List[str]
    primary: bool


class GraphEdge(BaseModel):
    source: str
    target: str


class GraphContext(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge]


def _search_hits(query: str, limit: int) -> List:
    client = get_qdrant_client()
    vector = embed_text(query)
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

    outro = (
        "#### Что дальше?\n"
        "Если нужен пошаговый план или хочется раскрыть конкретный шаг, задай уточняющий вопрос — "
        "я подберу дополнительные материалы."
    )

    sections = [intro, "", *summary_section, "", _format_context_section(sources), "", outro]
    return "\n".join(sections)


def _format_context_section(sources: List[SourceChunk]) -> str:
    lines = ["#### Использованные фрагменты", ""]
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
        lines.append(bullet)
    return "\n".join(lines)


def _build_graph_context(topics: Set[str]) -> Optional[GraphContext]:
    if not topics:
        return None

    try:
        driver = get_neo4j_driver()
    except Exception as exc:  # pragma: no cover
        print(f"[warning] Neo4j driver unavailable ({exc})")
        return None

    nodes: Dict[str, GraphNode] = {}
    edges: List[GraphEdge] = []
    edge_seen: Set[str] = set()

    with driver.session() as session:
        edge_records = session.run(
            """
            MATCH (c:Concept)-[:RELATES_TO]->(n:Concept)
            WHERE c.id IN $topics
            RETURN c.id AS source, c.name AS source_name, n.id AS target, n.name AS target_name
            LIMIT $limit
            """,
            topics=list(topics),
            limit=40,
        )
        for record in edge_records:
            source = record["source"]
            target = record["target"]
            if not source or not target:
                continue
            source_label = record["source_name"] or source
            target_label = record["target_name"] or target
            nodes.setdefault(
                source,
                GraphNode(topic=source, label=source_label, assignments=[], primary=source in topics),
            )
            nodes.setdefault(
                target,
                GraphNode(topic=target, label=target_label, assignments=[], primary=target in topics),
            )
            key = f"{source}->{target}"
            if key not in edge_seen:
                edges.append(GraphEdge(source=source, target=target))
                edge_seen.add(key)

        node_ids = list(nodes.keys() | set(topics))
        if node_ids:
            assignment_records = session.run(
                """
                MATCH (c:Concept)<-[:ASSOCIATED_WITH]-(a:Assignment)
                WHERE c.id IN $topics
                RETURN c.id AS topic, collect(a.title)[0..3] AS titles
                """,
                topics=node_ids,
            )
            for record in assignment_records:
                topic = record["topic"]
                titles = record["titles"] or []
                label = topic
                node = nodes.setdefault(
                    topic,
                    GraphNode(topic=topic, label=label, assignments=[], primary=topic in topics),
                )
                node.assignments = list(titles)

    if not nodes:
        return None

    return GraphContext(nodes=list(nodes.values()), edges=edges)


def _build_llm_answer(prompt: str, sources: List[SourceChunk]) -> Optional[str]:
    if not sources:
        return None

    context_lines: list[str] = []
    for idx, src in enumerate(sources, start=1):
        fragment = textwrap.shorten(src.content.strip(), width=500, placeholder="…")
        topic = f" · {src.topic}" if src.topic else ""
        source = f" ({src.source}, chunk #{src.chunk_number})" if src.source else ""
        context_lines.append(f"{idx}. {src.assignment_title}{topic}{source}: {fragment}")

    context_blob = "\n".join(context_lines)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for example in FEW_SHOT_EXAMPLES:
        messages.append(
            {
                "role": "user",
                "content": f"Вопрос: {example['question']}\nКонтекст:\n{example['context']}",
            }
        )
        messages.append({"role": "assistant", "content": example["answer"]})

    messages.append(
        {
            "role": "user",
            "content": (
                f"Вопрос: {prompt}\n"
                f"Контекст:\n{context_blob}\n\n"
                "Ответь по шагам, выдели ключевые идеи маркдаун-списками и упомяни источники в скобках."
            ),
        }
    )

    llm_output = chat_completion(messages)
    if not llm_output:
        return None

    context_section = _format_context_section(sources)
    outro = (
        "\n\n#### Что дальше?\n"
        "Если нужен пошаговый план или хочется раскрыть конкретный шаг, задай уточняющий вопрос — "
        "я подберу дополнительные материалы."
    )
    return f"{llm_output}\n\n{context_section}{outro}"


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

    topics = {src.topic for src in sources if src.topic}
    graph_context = _build_graph_context(topics)

    llm_answer = _build_llm_answer(req.message, sources)
    answer = llm_answer or _build_answer(req.message, sources)
    return ChatResponse(answer=answer, sources=sources, graph=graph_context)
