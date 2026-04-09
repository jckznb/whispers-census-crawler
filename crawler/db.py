"""Supabase PostgREST client — uses httpx directly, no supabase-py needed."""
import logging
import httpx
from .config import SUPABASE_URL, SUPABASE_SERVICE_KEY

logger = logging.getLogger(__name__)

_BATCH_SIZE = 1000
_REST_BASE = f'{SUPABASE_URL}/rest/v1'
_TIMEOUT     = 120   # seconds — large batch inserts
_RPC_TIMEOUT = 660   # seconds — aggregation RPCs can run 5-10 min on full datasets


def _headers(prefer: str = 'return=minimal') -> dict:
    return {
        'apikey': SUPABASE_SERVICE_KEY,
        'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': prefer,
    }


def upsert(
    table: str,
    rows: list[dict],
    on_conflict: str = None,
    conflict_resolution: str = 'merge-duplicates',
) -> None:
    """
    Upsert rows into a table in batches.

    conflict_resolution:
      'merge-duplicates' (default) — UPDATE all columns on conflict
      'ignore-duplicates'          — DO NOTHING on conflict (safe for discovery inserts)
    """
    if not rows:
        return
    params = {}
    if on_conflict:
        params['on_conflict'] = on_conflict
    headers = _headers(f'resolution={conflict_resolution},return=minimal')
    for i in range(0, len(rows), _BATCH_SIZE):
        chunk = rows[i:i + _BATCH_SIZE]
        r = httpx.post(f'{_REST_BASE}/{table}', json=chunk, headers=headers, params=params, timeout=_TIMEOUT)
        if not r.is_success:
            logger.error(f'Upsert {table} batch {i}–{i+len(chunk)} failed {r.status_code}: {r.text[:500]}')
        r.raise_for_status()
    logger.debug(f'Upserted {len(rows)} rows into {table}')


def query(table: str, params: dict) -> list[dict]:
    """
    Raw PostgREST query with arbitrary filter params.

    Use this for queries that need non-equality operators (is.null, not.is.null, etc.)
    or ordering/limiting that db.select() doesn't support.

    Examples:
        db.query('census_guilds', {
            'select': 'id,name,realm_slug',
            'region': 'eq.us',
            'last_crawled_at': 'is.null',
            'order': 'crawl_priority.desc',
            'limit': '200',
        })
    """
    results = []
    offset = int(params.pop('offset', 0))
    limit = params.get('limit')
    page_size = int(limit) if limit else 1000

    while True:
        page_params = {**params, 'limit': page_size, 'offset': offset}
        r = httpx.get(f'{_REST_BASE}/{table}', params=page_params, headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        page = r.json()
        results.extend(page)
        # If a limit was specified by caller, return exactly that many
        if limit or len(page) < page_size:
            break
        offset += page_size
    return results


def insert(table: str, rows: list[dict]) -> list[dict]:
    """Insert rows, returning inserted data (including generated IDs)."""
    if not rows:
        return []
    result = []
    for i in range(0, len(rows), _BATCH_SIZE):
        chunk = rows[i:i + _BATCH_SIZE]
        r = httpx.post(f'{_REST_BASE}/{table}', json=chunk, headers=_headers('return=representation'))
        r.raise_for_status()
        result.extend(r.json())
    return result


def select(table: str, filters: dict = None, columns: str = '*') -> list[dict]:
    """Simple equality-filtered select. Handles pagination automatically."""
    params = {'select': columns}
    for key, value in (filters or {}).items():
        params[key] = f'eq.{value}'

    results = []
    offset = 0
    page_size = 1000
    while True:
        page_params = {**params, 'limit': page_size, 'offset': offset}
        r = httpx.get(f'{_REST_BASE}/{table}', params=page_params, headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        page = r.json()
        results.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return results


def delete(table: str, filters: dict) -> None:
    """Delete rows matching filters. Supports eq (scalar) and in (list) per key."""
    if not filters:
        raise ValueError('delete() requires at least one filter')
    params = {}
    for key, value in filters.items():
        if isinstance(value, list):
            params[key] = f'in.({",".join(str(v) for v in value)})'
        else:
            params[key] = f'eq.{value}'
    r = httpx.delete(f'{_REST_BASE}/{table}', params=params, headers=_headers(), timeout=_TIMEOUT)
    r.raise_for_status()
    logger.debug(f'Deleted from {table} where {filters}')


def rpc(function_name: str, params: dict = None) -> any:
    """Call a Postgres function via RPC."""
    r = httpx.post(f'{_REST_BASE}/rpc/{function_name}', json=params or {}, headers=_headers(), timeout=_RPC_TIMEOUT)
    if not r.is_success:
        logger.error(f'RPC {function_name} failed {r.status_code}: {r.text[:1000]}')
    r.raise_for_status()
    return r.json() if r.content else None
