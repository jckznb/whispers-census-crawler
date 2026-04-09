"""
Build and upload the demographics JSON blob to Vercel Blob storage.

The blob is a single file served by Vercel's CDN.
The frontend fetches it once per page load — no Supabase connections needed.

JSON shape:
{
  "updated": "2026-04-08",
  "pvp": {
    "total": 151717,
    "combos":      [{"race","faction","class","count","pct"}, ...],
    "specs":       [{"class","spec","role","count","pct"}, ...],
    "spec_combos": [{"race","faction","class","spec","count","pct"}, ...],
    "professions": [{"name","type","count","pct"}, ...]
  },
  "pve": { ... },
  "general": null
}
"""
import json
import logging
from datetime import date
import httpx
from . import db
from .config import BLOB_READ_WRITE_TOKEN

logger = logging.getLogger(__name__)

BLOB_PATHNAME = 'demographics.json'
BLOB_UPLOAD_URL = f'https://blob.vercel-storage.com/{BLOB_PATHNAME}'

# Maps blob key → demographics_snapshot context value
CONTEXT_MAP = {
    'pvp':     'pvp_all',
    'pve':     'mplus_all',
    'general': 'general',
}

# Maps blob key → profession RPC function name (None = no professions for that context)
PROFESSION_RPC_MAP = {
    'pvp':     'get_pvp_profession_stats',
    'pve':     'get_mplus_profession_stats',
    'general': None,
}


def _load_lookups() -> tuple[dict, dict, dict]:
    """Fetch races, classes, and specs from Supabase, return id→data dicts."""
    races   = {r['id']: r for r in db.select('races',   columns='id,name,faction')}
    classes = {c['id']: c for c in db.select('classes', columns='id,name')}
    specs   = {s['id']: s for s in db.select('specs',   columns='id,name,class_id,role')}
    return races, classes, specs


def _build_professions(rpc_name: str, snapshot_date: date, region: str) -> list[dict]:
    """
    Call a profession stats RPC and return a list of
    {"name", "type", "count", "pct"} dicts, or [] on error / no data.
    """
    try:
        rows = db.rpc(rpc_name, {
            'p_snapshot_date': snapshot_date.isoformat(),
            'p_region': region,
        })
    except Exception as exc:
        logger.warning(f'Profession RPC {rpc_name} failed: {exc}')
        return []

    if not rows:
        return []

    return [
        {
            'name':  r['name'],
            'type':  r['type'],
            'count': r['count'],
            'pct':   float(r['pct']) if r['pct'] is not None else 0.0,
        }
        for r in rows
    ]


