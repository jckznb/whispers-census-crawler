"""
Phase 3: General population census via guild roster snowballing.

Discovery strategy:
  1. Characters already in the DB (from PvP/M+ crawls) have guild_name stored
     on their profile. Seed the census_guilds queue from these.
  2. Fetch guild rosters → each roster gives us 20-500 max-level characters
     for the price of a single API call. Very efficient.
  3. New characters from rosters are inserted into the characters table.
  4. On each re-crawl pass, freshly-fetched leaderboard characters update their
     guild data, which seeds new guilds to crawl.

Guild roster discovery adds race + class + level data. Spec data is NULL unless
a character also appears on a leaderboard. resolve_census_specs() runs after
each batch to profile a chunk of spec-null characters via the Blizzard profile
endpoint, filling in active_spec_id incrementally across daily runs.

Usage (via run_crawl.py):
    python -m scripts.run_crawl --phase census --region us --mode seed
    python -m scripts.run_crawl --phase census --region us --mode roster --batch-size 500
    python -m scripts.run_crawl --phase census --region us --mode all --batch-size 500
"""
import asyncio
import logging
import re
from datetime import date, datetime, timezone, timedelta
import httpx
from . import client as api
from . import db
from .config import CENSUS_TARGET_REALMS, MANUAL_GUILD_SEEDS

logger = logging.getLogger(__name__)

_CONCURRENCY = 50

# Flat set of all target realm slugs for quick membership checks
_TARGET_REALM_SLUGS: set[str] = {
    slug
    for slugs in CENSUS_TARGET_REALMS.values()
    for slug in slugs
}

# Level at which a character is considered "endgame" and worth counting
_MIN_LEVEL = 80

# How long before we re-crawl a guild for new/changed members (days)
_GUILD_STALENESS_DAYS = 30

# Characters to profile per census run when resolving missing spec data.
# At daily cadence this processes ~5k chars/day — enough to clear any backlog
# within a few weeks without adding more than a few minutes to each run.
_SPEC_RESOLVE_BATCH = 5000


# ---- Guild name slugification -----------------------------------------------

def _guild_slug(name: str) -> str:
    """
    Convert a guild name to the URL slug used by the Blizzard API roster endpoint.
    Lowercased, spaces → hyphens, non-alphanumeric-or-hyphen stripped.
    """
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug


# ---- Seed phase: populate census_guilds from existing characters -------------

def seed_guilds(region: str) -> int:
    """
    Pull guild references from characters on TARGET_REALMS that have
    guild_name populated (from PvP/M+ profile fetches) and upsert into census_guilds.

    Only guilds on realms listed in CENSUS_TARGET_REALMS are queued — this
    bounds the census to a curated set of representative servers rather than
    snowballing the entire region.

    Returns the number of new guilds inserted.
    """
    target_slugs = list(_TARGET_REALM_SLUGS)
    logger.info(
        f'Seeding census_guilds from {len(target_slugs)} target realms '
        f'(region={region})...'
    )

    # Pull distinct (guild_name, guild_realm_slug) from target realms only
    slugs_csv = ','.join(target_slugs)
    rows = db.query('characters', {
        'select':             'guild_name,guild_realm_slug',
        'region':             f'eq.{region}',
        'guild_name':         'not.is.null',
        'guild_realm_slug':   f'in.({slugs_csv})',
    })

    if not rows:
        logger.info('No characters with guild data found — run a PvP/M+ crawl first')
        return 0

    # Deduplicate
    seen: set[tuple] = set()
    guild_rows = []
    for r in rows:
        name = r['guild_name']
        realm = r['guild_realm_slug']
        if not name or not realm:
            continue
        key = (name.lower(), realm, region)
        if key in seen:
            continue
        seen.add(key)
        guild_rows.append({
            'name':       name,
            'name_slug':  _guild_slug(name),
            'realm_slug': realm,
            'region':     region,
        })

    logger.info(f'Found {len(guild_rows)} distinct guilds from character data')

    if guild_rows:
        db.upsert(
            'census_guilds',
            guild_rows,
            on_conflict='name_slug,realm_slug,region',
            conflict_resolution='ignore-duplicates',  # don't reset last_crawled_at
        )
        logger.info(f'Seeded {len(guild_rows)} guild records into census_guilds')

    return len(guild_rows)


