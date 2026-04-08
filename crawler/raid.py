"""Phase 2: Mythic raid leaderboard crawler (stub — implemented in Sprint 3)."""
import logging

logger = logging.getLogger(__name__)


def crawl_raid(region: str = 'us') -> None:
    # TODO Phase 2
    # 1. GET /data/wow/mythic-raid-leaderboard/{raid}/{faction} for current tier
    # 2. Fetch guild rosters for each top guild
    # 3. Store in raid_characters table
    logger.info('Raid crawler not yet implemented (Phase 2)')
