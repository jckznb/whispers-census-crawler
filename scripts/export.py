"""
Re-export the demographics JSON blob from existing Supabase data.
Use this after changing exporter logic without needing to re-crawl.

Usage:
    python -m scripts.export
    python -m scripts.export --date 2026-04-02
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
    parser = argparse.ArgumentParser(description='Re-export Whispers Census demographics blob')
    parser.add_argument('--date', default=None,
                        help='Snapshot date label (YYYY-MM-DD). Defaults to today.')
    parser.add_argument('--region', default='us',
                        help='Region for profession stats RPC (default: us)')
    args = parser.parse_args()

    snapshot_date = date.fromisoformat(args.date) if args.date else date.today()

    from crawler.exporter import export_demographics
    url = export_demographics(snapshot_date=snapshot_date, region=args.region)
    if url:
        print(f'\nBlob URL: {url}')


if __name__ == '__main__':
    main()
