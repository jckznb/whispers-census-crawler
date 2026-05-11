"""
Vercel Blob writer for census output blobs.

Each context (pvp, mplus, general, general_rp) gets:
  - {name}_latest.json       — always overwritten, frontend default fetch
  - {name}_YYYY-MM-DD.json   — dated snapshot, kept for KEEP_DAYS
  - {name}_manifest.json     — lists the last MAX_HISTORY dated blob URLs,
                               for frontend trend data

Blobs older than KEEP_DAYS are pruned automatically after each write.
"""
import json
import logging
from datetime import date, datetime, timezone, timedelta
import httpx
from .config import BLOB_READ_WRITE_TOKEN

logger = logging.getLogger(__name__)

BLOB_BASE    = 'https://blob.vercel-storage.com'
KEEP_DAYS    = 28   # prune dated blobs older than this
MAX_HISTORY  = 4    # entries to include in the manifest

_UPLOAD_HEADERS = {
    'Authorization':           f'Bearer {BLOB_READ_WRITE_TOKEN}',
    'x-add-random-suffix':     '0',
    'x-cache-control-max-age': '3600',
}


def _put(pathname: str, data: dict) -> str:
    """Upload data as JSON to Vercel Blob. Returns the public URL."""
    body = json.dumps(data, separators=(',', ':')).encode()
    r = httpx.put(
        f'{BLOB_BASE}/{pathname}',
        content=body,
        headers={**_UPLOAD_HEADERS, 'Content-Type': 'application/json'},
        timeout=30,
    )
    r.raise_for_status()
    url = r.json()['url']
    logger.info(f'Uploaded {pathname} ({len(body):,} bytes) → {url}')
    return url


def _list_prefix(prefix: str) -> list[dict]:
    """Return all blob metadata objects whose pathname starts with prefix."""
    blobs = []
    cursor = None
    while True:
        params = {'prefix': prefix, 'limit': 1000}
        if cursor:
            params['cursor'] = cursor
        r = httpx.get(
            BLOB_BASE,
            params=params,
            headers={'Authorization': f'Bearer {BLOB_READ_WRITE_TOKEN}'},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        blobs.extend(body.get('blobs', []))
        cursor = body.get('cursor')
        if not body.get('hasMore'):
            break
    return blobs


def _delete(urls: list[str]) -> None:
    """Delete a list of blobs by URL."""
    if not urls:
        return
    r = httpx.delete(
        BLOB_BASE,
        json={'urls': urls},
        headers={
            'Authorization': f'Bearer {BLOB_READ_WRITE_TOKEN}',
            'Content-Type':  'application/json',
        },
        timeout=30,
    )
    r.raise_for_status()
    logger.info(f'Deleted {len(urls)} old blob(s)')


def write(name: str, data: dict, snapshot_date: date = None) -> str:
    """
    Write a census output blob.

    Uploads:
      - {name}_latest.json        (always overwritten)
      - {name}_{date}.json        (dated snapshot)
      - {name}_manifest.json      (last MAX_HISTORY dated URLs)

    Prunes dated blobs older than KEEP_DAYS.

    Returns the URL of the dated blob.
    """
    if not BLOB_READ_WRITE_TOKEN:
        logger.warning('BLOB_READ_WRITE_TOKEN not set — skipping blob write')
        return ''

    if snapshot_date is None:
        snapshot_date = date.today()

    date_str    = snapshot_date.isoformat()
    dated_path  = f'{name}_{date_str}.json'
    latest_path = f'{name}_latest.json'

    # Upload latest (always)
    _put(latest_path, data)

    # Upload dated snapshot
    dated_url = _put(dated_path, data)

    # List all dated snapshots for this name (skip "_latest" and "_manifest")
    all_blobs = _list_prefix(f'{name}_20')   # dated blobs start with name_20YY-…
    all_blobs.sort(key=lambda b: b['pathname'], reverse=True)  # newest first

    # Build manifest from most recent MAX_HISTORY blobs
    manifest_entries = []
    for b in all_blobs[:MAX_HISTORY]:
        # Extract date from pathname like "pvp_2026-05-11.json"
        try:
            blob_date = b['pathname'].replace(f'{name}_', '').replace('.json', '')
            manifest_entries.append({'date': blob_date, 'url': b['url']})
        except Exception:
            pass

    _put(f'{name}_manifest.json', {'history': manifest_entries})

    # Prune old dated blobs
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    to_delete = [
        b['url'] for b in all_blobs
        if datetime.fromisoformat(
            b.get('uploadedAt', '1970-01-01T00:00:00.000Z').replace('Z', '+00:00')
        ) < cutoff
    ]
    if to_delete:
        logger.info(f'Pruning {len(to_delete)} blob(s) older than {KEEP_DAYS} days')
        _delete(to_delete)

    return dated_url
