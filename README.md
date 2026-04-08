# Whispers Census — Crawler

Data pipeline for [whisperscensus.com](https://whisperscensus.com) — a WoW character demographics visualization app. This repo handles all data collection: Blizzard API crawling, Supabase writes, and pre-aggregated JSON export to Vercel Blob.

The frontend lives in a separate private repo. This crawler is public so GitHub Actions can run scheduled crawls with unlimited free minutes.

## What it does

Pulls character demographic data (race, class, spec) from the Blizzard API across three contexts:

| Phase | Source | Status |
|-------|--------|--------|
| PvP | Rated PvP leaderboards (2v2, 3v3, RBG, Solo Shuffle) | Live |
| Mythic+ | Mythic Keystone dungeon leaderboards | Live |
| General Population | Guild roster snowball from discovered characters | In progress |

Crawled data flows: **Blizzard API → Supabase (raw) → aggregation → Vercel Blob (pre-aggregated JSON) → frontend**

## Automated schedules

| Workflow | Schedule | Notes |
|----------|----------|-------|
| `pvp-crawl.yml` | Weekly, Tuesday 16:00 UTC | After NA weekly reset |
| `mplus-crawl.yml` | Weekly, Wednesday 16:00 UTC | Day after reset, leaderboards populate |
| `census-crawl.yml` | Daily 06:00 UTC | Disabled — uncomment when census.py is ready |

All workflows also have a manual trigger (Actions tab → Run workflow).

## Setup

### 1. Clone and install

```bash
git clone https://github.com/your-username/whispers-census-crawler
cd whispers-census-crawler
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Fill in your values — see .env.example for where to find each one
```

### 3. GitHub Actions secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

Add all five secrets from your `.env`:
- `BLIZZARD_CLIENT_ID`
- `BLIZZARD_CLIENT_SECRET`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `BLOB_READ_WRITE_TOKEN`

## Usage

### Run a crawl manually

```bash
# PvP — crawl + aggregate + export blob
python -m scripts.run_crawl --phase pvp --region us

# Mythic+ — crawl + aggregate + export blob
python -m scripts.run_crawl --phase mplus --region us

# Skip re-crawling, just re-export the blob from existing Supabase data
python -m scripts.export

# Skip re-crawling, just re-export with a specific snapshot date label
python -m scripts.export --date 2026-04-02
```

### Seed reference data (run once after DB migration)

```bash
python -m scripts.seed_reference --region us
```

## Project structure

```
crawler/
  auth.py          OAuth token management (Blizzard client credentials)
  client.py        Blizzard API HTTP client — rate limiting, retries
  config.py        Credentials and constants from .env
  db.py            Supabase PostgREST client (httpx, no supabase-py)
  pvp.py           Phase 1: PvP leaderboard crawler
  mythic_plus.py   Phase 2: M+ leaderboard crawler
  census.py        Phase 3: Guild roster snowball (in progress)
  aggregator.py    Computes demographics_snapshot from raw data
  exporter.py      Builds and uploads the demographics JSON blob
  characters.py    Character profile lookups with dedup and batching
  reference.py     Seeder for races, classes, specs, realms

scripts/
  run_crawl.py     CLI entry point for full crawl phases
  aggregate.py     Recompute snapshots without re-crawling
  export.py        Re-export blob from existing Supabase data
  seed_reference.py  Populate reference tables from Blizzard Game Data API
```

## Attribution

Data provided by Blizzard Entertainment. Whispers Census is not affiliated with Blizzard Entertainment.
