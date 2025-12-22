#!/usr/bin/env python3
"""
Populate the local stack with demo assignments, metadata and Qdrant vectors.
"""

from __future__ import annotations

import json
import os
import uuid
import contextlib
from pathlib import Path
from typing import Iterable

from pymongo import MongoClient
from qdrant_client import QdrantClient
from qdrant_client.http import exceptions as qexc
from qdrant_client.http.models import Distance, PointStruct, VectorParams
from redis import Redis

from scripts.common import (
    EMBEDDING_DIM,
    QDRANT_COLLECTION,
    embed_text,
    get_pg_connection,
    get_qdrant_client,
    get_neo4j_driver,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "materials.json"


def ensure_collection(client: QdrantClient) -> None:
    def recreate():
        client.recreate_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )

    try:
        collection = client.get_collection(QDRANT_COLLECTION)
        current_vectors = getattr(collection.config.params, "vectors", None)
        current_size = getattr(current_vectors, "size", None) if current_vectors else None
        if current_size != EMBEDDING_DIM:
            print(
                f"[info] Recreating Qdrant collection '{QDRANT_COLLECTION}' "
                f"to match vector size {EMBEDDING_DIM} (was {current_size})."
            )
            recreate()
    except qexc.UnexpectedResponse:
        recreate()


def load_materials(path: Path = DATA_PATH) -> Iterable[dict]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _upsert_assignment_graph(tx, assignment_id: str, title: str, topic: str | None) -> None:
    if not topic:
        return
    tx.run(
        """
        MERGE (a:Assignment {id: $id})
        SET a.title = $title, a.topic = $topic
        WITH a
        MERGE (c:Concept {id: $topic})
        SET c.name = $topic
        MERGE (a)-[:ASSOCIATED_WITH]->(c)
        """,
        id=assignment_id,
        title=title,
        topic=topic,
    )


def _link_concepts(tx, left_topic: str | None, right_topic: str | None) -> None:
    if not left_topic or not right_topic:
        return
    tx.run(
        """
        MERGE (c1:Concept {id: $left})
        MERGE (c2:Concept {id: $right})
        MERGE (c1)-[:RELATES_TO]->(c2)
        MERGE (c2)-[:RELATES_TO]->(c1)
        """,
        left=left_topic,
        right=right_topic,
    )


def main() -> None:
    materials = load_materials()

    pg_conn = get_pg_connection()
    mongo = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
    redis_client = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    qdrant = get_qdrant_client()

    ensure_collection(qdrant)
    try:
        neo4j_driver = get_neo4j_driver()
    except Exception as exc:  # pragma: no cover
        print(f"[warning] Neo4j unavailable ({exc}). Skipping graph enrichment.")
        neo4j_driver = None
    previous_topic: str | None = None

    inserted = 0
    stream_key = os.getenv("INGEST_STREAM", "stream:ingest")
    mongo_collection = mongo["rag"]["materials"]

    neo_session_context = neo4j_driver.session() if neo4j_driver else contextlib.nullcontext()

    with pg_conn.cursor() as cur, neo_session_context as neo_session:
        for item in materials:
            assignment_id = uuid.uuid4()
            cur.execute(
                """
                INSERT INTO assignments (id, title, description, topic)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    assignment_id,
                    item["title"],
                    item.get("description"),
                    item.get("topic"),
                ),
            )

            mongo_doc = {
                "assignment_id": str(assignment_id),
                "source": item["source"],
                "notes": item.get("notes", []),
                "chunks": item["chunks"],
            }
            mongo_collection.insert_one(mongo_doc)

            for idx, chunk in enumerate(item["chunks"]):
                cur.execute(
                    """
                    INSERT INTO documents (assignment_id, source, chunk_number, content)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (assignment_id, item["source"], idx, chunk),
                )
                document_id = cur.fetchone()[0]
                point_id = str(uuid.uuid4())
                vector = embed_text(chunk)

                qdrant.upsert(
                    collection_name=QDRANT_COLLECTION,
                    points=[
                        PointStruct(
                            id=point_id,
                            vector=vector,
                            payload={
                                "assignment_id": str(assignment_id),
                                "document_id": str(document_id),
                                "chunk_number": idx,
                                "topic": item.get("topic"),
                                "source": item["source"],
                            },
                        )
                    ],
                )

                cur.execute(
                    """
                    INSERT INTO vector_refs (document_id, qdrant_collection, point_id)
                    VALUES (%s, %s, %s)
                    """,
                    (document_id, QDRANT_COLLECTION, point_id),
                )

                redis_client.xadd(
                    stream_key,
                    {
                        "assignment_id": str(assignment_id),
                        "document_id": str(document_id),
                        "point_id": point_id,
                    },
                )

                inserted += 1

            if neo_session:
                neo_session.execute_write(
                    _upsert_assignment_graph,
                    str(assignment_id),
                    item["title"],
                    item.get("topic"),
                )

                if previous_topic and previous_topic != item.get("topic"):
                    neo_session.execute_write(
                        _link_concepts,
                        previous_topic,
                        item.get("topic"),
                    )

            previous_topic = item.get("topic")

    print(f"Inserted {inserted} chunks into Qdrant collection '{QDRANT_COLLECTION}'.")


if __name__ == "__main__":
    main()
