"""OAuth 2.0 Client Credentials token management for the Blizzard API."""
import time
import logging
import httpx
from .config import BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET, BLIZZARD_TOKEN_URL

logger = logging.getLogger(__name__)

# In-memory cache keyed by region
_token_cache: dict[str, dict] = {}


def get_token(region: str = 'us') -> str:
    """Return a valid access token, fetching a new one if needed."""
    now = time.time()
    cached = _token_cache.get(region)

    # Refresh 60 seconds before actual expiry to avoid edge cases
    if cached and cached['expires_at'] > now + 60:
        return cached['access_token']

    logger.debug(f'Fetching new Blizzard OAuth token for region={region}')
    resp = httpx.post(
        BLIZZARD_TOKEN_URL,
        data={'grant_type': 'client_credentials'},
        auth=(BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    _token_cache[region] = {
        'access_token': data['access_token'],
        'expires_at': now + data['expires_in'],
    }
    logger.info(f'Obtained new token for region={region}, expires in {data["expires_in"]}s')
    return data['access_token']