# ---- Manual seed phase: insert known guilds for RP / low-leaderboard realms --

def seed_guilds_manual(region: str) -> int:
    """
    Insert a curated list of known large guilds directly into census_guilds,
    bypassing the character-based seed for realms where PvP/M+ data is sparse.

    Guild names come from MANUAL_GUILD_SEEDS in config.py — fill that list in
    with well-known guilds on each target realm, then run:

        python -m scripts.run_crawl --phase census --region us --mode seed

    The roster crawl will fan out from these seeds into the broader guild graph.
    Returns the number of rows inserted (skips existing entries).
    """
    guild_rows = []
    for realm_slug, names in MANUAL_GUILD_SEEDS.items():
        for name in names:
            name = name.strip()
            if not name:
                continue
            guild_rows.append({
                'name':       name,
                'name_slug':  _guild_slug(name),
                'realm_slug': realm_slug,
                'region':     region,
            })

    if not guild_rows:
        logger.info('MANUAL_GUILD_SEEDS is empty — add guild names to config.py first')
        return 0

    db.upsert(
        'census_guilds',
        guild_rows,
        on_conflict='name_slug,realm_slug,region',
        conflict_resolution='ignore-duplicates',
    )
    logger.info(f'Manually seeded {len(guild_rows)} guilds into census_guilds')
    return len(guild_rows)


# ---- Roster crawl phase: fetch guild rosters, discover characters ------------

def crawl_guild_batch(
    region: str,
    batch_size: int = 200,
    snapshot_date: date = None,
) -> int:
    """
    Fetch rosters for up to batch_size uncrawled (or stale) guilds.
    Inserts newly discovered characters into the characters table.
    Marks each guild as crawled.

    Returns total characters discovered (new inserts, not including skips).
    """
    if snapshot_date is None:
        snapshot_date = date.today()

    stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=_GUILD_STALENESS_DAYS)).isoformat()

    # Pick guilds that have never been crawled (priority 1) or are stale (priority 2)
    # First: never crawled
    guilds = db.query('census_guilds', {
        'select':           'id,name,name_slug,realm_slug',
        'region':           f'eq.{region}',
        'last_crawled_at':  'is.null',
        'order':            'crawl_priority.desc,id.asc',
        'limit':            str(batch_size),
    })

    # Fill remainder with stale guilds
    remaining = batch_size - len(guilds)
    if remaining > 0:
        stale = db.query('census_guilds', {
            'select':           'id,name,name_slug,realm_slug',
            'region':           f'eq.{region}',
            'last_crawled_at':  f'lt.{stale_cutoff}',
            'order':            'last_crawled_at.asc',
            'limit':            str(remaining),
        })
        guilds.extend(stale)

    if not guilds:
        logger.info('No guilds to crawl — run seed_guilds first or wait for more guild data')
        return 0

    logger.info(f'Crawling {len(guilds)} guild rosters (region={region})...')

    # Load race→faction lookup so we can infer faction from race_id
    race_faction: dict[int, str] = {
        r['id']: r['faction']
        for r in db.select('races', columns='id,faction')
    }

    # Pre-warm auth token
    from .auth import get_token
    get_token(region)

    # Process guilds in async chunks to cap coroutine count and make failures
    # recoverable — a network blip only loses one chunk, not the whole run.
    _ASYNC_CHUNK = 2000
    total_chars = 0
    now = datetime.now(timezone.utc).isoformat()

    for chunk_start in range(0, len(guilds), _ASYNC_CHUNK):
        chunk = guilds[chunk_start:chunk_start + _ASYNC_CHUNK]
        logger.info(
            f'  Chunk {chunk_start // _ASYNC_CHUNK + 1}/'
            f'{(len(guilds) - 1) // _ASYNC_CHUNK + 1}: '
            f'{len(chunk)} guilds'
        )

        results = asyncio.run(_fetch_rosters_async(chunk, region, race_faction))

        for guild, members in results:
            if members is None:
                # 404 or error — mark as crawled so we don't keep retrying immediately
                _mark_guild_crawled(guild['id'], member_count=None)
                continue

            # Build character rows — only max-level members.
            # resolve_census_specs() finds characters with active_spec_id IS NULL
            # and profiles them regardless of last_api_update (force=True), so
            # setting last_api_update=now here doesn't prevent spec resolution.
            char_rows = []
            for m in members:
                level = m.get('level', 0)
                if level < _MIN_LEVEL:
                    continue
                char_rows.append({
                    'name':             m['name'],
                    'realm_slug':       m['realm_slug'],
                    'region':           region,
                    'race_id':          m.get('race_id'),
                    'class_id':         m.get('class_id'),
                    'level':            level,
                    'faction':          race_faction.get(m.get('race_id'), ''),
                    'guild_name':       guild['name'],
                    'guild_realm_slug': guild['realm_slug'],
                    'last_api_update':  now,
                    'first_seen':       now,
                })

            if char_rows:
                # Filter to target realms — the roster may include characters from
                # connected realms outside our target list; skip those.
                char_rows = [r for r in char_rows if r['realm_slug'] in _TARGET_REALM_SLUGS]

            if char_rows:
                # ignore-duplicates: don't overwrite spec/ilvl data for leaderboard chars
                db.upsert(
                    'characters',
                    char_rows,
                    on_conflict='name,realm_slug,region',
                    conflict_resolution='ignore-duplicates',
                )
                total_chars += len(char_rows)

            _mark_guild_crawled(guild['id'], member_count=len(members))
            logger.debug(f'  {guild["name"]} ({guild["realm_slug"]}): {len(char_rows)} max-level members')

    logger.info(f'Guild batch complete: {len(guilds)} guilds, {total_chars} characters discovered')
    return total_chars


