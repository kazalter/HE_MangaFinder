# HE_MangaFinder Performance Optimization Report (`perf/optimization-plan`)

- **Branch**: `perf/optimization-plan`
- **Baseline Reference**: `23dc528a287234376d15eab8cc02d7fc566d568f` + T0 instrumentation [`10b25d1`](../../scripts/benchmark_suite.py) ([`baseline_t0.md`](baseline_t0.md), [`baseline_t0.json`](baseline_t0.json))
- **Post-Optimization Benchmark**: [`after_t6.json`](after_t6.json) (recorded at commit `7d01f8f703de69166a0904d7ff77541d3d11e80d`, `repeats = 5`)
- **Benchmark Entry**: [`scripts/benchmark_suite.py`](../../scripts/benchmark_suite.py) (isolated `/dev/shm` temp directories, production `.env` and `MANGAFINDER_*` / `SOCIAL_COLLECTOR_*` env vars cleared)

---

## 1. Completed Optimization Scope (T0–T7)

| Task | Commit | Summary of Changes & Actual Scope |
| --- | --- | --- |
| **T0** Baseline & Observability | `10b25d1` | Added [`apps/api/app/core/observability.py`](../../apps/api/app/core/observability.py), [`apps/social_collector/app/observability.py`](../../apps/social_collector/app/observability.py), [`scripts/benchmark_suite.py`](../../scripts/benchmark_suite.py), and [`baseline_t0.json`](baseline_t0.json). |
| **T1** Lazy Chromium in `social_collector` | `876112d` | Deferred `BrowserCollector.start()` in [`apps/social_collector/app/main.py`](../../apps/social_collector/app/main.py) until a browser endpoint or fallback actually requires Chromium; `/health` and GraphQL-only syncs do not launch Chromium. |
| **T2** X Sync Reuse, Caches & Budgets | `88e8fc0` | Added persistent GraphQL session reuse, bounded user-identity cache, credential hash invalidation, bounded worker concurrency (`SOCIAL_COLLECTOR_GRAPHQL_WORKERS=4`), and end-to-end batch budget + cancellation propagation across `social_collector` (`SOCIAL_COLLECTOR_BROWSER_BATCH_BUDGET_SECONDS`) and `apps/api` (`social_collector_batch_budget_seconds=65.0`, `social_collector_request_timeout_seconds=95.0`). |
| **T3** Incremental Catalog Discovery | `21862cc` | Added [`apps/api/app/modules/catalog/incremental.py`](../../apps/api/app/modules/catalog/incremental.py), list-first incremental scan with detail TTL cache (`catalog_detail_cache_ttl_hours=168`), identity cache (`catalog_identity_cache_ttl_hours=168`), overlap-window stop logic (`catalog_recent_scan_overlap_pages=2`), bounded deep reconcile (`catalog_deep_scan_max_pages_per_run=3`), and missing-field preservation in [`apps/api/app/modules/catalog/repository.py`](../../apps/api/app/modules/catalog/repository.py). |
| **T4** Author Aggregation Invalidation | `b4b9f1a` | Added `AuthorAggregationState` and [`apps/api/app/modules/catalog/invalidation.py`](../../apps/api/app/modules/catalog/invalidation.py) with snapshot-revision guarded completion, batch candidate loading, and skip logic when author inputs and algorithm version are unchanged (`agg-v4`). |
| **T5** Media Memory & OCR Lifecycle | `9bea934` | Added [`apps/api/app/modules/social/media_io.py`](../../apps/api/app/modules/social/media_io.py) with cross-loop `AsyncConcurrencyGate` (`social_media_decode_concurrency=2`, `social_ocr_concurrency=2`), chunk-level streaming size limits (`social_ocr_max_image_bytes=32 MiB`), pre-decode pixel limits (`social_media_max_pixels=40_000_000`, `social_ocr_max_image_pixels=40_000_000`), `SpooledTemporaryFile` + thumbnail-first EXIF transpose, and Tesseract subprocess kill+reap on timeout/cancellation in [`apps/api/app/modules/social/ocr.py`](../../apps/api/app/modules/social/ocr.py), plus OCR cache invalidation on media change and `_merge_ocr_text` on partial retry. |
| **T6** Cover Maintenance Keyset & Resume | `7d01f8f` | Rewrote [`CoverFingerprintRefreshService`](../../apps/api/app/modules/catalog/cover_maintenance.py) with `work_id` keyset pagination, scalar column projection, batch `WorkSource` loading, durable `CoverMaintenanceState` checkpoints bounded by `max_work_id`, bounded per-round retries (`3`) + `24h` cooldown (`cover_failure_count`, `cover_next_retry_at`, `cover_failed_url`), conditional URL writeback, T4 author invalidation on updates, and `has_more` continuation in [`apps/api/app/modules/jobs/worker.py`](../../apps/api/app/modules/jobs/worker.py). |
| **T7** Conditional Rust Pilot | Exited | Profiled `cover_fingerprint.py` and evaluated host/container toolchains; trigger conditions were not met (see Section 5). Python implementation retained. |

