"""
Profession crawler module.

Fetches /professions endpoint for characters that were freshly pulled from
the Blizzard API during this crawl pass, but only if their profession data
is older than PROFESSION_STALENESS_DAYS. This prevents re-fetching ~150k
profession endpoints every week — after the first full pass, only genuinely
new or expired characters are re-fetched.

Character profile staleness (24h) and profession staleness (7d) are decoupled:
a character can be re-profiled weekly but only get a profession refresh once a week.
"""
import asyncio
import logging
from datetime import date, timedelta
import httpx
from . import client as api
from . import db
from .config import PROFESSION_STALENESS_DAYS

logger = logging.getLogger(__name__)

_CONCURRENCY = 20  # lower than profile concurrency to avoid 429s on profession endpoints


def _filter_to_stale_professions(
    candidate_ids: set[int],
    snapshot_date: date,
) -> set[int]:
    """
    Given a set of character IDs, return only those whose profession data
    is missing or older than PROFESSION_STALENESS_DAYS.

    Queries character_professions in batches (PostgREST `in.()` operator).
    """
    cutoff = (snapshot_date - timedelta(days=PROFESSION_STALENESS_DAYS)).isoformat()

    # Pull character_ids that have a recent profession snapshot
    # PostgREST `in.()` with large lists can be slow — batch if needed
    all_recent: set[int] = set()
    ids_list = list(candidate_ids)
    batch_size = 500  # keep URL length manageable

    for i in range(0, len(ids_list), batch_size):
        chunk = ids_list[i:i + batch_size]
        ids_csv = ','.join(str(cid) for cid in chunk)
        rows = db.query('character_professions', {
            'select':        'character_id',
            'character_id':  f'in.({ids_csv})',
            'snapshot_date': f'gte.{cutoff}',
        })
        for r in rows:
            all_recent.add(r['character_id'])

    stale = candidate_ids - all_recent
    logger.info(
        f'Profession staleness check: {len(candidate_ids)} candidates, '
        f'{len(all_recent)} already fresh (<{PROFESSION_STALENESS_DAYS}d), '
        f'{len(stale)} need fetch'
    )
    return stale


def resolve_professions(
    char_id_map: dict[tuple, int],
    fresh_keys: set[tuple],
    snapshot_date: date,
) -> None:
    """
    Fetch profession data for characters that were freshly profile-fetched
    AND whose profession data is stale (older than PROFESSION_STALENESS_DAYS).

    This decouples character-profile staleness (24h) from profession staleness
    (7d), so a weekly crawl doesn't re-fetch 150k profession endpoints every run.

    Args:
        char_id_map:   (name, realm_slug, region) → character DB id
        fresh_keys:    subset of char_id_map keys that were actually fetched
                       from Blizzard this run (not from 24h DB cache)
        snapshot_date: crawl date to tag profession rows
    """
    if not fresh_keys:
        logger.info('No fresh characters — skipping profession fetch')
        return

    # Map fresh keys → character IDs
    candidate_ids: set[int] = {
        char_id_map[k] for k in fresh_keys if k in char_id_map
    }

    if not candidate_ids:
        return

    # Filter to only characters whose profession data is actually stale
    stale_ids = _filter_to_stale_professions(candidate_ids, snapshot_date)

    if not stale_ids:
        logger.info('All fresh characters already have current profession data — skipping')
        return

    # Build the fetch list from the stale IDs
    id_to_key: dict[int, tuple] = {v: k for k, v in char_id_map.items() if v in stale_ids}
    chars_to_fetch = [
        {'name': k[0], 'realm_slug': k[1], 'region': k[2], 'character_id': cid}
        for cid, k in id_to_key.items()
    ]

    logger.info(f'Fetching professions for {len(chars_to_fetch)} characters...')

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
