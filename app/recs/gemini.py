"""Optional Gemini-generated themed catalogs. Failure-tolerant: any error
returns [] and the caller fills the slots with non-LLM rows instead."""

import json
import logging

import httpx

from app.recs import config

logger = logging.getLogger("nuvio-recs")

PROMPT = """You are a film/TV recommendation curator. Below is a sample of what \
one viewer has watched recently (with their 1-10 ratings where given).

Create exactly 2 themed recommendation rows for this viewer. Each row needs a \
short, catchy, specific title (max 40 chars, no emoji) that references their \
taste (e.g. "Slow-burn thrillers for late nights"), and 20 items they have NOT \
watched from the list below. Mix well-known and lesser-known picks. Use real, \
existing movies and series only.

Respond with ONLY valid JSON, no markdown fences:
{"rows": [{"title": "...", "items": [{"name": "...", "year": 2020, "type": "movie"}]}]}
"type" must be "movie" or "series".

Viewer history:
"""


async def themed_rows(history_sample: list[dict], kid_age: int | None = None) -> list[dict]:
    """history_sample: [{title, year, type, rating?}]. Returns parsed rows or []."""
    if not config.GEMINI_ENABLED:
        return []
    lines = []
    for h in history_sample[:60]:
        rating = f" (rated {h['rating']}/10)" if h.get("rating") else ""
        lines.append(f"- {h['title']} ({h.get('year', '?')}, {h['type']}){rating}")
    prompt = PROMPT
    if kid_age is not None:
        prompt += (f"\nIMPORTANT: This viewer is a CHILD aged {kid_age}. Every pick "
                   f"must be age-appropriate for a {kid_age}-year-old (think G/PG "
                   "or TV-Y/TV-G/TV-PG level). No horror, no mature themes.\n")
    body = {
        "contents": [{"parts": [{"text": prompt + "\n".join(lines)}]}],
        "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"},
    }
    # Fall through cheaper models on quota/availability errors (free-tier
    # quotas are per-model, and the key is shared with other tools).
    models = [config.GEMINI_MODEL, "gemini-flash-latest", "gemini-2.0-flash",
              "gemini-flash-lite-latest"]
    rows = None
    for model in dict.fromkeys(models):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent",
                    params={"key": config.GEMINI_API_KEY},
                    json=body,
                )
                r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            rows = json.loads(text).get("rows", [])
            break
        except httpx.HTTPStatusError as e:
            logger.warning(f"gemini {model}: HTTP {e.response.status_code}")
            if e.response.status_code not in (404, 429, 500, 503):
                return []
        except Exception as e:
            logger.warning(f"gemini {model} failed: {e!r}")
            return []
    if rows is None:
        logger.warning("gemini: all models exhausted, skipping AI rows")
        return []
    out = []
    for row in rows[:2]:
        items = [i for i in row.get("items", [])
                 if i.get("name") and i.get("type") in ("movie", "series")]
        if row.get("title") and len(items) >= 10:
            out.append({"title": row["title"][:60], "items": items})
    logger.info(f"gemini produced {len(out)} themed rows")
    return out
