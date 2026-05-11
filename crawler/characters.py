"""Character profile resolution with DB deduplication and staleness checks."""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import httpx
from . import client as api
from . import db
from .config import STALENESS_HOURS

logger = logging.getLogger(__name__)

_CONCURRENCY = 50  # concurrent Blizzard API requests

# Max character names per PostgREST IN() query — WoW names ≤12 chars,
# so 300 names ≈ 3600 chars in the URL param, well within limits.
_NAME_BATCH = 300

# After each chunk of profiles is fetched and upserted, the DB has a durable
# checkpoint. If a crawl job times out mid-run, the next run finds those
# characters fresh (< STALENESS_HOURS) and skips them automatically.
_CHUNK_SIZE = 5_000


def upsert_char_stubs(chars: list[dict]) -> dict[tuple, int]:
    """
    Insert character stubs (name, realm_slug, region) without overwriting
    existing profile data. Used by leaderboard-only crawl mode to register
    new characters before storing run/entry data that references their IDs.

    Uses ignore-duplicates (DO NOTHING on conflict) so existing characters
    with full profile data are never touched.

    Returns (name, realm_slug, region) → character DB id.
    """
    if not chars:
        return {}

    now = datetime.now(timezone.utc).isoformat()
    stub_rows = [
        {
            'name':        c['name'],
            'realm_slug':  c['realm_slug'],
            'region':      c['region'],
            'first_seen':  now,
            # last_api_update intentionally omitted → stays NULL,
            # which signals "never enriched" to resolve_characters.
        }
        for c in chars
    ]

    # ignore-duplicates: existing characters are untouched (profile data preserved)
    db.upsert(
        'characters',
        stub_rows,
        on_conflict='name,realm_slug,region',
        conflict_resolution='ignore-duplicates',
    )
    logger.info(f'Upserted {len(stub_rows)} character stubs (ignore-duplicates)')

    # Fetch IDs back in name-batched queries (same approach as resolve_characters)
    by_region: dict[str, list[dict]] = {}
    for c in chars:
        by_region.setdefault(c['region'], []).append(c)

    id_map: dict[tuple, int] = {}
    for region, region_chars in by_region.items():
        unique_names = list({c['name'] for c in region_chars})
        for i in range(0, len(unique_names), _NAME_BATCH):
            batch = unique_names[i:i + _NAME_BATCH]
            names_csv = ','.join(batch)
            rows = db.query('characters', {
                'select': 'id,name,realm_slug,region',
                'name':   f'in.({names_csv})',
                'region': f'eq.{region}',
            })
            for r in rows:
                key = (r['name'], r['realm_slug'], r['region'])
                id_map[key] = r['id']

    logger.info(f'Resolved {len(id_map)}/{len(chars)} character IDs from stubs')
    return id_map


