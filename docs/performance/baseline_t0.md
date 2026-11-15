# T0 Performance Baseline (`baseline_t0`)

- **Baseline Commit**: `23dc528a287234376d15eab8cc02d7fc566d568f` (instrumentation added on branch `perf/optimization-plan`)
- **Generated At (UTC)**: `2026-11-15T11:17:29.758554+00:00`
- **Repeats per Benchmark**: `5`
- **Raw JSON**: [baseline_t0.json](baseline_t0.json)

## 1. Host & Production Container Environment

- **Host**: Linux `6.8.0-149-generic` (`x86_64`), Intel Core i5-4590S @ 3.00GHz (4 CPUs), 7.6 GiB RAM + 4.0 GiB Swap
- **Deployment Compose Profiles**: `COMPOSE_PROFILES=social` (`/opt/stacks/mangafinder/compose.yaml`)
- **`mangafinder-mangafinder-1` (`apps/api`)**:
  - Runtime: Python 3.12.16, FastAPI 0.162.2, SQLAlchemy 2.1.8, httpx 0.28.1, Pillow 12.3.0, sqlite3 3.46.1
  - Observed Idle Footprint: `~90.57 MiB` RAM, `11` PIDs
- **`mangafinder-social-collector-1` (`apps/social_collector`)**:
  - Runtime: Python 3.12.13, FastAPI 0.139.2, curl_cffi 0.15.0, playwright 1.55.0
  - Observed Idle Footprint: `~109.1 MiB` RAM, `108` PIDs (`uvicorn` 32 MB RSS + `node` 22.5 MB RSS + 8 Chromium/crashpad processes)

## 2. Verification Baseline (T0)

- **Baseline (`23dc528`) Tests**:
  - `apps/api`: `220 passed in 14.29s`
  - `apps/social_collector`: `6 passed in 0.79s`
- **Post-T0 Instrumentation Tests**:
  - `apps/api`: `222 passed in 16.33s`
  - `apps/social_collector`: `7 passed in 0.40s`
  - `ruff check`: `0` errors on `apps/api/app`, `apps/api/tests/test_observability.py`, `apps/social_collector/app`, `apps/social_collector/tests/test_observability.py`

## 3. Fixed-Scenario Benchmark Summary (`B0`–`B6`)

### `B0` — Social Collector Idle Startup & `/health`
- `health_status`: `"ok"`, `graphql_session_present`: `true`
- `browser_started`: `true`, `chromium_launches`: `1`, `mock_launch_calls`: `1`
- `collector_rss_kib` (with mocked Playwright browser object): `52,396 KiB`
- **Observation**: Even without browser-dependent requests, `BrowserCollector.start()` is invoked unconditionally in `lifespan`, explaining the 108 PIDs / Chromium process tree seen in production.

### `B1` & `B2` — Author Discovery (`nh` + `wnacg`, Multi-Alias & Incremental Update)
- **`B1` Cold Scan (15 works)**:
  - `identity_lookups`: `3`, `list_requests`: `6`, `detail_requests`: `24`, `bytes_transferred`: `22,464`
  - `aggregation_executed`: `15`, `new_works`: `15`, `candidate_comparisons`: `5`
- **`B1` Warm Repeat (No-Change Scan)**:
  - `wall_ms`: median `420.088 ms` (min `385.11 ms`, max `481.564 ms`)
  - `cpu_ms`: median `419.935 ms` (min `385.085 ms`, max `481.317 ms`)
  - `identity_lookups`: `6`, `list_requests`: `11`, `detail_requests`: `48`, `bytes_transferred`: `43,283`
  - `aggregation_executed`: `25`, `known_works`: `21`, `updated_works`: `4`
  - **Observation**: Repeated no-change runs re-run artist identity resolution, re-fetch all detail pages, and re-trigger aggregation on unchanged items.
