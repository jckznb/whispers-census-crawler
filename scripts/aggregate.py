"""
Recompute demographics_snapshot tables without re-crawling.
Use this if you want to rebuild snapshots from existing raw data
(e.g., after changing aggregation logic).

Usage:
    python -m scripts.aggregate --phase pvp --region us
    python -m scripts.aggregate --phase pvp --region us --date 2025-04-01
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


def main() -> None:
    parser = argparse.ArgumentParser(description='Recompute Whispers Census demographic snapshots')
    parser.add_argument('--phase', required=True, choices=['pvp', 'mplus', 'raid'],
                        help='Which context to recompute')
    parser.add_argument('--region', default='us', choices=['us', 'eu'])
    parser.add_argument('--date', default=None,
                        help='Snapshot date (YYYY-MM-DD). Defaults to today.')
    args = parser.parse_args()

    snapshot_date = date.fromisoformat(args.date) if args.date else date.today()

    if args.phase == 'pvp':
        from crawler.aggregator import compute_pvp_snapshots
        compute_pvp_snapshots(region=args.region, snapshot_date=snapshot_date)
    else:
        print(f'Phase {args.phase} aggregation not yet implemented')


if __name__ == '__main__':
    main()
