"""Run the reviewed AI-search cases against the configured local model and TMDB.

Example: python -m tools.eval_ai_search --runs 3 --output /tmp/ai-search-report.json
Only reviewed queries, public title metadata, and aggregate timings are saved.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time

from app.recs import ai_search, tmdb, db
from app.recs.search_intent import interpret

DEFAULT_CASES = Path(__file__).resolve().parents[1] / 'tests/recs/fixtures/ai_search_cases.json'


async def _imdb_map(cases: list[dict]) -> dict[str, str]:
    refs = {ref for case in cases for ref in [*case.get('acceptable', {}), *case.get('prohibited', [])]}
    mapping: dict[str, str] = {}
    for ref in sorted(refs):
        media, tid = ref.split(':')
        try:
            detail = await tmdb.candidate_details(media, int(tid))
            imdb = detail.get('imdb_id') or (detail.get('external_ids') or {}).get('imdb_id')
            if not imdb and media == 'tv':
                detail = await tmdb._get(f'/tv/{tid}/external_ids')
                imdb = detail.get('imdb_id')
            if imdb:
                mapping[ref] = imdb
        except Exception:
            continue
    return mapping


def _violations(case: dict, metas: list[dict]) -> list[str]:
    intent = interpret(case['query'])
    errors = []
    for meta in metas:
        name = meta.get('name', '')
        if intent.media_explicit and meta.get('type') and (('movie' if meta['type'] == 'movie' else 'tv') not in intent.media):
            errors.append(f'{name}: media')
        if intent.date and intent.date.required:
            value = str(meta.get('releaseInfo') or '')[:4]
            if not value.isdigit() or not (intent.date.start.year <= int(value) <= intent.date.end.year):
                errors.append(f'{name}: date')
        for excluded in intent.excluded_topics:
            if excluded.rstrip('s') in (name + ' ' + meta.get('description', '')).lower():
                errors.append(f'{name}: exclusion')
    return errors


async def evaluate(cases: list[dict], runs: int) -> dict:
    mapping = await _imdb_map(cases)
    by_case = []
    latencies = []
    graded_hits = graded_total = total_top5 = 0
    all_violations = []
    classic_counts = []
    for case in cases:
        samples = []
        for _ in range(runs):
            ai_search.clear_cache()
            start = time.monotonic()
            metas = await ai_search.search('evaluation', case['query'])
            elapsed = round(time.monotonic() - start, 3)
            latencies.append(elapsed)
            acceptable = {mapping[ref]: grade for ref, grade in case.get('acceptable', {}).items() if ref in mapping}
            prohibited = {mapping[ref] for ref in case.get('prohibited', []) if ref in mapping}
            grades = {**case.get('reviewed_imdb', {}), **acceptable}
            grades.update({imdb: 0 for imdb in prohibited})
            hits = [m.get('id') for m in metas[:5] if grades.get(m.get('id'), -1) >= 2]
            judged = [m for m in metas[:5] if m.get('id') in grades]
            total_top5 += len(metas[:5])
            graded_hits += len(hits)
            graded_total += len(judged)
            if case['query'] == 'scary movies from the 80s':
                classic_counts.append(sum(m.get('id') in acceptable for m in metas[:16]))
            violations = _violations(case, metas)
            all_violations.extend(f"{case['query']}: {v}" for v in violations)
            samples.append({'elapsed_s': elapsed, 'count': len(metas), 'top5': [{'id': m.get('id'), 'name': m.get('name'), 'year': m.get('releaseInfo')} for m in metas[:5]], 'reviewed_hits': len(hits), 'violations': violations})
        by_case.append({'query': case['query'], 'samples': samples})
    ordered = sorted(latencies)
    p95 = ordered[max(0, int(len(ordered) * .95 + .999) - 1)] if ordered else 0
    return {'runs_per_case': runs, 'cases': len(cases), 'model': ai_search.active_model(), 'latency_p95_s': p95, 'latency_median_s': statistics.median(latencies) if latencies else 0, 'reviewed_precision_at_5': round(graded_hits / graded_total, 3) if graded_total else None, 'graded_top5_count': graded_total, 'top5_count': total_top5, 'judgment_coverage': round(graded_total / total_top5, 3) if total_top5 else None, 'reviewed_classics_first16': classic_counts, 'violations': all_violations, 'results': by_case}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', type=Path, default=DEFAULT_CASES)
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())['cases']
    async def run():
        await db.init()
        try:
            return await evaluate(cases, max(1, args.runs))
        finally:
            await db.close()
    report = asyncio.run(run())
    data = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(data + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'results'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
