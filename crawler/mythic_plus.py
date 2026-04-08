"""Phase 2: Mythic+ leaderboard crawler."""
import asyncio
import logging
from datetime import date
import httpx
from . import client as api
from . import db

logger = logging.getLogger(__name__)

# Concurrent leaderboard fetches — higher than character profiles since these
# are game-data endpoints (not profile), same 100 req/s limit applies.
_CONCURRENCY = 40


# ---------------------------------------------------------------------------
# Connected realm discovery
# ---------------------------------------------------------------------------

def get_connected_realm_ids(region: str = 'us') -> list[int]:
    """Return all connected realm IDs for the region."""
    data = api.get(
        '/data/wow/connected-realm/index',
        region=region,
        namespace=f'dynamic-{region}',
    )
    if not data:
        return []
    ids = []
    for entry in data.get('connected_realms', []):
        href = entry.get('href', '')
        try:
            realm_id = int(href.split('/connected-realm/')[1].split('?')[0])
            ids.append(realm_id)
        except (IndexError, ValueError):
            pass
    logger.info(f'Found {len(ids)} connected realms for region={region}')
    return ids


# ---------------------------------------------------------------------------
# Async leaderboard index fetch (one per connected realm)
# ---------------------------------------------------------------------------

async def _fetch_realm_index(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    realm_id: int,
    region: str,
) -> tuple | None:
    """
    Returns (realm_id, dungeons, period_id) or None on failure.
    dungeons: list of {id, name}
    """
    path = f'/data/wow/connected-realm/{realm_id}/mythic-leaderboard/index'
    async with sem:
        data = await api.async_get(client, path, region=region, namespace=f'dynamic-{region}')
    if not data:
        return None

    # Period ID lives in each leaderboard's key.href, e.g.:
    # ".../mythic-leaderboard/161/period/1057?namespace=..."
    # Extract from the first entry; all entries share the same period.
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

    if not dungeons:
        return None

    return (realm_id, dungeons, period_id)


