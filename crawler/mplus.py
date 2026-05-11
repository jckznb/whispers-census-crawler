"""
Mythic+ spec distribution crawler.

Fetches all dungeon leaderboards for the current M+ period.
spec_id is embedded directly in each leaderboard member entry — no character
profile calls needed. Deduplicates members across all realms and dungeons,
then aggregates spec counts and writes mplus_latest.json to Vercel Blob.

Output blob shape:
{
  "updated":  "2026-05-11",
  "season":   41,
  "period":   1057,
  "total":    261843,
  "by_spec":  { "65": 18400, ... }   # spec_id → unique character count
}
"""
import asyncio
import logging
from datetime import date
import httpx
from . import client as api
from . import blob as blob_store

logger = logging.getLogger(__name__)

_CONCURRENCY = 40


# ---------------------------------------------------------------------------
# Connected realm discovery
# ---------------------------------------------------------------------------

def _get_connected_realm_ids(region: str) -> list[int]:
    data = api.get('/data/wow/connected-realm/index', region=region, namespace=f'dynamic-{region}')
    if not data:
        raise RuntimeError(f'Could not fetch connected realm index for region={region}')
    ids = []
    for entry in data.get('connected_realms', []):
        href = entry.get('href', '')
        try:
            ids.append(int(href.split('/connected-realm/')[1].split('?')[0]))
        except (IndexError, ValueError):
            pass
    logger.info(f'Found {len(ids)} connected realms')
    return ids


# ---------------------------------------------------------------------------
# Async leaderboard index (one per connected realm)
# ---------------------------------------------------------------------------

async def _fetch_realm_index(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    limiter: api.AsyncRateLimiter,
    realm_id: int,
    region: str,
) -> tuple | None:
    """Returns (realm_id, dungeons, period_id) or None."""
    path = f'/data/wow/connected-realm/{realm_id}/mythic-leaderboard/index'
    async with sem:
        data = await api.async_get(client, path, region=region, namespace=f'dynamic-{region}', limiter=limiter)
    if not data:
        return None

    dungeons = []
    period_id = 0
    for lb in data.get('current_leaderboards', []):
        dungeon_id = lb.get('id')
        if not dungeon_id:
            continue
        if not period_id:
            href = lb.get('key', {}).get('href', '')
            try:
                period_id = int(href.split('/period/')[1].split('?')[0])
            except (IndexError, ValueError):
                pass
        dungeons.append({'id': dungeon_id, 'name': lb.get('name', '')})

    return (realm_id, dungeons, period_id) if dungeons else None


async def _fetch_all_realm_indexes(realm_ids: list[int], region: str) -> list[tuple]:
    sem = asyncio.Semaphore(_CONCURRENCY)
    limiter = api.AsyncRateLimiter()
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=_CONCURRENCY)) as client:
        tasks = [_fetch_realm_index(client, sem, limiter, rid, region) for rid in realm_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    valid = [r for r in results if isinstance(r, tuple)]
    errors = sum(1 for r in results if isinstance(r, Exception))
    if errors:
        logger.warning(f'{errors} realm index fetches failed')
    logger.info(f'Got indexes for {len(valid)}/{len(realm_ids)} realms')
    return valid


# ---------------------------------------------------------------------------
# Async dungeon leaderboard fetch
# ---------------------------------------------------------------------------

async def _fetch_leaderboard(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    limiter: api.AsyncRateLimiter,
    realm_id: int,
    dungeon_id: int,
    period_id: int,
    region: str,
) -> list[tuple[str, str, int | None]]:
    """
    Returns list of (name, realm_slug, spec_id) for every member of every
    leading group on this leaderboard. spec_id may be None if missing.
    """
    path = (
        f'/data/wow/connected-realm/{realm_id}'
        f'/mythic-leaderboard/{dungeon_id}/period/{period_id}'
    )
    async with sem:
        data = await api.async_get(client, path, region=region, namespace=f'dynamic-{region}', limiter=limiter)
    if not data:
        return []

    members = []
    for group in data.get('leading_groups', []):
        for m in group.get('members', []):
            profile = m.get('profile', {})
            name = profile.get('name')
            realm_slug = profile.get('realm', {}).get('slug')
            if not name or not realm_slug:
                continue
            spec_id = m.get('specialization', {}).get('id')
            members.append((name, realm_slug, spec_id))
    return members


async def _fetch_all_leaderboards(
    tasks_info: list[tuple],
    region: str,
) -> list[tuple[str, str, int | None]]:
    sem = asyncio.Semaphore(_CONCURRENCY)
    limiter = api.AsyncRateLimiter()
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=_CONCURRENCY)) as client:
        tasks = [
            _fetch_leaderboard(client, sem, limiter, realm_id, dungeon_id, period_id, region)
            for realm_id, dungeon_id, period_id in tasks_info
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = sum(1 for r in results if isinstance(r, Exception))
    if errors:
        logger.warning(f'{errors} leaderboard fetches failed')

    all_members = []
    for r in results:
        if isinstance(r, list):
            all_members.extend(r)
    return all_members


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def crawl_mplus(region: str = 'us', snapshot_date: date = None) -> None:
    """
    Fetch all M+ dungeon leaderboards, aggregate spec counts from the embedded
    spec_id field (no profile API calls), and write to Vercel Blob.
    """
    if snapshot_date is None:
        snapshot_date = date.today()

    logger.info(f'Starting M+ crawl: region={region} date={snapshot_date}')

    from .auth import get_token
    get_token(region)

    realm_ids = _get_connected_realm_ids(region)

    logger.info(f'Fetching leaderboard indexes for {len(realm_ids)} realms...')
    realm_indexes = asyncio.run(_fetch_all_realm_indexes(realm_ids, region))

    if not realm_indexes:
        logger.error('No realm indexes returned — aborting')
        return

    # Derive season from the period embedded in the first realm index
    # (Blizzard doesn't expose a clean current-season endpoint for M+)
    period_id = realm_indexes[0][2]

    tasks_info = [
        (realm_id, dungeon['id'], period_id)
        for realm_id, dungeons, period_id in realm_indexes
        for dungeon in dungeons
    ]
    logger.info(f'Fetching {len(tasks_info)} dungeon leaderboards...')

    all_members = asyncio.run(_fetch_all_leaderboards(tasks_info, region))
    logger.info(f'Collected {len(all_members)} raw member entries')

    if not all_members:
        logger.warning('No member data returned — aborting')
        return

    # Deduplicate by (name, realm_slug) — a character appearing in multiple
    # dungeons or on multiple realm shards counts only once.
    # Keep the first spec_id seen (consistent within a season).
    seen: dict[tuple, int | None] = {}
    for name, realm_slug, spec_id in all_members:
        key = (name, realm_slug)
        if key not in seen:
            seen[key] = spec_id

    total = len(seen)
    logger.info(f'{total} unique characters after dedup')

    # Aggregate spec counts
    by_spec: dict[str, int] = {}
    for spec_id in seen.values():
        if spec_id is not None:
            key = str(spec_id)
            by_spec[key] = by_spec.get(key, 0) + 1

    payload = {
        'updated': snapshot_date.isoformat(),
        'period':  period_id,
        'total':   total,
        'by_spec': by_spec,
    }

    logger.info(f'Writing mplus blob: {total:,} unique chars, {len(by_spec)} specs')
    blob_store.write('mplus', payload, snapshot_date)
    logger.info('M+ crawl complete')