def _mark_guild_crawled(guild_id: int, member_count: int | None) -> None:
    """Update last_crawled_at and member_count for a guild via direct httpx call."""
    from .config import SUPABASE_URL, SUPABASE_SERVICE_KEY
    import httpx as _httpx

    payload: dict = {'last_crawled_at': datetime.now(timezone.utc).isoformat()}
    if member_count is not None:
        payload['member_count'] = member_count

    try:
        _httpx.patch(
            f'{SUPABASE_URL}/rest/v1/census_guilds',
            json=payload,
            params={'id': f'eq.{guild_id}'},
            headers={
                'apikey':        SUPABASE_SERVICE_KEY,
                'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
                'Content-Type':  'application/json',
                'Prefer':        'return=minimal',
            },
            timeout=30,
        ).raise_for_status()
    except Exception as exc:
        # Non-fatal — worst case we re-crawl this guild on the next pass
        logger.warning(f'Failed to mark guild {guild_id} as crawled: {exc}')


# ---- Async roster fetching --------------------------------------------------

async def _fetch_rosters_async(
    guilds: list[dict],
    region: str,
    race_faction: dict,
) -> list[tuple[dict, list | None]]:
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=_CONCURRENCY)
    ) as client:
        tasks = [
            _fetch_roster_one(client, sem, guild, region)
            for guild in guilds
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    output = []
    errors = 0
    for guild, result in zip(guilds, results):
        if isinstance(result, Exception):
            errors += 1
            output.append((guild, None))
        else:
            output.append((guild, result))

    if errors:
        logger.warning(f'{errors} guild roster fetches failed')
    return output


async def _fetch_roster_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    guild: dict,
    region: str,
) -> list[dict] | None:
    """
    Fetch the roster for a single guild. Returns a list of member dicts or None on error.
    """
    async with sem:
        data = await api.async_get(
            client,
            f'/data/wow/guild/{guild["realm_slug"]}/{guild["name_slug"]}/roster',
            region=region,
            namespace=f'profile-{region}',
        )

    if not data:
        return None  # 404 — guild deleted, renamed, or slug mismatch

    members = []
    for entry in data.get('members', []):
        char = entry.get('character', {})
        name = char.get('name')
        realm_slug = char.get('realm', {}).get('slug')
        if not name or not realm_slug:
            continue
        members.append({
            'name':       name,
            'realm_slug': realm_slug,
            'level':      char.get('level', 0),
            'race_id':    char.get('playable_race', {}).get('id'),
            'class_id':   char.get('playable_class', {}).get('id'),
        })

    return members


