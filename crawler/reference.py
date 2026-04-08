"""Fetch and seed reference data (races, classes, specs, realms) from Blizzard Game Data API."""
import logging
from . import client as api
from . import db

logger = logging.getLogger(__name__)


def seed_races(region: str = 'us') -> None:
    index = api.get('/data/wow/playable-race/index', region=region, namespace=f'static-{region}')
    if not index:
        logger.error('Could not fetch race index')
        return

    rows = []
    for race_ref in index['races']:
        detail = api.get(f'/data/wow/playable-race/{race_ref["id"]}', region=region, namespace=f'static-{region}')
        if not detail:
            continue
        faction_data = detail.get('faction')
        faction = faction_data['type'].lower() if faction_data else 'neutral'
        rows.append({'id': detail['id'], 'name': detail['name'], 'faction': faction})

    db.upsert('races', rows)
    logger.info(f'Seeded {len(rows)} races')


def seed_classes(region: str = 'us') -> None:
    index = api.get('/data/wow/playable-class/index', region=region, namespace=f'static-{region}')
    if not index:
        logger.error('Could not fetch class index')
        return

    rows = [{'id': c['id'], 'name': c['name']} for c in index['classes']]
    db.upsert('classes', rows)
    logger.info(f'Seeded {len(rows)} classes')


def seed_specs(region: str = 'us') -> None:
    index = api.get('/data/wow/playable-specialization/index', region=region, namespace=f'static-{region}')
    if not index:
        logger.error('Could not fetch spec index')
        return

    rows = []
    for spec_ref in index['character_specializations']:
        detail = api.get(
            f'/data/wow/playable-specialization/{spec_ref["id"]}',
            region=region,
            namespace=f'static-{region}',
        )
        if not detail:
            continue
        role = detail.get('role', {}).get('type', 'DPS').lower()
        rows.append({
            'id': detail['id'],
            'name': detail['name'],
            'class_id': detail['playable_class']['id'],
            'role': role,
        })

    db.upsert('specs', rows)
    logger.info(f'Seeded {len(rows)} specs')


def seed_realms(region: str = 'us') -> None:
    index = api.get('/data/wow/connected-realm/index', region=region, namespace=f'dynamic-{region}')
    if not index:
        logger.error('Could not fetch connected realm index')
        return

    rows = []
    for cr_ref in index['connected_realms']:
        # Extract connected realm ID from the href URL
        cr_id = int(cr_ref['href'].split('/connected-realm/')[1].split('?')[0])
        detail = api.get(f'/data/wow/connected-realm/{cr_id}', region=region, namespace=f'dynamic-{region}')
        if not detail:
            continue
        for realm in detail.get('realms', []):
            rows.append({
                'id': realm['id'],
                'name': realm['name'],
                'slug': realm['slug'],
                'connected_realm_id': cr_id,
                'region': region,
            })

    db.upsert('realms', rows)
    logger.info(f'Seeded {len(rows)} realms for {region}')


def seed_all(region: str = 'us') -> None:
    logger.info(f'Seeding all reference data for region={region}')
    seed_classes(region)   # classes before specs (FK dependency)
    seed_specs(region)
    seed_races(region)
    seed_realms(region)
    logger.info('Reference data seeding complete')
