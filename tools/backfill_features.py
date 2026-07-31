"""One-time backfill of feature tokens for already-cached titles.

Features ride along on `tmdb.resolve_meta` from now on, so this only exists to
catch up the titles cached before that was true. It drains to nothing and stays
there; running it again is harmless and cheap.

    docker exec -i stream-picker python -m tools.backfill_features [--limit N]

Deliberately not wired into startup. It is a bulk external fetch, and a
recommendation build must never wait on one.
"""

import argparse
import asyncio
import logging
import time

import httpx

from app.recs import db, features, tmdb

logger = logging.getLogger("nuvio-recs")

# TMDB tolerates far more, but there is nothing to be gained by racing: this
# runs once and shares a process with live catalog serving.
CONCURRENCY = 8
PROGRESS_EVERY = 250


async def _one(row: dict, semaphore: asyncio.Semaphore) -> bool:
    media_type = row["media_type"]
    append = ("external_ids,release_dates,keywords,credits"
              if media_type == "movie"
              else "external_ids,content_ratings,keywords,aggregate_credits")
    async with semaphore:
        try:
            detail = await tmdb._get(f"/{media_type}/{row['tmdb_id']}",
                                     {"append_to_response": append})
        except (httpx.HTTPError, ValueError):
            return False
    tokens = features.extract(detail, media_type)
    # Stored even when empty: a title with no keywords must not come back on
    # the worklist for ever.
    await db.cache_put_features(row["tmdb_id"], media_type,
                                row["imdb_id"], tokens)
    return True


async def run(limit: int) -> None:
    await db.init()
    before = await db.feature_coverage()
    pending = await db.titles_missing_features(limit)
    print(f"features {before['features']}/{before['metas']} cached; "
          f"{len(pending)} to fetch")
    if not pending:
        return
    semaphore = asyncio.Semaphore(CONCURRENCY)
    started = time.monotonic()
    done = failed = 0
    for index in range(0, len(pending), PROGRESS_EVERY):
        batch = pending[index:index + PROGRESS_EVERY]
        results = await asyncio.gather(
            *[_one(row, semaphore) for row in batch], return_exceptions=True)
        done += sum(1 for r in results if r is True)
        failed += sum(1 for r in results if r is not True)
        rate = done / max(0.001, time.monotonic() - started)
        remaining = (len(pending) - index - len(batch)) / max(rate, 0.001)
        print(f"  {done}/{len(pending)} stored, {failed} skipped, "
              f"{rate:.1f}/s, ~{remaining/60:.0f} min left", flush=True)
    after = await db.feature_coverage()
    print(f"done: features {after['features']}/{after['metas']} "
          f"in {(time.monotonic()-started)/60:.1f} min")
    await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run(args.limit))


if __name__ == "__main__":
    main()