def resolve_characters(
    chars: list[dict],
    force: bool = False,
) -> tuple[dict[tuple, int], set[tuple]]:
    """
    Given a list of {name, realm_slug, region} dicts, return:
      - id_map:     (name, realm_slug, region) -> character DB id
      - fresh_keys: subset of id_map keys that were actually fetched from
                    the Blizzard API this run (not returned from staleness cache)

    Characters updated within STALENESS_HOURS are returned from the DB cache.
    Others are fetched from the Blizzard API concurrently and upserted in
    chunks of _CHUNK_SIZE — each chunk is committed to the DB immediately so
    a timeout mid-run still saves partial progress for the next attempt.

    force=True bypasses the staleness check and re-fetches all characters.
    Used by census spec resolution to profile census-only characters that have
    never had a Blizzard profile fetch (active_spec_id = NULL).
    """
    if not chars:
        return {}, set()

    stale_threshold = datetime.now(timezone.utc) - timedelta(hours=STALENESS_HOURS)

    # Load existing characters in targeted name-batched queries instead of
    # scanning the entire table — critical for scale (the table has 800k+ rows).
    by_region: dict[str, list[dict]] = {}
    for c in chars:
        by_region.setdefault(c['region'], []).append(c)

    existing_rows: list[dict] = []
    for region, region_chars in by_region.items():
        unique_names = list({c['name'] for c in region_chars})
        for i in range(0, len(unique_names), _NAME_BATCH):
            batch = unique_names[i:i + _NAME_BATCH]
            names_csv = ','.join(batch)
            rows = db.query('characters', {
                'select': 'id,name,realm_slug,region,last_api_update',
                'name':   f'in.({names_csv})',
                'region': f'eq.{region}',
            })
            existing_rows.extend(rows)

    existing: dict[tuple, dict] = {
        (r['name'], r['realm_slug'], r['region']): r
        for r in existing_rows
    }

    id_map: dict[tuple, int] = {}
    to_fetch: list[dict] = []

    for char in chars:
        key = (char['name'], char['realm_slug'], char['region'])
        row = existing.get(key)
        if not force and row and row.get('last_api_update'):
            last_update = datetime.fromisoformat(row['last_api_update'].replace('Z', '+00:00'))
            if last_update > stale_threshold:
                id_map[key] = row['id']
                continue
        to_fetch.append(char)

    logger.info(f'{len(id_map)} characters fresh in DB, need to fetch {len(to_fetch)} profiles')

    if not to_fetch:
        return id_map, set()

    # Pre-warm the token cache synchronously so async tasks hit the cache
    from .auth import get_token
    for region in {c['region'] for c in to_fetch}:
        get_token(region)

    fresh_keys: set[tuple] = set()
    total_chunks = (len(to_fetch) + _CHUNK_SIZE - 1) // _CHUNK_SIZE

    for chunk_idx in range(total_chunks):
        chunk_start = chunk_idx * _CHUNK_SIZE
        chunk = to_fetch[chunk_start:chunk_start + _CHUNK_SIZE]
        logger.info(
            f'Profile fetch: chunk {chunk_idx + 1}/{total_chunks} '
            f'({len(chunk)} chars, {chunk_start + len(chunk)}/{len(to_fetch)} total)'
        )

        fetched_rows = asyncio.run(_fetch_profiles_async(chunk))
        logger.info(f'Fetched {len(fetched_rows)} profiles in chunk {chunk_idx + 1}')

        if not fetched_rows:
            continue

        # Upsert this chunk immediately — durable checkpoint even if we time out later
        db.upsert('characters', fetched_rows, on_conflict='name,realm_slug,region')

        # Reload IDs for only this chunk
        by_region_chunk: dict[str, list[dict]] = {}
        for c in chunk:
            by_region_chunk.setdefault(c['region'], []).append(c)

        for region, region_chars in by_region_chunk.items():
            unique_names = list({c['name'] for c in region_chars})
            for i in range(0, len(unique_names), _NAME_BATCH):
                batch = unique_names[i:i + _NAME_BATCH]
                names_csv = ','.join(batch)
                rows = db.query('characters', {
                    'select': 'id,name,realm_slug,region',
                    'name':   f'in.({names_csv})',
                    'region': f'eq.{region}',
                })
                for r in rows:
                    key = (r['name'], r['realm_slug'], r['region'])
                    id_map[key] = r['id']
                    fresh_keys.add(key)

    return id_map, fresh_keys


async def _fetch_profiles_async(chars: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(_CONCURRENCY)
    limiter = api.AsyncRateLimiter()
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=_CONCURRENCY)) as client:
        tasks = [_fetch_one(client, sem, limiter, c['name'], c['realm_slug'], c['region']) for c in chars]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = sum(1 for r in results if isinstance(r, Exception))
    if errors:
        logger.warning(f'{errors} character profiles failed after all retries — skipping')
    return [r for r in results if r is not None and not isinstance(r, Exception)]


async def _fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    limiter: api.AsyncRateLimiter,
    name: str,
    realm_slug: str,
    region: str,
) -> dict | None:
    async with sem:
        data = await api.async_get(
            client,
            f'/profile/wow/character/{realm_slug}/{name.lower()}',
            region=region,
            namespace=f'profile-{region}',
            limiter=limiter,
        )
    if not data:
        return None

    now = datetime.now(timezone.utc).isoformat()
    gender_type = data.get('gender', {}).get('type', 'MALE')

    guild = data.get('guild') or {}
    guild_name      = guild.get('name')
    guild_realm_slug = guild.get('realm', {}).get('slug') if guild else None

    return {
        'name':               data.get('name', name),
        'realm_slug':         realm_slug,
        'region':             region,
        'race_id':            data.get('race', {}).get('id'),
        'class_id':           data.get('character_class', {}).get('id'),
        'active_spec_id':     data.get('active_spec', {}).get('id'),
        'gender':             1 if gender_type == 'FEMALE' else 0,
        'level':              data.get('level'),
        'faction':            data.get('faction', {}).get('type', '').lower(),
        'equipped_item_level': data.get('equipped_item_level'),
        'guild_name':         guild_name,
        'guild_realm_slug':   guild_realm_slug,
        'last_api_update':    now,
        'first_seen':         now,
    }
