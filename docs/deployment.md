# Daily automation

`.github/workflows/daily.yml` runs at **04:00 UTC** (after the ~03:26 UTC
`prod-0` release drops) and uploads `legs/` to Cloudflare R2 via `rclone`. It
builds tippecanoe from source (cached by `TIPPECANOE_REF`) for the hexes step.
The `bundle` phase runs `verify_bundle.py` before upload and fails the job on any
check — a bundle with wrong byte offsets is worse than no bundle.

## Configuration

Repo **variable**: `R2_BUCKET` — the R2 bucket name.

Repo **secrets**:

| secret | value |
|---|---|
| `R2_ACCESS_KEY_ID` | R2 API token access key |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret |
| `R2_ENDPOINT` | `https://<account-id>.r2.cloudflarestorage.com` |

R2 has **no egress fees**, so the browser range-fetches partitions directly.

## Frontend read URL

Public bucket base (r2.dev dev URL — rate-limited, not CDN-cached, fine to start):

```
https://pub-135f2252a0074f0b9761b0dc93a75fa5.r2.dev/legs
```

**Data is partitioned by day.** Each day the pipeline processes lives under its own
`date=YYYY-MM-DD/` prefix (the newest `RETENTION_DAYS` days are kept, older pruned).
The date is the *data* date, taken from the release tag.

1. **Discover available days** — fetch the manifest (a browser can't list a bucket):
   ```jsonc
   .../legs/dates.json
   {"dates": ["2026-07-18", "2026-07-19"], "latest": "2026-07-19",
    "days": {"2026-07-19": {"flights_schema": 3, "base_ts_uniform": true,
                            "t_epoch": 1784419200, "n_legs": 128697}}}
   ```
   Default the day picker to `latest`. **`dates` has holes** — an upstream
   release can be missing and retention prunes from the far end, so never assume
   `D-1` exists because `D` does. `days[date]` carries that partition's
   `flights_schema` (mirroring its `legs_meta.json`); a date missing from `days`
   was built before the map existed, which means schema 1 or 2 — feature-detect
   before trusting `t_off`/`base_ts`.
2. **A partition** for a chosen `<DATE>`:
   `https://pub-135f2252a0074f0b9761b0dc93a75fa5.r2.dev/legs/date=<DATE>/airports/airport=<ICAO>/data_0.parquet`
3. **The leg index** for that day: `.../legs/date=<DATE>/flights.parquet`
4. **The traffic tiles** for that day: `.../legs/date=<DATE>/traffic.pmtiles`
   (plus `traffic.pmtiles.stats.json`)
5. **The day bundle** for that day: `.../legs/date=<DATE>/` →
   `meta.json`, `legs.parquet`, `cells.bin`, `tracks.bin`
6. **The overnight artifacts**: `.../legs/date=<DATE>/` →
   `airports_utc.parquet`, `overnight_meta.json`, `overnight.parquet` and
   `spliced.parquet` — the last two land a day later than the others, since
   they are settled during the *following* night's run (see *Static artifacts
   for external clients*)
7. **The UTC-offset table for any retained date**: `.../legs/airports_utc.parquet`
   and `.../legs/overnight_meta.json` — at the **root**, not in a partition,
   spanning the whole retention window and never pruned. This is the one to
   fetch for local-time queries; the per-date copies resolve only their own
   D+1…D-2.
8. **What a partition is**: `.../legs/date=<DATE>/legs_meta.json` →
   `flights_schema`, `base_ts_uniform`, `t_epoch`, `n_legs`. Absent on days
   built before it existed.

**Exactly one file per airport.** The per-airport write is single-threaded so each
partition is a single `data_0.parquet` (DuckDB's parallel partitioned write would
otherwise emit `data_0`, `data_1`, … per busy airport, which a browser can't
discover over HTTP since it can't list a directory). Fetch `data_0.parquet` and
you have the whole airport for that day.

To move to a CDN-cached custom domain later (e.g. `splines.<domain>`), connect it
in **R2 → bucket → Settings → Custom Domains**; only this base URL changes on the
frontend — the pipeline is unaffected.

## CORS

hyparquet and pmtiles.js both issue cross-origin **Range** requests, so set the
bucket CORS policy (**R2 → bucket → Settings → CORS**) to allow your frontend
origin:

```json
[{"AllowedOrigins":["https://timefli.es","http://localhost:4200"],
  "AllowedMethods":["GET","HEAD"],
  "AllowedHeaders":["range","content-type"],
  "ExposeHeaders":["content-length","content-range","accept-ranges","etag"],
  "MaxAgeSeconds":3600}]
```

(`etag` is exposed for pmtiles.js, which uses it to detect an archive changing
underneath a partially-read index.)

(Add `https://www.timefli.es` or other dev ports here if the frontend ever loads
from them — CORS origins must match scheme + host + port exactly.)
