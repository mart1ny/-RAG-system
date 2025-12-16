#!/usr/bin/env python3
"""
Populate the local stack with demo assignments, metadata and Qdrant vectors.
"""

from __future__ import annotations

import json
import os
import uuid
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
    fake_embed,
    get_pg_connection,
    get_qdrant_client,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "materials.json"


def ensure_collection(client: QdrantClient) -> None:
    try:
        client.get_collection(QDRANT_COLLECTION)
    except qexc.UnexpectedResponse:
        client.recreate_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )


def load_materials(path: Path = DATA_PATH) -> Iterable[dict]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    materials = load_materials()

    pg_conn = get_pg_connection()
    mongo = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
    redis_client = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    qdrant = get_qdrant_client()

    ensure_collection(qdrant)

    inserted = 0
    stream_key = os.getenv("INGEST_STREAM", "stream:ingest")
    mongo_collection = mongo["rag"]["materials"]

    with pg_conn.cursor() as cur:
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
                vector = fake_embed(chunk)

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

    print(f"Inserted {inserted} chunks into Qdrant collection '{QDRANT_COLLECTION}'.")


if __name__ == "__main__":
    main()
