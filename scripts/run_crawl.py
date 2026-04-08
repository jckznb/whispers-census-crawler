"""
Main crawl entry point.

Usage:
    python -m scripts.run_crawl --phase pvp --region us
    python -m scripts.run_crawl --phase pvp --region us --no-aggregate
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
    parser.add_argument('--phase', required=True, choices=['pvp', 'mplus', 'raid', 'census'],
                        help='Which crawl phase to run')
    parser.add_argument('--region', default='us', choices=['us', 'eu'],
                        help='Blizzard API region')
    parser.add_argument('--date', default=None,
                        help='Snapshot date (YYYY-MM-DD). Defaults to today.')
    parser.add_argument('--no-aggregate', action='store_true',
                        help='Skip recomputing demographics_snapshot after crawl')
    parser.add_argument('--no-export', action='store_true',
                        help='Skip uploading demographics JSON to Vercel Blob')
    args = parser.parse_args()

    snapshot_date = date.fromisoformat(args.date) if args.date else date.today()

    if args.phase == 'pvp':
        from crawler.pvp import crawl_pvp
        crawl_pvp(region=args.region, snapshot_date=snapshot_date)
        if not args.no_aggregate:
            from crawler.aggregator import compute_pvp_snapshots
            compute_pvp_snapshots(region=args.region, snapshot_date=snapshot_date)
        if not args.no_export:
            from crawler.exporter import export_demographics
            export_demographics(snapshot_date=snapshot_date)

    elif args.phase == 'mplus':
        from crawler.mythic_plus import crawl_mythic_plus
        crawl_mythic_plus(region=args.region, snapshot_date=snapshot_date)
        if not args.no_aggregate:
            from crawler.aggregator import compute_mplus_snapshots
            compute_mplus_snapshots(region=args.region, snapshot_date=snapshot_date)
        if not args.no_export:
            from crawler.exporter import export_demographics
            export_demographics(snapshot_date=snapshot_date)

    elif args.phase == 'raid':
        from crawler.raid import crawl_raid
        crawl_raid(region=args.region)

    elif args.phase == 'census':
        from crawler.census import crawl_census
        crawl_census(region=args.region)

    logger.info('Crawl complete')


if __name__ == '__main__':
    main()
