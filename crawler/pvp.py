"""Phase 1: PvP leaderboard crawler."""
import logging
from datetime import date
from . import client as api
from . import db

logger = logging.getLogger(__name__)


def get_current_season(region: str = 'us') -> int:
    data = api.get('/data/wow/pvp-season/index', region=region, namespace=f'dynamic-{region}')
    if not data:
        raise RuntimeError(f'Could not fetch PvP season index for region={region}')
    return data['current_season']['id']


def get_brackets(season_id: int, region: str = 'us') -> list[str]:
    data = api.get(
        f'/data/wow/pvp-season/{season_id}/pvp-leaderboard/index',
        region=region,
        namespace=f'dynamic-{region}',
    )
    if not data:
        return []
    return [lb['name'] for lb in data.get('leaderboards', []) if lb.get('name')]


def fetch_leaderboard(season_id: int, bracket: str, region: str = 'us') -> list[dict]:
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
        if not name or not realm_slug:
            continue
        entries.append({
            'name': name,
            'realm_slug': realm_slug,
            'region': region,
            'rating': entry.get('rating', 0),
            'rank': entry.get('rank'),
            'bracket': bracket,
            'season_id': season_id,
        })

    logger.info(f'Fetched {len(entries)} entries for bracket={bracket} region={region}')
    return entries


def _fetch_all_entries(region: str) -> tuple[int, list[dict]]:
    """Fetch all leaderboard entries across all brackets. Returns (season_id, entries)."""
    season_id = get_current_season(region)
    logger.info(f'PvP season={season_id} region={region}')

    brackets = get_brackets(season_id, region)
    logger.info(f'Found {len(brackets)} brackets: {brackets}')

    all_entries: list[dict] = []
    for bracket in brackets:
        entries = fetch_leaderboard(season_id, bracket, region)
        all_entries.extend(entries)

    return season_id, all_entries


def _store_pvp_entries(
    all_entries: list[dict],
    char_id_map: dict[tuple, int],
    season_id: int,
    snapshot_date: date,
) -> None:
    """Deduplicate on (character_id, bracket), keeping highest rating, then upsert."""
    pvp_dedup: dict[tuple, dict] = {}
    for entry in all_entries:
        key = (entry['name'], entry['realm_slug'], entry['region'])
        char_id = char_id_map.get(key)
        if char_id is None:
            continue
        dedup_key = (char_id, entry['season_id'], entry['bracket'], snapshot_date.isoformat())
        row = {
            'character_id': char_id,
            'season_id':    entry['season_id'],
            'bracket':      entry['bracket'],
            'rating':       entry['rating'],
            'rank':         entry['rank'],
            'snapshot_date': snapshot_date.isoformat(),
        }
        existing = pvp_dedup.get(dedup_key)
        if existing is None or row['rating'] > existing['rating']:
            pvp_dedup[dedup_key] = row

    pvp_rows = list(pvp_dedup.values())
    db.upsert('pvp_entries', pvp_rows, on_conflict='character_id,season_id,bracket,snapshot_date')
    logger.info(f'Stored {len(pvp_rows)} PvP entries for snapshot={snapshot_date}')


def crawl_pvp(
    region: str = 'us',
    snapshot_date: date = None,
    leaderboard_only: bool = False,
    enrich_only: bool = False,
) -> None:
    """
    Run the PvP leaderboard crawl.

    leaderboard_only=True:
        Fetch all bracket leaderboards, upsert character stubs (name/realm/region
        only, DO NOTHING on conflict), and store pvp_entries. Skips profile
        enrichment and profession fetches. Fast (~5 min). Intended to run as a
        separate GHA job before the enrich job.

    enrich_only=True:
        Re-fetch the leaderboards (fast, ~85 requests) to get the current character
        list, then resolve stale character profiles and professions. Does NOT store
        pvp_entries (already done by the leaderboard job). Intended to run as a
        separate long-running GHA job (up to 330 min timeout).

    Default (both False):
        Full crawl in one pass: leaderboard fetch → profile resolution →
        pvp_entry storage → profession fetch.
    """
    from .characters import resolve_characters, upsert_char_stubs

    if snapshot_date is None:
        snapshot_date = date.today()

    logger.info(
        f'Starting PvP crawl: region={region} snapshot_date={snapshot_date} '
        f'leaderboard_only={leaderboard_only} enrich_only={enrich_only}'
    )

    season_id, all_entries = _fetch_all_entries(region)

    if not all_entries:
        logger.warning('No PvP entries fetched — aborting')
        return

    # Deduplicate characters from the raw entry list
    seen: dict[tuple, dict] = {}
    for entry in all_entries:
        key = (entry['name'], entry['realm_slug'], entry['region'])
        if key not in seen:
            seen[key] = {'name': entry['name'], 'realm_slug': entry['realm_slug'], 'region': entry['region']}

    unique_chars = list(seen.values())
    logger.info(f'Found {len(unique_chars)} unique characters in leaderboard')

    if leaderboard_only:
        # Upsert character stubs so pvp_entries FK is satisfiable, then store entries.
        char_id_map = upsert_char_stubs(unique_chars)
        _store_pvp_entries(all_entries, char_id_map, season_id, snapshot_date)
        logger.info('PvP leaderboard crawl complete — run enrich job separately')
        return

    if enrich_only:
        # PvP entries already stored by the leaderboard job.
        # Resolve stale character profiles and professions only.
        logger.info(f'Enriching {len(unique_chars)} characters (profiles + professions)...')
        char_id_map, fresh_keys = resolve_characters(unique_chars)
        from .professions import resolve_professions
        resolve_professions(char_id_map, fresh_keys, snapshot_date)
        logger.info('PvP enrich complete')
        return

    # Normal mode: full crawl in one pass
    logger.info(f'Resolving {len(unique_chars)} unique characters')
    char_id_map, fresh_keys = resolve_characters(unique_chars)

    _store_pvp_entries(all_entries, char_id_map, season_id, snapshot_date)

    from .professions import resolve_professions
    resolve_professions(char_id_map, fresh_keys, snapshot_date)

    logger.info('PvP crawl complete')
