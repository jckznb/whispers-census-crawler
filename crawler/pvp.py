"""
PvP spec/race/class distribution crawler.

Fetches all 85 bracket leaderboards for the current PvP season, deduplicates
characters across brackets, then fetches a character profile for each unique
character to get race_id, class_id, and active_spec_id.

Aggregates counts and writes pvp_latest.json to Vercel Blob.

Output blob shape:
{
  "updated":      "2026-05-11",
  "season":       41,
  "total":        78432,
  "by_spec":      { "65": 4821, ... },   # spec_id  → count
  "by_class":     { "2":  14200, ... },  # class_id → count
  "by_race":      { "10": 18400, ... },  # race_id  → count
  "by_race_spec": { "10_65": 1200, ... },
  "by_race_class":{ "10_2": 3800, ... }
}
"""
import asyncio
import logging
from datetime import date
import httpx
from . import client as api
from . import blob as blob_store

logger = logging.getLogger(__name__)

_CONCURRENCY = 50   # profile fetches — same as rate limiter ceiling


# ---------------------------------------------------------------------------
# Leaderboard fetch (synchronous — 85 sequential calls, fast enough)
# ---------------------------------------------------------------------------

def _get_current_season(region: str) -> int:
    data = api.get('/data/wow/pvp-season/index', region=region, namespace=f'dynamic-{region}')
    if not data:
        raise RuntimeError(f'Could not fetch PvP season index for region={region}')
    return data['current_season']['id']


def _get_brackets(season_id: int, region: str) -> list[str]:
    data = api.get(
        f'/data/wow/pvp-season/{season_id}/pvp-leaderboard/index',
        region=region,
        namespace=f'dynamic-{region}',
    )
    if not data:
        return []
    return [lb['name'] for lb in data.get('leaderboards', []) if lb.get('name')]


def _fetch_bracket(season_id: int, bracket: str, region: str) -> list[tuple[str, str]]:
    """Returns list of (name, realm_slug) entries from one bracket."""
    data = api.get(
        f'/data/wow/pvp-season/{season_id}/pvp-leaderboard/{bracket}',
        region=region,
        namespace=f'dynamic-{region}',
    )
    if not data:
        return []

    entries = []
    for entry in data.get('entries', []):
        char = entry.get('character', {})
        name = char.get('name')
        realm_slug = char.get('realm', {}).get('slug')
        if name and realm_slug:
            entries.append((name, realm_slug))
    logger.info(f'  {bracket}: {len(entries)} entries')
    return entries


# ---------------------------------------------------------------------------
# Async character profile fetch
# ---------------------------------------------------------------------------

async def _fetch_profile(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    limiter: api.AsyncRateLimiter,
    name: str,
    realm_slug: str,
    region: str,
) -> dict | None:
    """Returns {name, realm_slug, race_id, class_id, spec_id} or None on error."""
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

    return {
        'name':       name,
        'realm_slug': realm_slug,
        'race_id':    data.get('race', {}).get('id'),
        'class_id':   data.get('character_class', {}).get('id'),
        'spec_id':    data.get('active_spec', {}).get('id'),
    }


async def _fetch_profiles_async(
    chars: list[tuple[str, str]],
    region: str,
) -> list[dict]:
    """Fetch profiles for all (name, realm_slug) pairs concurrently."""
    sem = asyncio.Semaphore(_CONCURRENCY)
    limiter = api.AsyncRateLimiter()
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=_CONCURRENCY)) as client:
        tasks = [
            _fetch_profile(client, sem, limiter, name, realm_slug, region)
            for name, realm_slug in chars
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = sum(1 for r in results if isinstance(r, Exception))
    if errors:
        logger.warning(f'{errors} profile fetches failed — skipping those characters')
    return [r for r in results if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def crawl_pvp(region: str = 'us', snapshot_date: date = None) -> None:
    """
    Fetch all PvP bracket leaderboards, dedup characters across brackets,
    fetch a profile for each unique character, aggregate race/class/spec
    counts, and write pvp_latest.json to Vercel Blob.
    """
    if snapshot_date is None:
        snapshot_date = date.today()

    logger.info(f'Starting PvP crawl: region={region} date={snapshot_date}')

    season_id = _get_current_season(region)
    logger.info(f'Season: {season_id}')

    brackets = _get_brackets(season_id, region)
    logger.info(f'Fetching {len(brackets)} brackets...')

    # Collect all (name, realm_slug) entries across all brackets
    seen: set[tuple[str, str]] = set()
    for bracket in brackets:
        for entry in _fetch_bracket(season_id, bracket, region):
            seen.add(entry)

    unique_chars = list(seen)
    logger.info(f'{len(unique_chars)} unique characters across all brackets')

    if not unique_chars:
        logger.warning('No characters found — aborting')
        return

    # Pre-warm token before async context
    from .auth import get_token
    get_token(region)

    logger.info(f'Fetching {len(unique_chars)} character profiles...')
    profiles = asyncio.run(_fetch_profiles_async(unique_chars, region))
    logger.info(f'Fetched {len(profiles)} profiles successfully')

    if not profiles:
        logger.warning('No profiles returned — aborting')
        return

    # Aggregate counts
    by_spec:       dict[str, int] = {}
    by_class:      dict[str, int] = {}
    by_race:       dict[str, int] = {}
    by_race_spec:  dict[str, int] = {}
    by_race_class: dict[str, int] = {}

    for p in profiles:
        spec_id  = p.get('spec_id')
        class_id = p.get('class_id')
        race_id  = p.get('race_id')

        if spec_id is not None:
            k = str(spec_id)
            by_spec[k] = by_spec.get(k, 0) + 1

        if class_id is not None:
            k = str(class_id)
            by_class[k] = by_class.get(k, 0) + 1

        if race_id is not None:
            k = str(race_id)
            by_race[k] = by_race.get(k, 0) + 1

        if race_id is not None and spec_id is not None:
            k = f'{race_id}_{spec_id}'
            by_race_spec[k] = by_race_spec.get(k, 0) + 1

        if race_id is not None and class_id is not None:
            k = f'{race_id}_{class_id}'
            by_race_class[k] = by_race_class.get(k, 0) + 1

    payload = {
        'updated':       snapshot_date.isoformat(),
        'season':        season_id,
        'total':         len(profiles),
        'by_spec':       by_spec,
        'by_class':      by_class,
        'by_race':       by_race,
        'by_race_spec':  by_race_spec,
        'by_race_class': by_race_class,
    }

    logger.info(
        f'Writing PvP blob: {len(profiles):,} chars, '
        f'{len(by_spec)} specs, {len(by_race)} races'
    )
    blob_store.write('pvp', payload, snapshot_date)
    logger.info('PvP crawl complete')
