# Daily automation

`.github/workflows/daily.yml` runs at **04:00 UTC** (after the ~03:26 UTC
`prod-0` release drops) and uploads `legs/` to OVH Object Storage via `rclone`.
GitHub routinely fires the schedule hours late — observed starting between 08:34
and 09:39 UTC across a week — so treat 04:00 as the earliest, not the time. It
builds tippecanoe from source (cached by `TIPPECANOE_REF`) for the hexes step.
The `bundle` phase runs `verify_bundle.py` before upload and fails the job on any
check — a bundle with wrong byte offsets is worse than no bundle.

## Configuration

Repo **variable**: `R2_BUCKET` — the bucket name.

Repo **secrets**:

| secret | value |
|---|---|
| `R2_ACCESS_KEY_ID` | S3 access key |
| `R2_SECRET_ACCESS_KEY` | S3 secret key |
| `R2_ENDPOINT` | `https://s3.<region>.io.cloud.ovh.net` |

The names are historical, as is the rclone remote called `r2` that the scripts
address as `r2:` in ~30 places. They point at OVH. Renaming is cosmetic and has
not been worth doing during a live migration.

The remote is assembled entirely from `RCLONE_CONFIG_R2_*` env vars in
`daily.yml` and `render.yml`, so there is no config file. Two values are not
obvious:

- `PROVIDER: Other` — rclone has no OVH entry, and `Cloudflare` would keep R2's
  quirk handling (checksum and multipart behaviour) pointed at a bucket that
  does not share those quirks.
- `REGION: eu-west-par` — R2 accepts `auto`; OVH wants the real region, and it
  must agree with the endpoint host.

**Egress, data retrieval and API requests are all free** on OVH Object Storage
Standard, so the browser range-fetches partitions directly. Storage is the only
metered line, ~$0.0157/GiB/month for the first 50 TiB. Use the **Standard**
class, not Cold Archive, which charges for retrieval.

## Frontend read URL

> **Mid-migration.** The pipeline WRITES to OVH; the frontend still READS from
> R2, so the URL below is deliberately still the `r2.dev` one. The OVH bucket is
> public, range-readable and CORS-enabled, but holds only the days uploaded since
> the switch, while R2 holds the full retention window. Cutting over means
> changing this base URL in `docs/frontend-cell-query.md`, here, and
> `overnight/overnight_client.py` — and either accepting a short day picker until
> the nightly runs refill, or backfilling R2 → OVH first. Keep R2 as a rollback
> until OVH has a few days on it.

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

To move to a CDN-cached custom domain later (e.g. `splines.<domain>`), put a CDN
in front of the bucket; only this base URL changes on the frontend — the pipeline
is unaffected.

## CORS

hyparquet and pmtiles.js both issue cross-origin **Range** requests, so the
bucket needs a CORS policy allowing your frontend origin. OVH has no dashboard
panel for this; apply [`cors.json`](../cors.json) with the S3 API:

```sh
aws --profile <owner> s3api put-bucket-cors \
    --bucket <bucket> --cors-configuration file://cors.json \
    --endpoint-url https://s3.<region>.io.cloud.ovh.net
```

`range` in `AllowedHeaders` is load-bearing: without it the preflight rejects the
header hyparquet sends and every ranged read fails, which is all of them. `etag`
is exposed for pmtiles.js, which uses it to detect an archive changing underneath
a partially-read index. Origins match scheme + host + port exactly, so
`https://timefli.es` does not cover `https://www.timefli.es` and one dev port
does not cover another.

**It must be applied by the bucket OWNER, not the pipeline user.** This is worth
stating plainly because the failure is opaque: `PutBucketCors` returns a bare
`AccessDenied` while uploads, listing and even `put-object-acl` keep working.
CORS is a bucket *sub-resource*, and per OVH's own documentation only the account
that created a resource has full control over its sub-resources. ACLs cannot
express it either — they offer only READ, WRITE, READ_ACP and FULL_CONTROL, none
of which is a CORS permission. The pipeline user reaches its objects through a
role and holds no ACL grant on the bucket at all, so that path can never set
CORS. Use the owner's credentials, or attach a user policy granting `s3:*`.

Verify from outside, since a bucket that works under `curl` can still be
unreadable in a browser — `curl` does not enforce CORS:

```sh
curl -sD- -o /dev/null -X OPTIONS -H "Origin: https://timefli.es" \
     -H "Access-Control-Request-Method: GET" \
     -H "Access-Control-Request-Headers: range" \
     "https://<bucket>.s3.<region>.io.cloud.ovh.net/legs/dates.json"
```

Expect `200` echoing `Access-Control-Allow-Origin` and listing `content-range`
and `accept-ranges` under `Access-Control-Expose-Headers`. An origin that is not
on the list should come back with no `Access-Control-Allow-Origin` at all.

## Object ACLs

`RCLONE_CONFIG_R2_ACL` is **`public-read`**, and it has to be. OVH grants
anonymous access through ACLs and lists bucket policies as "not yet available for
Object Storage", so there is no `Principal: "*"` policy to fall back on. R2
served public reads through its `r2.dev` domain and object ACLs played no part,
which made `private` harmless there and silently fatal here: the first OVH upload
reported success and every object answered an anonymous GET with 403.

Objects only. The **bucket** keeps a private ACL, because READ at bucket level
means "list every object" and the frontend never lists — `dates.json` exists so
it does not have to.

`rclone sync` compares size and checksum, **not ACLs**, so changing this setting
does not relabel what is already uploaded. Objects written under the old setting
need a server-side copy onto themselves:

```sh
aws --profile <owner> s3 cp s3://<bucket>/legs/ s3://<bucket>/legs/ \
    --recursive --acl public-read --metadata-directive REPLACE \
    --endpoint-url https://s3.<region>.io.cloud.ovh.net
```

`--metadata-directive REPLACE` is required, not optional: S3 rejects a copy of an
object onto itself unless something changes.
