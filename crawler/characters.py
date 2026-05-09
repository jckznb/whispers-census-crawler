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


def resolve_characters(
    chars: list[dict],
    force: bool = False,
) -> tuple[dict[tuple, int], set[tuple]]:
    """
    Given a list of {name, realm_slug, region} dicts, return:
      - id_map:     (name, realm_slug, region) -> character DB id
      - fresh_keys: subset of id_map keys that were actually fetched from
                    the Blizzard API this run (not returned from 24h cache)

    Characters updated within STALENESS_HOURS are returned from the DB cache.
    Others are fetched from the Blizzard API concurrently and upserted.

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

    fetched_rows = asyncio.run(_fetch_profiles_async(to_fetch))
    logger.info(f'Fetched {len(fetched_rows)} profiles from API')

    fresh_keys: set[tuple] = set()
    if fetched_rows:
        db.upsert('characters', fetched_rows, on_conflict='name,realm_slug,region')
        # Reload only the characters we just fetched — never scan the whole table.
        by_region_fetch: dict[str, list[dict]] = {}
        for c in to_fetch:
            by_region_fetch.setdefault(c['region'], []).append(c)

        refreshed_rows: list[dict] = []
        for region, region_chars in by_region_fetch.items():
            unique_names = list({c['name'] for c in region_chars})
            for i in range(0, len(unique_names), _NAME_BATCH):
                batch = unique_names[i:i + _NAME_BATCH]
                names_csv = ','.join(batch)
                rows = db.query('characters', {
                    'select': 'id,name,realm_slug,region',
                    'name':   f'in.({names_csv})',
                    'region': f'eq.{region}',
                })
                refreshed_rows.extend(rows)

        refreshed_map: dict[tuple, int] = {
            (r['name'], r['realm_slug'], r['region']): r['id']
            for r in refreshed_rows
        }
        for char in to_fetch:
            key = (char['name'], char['realm_slug'], char['region'])
            if key in refreshed_map:
                id_map[key] = refreshed_map[key]
                fresh_keys.add(key)

    return id_map, fresh_keys


async def _fetch_profiles_async(chars: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=_CONCURRENCY)) as client:
        tasks = [_fetch_one(client, sem, c['name'], c['realm_slug'], c['region']) for c in chars]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = sum(1 for r in results if isinstance(r, Exception))
    if errors:
        logger.warning(f'{errors} character profiles failed after all retries — skipping')
    return [r for r in results if r is not None and not isinstance(r, Exception)]


async def _fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
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
