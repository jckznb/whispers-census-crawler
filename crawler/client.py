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


class AsyncRateLimiter:
    """Token-bucket rate limiter for async contexts.

    Ensures at most `rate` requests per second are started across all
    concurrent tasks sharing this limiter. The internal lock serializes
    the throttle check so bursts of tasks space themselves out rather
    than all firing at once and triggering Blizzard 429s.

    Create one instance per asyncio.run() context and pass it through
    to every async_get() call.
    """

    def __init__(self, rate: float = RATE_LIMIT_RPS) -> None:
        self._min_interval = 1.0 / rate
        self._lock = asyncio.Lock()
        self._last: float = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()


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
    limiter: 'AsyncRateLimiter | None' = None,
) -> dict | None:
    """
    Async GET a Blizzard API endpoint. Uses shared AsyncClient for connection pooling.

    Pass an AsyncRateLimiter instance via `limiter` to enforce the global rate
    limit across all concurrent tasks. Without it, concurrent tasks can flood
    the Blizzard API and trigger 429 cascades.

    Returns parsed JSON on success, None on 404.
    Raises on persistent errors.
    """
    base = BLIZZARD_API_BASE[region]
    all_params = dict(params or {})
    if namespace:
        all_params['namespace'] = namespace
    all_params['locale'] = 'en_US'

    for attempt in range(retries):
        if limiter:
            await limiter.acquire()
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