- **`B2` Incremental Update + Alias Scan**:
  - `result`: `found=16`, `new=1`, `updated=4`, `excluded=0`, `status="succeeded"`
  - `identity_lookups`: `4`, `list_requests`: `7`, `detail_requests`: `32`, `bytes_transferred`: `29,295`
  - `aggregation_executed`: `16`, `b2_total_works`: `16`, `b2_total_events`: `26`

### `B3` — X Sync (`WebGraphQLCollector` + `SocialSyncService`)
- **Collector `posts_batch` Replay (`repeats=5`)**:
  - `wall_ms`: median `1.066 ms` (min `1.034 ms`, max `1.323 ms`)
  - `cpu_ms`: median `1.067 ms` (min `1.035 ms`, max `1.323 ms`)
  - `identity_lookups`: `2`, `graphql_calls`: `4`, `bytes_transferred`: `1,794`, `timeline_pages`: `2`
  - Pagination / Completeness: `b1_ids=["205", "204", "100"]` (`complete=false`), `b2_ids=["204", "203"]` (`complete=true`)
- **API `SocialSyncService` Two-Step Resume**:
  - Step 1: `fetched=2`, `created=2`, `queued=2`, `complete=false`
  - Step 2: `fetched=1`, `created=1`, `queued=1`, `complete=true`
  - Final state: `api_saved_posts=3`, `api_final_watermark="205"`

### `B4` — Cover Fingerprint Maintenance (`200` Works)
- **Cold First Batch (`limit=75`)**:
  - `wall_ms`: `4,297.256 ms`, `cpu_ms`: `4,218.5 ms`
  - `cover_works_loaded`: `200` (loads all rows to select batch), `cover_works_refreshed`: `75`, `remaining`: `125`, `db_write_ms`: `55`
- **Warm No-Op (`pending=0`)**:
  - `wall_ms`: median `296.094 ms` (min `262.892 ms`, max `318.992 ms`)
  - `cpu_ms`: median `284.446 ms` (min `258.917 ms`, max `314.82 ms`)
  - `cover_works_loaded`: `200`, `cover_works_refreshed`: `0`
- **`force=True` Full Recompute (`200` works)**:
  - `wall_ms`: median `11,164.921 ms` (min `11,054.77 ms`, max `12,004.784 ms`)
  - `cpu_ms`: median `11,156.528 ms` (min `11,037.936 ms`, max `11,656.556 ms`)
  - `cover_works_loaded`: `200`, `cover_works_refreshed`: `200`, `db_write_ms`: `136`, `peak_rss_kib`: `94,356 KiB`
  - **Observation**: Full-table load happens on every run/no-op, and pure-Python 2D DCT accounts for >11s of CPU time per 200 covers (`>55 ms` per cover).

### `B5` — Social Media Archive + OCR
- Input fixtures: `valid.jpg` (`133,264 B`), `rotated.jpg` (`15,664 B`), `trans.png` (`5,153 B`), `corrupt.jpg` (`27 B`)
- Archived output dimensions: `[[800, 1200], [900, 900], [1200, 1600]]` (EXIF orientation and alpha channels preserved; corrupt file tolerated)
- `ocr_status`: `"retry"` (graceful retry state in local environment)
- `wall_ms`: median `1,152.52 ms` (min `1,139.675 ms`, max `1,175.296 ms`)
- `cpu_ms`: median `1,152.423 ms` (min `1,139.443 ms`, max `1,175.123 ms`)
- `media_bytes_downloaded`: `154,108`, `media_decode_count`: `4`, `peak_rss_kib`: `153,428 KiB`

### `B6` — Fault Injection
- **HTTP 429 `Retry-After`**: `rate_limit_works=1`, `rate_limited_429=1`, `retries=1`, `rate_limit_sleep_delays=[2.0]`
- **Job Recovery**: `jobs_recovered=1` (`RUNNING -> PENDING`)
- **Transient SQLite Lock**: `db_lock_retries=1`, `sqlite_lock_refreshed=1`
