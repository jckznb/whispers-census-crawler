"""
Whispers Census crawl entry point.

Usage:
    python -m scripts.run_crawl --phase pvp
    python -m scripts.run_crawl --phase mplus
    python -m scripts.run_crawl --phase general
    python -m scripts.run_crawl --phase pvp --region eu --date 2026-05-11
"""
import argparse
import logging
import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s — %(message)s',
)

logger = logging.getLogger('run_crawl')


def main() -> None:
    parser = argparse.ArgumentParser(description='Whispers Census data crawler')
    parser.add_argument(
        '--phase', required=True, choices=['pvp', 'mplus', 'general'],
        help='Which crawl to run',
    )
    parser.add_argument(
        '--region', default='us', choices=['us', 'eu'],
        help='Blizzard API region (default: us)',
    )
    parser.add_argument(
        '--date', default=None,
        help='Snapshot date YYYY-MM-DD (default: today)',
    )
    args = parser.parse_args()

    snapshot_date = date.fromisoformat(args.date) if args.date else date.today()

    if args.phase == 'pvp':
        from crawler.pvp import crawl_pvp
        crawl_pvp(region=args.region, snapshot_date=snapshot_date)

    elif args.phase == 'mplus':
        from crawler.mplus import crawl_mplus
        crawl_mplus(region=args.region, snapshot_date=snapshot_date)

    elif args.phase == 'general':
        from crawler.general import crawl_general
        crawl_general(region=args.region, snapshot_date=snapshot_date)

    logger.info('Done')


if __name__ == '__main__':
    main()
