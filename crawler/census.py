"""Phase 3: General population census via auction house → guild roster snowball (stub)."""
import logging

logger = logging.getLogger(__name__)


def crawl_census(region: str = 'us', realm_slug: str = None) -> None:
    # TODO Phase 3
    # 1. GET /data/wow/connected-realm/{id}/auctions — extract unique seller names
    # 2. For each seller: GET /profile/wow/character/{realm}/{name} — extract guild ref
    # 3. For each new guild: GET /data/wow/guild/{realm}/{name}/roster — bulk race/class data
    # 4. Track progress in crawl_state table for incremental resumption
    # 5. Run continuously in small batches (2-3 realms/day)
    logger.info('Census crawler not yet implemented (Phase 3)')
