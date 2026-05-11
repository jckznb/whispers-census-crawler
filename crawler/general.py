"""
General population census via guild roster crawl.

Fetches rosters for a curated list of guilds on each target realm.
Guild roster endpoints return race_id, class_id, and level directly —
no character profile calls needed.

Deduplicates members within each realm group in memory (handles guild-hoppers),
filters to level 80+, aggregates race × class counts, then combines all
high-pop realms into general_latest.json and all RP realms into
general_rp_latest.json.

Output blob shape (same for both general and general_rp):
{
  "updated":       "2026-05-11",
  "total":         412000,
  "by_race":       { "10": 91000, ... },   # race_id  → count
  "by_class":      { "2":  54000, ... },   # class_id → count
  "by_race_class": { "10_2": 22000, ... }
}
"""
import asyncio
import logging
import re
from datetime import date
import httpx
from . import client as api
from . import blob as blob_store
from .config import GENERAL_GUILDS, RP_GUILDS

logger = logging.getLogger(__name__)

_CONCURRENCY = 50
_MIN_LEVEL   = 80


def _guild_slug(name: str) -> str:
    """Convert a guild display name to the Blizzard API slug format."""
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug


# ---------------------------------------------------------------------------
# Async roster fetch
# ---------------------------------------------------------------------------

async def _fetch_roster(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    realm_slug: str,
    guild_name: str,
    region: str,
) -> list[dict] | None:
    """
    Fetch a guild roster. Returns a list of member dicts or None on error/404.
    Each dict has: name, realm_slug, level, race_id, class_id.
    """
    name_slug = _guild_slug(guild_name)
    async with sem:
        data = await api.async_get(
            client,
            f'/data/wow/guild/{realm_slug}/{name_slug}/roster',
            region=region,
            namespace=f'profile-{region}',
        )

    if not data:
        logger.debug(f'  No data for {guild_name} ({realm_slug}) — 404 or error')
        return None

    members = []
    for entry in data.get('members', []):
        char = entry.get('character', {})
        name = char.get('name')
        char_realm = char.get('realm', {}).get('slug')
        level = char.get('level', 0)
        if not name or not char_realm or level < _MIN_LEVEL:
            continue
        members.append({
            'name':       name,
            'realm_slug': char_realm,
            'level':      level,
            'race_id':    char.get('playable_race', {}).get('id'),
            'class_id':   char.get('playable_class', {}).get('id'),
        })
    return members


async def _fetch_all_rosters(
    guild_list: list[tuple[str, str]],   # [(realm_slug, guild_name), ...]
    region: str,
) -> list[tuple[str, str, list[dict] | None]]:
    """Returns [(realm_slug, guild_name, members_or_None), ...]."""
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=_CONCURRENCY)) as client:
        tasks = [
            _fetch_roster(client, sem, realm_slug, guild_name, region)
            for realm_slug, guild_name in guild_list
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    output = []
    errors = 0
    for (realm_slug, guild_name), result in zip(guild_list, results):
        if isinstance(result, Exception):
            errors += 1
            logger.warning(f'  Error fetching {guild_name} ({realm_slug}): {result}')
            output.append((realm_slug, guild_name, None))
        else:
            output.append((realm_slug, guild_name, result))

    if errors:
        logger.warning(f'{errors} roster fetch(es) failed')
    return output


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _aggregate_guild_config(
    guild_config: dict[str, list[str]],
    region: str,
) -> dict:
    """
    Fetch all guild rosters for a config dict (realm_slug → [guild_names]),
    dedup members across guilds within the same realm group, and return
    aggregated counts.
    """
    guild_list = [
        (realm_slug, guild_name)
        for realm_slug, names in guild_config.items()
        for guild_name in names
    ]
    total_guilds = len(guild_list)
    logger.info(f'Fetching {total_guilds} guild rosters...')

    from .auth import get_token
    get_token(region)

    roster_results = asyncio.run(_fetch_all_rosters(guild_list, region))

    # Dedup by (name, realm_slug) across all guilds in this config
    seen:        set[tuple]      = set()
    by_race:     dict[str, int]  = {}
    by_class:    dict[str, int]  = {}
    by_race_cls: dict[str, int]  = {}
    total        = 0

    for realm_slug, guild_name, members in roster_results:
        if not members:
            continue
        for m in members:
            key = (m['name'], m['realm_slug'])
            if key in seen:
                continue
            seen.add(key)

            race_id  = m.get('race_id')
            class_id = m.get('class_id')
            total   += 1

            if race_id is not None:
                k = str(race_id)
                by_race[k] = by_race.get(k, 0) + 1

            if class_id is not None:
                k = str(class_id)
                by_class[k] = by_class.get(k, 0) + 1

            if race_id is not None and class_id is not None:
                k = f'{race_id}_{class_id}'
                by_race_cls[k] = by_race_cls.get(k, 0) + 1

    guilds_ok = sum(1 for _, _, m in roster_results if m is not None)
    logger.info(
        f'{guilds_ok}/{total_guilds} guilds OK, '
        f'{total:,} unique level-{_MIN_LEVEL}+ characters'
    )

    return {
        'total':         total,
        'by_race':       by_race,
        'by_class':      by_class,
        'by_race_class': by_race_cls,
    }


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def crawl_general(region: str = 'us', snapshot_date: date = None) -> None:
    """
    Crawl guild rosters for all configured realms, aggregate race × class
    counts, and write general_latest.json and general_rp_latest.json to
    Vercel Blob.
    """
    if snapshot_date is None:
        snapshot_date = date.today()

    logger.info(f'Starting general census: region={region} date={snapshot_date}')

    # --- High-population realms ---
    logger.info(f'High-pop realms ({len(GENERAL_GUILDS)} realms)...')
    general_data = _aggregate_guild_config(GENERAL_GUILDS, region)
    general_data['updated'] = snapshot_date.isoformat()

    logger.info(f'Writing general blob: {general_data["total"]:,} chars')
    blob_store.write('general', general_data, snapshot_date)

    # --- RP realms ---
    logger.info(f'RP realms ({len(RP_GUILDS)} realms)...')
    rp_data = _aggregate_guild_config(RP_GUILDS, region)
    rp_data['updated'] = snapshot_date.isoformat()

    logger.info(f'Writing general_rp blob: {rp_data["total"]:,} chars')
    blob_store.write('general_rp', rp_data, snapshot_date)

    logger.info('General census complete')
