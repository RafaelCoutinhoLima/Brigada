"""Embed the reviews that have no vector for the configured model yet. Safe to re-run.

uv run python scripts/index_reviews.py            # EMBEDDING_MODEL from the environment
uv run python scripts/index_reviews.py --hashing  # offline, deterministic (tests, demos)
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brigada.mcp_server.db import connect  # noqa: E402
from brigada.rag.embeddings import Embedder, HashingEmbedder, default_embedder  # noqa: E402
from brigada.rag.retrieval import missing_reviews, store_embeddings  # noqa: E402


async def index(embedder: Embedder) -> int:
    with connect() as conn:
        rows = missing_reviews(conn, embedder.model)
        if not rows:
            return 0
        vectors = await embedder.embed([r["text"] for r in rows])
        with conn.transaction():
            return store_embeddings(conn, embedder.model, rows, vectors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hashing", action="store_true", help="use the offline embedder")
    args = parser.parse_args()
    embedder: Embedder = HashingEmbedder() if args.hashing else default_embedder()
    written = asyncio.run(index(embedder))
    print(f"{embedder.model}: {written} reviews embedded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
