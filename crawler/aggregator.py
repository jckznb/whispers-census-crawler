"""Compute precomputed demographics_snapshot from raw crawl data."""
import logging
from datetime import date
from . import db
from .pvp import get_current_season

logger = logging.getLogger(__name__)

# (context_name, bracket_LIKE_pattern)
# LIKE pattern: exact string matches itself, 'shuffle%' matches all solo shuffle brackets
PVP_CONTEXTS = [
    ('pvp_2v2',     '2v2'),
    ('pvp_3v3',     '3v3'),
    ('pvp_rbg',     'rbg'),
    ('pvp_shuffle', 'shuffle%'),
    ('pvp_all',     None),   # None = no bracket filter
]


def compute_pvp_snapshots(
    region: str = 'us',
    snapshot_date: date = None,
    season_id: int = None,
) -> None:
    if snapshot_date is None:
        snapshot_date = date.today()
    if season_id is None:
        season_id = get_current_season(region)

    logger.info(f'Computing PvP snapshots for season={season_id} date={snapshot_date} region={region}')

    for context, bracket_pattern in PVP_CONTEXTS:
        db.rpc('compute_demographics_snapshot', {
            'p_context': context,
            'p_bracket_pattern': bracket_pattern,
            'p_season_id': season_id,
            'p_snapshot_date': snapshot_date.isoformat(),
            'p_region': region,
        })
        logger.info(f'  Computed snapshot: {context}')

    logger.info('PvP snapshot computation complete')


def compute_mplus_snapshots(
    region: str = 'us',
    snapshot_date: date = None,
    min_keystone: int = 0,
) -> None:
    if snapshot_date is None:
        snapshot_date = date.today()

    logger.info(f'Computing M+ snapshots for date={snapshot_date} region={region} min_keystone={min_keystone}')

    db.rpc('compute_mplus_demographics_snapshot', {
        'p_snapshot_date': snapshot_date.isoformat(),
        'p_region': region,
        'p_min_keystone': min_keystone,
    })
    logger.info('M+ snapshot computation complete')
