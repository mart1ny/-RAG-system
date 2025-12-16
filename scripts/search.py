#!/usr/bin/env python3
"""
Simple CLI to query the local Qdrant collection and show linked metadata.
"""

from __future__ import annotations

import argparse
import uuid

from scripts.common import QDRANT_COLLECTION, fake_embed, get_pg_connection, get_qdrant_client


def search(query: str, limit: int) -> None:
    client = get_qdrant_client()
    vector = fake_embed(query)
    results = client.search(
        collection_name=QDRANT_COLLECTION,
        query_vector=vector,
        with_payload=True,
        limit=limit,
    )

    if not results:
        print("No matches found.")
        return

    with get_pg_connection() as conn, conn.cursor() as cur:
        for idx, hit in enumerate(results, start=1):
            payload = hit.payload or {}
            doc_id_str = payload.get("document_id")
            doc_uuid = uuid.UUID(doc_id_str) if doc_id_str else None
            title = topic = source = content = None
            chunk_no = payload.get("chunk_number")

            if doc_uuid:
                cur.execute(
                    """
                    SELECT a.title, a.topic, d.source, d.content
                    FROM documents d
                    JOIN assignments a ON d.assignment_id = a.id
                    WHERE d.id = %s
                    """,
                    (doc_uuid,),
                )
                row = cur.fetchone()
                if row:
                    title, topic, source, content = row

            print(f"[{idx}] score={hit.score:.4f}")
            if title:
                print(f"    assignment: {title} ({topic})")
            if source:
                print(f"    source: {source} chunk #{chunk_no}")
            if payload.get("topic") and not topic:
                print(f"    topic: {payload['topic']}")
            if content:
                print("    content:", content)
            print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search the demo Qdrant collection")
    parser.add_argument("query", help="Text to embed and search for")
    parser.add_argument("--limit", type=int, default=3, help="Number of hits to return (default: 3)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    search(args.query, args.limit)


if __name__ == "__main__":
    main()