# ---- Spec resolution: profile census characters that are missing spec data ---

def resolve_census_specs(
    region: str,
    snapshot_date: date,
    batch_limit: int = _SPEC_RESOLVE_BATCH,
) -> None:
    """
    Fetch Blizzard character profiles for census characters that are missing
    active_spec_id. Guild roster endpoints only return race/class/level —
    this function fills in spec (and profession) data for a batch of those
    characters per daily run.

    Uses force=True in resolve_characters to bypass the staleness cache, since
    census characters may have last_api_update=NULL (never profiled) or may
    have been inserted without a proper profile fetch.
    """
    target_slugs = list(_TARGET_REALM_SLUGS)
    slugs_csv = ','.join(target_slugs)

    unresolved = db.query('characters', {
        'select':         'name,realm_slug,region',
        'region':         f'eq.{region}',
        'realm_slug':     f'in.({slugs_csv})',
        'active_spec_id': 'is.null',
        'limit':          str(batch_limit),
    })

    if not unresolved:
        logger.info('All census characters have spec data — skipping spec resolution')
        return

    logger.info(
        f'Resolving specs for {len(unresolved)} census characters '
        f'(batch_limit={batch_limit})...'
    )

    from .characters import resolve_characters
    from .professions import resolve_professions

    # force=True bypasses staleness — these chars need a real profile fetch
    char_id_map, fresh_keys = resolve_characters(unresolved, force=True)

    if fresh_keys:
        logger.info(f'Profiled {len(fresh_keys)} characters, fetching professions...')
        resolve_professions(char_id_map, fresh_keys, snapshot_date)
    else:
        logger.info('No new profiles fetched during spec resolution')


# ---- Aggregation ------------------------------------------------------------

def aggregate_general(region: str, snapshot_date: date) -> None:
    """
    Run compute_general_demographics_snapshot twice — once per realm type —
    producing separate context='general' (high-pop realms) and
    context='general_rp' (RP realms) snapshots.
    """
    splits = [
        ('general',    'general',    CENSUS_TARGET_REALMS['general']),
        ('general_rp', 'RP realms',  CENSUS_TARGET_REALMS['rp']),
    ]
    for db_context, label, realm_slugs in splits:
        logger.info(
            f'Computing {label} demographics snapshot '
            f'({len(realm_slugs)} realms, date={snapshot_date})...'
        )
        db.rpc('compute_general_demographics_snapshot', {
            'p_snapshot_date': snapshot_date.isoformat(),
            'p_region':        region,
            'p_min_level':     _MIN_LEVEL,
            'p_context':       db_context,
            'p_realm_slugs':   realm_slugs,
        })
    logger.info('General demographics snapshots complete')


# ---- Orchestrator -----------------------------------------------------------

def crawl_census(
    region: str = 'us',
    snapshot_date: date = None,
    mode: str = 'roster',
    batch_size: int = 200,
    aggregate: bool = False,
) -> None:
    """
    General population census crawler.

    mode:
      'seed'   — extract guild refs from existing characters → populate census_guilds
      'roster' — crawl batch_size guild rosters, discover characters
      'all'    — seed then roster (useful for first run and daily automation)

    aggregate: if True, run demographics aggregation after roster crawl.
               Usually combined with exporter in run_crawl.py instead.
    """
    if snapshot_date is None:
        snapshot_date = date.today()

    if mode in ('seed', 'all'):
        seed_guilds(region)         # from existing PvP/M+ character data
        seed_guilds_manual(region)  # from MANUAL_GUILD_SEEDS in config.py

    if mode in ('roster', 'all'):
        crawl_guild_batch(region, batch_size=batch_size, snapshot_date=snapshot_date)
        # Profile a batch of census characters that are missing spec data.
        # This runs incrementally — each daily run chips away at the backlog.
        resolve_census_specs(region, snapshot_date)

    if aggregate:
        aggregate_general(region, snapshot_date)
