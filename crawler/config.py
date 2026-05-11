import os
from dotenv import load_dotenv

load_dotenv()

BLIZZARD_CLIENT_ID     = os.environ.get('BLIZZARD_CLIENT_ID', '')
BLIZZARD_CLIENT_SECRET = os.environ.get('BLIZZARD_CLIENT_SECRET', '')
BLOB_READ_WRITE_TOKEN  = os.environ.get('BLOB_READ_WRITE_TOKEN', '')

BLIZZARD_TOKEN_URL = 'https://oauth.battle.net/token'
BLIZZARD_API_BASE  = {
    'us': 'https://us.api.blizzard.com',
    'eu': 'https://eu.api.blizzard.com',
}

# Stay safely under the 100 req/s Blizzard rate limit
RATE_LIMIT_RPS = 50

# ---------------------------------------------------------------------------
# General census guild configuration
#
# Store display names exactly as they appear in-game. The crawler slugifies
# them at runtime for the API endpoint URL. Realm keys must match Blizzard's
# realm slug format exactly.
#
# High-population realms (combined into general_latest.json)
# ---------------------------------------------------------------------------
GENERAL_GUILDS: dict[str, list[str]] = {
    'area-52': [
        'xD',
        'Infinity',
        'Idiot',
        'Vesper',
        'Twitch Prime',
        'Muscle Memory',
        'Unorganized',
        'YEP',
        'Stupid Fat Hobbits',
        'now watch this drive',
    ],
    'stormrage': [
        'Consequence',
        'SOMA',
        'Hollow Purple',
        'Parallax Gaming',
        'Slurp Squad',
        'Commit',
        'Chaotic Aftermath',
        'Remnants of Shadow',
        'Efficient',
        'The Meme Team',
    ],
    'illidan': [
        'Liquid',
        'velocity',
        'DMG',
        'Melee Mechanics',
        'Country Club',
        'Vibrant',
        'Style',
        'Warpath',
        'Squirrel Squad',
        'Just Woke Up',
    ],
    'zuljin': [
        'Refined',
        'vodka',
        'Might',
        'ohno',
        'Room Forty',
        'Tempo',
        'Reforged',
        'Bound',
        'FwF',
        'Children',
    ],
    'malganis': [
        'Instant Dollars',
        'gn',
        'nVus',
        'Pathogen',
        'Void',
        'slurp',
        'Calm Down',
        'Stormbound',
        'just sit me',
        'Decidedly Uncouth',
    ],
    'tichondrius': [
        'poptart corndoG',
        'Incarnate',
        'Nurfed',
        'Blur',
        'Unbalanced',
        'Defenstrate',
        'Nerd Rage',
        'Tab Target',
        'Snowblind',
        'Review The Data',
    ],
    'thrall': [
        'Literacy Test',
        'JGGBT',
        'Speakeasy',
        'Do Over',
        'SDS',
        'Notion',
        'Unrivaled',
        'Stoic',
        'The Silent Circle',
        'Definitely Skoot',
    ],
    'ragnaros': [
        'Ascended',
        'Southern Sea',
        'Finesse',
        'INSANO',
        'Pineapple',
        'No Simps',
        'Essentials',
        'Los Mercenarios',
        'Ethereal',
        'The Burning Seagull',
    ],
    'sargeras': [
        'Humble',
        'comma',
        'Vulgar',
        'No Skill',
        'Business Class',
        'The Family Business',
        'Monkey Mash',
        'Skill Issue',
        'Mid',
        'Hello Kitty Club',
    ],
    'proudmoore': [
        'TRK',
        'Availed',
        'Only Raiders',
        'Seer',
        'Valkyrie',
        'Primarch',
        'Pull On Two',
        'Eternal Kingdom',
        'Game OVer',
        'Retirement',
    ],
}

# ---------------------------------------------------------------------------
# RP realms (combined into general_rp_latest.json)
# ---------------------------------------------------------------------------
RP_GUILDS: dict[str, list[str]] = {
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
