import os
from dotenv import load_dotenv

load_dotenv()

# Optional at import time — only required when making Blizzard API calls.
# Using .get() so aggregate/export jobs can import config without Blizzard creds.
BLIZZARD_CLIENT_ID = os.environ.get('BLIZZARD_CLIENT_ID', '')
BLIZZARD_CLIENT_SECRET = os.environ.get('BLIZZARD_CLIENT_SECRET', '')

SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_SERVICE_KEY = os.environ['SUPABASE_SERVICE_KEY']

BLOB_READ_WRITE_TOKEN = os.environ.get('BLOB_READ_WRITE_TOKEN', '')

REGIONS = ['us']  # Add 'eu' in Phase 2

BLIZZARD_TOKEN_URL = 'https://oauth.battle.net/token'
BLIZZARD_API_BASE = {
    'us': 'https://us.api.blizzard.com',
    'eu': 'https://eu.api.blizzard.com',
}

# Stay safely under 100 req/s Blizzard rate limit
RATE_LIMIT_RPS = 50

# Skip character profile lookups if updated more recently than this.
# 48h (2 days) ensures M+ crawl (Wednesday) benefits from PvP cache (Tuesday)
# without requiring a full re-fetch of every character every week.
STALENESS_HOURS = 48

# Targeted realms for the general population census.
# Two clusters chosen for distinct community character:
#
#   rp      — dedicated RP servers; skews toward casual/social playstyle
#               Moon Guard, Wyrmrest Accord, Emerald Dream
#
#   general — the ten highest-population US realms; broad cross-section
#               of the active playerbase weighted toward Horde (Area 52,
#               Illidan, Mal'Ganis dominate US pop rankings)
#               Illidan, Area 52, Mal'Ganis, Zul'jin, Tichondrius,
#               Stormrage, Thrall, Ragnaros, Azralon
#               + one TBD slot (see FIXME below)
#
# Slugs must match the Blizzard API realm slug format exactly.
# Verify unknown slugs via: GET /data/wow/realm/{slug}?namespace=dynamic-us
CENSUS_TARGET_REALMS: dict[str, list[str]] = {
    'rp': [
        'moon-guard',
        'wyrmrest-accord',
        'emerald-dream',
    ],
    'general': [
        'illidan',
        'area-52',
        'malganis',    # Mal'Ganis
        'zuljin',      # Zul'jin
        'tichondrius',
        'stormrage',
        'sargeras',    # Sargeras
        'thrall',
        'ragnaros',    # Latin America
        'azralon',     # Latin America
    ],
}

# Manual guild seeds for realms that are underrepresented in PvP/M+ leaderboards.
# RP realms in particular have large active populations but few rated players,
# so the character-based seed produces almost nothing for them.
#
# Add well-known large guilds here — the roster crawl will fan out from these
# into hundreds of additional guilds via the guild_name field on discovered chars.
#
# Guild names must match the in-game name exactly (slugification is automatic).
MANUAL_GUILD_SEEDS: dict[str, list[str]] = {
    'moon-guard': [
        'Edict',
        'vibes',
        'Power Word Furry',
        'Ouro',
        'Heroes for Hire',
        'Vibe Police',
        'Rare Art Traders',
        'whos looting',
        'Frequent War Crimes',
        'Knowledge is Power',
        'The Pit',
        'Wolves of Emberstorm',
        'Marvelous Misadventures',
        'Key Components',
        'Interwoven',
        'Renaissance Reborn',
        'Women of Azeroth',
        'The Fashion Brigade',
        'Dark Intentions',
        'GLOBOFOMO',
    ],
    'wyrmrest-accord': [
        'Life',
        'With Valor',
        'Avoidable Damage',
        'Foxtail Caravan',
        'Sisu',
        'Felforged',
        'Darkwind',
        'Hand of Algalon',
        'Ungoon',
        'Halfway Sane',
        'Metric',
        'Tale of Tails',
        'Requiem',
        'Carrion',
        'Puzzle Box',
        'Mid Knights',
        'Crann Taca',
        'Prepot Tylenol',
        'Uncrowned',
        'Whisper',
    ],
    'emerald-dream': [
        'Nascent',
        'Spiral Out',
        'The Depraved',
        'Noble House',
        'Nascent [ Team 8 ]',
        'Scuffed',
        'Lucid',
        'Diversus',
        'Pluck',
        'Wicked Claw',
        'quacks',
        'Add Violence',
        'Yes Chef',
        'Absolute',
        'Ironsworn Regiment',
        'Conclave Of Cool',
        'Wayward Company',
        'Asgard',
        'All You Can Eat',
        'Clickers Anonymous',
    ],
}

# Skip profession re-fetches for characters that already have a profession
# snapshot within this many days. Profession choices are stable mid-season,
# so weekly is fine. This prevents re-fetching ~150k profession endpoints
# every time the crawler runs (since all characters are always profile-stale).
PROFESSION_STALENESS_DAYS = 7
