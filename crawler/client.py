"""Blizzard API HTTP client with rate limiting and retry logic."""
import asyncio
import time
import logging
import httpx
from .auth import get_token
from .config import BLIZZARD_API_BASE, RATE_LIMIT_RPS

logger = logging.getLogger(__name__)

_last_request_time = 0.0
_min_interval = 1.0 / RATE_LIMIT_RPS


def _throttle() -> None:
    global _last_request_time
    now = time.monotonic()
    wait = _min_interval - (now - _last_request_time)
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.monotonic()


def get(
    path: str,
    region: str = 'us',
    namespace: str = None,
    params: dict = None,
    retries: int = 5,
) -> dict | None:
    """
    GET a Blizzard API endpoint.

    Returns parsed JSON on success, None on 404.
    Raises on persistent errors.
    """
    base = BLIZZARD_API_BASE[region]
    all_params = dict(params or {})
    if namespace:
        all_params['namespace'] = namespace
    all_params['locale'] = 'en_US'

    for attempt in range(retries):
        _throttle()
        token = get_token(region)
        try:
            resp = httpx.get(
                f'{base}{path}',
                headers={'Authorization': f'Bearer {token}'},
                params=all_params,
                timeout=30,
            )

            if resp.status_code == 404:
                logger.debug(f'404 {path} — skipping')
                return None

            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning(f'Rate limited on {path}, sleeping {wait}s')
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPStatusError as exc:
            if attempt == retries - 1:
                logger.error(f'HTTP {exc.response.status_code} on {path} after {retries} attempts')
                raise
            wait = 2 ** attempt
            logger.warning(f'HTTP {exc.response.status_code} on {path}, retry in {wait}s')
            time.sleep(wait)

        except httpx.RequestError as exc:
            if attempt == retries - 1:
                logger.error(f'Request error on {path}: {exc}')
                raise
            wait = 2 ** attempt
            logger.warning(f'Request error on {path}: {exc}, retry in {wait}s')
            time.sleep(wait)

    return None


async def async_get(
    client: httpx.AsyncClient,
    path: str,
    region: str = 'us',
    namespace: str = None,
    params: dict = None,
    retries: int = 7,
) -> dict | None:
    """
    Async GET a Blizzard API endpoint. Uses shared AsyncClient for connection pooling.

    Returns parsed JSON on success, None on 404.
    Raises on persistent errors.
    """
    base = BLIZZARD_API_BASE[region]
    all_params = dict(params or {})
    if namespace:
        all_params['namespace'] = namespace
    all_params['locale'] = 'en_US'

    for attempt in range(retries):
        token = get_token(region)  # sync, cached — safe to call from async context
        try:
            resp = await client.get(
                f'{base}{path}',
                headers={'Authorization': f'Bearer {token}'},
                params=all_params,
                timeout=30,
            )

            if resp.status_code == 404:
                logger.debug(f'404 {path} — skipping')
                return None

            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning(f'Rate limited on {path}, sleeping {wait}s')
                await asyncio.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPStatusError as exc:
            if attempt == retries - 1:
                logger.error(f'HTTP {exc.response.status_code} on {path} after {retries} attempts')
                raise
            wait = 2 ** attempt
            logger.warning(f'HTTP {exc.response.status_code} on {path}, retry in {wait}s')
            await asyncio.sleep(wait)

        except httpx.RequestError as exc:
            if attempt == retries - 1:
                logger.error(f'Request error on {path}: {exc}')
                raise
            wait = 2 ** attempt
            logger.warning(f'Request error on {path}: {exc}, retry in {wait}s')
            await asyncio.sleep(wait)

    return None
