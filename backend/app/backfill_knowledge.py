"""CLI for previewing and executing knowledge-asset backfills."""

import argparse
import asyncio
import json

from .database import init_db
from .knowledge import backfill_knowledge


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill SnapNote knowledge assets without AI calls")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="validate only; do not write knowledge tables")
    mode.add_argument("--execute", action="store_true", help="build candidate assets")
    parser.add_argument("--task-id", action="append", dest="task_ids", help="limit to one or more task IDs")
    parser.add_argument("--retry-failed", action="store_true", help="only retry failed or degraded assets")
    parser.add_argument("--force-rebuild", action="store_true", help="create a new version even when source is unchanged")
    return parser


async def _main() -> None:
    args = _parser().parse_args()
    await init_db()
    report = await backfill_knowledge(
        task_ids=args.task_ids,
        dry_run=args.dry_run or not args.execute,
        retry_failed=args.retry_failed,
        force=args.force_rebuild,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(_main())
