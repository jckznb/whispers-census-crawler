"""
Talent build crawler module.

Fetches /specializations endpoint for leaderboard characters that were
freshly profile-fetched AND whose build data is stale.

Stores the active talent loadout code (WoW's base64 export string) in
character_builds. The same 7-day staleness window as professions is used
so both can be refreshed together in a single crawl pass.

Build data lets us compute "most popular build per spec" for PvP/M+ contexts.
Decoding the talent string into individual talent choices is a frontend concern
(requires WoW game data files).
"""
import asyncio
import logging
from datetime import date, timedelta
import httpx
from . import client as api
from . import db
from .config import PROFESSION_STALENESS_DAYS  # reuse same staleness window

logger = logging.getLogger(__name__)

_CONCURRENCY = 20  # conservative to share bandwidth with profession fetches


def _filter_to_stale_builds(
    candidate_ids: set[int],
    snapshot_date: date,
) -> set[int]:
    """Return character IDs whose build data is missing or older than staleness window."""
    cutoff = (snapshot_date - timedelta(days=PROFESSION_STALENESS_DAYS)).isoformat()

    all_recent: set[int] = set()
    ids_list = list(candidate_ids)
    batch_size = 500

    for i in range(0, len(ids_list), batch_size):
        chunk = ids_list[i:i + batch_size]
        ids_csv = ','.join(str(cid) for cid in chunk)
        rows = db.query('character_builds', {
            'select':        'character_id',
            'character_id':  f'in.({ids_csv})',
            'snapshot_date': f'gte.{cutoff}',
        })
        for r in rows:
            all_recent.add(r['character_id'])

    stale = candidate_ids - all_recent
    logger.info(
        f'Build staleness check: {len(candidate_ids)} candidates, '
        f'{len(all_recent)} already fresh (<{PROFESSION_STALENESS_DAYS}d), '
        f'{len(stale)} need fetch'
    )
    return stale


def resolve_builds(
    char_id_map: dict[tuple, int],
    fresh_keys: set[tuple],
    snapshot_date: date,
) -> None:
    """
    Fetch active talent loadout codes for characters that were freshly
    profile-fetched and whose build data is stale.

    Args:
        char_id_map:   (name, realm_slug, region) → character DB id
        fresh_keys:    subset of char_id_map keys fetched from Blizzard this run
        snapshot_date: crawl date to tag build rows
    """
    if not fresh_keys:
        logger.info('No fresh characters — skipping build fetch')
        return

    candidate_ids: set[int] = {
        char_id_map[k] for k in fresh_keys if k in char_id_map
    }
    if not candidate_ids:
        return

    stale_ids = _filter_to_stale_builds(candidate_ids, snapshot_date)
    if not stale_ids:
        logger.info('All fresh characters already have current build data — skipping')
        return

    id_to_key: dict[int, tuple] = {v: k for k, v in char_id_map.items() if v in stale_ids}
    chars_to_fetch = [
        {'name': k[0], 'realm_slug': k[1], 'region': k[2], 'character_id': cid}
        for cid, k in id_to_key.items()
    ]

    logger.info(f'Fetching builds for {len(chars_to_fetch)} characters...')

    from .auth import get_token
    for region in {c['region'] for c in chars_to_fetch}:
        get_token(region)

    results = asyncio.run(_fetch_builds_async(chars_to_fetch))
    logger.info(f'Fetched build data for {len(results)} characters')

    if not results:
        return

    rows = [
        {
            'character_id':  char_id,
            'spec_id':       spec_id,
            'loadout_code':  code,
            'snapshot_date': snapshot_date.isoformat(),
        }
        for char_id, spec_id, code in results
    ]

    db.upsert(
        'character_builds',
        rows,
        on_conflict='character_id,snapshot_date',
    )
    logger.info(f'Upserted {len(rows)} build rows for snapshot={snapshot_date}')


async def _fetch_builds_async(
    chars: list[dict],
) -> list[tuple[int, int | None, str]]:
    """Returns list of (character_id, spec_id, loadout_code)."""
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
        logger.warning(f'{errors} build fetches failed — skipping those characters')

    return [r for r in results if r is not None and not isinstance(r, Exception)]


async def _fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    character_id: int,
    name: str,
    realm_slug: str,
    region: str,
) -> tuple[int, int | None, str] | None:
    async with sem:
        data = await api.async_get(
            client,
            f'/profile/wow/character/{realm_slug}/{name.lower()}/specializations',
            region=region,
            namespace=f'profile-{region}',
        )

    if not data:
        return None

    return _parse_specializations(character_id, data)


def _parse_specializations(
    character_id: int,
    data: dict,
) -> tuple[int, int | None, str] | None:
    """
    Extract the active spec's active loadout code from the specializations response.
    Returns (character_id, spec_id, loadout_code) or None if no active loadout found.
    """
    for spec_entry in data.get('specializations') or []:
        spec_id = spec_entry.get('specialization', {}).get('id')

        for loadout in spec_entry.get('loadouts') or []:
            if not loadout.get('is_active'):
                continue
            code = loadout.get('talent_loadout_code')
            if code:
                return (character_id, spec_id, code)

    return None
