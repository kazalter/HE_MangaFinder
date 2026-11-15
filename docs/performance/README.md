# MangaFinder Performance & Optimization Baselines

This directory stores reproducible performance baselines, profiling snapshots, and stage-by-stage regression comparisons for the backend optimization plan (`/mnt/hdd/HE_MangaFinder_Optimization_Plan.md`).

## How to Run the Fixed-Scenario Suite (`B0`–`B6`)

Always run against isolated `/dev/shm` temporary databases and directories, without loading production `.env`, data, or media directories:

```bash
cd /home/user1/Desktop/HE_MangaFinder
/home/user1/.cache/mangafinder-venvs/venv-api/bin/python scripts/benchmark_suite.py \
  --repeats 5 \
  --output docs/performance/baseline_t0.json
```

Use `--collector-python` to override the `apps/social_collector` Python interpreter if needed (defaults to `/home/user1/.cache/mangafinder-venvs/venv-collector/bin/python`).

## Scenario Definitions

| Scenario | Scope | What It Measures |
| :--- | :--- | :--- |
| `B0` | `apps/social_collector` startup & `/health` | Whether Chromium launches eagerly at idle startup, `chromium_launches`, process RSS (`collector_rss_kib`), `/health` payload fields |
| `B1` | Author discovery (`nh` + `wnacg`), repeated no-change scan | Cold vs warm discovery runtime (`wall_ms`, `cpu_ms`), `identity_lookups`, `list_requests`, `detail_requests`, `bytes_transferred`, `aggregation_executed`, `known_works`, `updated_works` |
| `B2` | Author discovery, multi-alias + 1 new work + changed fields | Incremental discovery correctness (`found`, `new`, `updated`, `b2_total_works`, `b2_total_events`), redundant request & aggregation counts |
| `B3` | X sync (`collector` + `SocialSyncService`) | Collector GraphQL wall/CPU time, `graphql_calls`, `identity_lookups`, `timeline_pages`, cursor pagination completeness & watermark continuity |
| `B4` | Cover fingerprint refresh (`CoverFingerprintRefreshService`, 200 works) | Cold batch (75), warm no-op (0 pending but full scan), and `force=True` full recompute (`cover_works_loaded`, `db_write_ms`, `wall_ms`, `cpu_ms`, `peak_rss_kib`) |
| `B5` | Social media WebP archive + OCR (`SocialMediaArchive`, `LocalMediaOcr`) | Decode/encode wall/CPU time, `media_bytes_downloaded`, `media_decode_count`, `peak_rss_kib`, EXIF rotation / alpha / corrupt image tolerance |
| `B6` | Fault injection | `429 Retry-After` honor & retry counters, transient SQLite lock retry recovery, interrupted `RUNNING` job recovery |

## Metric Hygiene & Security Rules

- Secrets (cookies, CSRF tokens, authorization headers, `storage_state` payloads) must never appear in metric logs or outputs.
- Volatile observability counters, timings, and RSS values must never be persisted into `raw_metadata` or change functional API contracts.
- Performance claims must record `repeats >= 5` (`median`, `min`, `max`, and raw arrays in the JSON report) alongside container idle stats when applicable.
