"""
Main crawl entry point.

Usage:
    python -m scripts.run_crawl --phase pvp --region us
    python -m scripts.run_crawl --phase pvp --region us --leaderboard-only --no-aggregate --no-export
    python -m scripts.run_crawl --phase pvp --region us --enrich-only --no-aggregate --no-export
    python -m scripts.run_crawl --phase pvp --region us --aggregate-only
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
    parser.add_argument('--aggregate-only', action='store_true',
                        help='Skip crawling; run aggregate + export only')

    # Split-job flags for pvp/mplus — run leaderboard fetch and character enrichment
    # as separate GHA jobs so each has its own independent 6-hour timeout.
    parser.add_argument('--leaderboard-only', action='store_true',
                        help='Fetch leaderboard data and store runs/entries with character stubs; '
                             'skip full profile enrichment. Fast (~5-15 min).')
    parser.add_argument('--enrich-only', action='store_true',
                        help='Re-fetch leaderboard to get char list, then resolve stale profiles '
                             'and professions; skip storing runs/entries. Slow (up to 5 hr).')

    # Census-specific args
    parser.add_argument('--mode', default='roster',
                        choices=['seed', 'roster', 'all'],
                        help='Census mode: seed (build guild queue), roster (crawl guilds), all (both)')
    parser.add_argument('--batch-size', type=int, default=200,
                        help='Number of guilds to crawl per census roster run (default: 200)')

    args = parser.parse_args()

    # Validate mutually exclusive split-job flags
    if args.leaderboard_only and args.enrich_only:
        parser.error('--leaderboard-only and --enrich-only are mutually exclusive')
    if args.aggregate_only and (args.leaderboard_only or args.enrich_only):
        parser.error('--aggregate-only cannot be combined with --leaderboard-only or --enrich-only')

    snapshot_date = date.fromisoformat(args.date) if args.date else date.today()

    if args.phase == 'pvp':
        if not args.aggregate_only:
            from crawler.pvp import crawl_pvp
            crawl_pvp(
                region=args.region,
                snapshot_date=snapshot_date,
                leaderboard_only=args.leaderboard_only,
                enrich_only=args.enrich_only,
            )
        if not args.no_aggregate:
            from crawler.aggregator import compute_pvp_snapshots
            compute_pvp_snapshots(region=args.region, snapshot_date=snapshot_date)
        if not args.no_export:
            from crawler.exporter import export_demographics
            export_demographics(snapshot_date=snapshot_date, region=args.region)

    elif args.phase == 'mplus':
        if not args.aggregate_only:
            from crawler.mythic_plus import crawl_mythic_plus
            crawl_mythic_plus(
                region=args.region,
                snapshot_date=snapshot_date,
                leaderboard_only=args.leaderboard_only,
                enrich_only=args.enrich_only,
            )
        if not args.no_aggregate:
            from crawler.aggregator import compute_mplus_snapshots
            compute_mplus_snapshots(region=args.region, snapshot_date=snapshot_date)
        if not args.no_export:
            from crawler.exporter import export_demographics
            export_demographics(snapshot_date=snapshot_date, region=args.region)

    elif args.phase == 'raid':
        from crawler.raid import crawl_raid
        crawl_raid(region=args.region)

    elif args.phase == 'census':
        from crawler.census import crawl_census, aggregate_general
        if not args.aggregate_only:
            crawl_census(
                region=args.region,
                snapshot_date=snapshot_date,
                mode=args.mode,
                batch_size=args.batch_size,
            )
        if not args.no_aggregate:
            aggregate_general(region=args.region, snapshot_date=snapshot_date)
        if not args.no_export:
            from crawler.exporter import export_demographics
            export_demographics(snapshot_date=snapshot_date, region=args.region)

    logger.info('Crawl complete')


if __name__ == '__main__':
    main()