async def _fetch_all_realm_indexes(realm_ids: list[int], region: str) -> list[tuple]:
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=_CONCURRENCY)) as client:
        tasks = [_fetch_realm_index(client, sem, rid, region) for rid in realm_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    valid = [r for r in results if isinstance(r, tuple)]
    errors = sum(1 for r in results if isinstance(r, Exception))
    if errors:
        logger.warning(f'{errors} realm index fetches failed')
    logger.info(f'Got leaderboard indexes for {len(valid)}/{len(realm_ids)} realms')
    return valid


# ---------------------------------------------------------------------------
# Async dungeon leaderboard fetch
# ---------------------------------------------------------------------------

async def _fetch_leaderboard(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    realm_id: int,
    dungeon_id: int,
    dungeon_name: str,
    period_id: int,
    region: str,
    snapshot_date: date,
) -> list[dict]:
    """Returns a list of run dicts (each with a 'members' list) from one leaderboard."""
    path = (
        f'/data/wow/connected-realm/{realm_id}'
        f'/mythic-leaderboard/{dungeon_id}/period/{period_id}'
    )
    async with sem:
        data = await api.async_get(client, path, region=region, namespace=f'dynamic-{region}')
    if not data:
        return []

    runs = []
    for group in data.get('leading_groups', []):
        members = []
        for m in group.get('members', []):
            # M+ leaderboard uses 'profile' (not 'character'); no race field present
            profile = m.get('profile', {})
            name = profile.get('name')
            realm_slug = profile.get('realm', {}).get('slug')
            if not name or not realm_slug:
                continue
            members.append({
                'name': name,
                'realm_slug': realm_slug,
                'region': region,
                'spec_id': m.get('specialization', {}).get('id'),
            })

        if not members:
            continue

        runs.append({
            'dungeon_id': dungeon_id,
            'dungeon_name': dungeon_name,
            'keystone_level': group.get('keystone_level', 0),
            'completed_timestamp': group.get('completed_timestamp'),
            'connected_realm_id': realm_id,
            'period': period_id,
            'snapshot_date': snapshot_date.isoformat(),
            'members': members,
        })

    return runs


async def _fetch_all_leaderboards(
    tasks_info: list[tuple],
    region: str,
    snapshot_date: date,
) -> list[dict]:
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=_CONCURRENCY)) as client:
        tasks = [
            _fetch_leaderboard(client, sem, realm_id, dungeon_id, dungeon_name, period_id, region, snapshot_date)
            for realm_id, dungeon_id, dungeon_name, period_id in tasks_info
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = sum(1 for r in results if isinstance(r, Exception))
    if errors:
        logger.warning(f'{errors} leaderboard fetches failed')

    all_runs = []
    for r in results:
        if isinstance(r, list):
            all_runs.extend(r)
    return all_runs


# ---------------------------------------------------------------------------
# DB storage
# ---------------------------------------------------------------------------

def _store_runs(
    all_runs: list[dict],
    char_id_map: dict[tuple, int],
    snapshot_date: date,
) -> None:
    """
    Insert runs and members for snapshot_date.
    Deletes any existing data for that date first so re-runs are idempotent.
    """
    # Clear existing data for this snapshot (members first due to FK)
    existing_runs = db.select(
        'mythic_plus_runs',
        filters={'snapshot_date': snapshot_date.isoformat()},
        columns='id',
    )
    if existing_runs:
        run_ids = [r['id'] for r in existing_runs]
        logger.info(f'Clearing {len(run_ids)} existing runs for snapshot_date={snapshot_date}')
        # Delete members in chunks — large IN lists exceed URL length limits
        chunk_size = 200
        for i in range(0, len(run_ids), chunk_size):
            db.delete('mythic_plus_members', {'run_id': run_ids[i:i + chunk_size]})
        db.delete('mythic_plus_runs', {'snapshot_date': snapshot_date.isoformat()})

    logger.info(f'Inserting {len(all_runs)} runs...')
    # Insert runs (without members) and get back generated IDs
    run_rows = [
        {
            'dungeon_id': r['dungeon_id'],
            'dungeon_name': r['dungeon_name'],
            'keystone_level': r['keystone_level'],
            'completed_timestamp': r['completed_timestamp'],
            'connected_realm_id': r['connected_realm_id'],
            'period': r['period'],
            'snapshot_date': r['snapshot_date'],
        }
        for r in all_runs
    ]
    inserted_runs = db.insert('mythic_plus_runs', run_rows)
    logger.info(f'Inserted {len(inserted_runs)} runs, building member rows...')

    # Build member rows using the run IDs returned from insert
    member_rows = []
    skipped = 0
    for run_db, run_src in zip(inserted_runs, all_runs):
        run_id = run_db['id']
        for m in run_src['members']:
            key = (m['name'], m['realm_slug'], m['region'])
            char_id = char_id_map.get(key)
            if char_id is None:
                skipped += 1
                continue
            spec_id = m.get('spec_id')
            member_rows.append({
                'run_id': run_id,
                'character_id': char_id,
                'spec_id': spec_id,
            })

    if skipped:
        logger.warning(f'Skipped {skipped} members with unresolved character IDs')

    logger.info(f'Inserting {len(member_rows)} member rows...')
    db.upsert('mythic_plus_members', member_rows)
    logger.info(f'Done inserting members')


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def crawl_mythic_plus(region: str = 'us', snapshot_date: date = None) -> None:
    if snapshot_date is None:
        snapshot_date = date.today()

    logger.info(f'Starting M+ crawl: region={region} snapshot_date={snapshot_date}')

    # 1. Connected realm IDs
    realm_ids = get_connected_realm_ids(region)
    if not realm_ids:
        logger.error('No connected realms found — aborting')
        return

    # Pre-warm auth token before entering async context
    from .auth import get_token
    get_token(region)

    # 2. Fetch leaderboard indexes for all realms
    logger.info(f'Fetching leaderboard indexes for {len(realm_ids)} realms...')
    realm_indexes = asyncio.run(_fetch_all_realm_indexes(realm_ids, region))

    # 3. Build task list: (realm_id, dungeon_id, dungeon_name, period_id)
    tasks_info = [
        (realm_id, dungeon['id'], dungeon['name'], period_id)
        for realm_id, dungeons, period_id in realm_indexes
        for dungeon in dungeons
    ]
    logger.info(f'Fetching {len(tasks_info)} dungeon leaderboards...')

    # 4. Fetch all dungeon leaderboards
    all_runs = asyncio.run(_fetch_all_leaderboards(tasks_info, region, snapshot_date))
    logger.info(f'Collected {len(all_runs)} runs across all realms/dungeons')

    if not all_runs:
        logger.warning('No runs fetched — aborting')
        return

    # 5. Collect unique characters with their leaderboard-provided race/spec
    all_chars: list[dict] = []
    seen_chars: set[tuple] = set()
    for run in all_runs:
        for m in run['members']:
            key = (m['name'], m['realm_slug'], m['region'])
            if key not in seen_chars:
                all_chars.append(m)
                seen_chars.add(key)

    logger.info(f'Resolving {len(all_chars)} unique characters (profile lookups for unknowns)...')
    from .characters import resolve_characters
    char_id_map = resolve_characters(all_chars)

    # 6. Persist
    logger.info('Storing runs and members...')
    _store_runs(all_runs, char_id_map, snapshot_date)

    logger.info('M+ crawl complete')
