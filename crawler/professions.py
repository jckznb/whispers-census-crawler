"""
Profession crawler module.

Fetches /professions endpoint for characters that were freshly pulled from
the Blizzard API during this crawl pass. Characters returned from the 24-hour
DB cache are skipped — their profession data is already current.

This is not a discovery mechanism; it piggybacks on characters already
found via PvP, M+, or other crawl phases.
"""
import asyncio
import logging
from datetime import date
import httpx
from . import client as api
from . import db

logger = logging.getLogger(__name__)

_CONCURRENCY = 50  # match character profile concurrency


def resolve_professions(
    char_id_map: dict[tuple, int],
    fresh_keys: set[tuple],
    snapshot_date: date,
) -> None:
    """
    Fetch profession data for characters that were freshly retrieved from
    the Blizzard API (i.e., keys in fresh_keys, a subset of char_id_map).

    Writes rows to character_professions.

    Args:
        char_id_map:   (name, realm_slug, region) → character DB id
        fresh_keys:    subset of char_id_map keys that were actually fetched
                       from Blizzard this run (not from 24h DB cache)
        snapshot_date: crawl date to tag profession rows
    """
    if not fresh_keys:
        logger.info('No fresh characters — skipping profession fetch')
        return

    # Build the list of chars to fetch professions for
    chars_to_fetch = [
        {'name': k[0], 'realm_slug': k[1], 'region': k[2], 'character_id': char_id_map[k]}
        for k in fresh_keys
        if k in char_id_map
    ]

    if not chars_to_fetch:
        return

    logger.info(f'Fetching professions for {len(chars_to_fetch)} fresh characters...')

    # Pre-warm auth token
    from .auth import get_token
    for region in {c['region'] for c in chars_to_fetch}:
        get_token(region)

    results = asyncio.run(_fetch_professions_async(chars_to_fetch))
    logger.info(f'Fetched profession data for {len(results)} characters')

    if not results:
        return

    # Flatten into upsert rows
    rows = []
    for char_id, professions in results:
        for prof in professions:
            rows.append({
                'character_id':       char_id,
                'profession_id':      prof['profession_id'],
                'is_primary':         prof['is_primary'],
                'current_tier_name':  prof.get('current_tier_name'),
                'current_tier_skill': prof.get('current_tier_skill'),
                'current_tier_max':   prof.get('current_tier_max'),
                'snapshot_date':      snapshot_date.isoformat(),
            })

    if rows:
        db.upsert(
            'character_professions',
            rows,
            on_conflict='character_id,profession_id,snapshot_date',
        )
        logger.info(f'Upserted {len(rows)} profession rows for snapshot={snapshot_date}')


async def _fetch_professions_async(
    chars: list[dict],
) -> list[tuple[int, list[dict]]]:
    """
    Concurrently fetch /professions for each character.
    Returns list of (character_id, parsed_professions) pairs.
    """
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=_CONCURRENCY)
    ) as client:
        tasks = [
            _fetch_one(client, sem, c['character_id'], c['name'], c['realm_slug'], c['region'])
            for c in chars
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = sum(1 for r in results if isinstance(r, Exception))
    if errors:
        logger.warning(f'{errors} profession fetches failed — skipping those characters')

    return [r for r in results if r is not None and not isinstance(r, Exception)]


async def _fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    character_id: int,
    name: str,
    realm_slug: str,
    region: str,
) -> tuple[int, list[dict]] | None:
    async with sem:
        data = await api.async_get(
            client,
            f'/profile/wow/character/{realm_slug}/{name.lower()}/professions',
            region=region,
            namespace=f'profile-{region}',
        )

    if not data:
        return None

    parsed = _parse_professions_response(data)
    if not parsed:
        return None

    return (character_id, parsed)


def _parse_professions_response(response_json: dict) -> list[dict]:
    """
    Extract primary and secondary professions from the API response.

    For each profession, captures the latest expansion tier (last in the
    tiers array — Blizzard orders them oldest → newest).

    Returns a list of dicts ready for DB upsert (without character_id or
    snapshot_date, those are added by the caller).
    """
    result = []

    for is_primary, section_key in [(True, 'primaries'), (False, 'secondaries')]:
        for prof_entry in response_json.get(section_key) or []:
            prof = prof_entry.get('profession', {})
            profession_id = prof.get('id')
            if not profession_id:
                continue

            # Latest expansion tier = last item in the tiers list
            tiers = prof_entry.get('tiers') or []
            current_tier_name  = None
            current_tier_skill = None
            current_tier_max   = None

            if tiers:
                latest = tiers[-1]
                tier_info = latest.get('skill_tier', {})
                current_tier_name  = tier_info.get('name')
                current_tier_skill = latest.get('skill_points')
                current_tier_max   = latest.get('max_skill_points')

            result.append({
                'profession_id':      profession_id,
                'is_primary':         is_primary,
                'current_tier_name':  current_tier_name,
                'current_tier_skill': current_tier_skill,
                'current_tier_max':   current_tier_max,
            })

    return result
