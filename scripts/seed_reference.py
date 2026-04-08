"""
Populate reference tables (races, classes, specs, realms) from the Blizzard Game Data API.
Run once before the first crawl, then again after major patches.

Usage:
    python -m scripts.seed_reference
    python -m scripts.seed_reference --region eu
"""
import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from crawler.reference import seed_all

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s — %(message)s',
)


def main() -> None:
    parser = argparse.ArgumentParser(description='Seed WoW reference data into Supabase')
    parser.add_argument('--region', default='us', choices=['us', 'eu'], help='Blizzard API region')
    args = parser.parse_args()

    seed_all(region=args.region)


if __name__ == '__main__':
    main()
