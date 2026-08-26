"""CLI for generating the optional semantic index for existing assets."""

import argparse
import asyncio

from .embedding import EmbeddingUnavailable
from .knowledge import rebuild_knowledge_embeddings


async def main() -> None:
    parser = argparse.ArgumentParser(description="Build SnapNote knowledge embeddings")
    parser.add_argument("--asset-id", help="Only index one knowledge asset")
    parser.add_argument("--owner-scope", default="local")
    args = parser.parse_args()
    try:
        result = await rebuild_knowledge_embeddings(
            asset_id=args.asset_id, owner_scope=args.owner_scope
        )
    except EmbeddingUnavailable as error:
        raise SystemExit(str(error)) from error
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
