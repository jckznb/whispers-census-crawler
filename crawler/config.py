import os
from dotenv import load_dotenv

load_dotenv()

BLIZZARD_CLIENT_ID = os.environ['BLIZZARD_CLIENT_ID']
BLIZZARD_CLIENT_SECRET = os.environ['BLIZZARD_CLIENT_SECRET']

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

# Skip character profile lookups if updated more recently than this
STALENESS_HOURS = 24