def _build_all_for_context(
    db_context: str,
    races: dict,
    classes: dict,
    specs: dict,
) -> dict | None:
    """
    Single pass over demographics_snapshot for db_context.
    Returns {"total", "combos", "specs", "spec_combos"} or None if no data.
    """
    rows = db.select('demographics_snapshot', filters={'context': db_context})
    if not rows:
        return None

    # Use the latest snapshot date only
    latest = max(r['snapshot_date'] for r in rows)
    rows = [r for r in rows if r['snapshot_date'] == latest]

    combo_counts:    dict[tuple, int] = {}  # (race_id, class_id)
    spec_counts:     dict[tuple, int] = {}  # (class_id, spec_id)
    sc_counts:       dict[tuple, int] = {}  # (race_id, class_id, spec_id)

    for row in rows:
        race_id  = row['race_id']
        class_id = row['class_id']
        spec_id  = row['spec_id']
        count    = row['count']

        # race+class (always)
        key = (race_id, class_id)
        combo_counts[key] = combo_counts.get(key, 0) + count

        # spec-level aggregations (only when spec_id is known)
        if spec_id is not None:
            sk = (class_id, spec_id)
            spec_counts[sk] = spec_counts.get(sk, 0) + count

            sck = (race_id, class_id, spec_id)
            sc_counts[sck] = sc_counts.get(sck, 0) + count

    combo_total = sum(combo_counts.values())
    if combo_total == 0:
        return None

    spec_total = sum(spec_counts.values())
    sc_total   = sum(sc_counts.values())

    # Build combos
    combos = []
    for (race_id, class_id), count in sorted(combo_counts.items(), key=lambda x: -x[1]):
        race = races.get(race_id)
        cls  = classes.get(class_id)
        if not race or not cls:
            continue
        combos.append({
            'race':    race['name'],
            'faction': race['faction'],
            'class':   cls['name'],
            'count':   count,
            'pct':     round(count * 100.0 / combo_total, 2),
        })

    # Build specs (class+spec popularity)
    specs_list = []
    if spec_total > 0:
        for (class_id, spec_id), count in sorted(spec_counts.items(), key=lambda x: -x[1]):
            cls  = classes.get(class_id)
            spec = specs.get(spec_id)
            if not cls or not spec:
                continue
            specs_list.append({
                'class': cls['name'],
                'spec':  spec['name'],
                'role':  spec['role'],
                'count': count,
                'pct':   round(count * 100.0 / spec_total, 2),
            })

    # Build spec_combos (race+class+spec, for heatmap drill-down)
    spec_combos = []
    if sc_total > 0:
        for (race_id, class_id, spec_id), count in sorted(sc_counts.items(), key=lambda x: -x[1]):
            race = races.get(race_id)
            cls  = classes.get(class_id)
            spec = specs.get(spec_id)
            if not race or not cls or not spec:
                continue
            spec_combos.append({
                'race':    race['name'],
                'faction': race['faction'],
                'class':   cls['name'],
                'spec':    spec['name'],
                'count':   count,
                'pct':     round(count * 100.0 / sc_total, 2),
            })

    result = {
        'total':  combo_total,
        'combos': combos,
    }
    if specs_list:
        result['specs'] = specs_list
    if spec_combos:
        result['spec_combos'] = spec_combos

    return result


def export_demographics(snapshot_date: date = None, region: str = 'us') -> str | None:
    """
    Build and upload the demographics JSON. Returns the blob URL, or None on failure.
    """
    if not BLOB_READ_WRITE_TOKEN:
        logger.warning('BLOB_READ_WRITE_TOKEN not set — skipping export')
        return None

    if snapshot_date is None:
        snapshot_date = date.today()

    logger.info('Building demographics export...')
    races, classes, specs = _load_lookups()

    payload = {'updated': snapshot_date.isoformat()}
    for blob_key, db_context in CONTEXT_MAP.items():
        ctx_data = _build_all_for_context(db_context, races, classes, specs)
        payload[blob_key] = ctx_data

        if ctx_data is None:
            logger.info(f'  {blob_key}: no data')
            continue

        # Attach profession stats if available for this context
        prof_rpc = PROFESSION_RPC_MAP.get(blob_key)
        if prof_rpc:
            professions = _build_professions(prof_rpc, snapshot_date, region)
            if professions:
                ctx_data['professions'] = professions
                logger.info(f'  {blob_key}: {len(professions)} profession rows')
            else:
                logger.info(f'  {blob_key}: no profession data yet')

        n_specs = len(ctx_data.get('specs', []))
        n_sc    = len(ctx_data.get('spec_combos', []))
        n_prof  = len(ctx_data.get('professions', []))
        logger.info(
            f"  {blob_key}: {ctx_data['total']:,} characters, "
            f"{len(ctx_data['combos'])} combos, "
            f"{n_specs} spec rows, {n_sc} spec_combo rows, "
            f"{n_prof} profession rows"
        )

    json_bytes = json.dumps(payload, separators=(',', ':')).encode()
    logger.info(f'Uploading {len(json_bytes):,} bytes to Vercel Blob...')

    r = httpx.put(
        BLOB_UPLOAD_URL,
        content=json_bytes,
        headers={
            'Authorization': f'Bearer {BLOB_READ_WRITE_TOKEN}',
            'Content-Type': 'application/json',
            'x-add-random-suffix': '0',
        },
        timeout=30,
    )
    r.raise_for_status()

    blob_url = r.json().get('url')
    logger.info(f'Upload complete: {blob_url}')
    return blob_url