---

## 2. Environment, Dataset Scale & Cache Conditions

- **Host**: Linux `6.8.0-149-generic` (`x86_64`), Intel Core i5-4590S @ 3.00GHz (4 CPUs), 7.6 GiB RAM + 4.0 GiB Swap
- **API Python Env** (`/home/user1/.cache/mangafinder-venvs/venv-api/bin/python`): Python `3.12.3`, SQLite `3.45.1`, `fastapi 0.163.1`, `httpx 0.28.1`, `pillow 12.3.0`, `pydantic-settings 2.16.3`, `sqlalchemy 2.1.8`, `uvicorn 0.54.0`, `pytest 8.4.2`
- **Collector Python Env** (`/home/user1/.cache/mangafinder-venvs/venv-collector/bin/python`): Python `3.12.3`, `fastapi 0.163.1`, `curl_cffi 0.17.0`, `playwright 1.55.0`, `uvicorn 0.54.0`, `pytest 8.4.2`
- **Host Tooling Gaps**: `tesseract` binary and `cargo`/`rustc` toolchain are not installed on the local benchmark host.
- **Rate Limits & Mock Conditions**: `B1`/`B2` use `httpx.MockTransport` with `noop_sleep` (eliminating real network latency/sleeps to isolate request counts, byte transfers, cache hits, and local CPU); `B3` replays 2 GraphQL timeline pages (including pinned old post and cross-page duplicates); `B4` seeds 200 `Work` rows (`batch_size=75`); `B6` injects HTTP `429` (`Retry-After: 2`), crashed `RUNNING` jobs, and transient SQLite `database is locked` errors.
- **Note on `ru_maxrss` vs `VmHWM`**: [`process_memory_kib()`](../../apps/api/app/core/observability.py#L42-L59) computes `max(vm_hwm_kib, ru_maxrss)`. When spawned under a parent runner shell with high `ru_maxrss`, `after_t6.json` recorded `1,464,740 KiB` for both `b4.force_run` and `b5` solely from inherited `ru_maxrss`. Isolated subprocess measurements reading Linux `/proc/self/status` `VmHWM` directly are reported alongside `after_t6.json` below.

---

## 3. Baseline vs Post-Optimization Comparison (`B0`–`B6`)

| 指标 | 基线 (`baseline_t0`) | 优化后 (`after_t6`) | 变化 | 场景/方法/限制 |
| --- | --- | --- | --- | --- |
| API 空闲内存 | ~90.57 MiB (11 PIDs, T0 容器观测) | 未测 | 未测 | 本轮未重测生产 Docker 容器 (`apps/api` 单测进程空载 VmRSS 约 22.4 MiB) |
| collector 空闲内存，含 Chromium | `browser_started=true`, `chromium_launches=1`, `collector_rss_kib=52,396 KiB` (T0 容器 ~109.1 MiB / 108 PIDs) | `browser_started=false`, `chromium_launches=0`, `collector_rss_kib=52,504 KiB` (mock) | Chromium 启动 `1 -> 0` | `B0` GraphQL-only replay（无实际启动 Chromium 子进程；未重测生产容器 RSS） |
| 同输入重复轮询总耗时 (`B1` warm) | wall 中位数 `420.088 ms` (`385.11..481.564`), cpu `419.935 ms` | wall 中位数 `791.707 ms` (`779.237..794.833`), cpu `788.432 ms` | wall `+88.5%` (见说明) | `B1` MockTransport + `noop_sleep` (0 网络延迟)。详请/聚合降为 0，但列表中 `thumb.png` 与库中 `cover.png` URL 不同步导致每轮重算 20 次 `fingerprint_image()` (`~39 ms/cover`)；真实网络下省去 48 次 detail HTTP 请求及限流等待 |
| `B3` X 同步耗时 | wall `1.066 ms` (`1.034..1.323`), cpu `1.067 ms` | wall `0.501 ms` (`0.471..0.873`), cpu `0.502 ms` | wall `-53.0%`, cpu `-53.0%` | `B3` GraphQL timeline replay (`repeats=5`) |
| `B4` 封面维护空跑耗时 (`warm_noop`) | wall `296.094 ms` (`262.892..318.992`), cpu `284.446 ms` | wall `42.451 ms` (`41.91..43.915`), cpu `42.44 ms` | wall `-85.7%`, cpu `-85.1%` | `B4` 200 works keyset 标量扫描 + 跳过无更新的建议原因刷新 |
| `B4` 封面全量重算 (`force=True`, 200 works) | 单次加载并重算 200 works: `11,164.921 ms` (`11,054.77..12,004.784`) | 单批 (`batch_size=75`): `4,135.164 ms` (`2,764.172..4,293.009`); 完整 3 批 200 works: `10,903.49 ms` (`10,878.94..10,987.97`) | 整轮 `-2.3%`；单次工作量上限 `200 -> 75` | 每次读入行数从全表 200 降为 75 (`-62.5%`)，可跨重启续跑 |
| 身份/列表/详情请求数 (`B1` warm) | identity `6`, list `11`, detail `48`, bytes `43,283` | identity `2` (`hits=5`), list `8`, detail `0` (`hits=20`), bytes `13,615` | identity `-66.7%`, list `-27.3%`, detail `-100%`, bytes `-68.5%` | `B1` 同输入连续 2 位作者多别名重复轮询 |
| 身份/列表/详情请求数 (`B2` incremental) | identity `4`, list `7`, detail `32`, bytes `29,295` | identity `0` (`hits=4`), list `4`, detail `2` (`hits=9`), bytes `7,958` | identity `-100%`, list `-42.9%`, detail `-93.8%`, bytes `-72.8%` | `B2` 新增 1 部 + 更新 1 部增量发现 (`b2_total_works=16`, `b2_total_events=26`) |
| `B3` GraphQL 请求数 | `identity_lookups=2`, `graphql_calls=4`, bytes `1,794` | `identity_lookups=0` (`hits=2`), `graphql_calls=2`, bytes `1,540` | `graphql_calls -50%`, bytes `-14.2%` | `B3` 两步断点续采与去重 |
| 聚合次数/候选比较量 | `B1` warm: `aggregation_executed=25`, `updated_works=4`; `B2`: `aggregation_executed=16` | `B1` warm: `aggregation_executed=0` (`skipped=7`), `updated_works=0`; `B2`: `aggregation_executed=2` (`skipped=3`, `candidate_loads=1`) | `B1` warm 聚合 `-100%`; `B2` `-87.5%` | `B1` 无变化与 `B2` 增量变化分开统计 |
| 图片/封面任务峰值内存 | `B4` force: `94,356 KiB`; `B5`: `153,428 KiB` | 独立进程实测 `VmHWM`: `B4` `87,188 KiB`, `B5` `148,164 KiB` (组合 JSON 中 `ru_maxrss` 受父进程污染为 `1,464,740 KiB`) | `B4 -7.6%`, `B5 -3.4%` | `/proc/self/status` `VmHWM` 实测；见第 4 节 `B5`/T5 图片集明细 |
| `B5` 媒体归档 + OCR 耗时 | wall `1,152.52 ms` (`1,139.675..1,175.296`), cpu `1,152.423 ms` | wall `1,146.917 ms` (`1,120.086..1,171.141`), cpu `1,140.553 ms` | wall `-0.5%`, cpu `-1.0%` | 含 3 张有效图转 WebP + 1 张损坏图拒绝；本地无 `tesseract` 故 `ocr_status="retry"` |
| CPU 时间 | 见各项 `cpu_ms` (`B3 1.067`, `B4 noop 284.446`, `B4 full 11,156.5`, `B5 1,152.4`) | 见各项 `cpu_ms` (`B3 0.502`, `B4 noop 42.44`, `B4 full 10,899.5`, `B5 1,140.6`) | `B3 -53.0%`, `B4 noop -85.1%`, `B4 full -2.3%` | 固定输入下 MockTransport 基准；OCR 真实 Tesseract 进程 CPU 未在本地容器外实测 |
| 429、超时、锁重试 | `rate_limited_429=1`, `retries=1`, `sleep=[2.0]`, `db_lock_retries=1` | `rate_limited_429=1`, `retries=1`, `sleep=[2.0]`, `db_lock_retries=1`, `db_write_ms=2` | 保持正确退避与锁重试 | `B6` 故障注入；10 次内锁冲突自动回滚重提交且不重下封面 |
| 最终发现集合/元数据/分组一致性 | `b2_total_works=16, events=26`; 封面 `200` | `b2_total_works=16, events=26`; `B3 saved=3, watermark="205"` | 一致通过 | 详情缓存 TTL 168h 期内非关键变更存在缓存延迟；缺失详情字段不覆盖已有值 |
| 重复事件/重复通知/孤儿进程 | `jobs_recovered=1` | `jobs_recovered=1`；T5 取消/超时 kill+reap 子进程且清理临时文件 | 单测验证通过 | Tesseract 超时/取消孤儿回收由 `test_media_ocr_lifecycle_t5.py` fake subprocess 验证 |

---

## 4. T5 Media & OCR Image Dataset Measurements

1. **Standard `B5` Image Set (`after_t6.json` + clean subprocess `VmHWM`)**:
   - **Inputs (4 files, total `154,108 B`)**: `valid.jpg` (`1800x2400`, `133,264 B`), `rotated.jpg` (`1200x800` EXIF orientation=6, `15,664 B`), `trans.png` (`900x900` RGBA, `5,153 B`), `corrupt.jpg` (`27 B`).
   - **Result**: `3/4` archived as WebP (`[800, 1200]`, `[900, 900]`, `[1200, 1600]`), corrupt file rejected; `ocr_status="retry"` (`tesseract` not installed on host).
   - **Performance (`repeats=5`)**: `wall_ms` median `1,146.917 ms` (`1,120.086..1,171.141`), `cpu_ms` median `1,140.553 ms`, clean-process `VmHWM = 148,164 KiB` (`144.7 MiB`, vs baseline `153,428 KiB`).

2. **Extended Multi-Size + Pixel-Bomb Image Set (`repeats=5`, clean subprocess `VmHWM`)**:
   - **Inputs (7 files)**: `large_1.jpg` (`3000x4000`, `289,909 B`), `large_2.jpg` (`2400x3200`, `205,444 B`), `mid_1.jpg` (`1800x2400`, `133,264 B`), `exif_rot.jpg` (`2000x1500` EXIF orientation=6, `47,664 B`), `alpha.png` (`1600x1600` RGBA, `13,672 B`), `corrupt.jpg` (`19 B`), `pixel_bomb.png` (`7000x6000` = `42,000,000 px > 40,000,000 px` limit, `53,559 B`).
   - **Result**: `5/7` succeeded (`corrupt.jpg` and 42 MP `pixel_bomb.png` rejected cleanly before full decompression); output WebP dimensions `[(1200, 1600) x4, (1600, 1600)]`.
   - **Performance**: `wall_ms` median `2,468.65 ms`, `cpu_ms` median `2,445.58 ms`, `VmHWM = 293,564 KiB` (`286.7 MiB`).

---

## 5. T7 Conditional Rust Pilot Decision

**Conclusion: Trigger conditions NOT met — T7 exited without adding Rust/PyO3.**

Empirical profiling and operational evidence:
1. **`cProfile` on `fingerprint_image()` (`75` covers, `400x600` JPEG = `975` variants, `10,923,978` calls in `5.786 s`)**:
   - Pillow C extension calls (`ImagingCore.resize` `2.145 s`, `convert` `0.261 s`, `copy` `0.144 s`, `crop` `0.066 s`, `decode` `0.056 s`) total `2.672 s` (`46.2%` of profiled time).
   - Pure-Python `_phash` (`sum()` + generator expressions at lines 166/175) totals `2.806 s` cumulative (`~48.5%` under `cProfile`, amplified by ~10.3M generator evaluations; unprofiled `75`-cover B4 batch wall time is `4.139 s`, i.e., `~55 ms/cover` including SQLite commits).
2. **End-to-End Bottleneck**:
   - In production, cover maintenance downloads remote images from `nhentai`/`wnacg` subject to upstream HTTP latency and rate limiting (hundreds to thousands of milliseconds per cover), making `~55 ms/cover` local CPU a secondary cost.
   - T6 already reduced routine idle cover polls (`warm_noop`) by **85.7%** (`296.094 ms -> 42.451 ms`) and bounded batch reads/memory (`200 -> 75`, `VmHWM 87,188 KiB`).
3. **Build & Deployment Cost**:
   - Neither `cargo`/`rustc` on the host nor Rust build stages in [`Dockerfile`](../../Dockerfile) / [`Dockerfile.dockge`](../../Dockerfile.dockge) exist (`apps/api` builds purely via `hatchling` + `python:3.12-slim`). Introducing PyO3 + maturin cross-architecture wheel builds for marginal end-to-end gain violates Section 11 cost-benefit criteria.

---

## 6. Functional Verification Summary

- **API Full Suite**: `PYTHONPATH=apps/api /home/user1/.cache/mangafinder-venvs/venv-api/bin/python -m pytest -c apps/api/pyproject.toml apps/api/tests` (247 tests covering discovery, aggregation invalidation, T5 media/OCR lifecycle, T6 keyset/retry/migrations, backup/snapshot, worker recovery, SQLite concurrency) — see final command run in Section 8 commit verification.
- **Social Collector Full Suite**: `PYTHONPATH=. /home/user1/.cache/mangafinder-venvs/venv-collector/bin/python -m pytest -c pyproject.toml tests` (18 tests covering lazy Chromium lifecycle, GraphQL session reuse, identity cache, and batch budget).
- **Live Upstream Sources / Docker Containers**: Not executed against live X, `nhentai`, or `wnacg` accounts or running Docker containers because this isolated checkout has no production `.env`, credentials, or running `tesseract` daemon; all protocol/lifecycle invariants are verified with reproducible `MockTransport`, SQLite fault injection, and fake subprocess tests.

---

## 7. Deployment, Additive Migrations & Rollback Guide

### 7.1 Consistent Snapshot Before Upgrade
Before deploying or upgrading SQLite databases, always create a WAL-consistent snapshot using [`apps/api/app/modules/backup/snapshot.py`](../../apps/api/app/modules/backup/snapshot.py) (which uses `sqlite3.Connection.backup()` rather than copying only the `.db` file and missing `-wal` pages):
```bash
PYTHONPATH=apps/api python -m app.modules.backup.snapshot create \
  --database ./data/mangafinder.db \
  --key ./data/system-secret.key \
  --destination ./data/backups/pre-opt-$(date +%Y%m%d-%H%M%S)
```

### 7.2 Schema Additions & Compatibility (`init_db()` Additive Migrations)
All schema changes in T3–T6 are **additive** (`ALTER TABLE ... ADD COLUMN` / `CREATE TABLE IF NOT EXISTS`) applied automatically at startup by `_apply_additive_migrations()` in [`apps/api/app/db/session.py`](../../apps/api/app/db/session.py):
- **T3**: `provider_author_identities`, `provider_scan_states`, `work_sources` (`list_signature`, `detail_fetched_at`, `detail_failure_count`, `detail_next_retry_at`, `detail_last_attempted_at`), and `provider_scan_runs` (`scan_mode`, `pages_covered`, `total_pages_hint`, `reached_end`, `budget_truncated`).
- **T4**: `author_aggregation_states`.
- **T6**: `work_fingerprints` (`cover_failure_count`, `cover_next_retry_at`, `cover_failed_url`) and `cover_maintenance_state`.

If rolling back code to pre-T3 (`23dc528`) while keeping a migrated DB fails `Base.metadata` strict table checks in `backup/snapshot.py` or ORM assumptions, restore the pre-upgrade snapshot via `app.modules.backup.snapshot restore <snapshot_dir> --destination <target_dir>`. All T3/T4/T6 cache and state tables (`provider_author_identities`, `provider_scan_states`, `author_aggregation_states`, `cover_maintenance_state`) contain derivable cache/progress state only and can be cleared without losing user works, exclusions, or manual overrides.

### 7.3 Configuration Knobs & Strategy Disable/Fallback Flags

| Env Variable (`MANGAFINDER_*` / `SOCIAL_COLLECTOR_*`) | Default | Effect / Rollback Usage |
| --- | --- | --- |
| `MANGAFINDER_CATALOG_DETAIL_CACHE_TTL_HOURS` | `168` | Set to `0` to disable T3 detail cache and force fresh detail fetches. |
| `MANGAFINDER_CATALOG_IDENTITY_CACHE_TTL_HOURS` | `168` | Set to `0` to disable T3 artist identity caching. |
| `MANGAFINDER_CATALOG_DETAIL_MAX_FETCHES_PER_RUN` | `8` | Raise to widen per-run detail backfill cap; bounded for 429 protection. |
| `MANGAFINDER_CATALOG_RECENT_SCAN_OVERLAP_PAGES` | `2` | Controls T3 incremental recent-scan overlap safety margin. |
| `MANGAFINDER_CATALOG_DEEP_SCAN_MAX_PAGES_PER_RUN` | `3` | Controls T3 deep scan pages per run. |
| `MANGAFINDER_SOCIAL_COLLECTOR_BATCH_BUDGET_SECONDS` | `65.0` | End-to-end collector batch budget sent by API (`95.0s` HTTP timeout). |
| `SOCIAL_COLLECTOR_GRAPHQL_WORKERS` | `4` | Max concurrent GraphQL collector workers. |
| `MANGAFINDER_SOCIAL_MEDIA_MAX_PIXELS` | `40000000` | Pre-decode pixel budget guarding T5 image archive. |
| `MANGAFINDER_SOCIAL_MEDIA_DECODE_CONCURRENCY` | `2` | Bounded in-flight image decodes per process (`1` reduces peak RSS further). |
| `MANGAFINDER_SOCIAL_OCR_MAX_IMAGE_BYTES` | `33554432` | Streaming download byte cap (`32 MiB`) for OCR inputs. |
| `MANGAFINDER_SOCIAL_OCR_MAX_IMAGE_PIXELS` | `40000000` | Pre-decode pixel budget for Tesseract OCR preprocessing. |
| `MANGAFINDER_SOCIAL_OCR_CONCURRENCY` | `2` | Bounded concurrent `tesseract` subprocesses. |

---

## 8. Completion Checklist (Section 13.3)

- [x] Diff against baseline `23dc528` verified; existing optimizations preserved without duplication.
- [x] `apps/api` (247 tests) and `apps/social_collector` (18 tests) suites pass; zero lint errors on T0–T6 changes and collector (`apps/api` root lint difference documented: pre-existing `tests/test_nhentai_provider.py` has 1 `I001` when run inside `apps/api`).
- [x] GraphQL-only and `/health` paths in `social_collector` do not launch Chromium (`chromium_launches=0`).
- [x] Repeated catalog polling cuts `detail_requests` (`48 -> 0`) and `aggregation_executed` (`25 -> 0`) while detecting updates cleanly after invalidation.
- [x] Missing list fields preserve existing detailed work metadata; identity, exclusion, and manual override invariants hold.
- [x] Cursor interruption, budget exhaustion, HTTP 429, timeouts, SQLite lock retries, and worker crash recovery verified by tests and `B6`.
- [x] Explicit bounds enforced on media bytes/pixels, decode/OCR concurrency & subprocess reaping, and DB cover-maintenance batch reads (`batch_size=75`, `max_work_id`).
- [x] Repeated replay memory (`VmHWM`) remains flat/bounded across T5/T6 batches without orphaned temp files or subprocesses.
- [x] Raw comparative benchmark JSON ([`baseline_t0.json`](baseline_t0.json), [`after_t6.json`](after_t6.json)) and unmeasured container/live-source items reported honestly.
- [x] Operational snapshot/migration/rollback guide and data-backed T7 exit rationale documented.
