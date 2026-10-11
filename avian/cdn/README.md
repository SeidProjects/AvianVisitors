# Optional CDN frontend

The normal installation reads BirdNET-Pi's local PHP endpoints. This optional mode lets a separately deployed static frontend read verified bird-data snapshots, recordings and artwork from an HTTPS CDN such as Cloudflare R2 with a custom domain. It leaves the local microphone and detector running on the Pi.

The committed frontend configuration selects local mode. No Cloudflare account, credentials, paid plan or external publisher is enabled by installing this repository.

## Configure the deployed frontend

Use Python 3.9 or newer. Copy the frontend and its `avian/` assets into your deployment directory first; retain the source repository's default `avian/frontend/public-config.js` unchanged. The HTML loads that configuration synchronously before `apt.js`. Its failure guard prevents a broken static release from falling back to local API requests.

Set the public variables in `.env.example` through your build environment. The generator reads the process environment; it does not automatically source a `.env` file or copy all environment values into JavaScript.

```sh
export AVIAN_CDN_ENABLED=1
export AVIAN_MEDIA_ORIGIN=https://media.example.net
export AVIAN_SNAPSHOT_INDEX_URL=https://media.example.net/data/snapshot-index.json
# Optional cached descriptions:
export AVIAN_WIKI_INDEX_URL=https://media.example.net/data/descriptions.json

python3 avian/cdn/generate_public_config.py \
  --output /srv/birds-release/avian/frontend/public-config.js
```

Only five public browser globals are emitted: `AVIAN_PUBLIC_STATIC_MODE`, `AVIAN_MEDIA_ORIGIN`, `AVIAN_SNAPSHOT_INDEX_URL`, `AVIAN_WIKI_INDEX_URL` and optional `AVIAN_UPDATES_URL`. These URLs are public. Keep S3 access keys, account identifiers, source paths and publisher configuration on the publisher host; never insert credentials into a browser configuration or public URL.

| Build settings | Result |
| --- | --- |
| No flag and no public URLs | Local mode |
| `AVIAN_CDN_ENABLED=0` | Local mode; stale CDN URLs cleared |
| Both required URLs, flag absent or `1` | Static CDN mode |
| Partial settings, invalid flag or unsafe URLs | Build fails before replacing the previous config |

URLs must be HTTPS without credentials, query strings or fragments. The snapshot and optional description URLs must use the media origin. An optional `AVIAN_UPDATES_URL` must be a clean `wss://` URL and may use a separate Worker origin; it has no credentials, query or fragment. Deploy configuration and the matching HTML/JS together; use versioned releases or revalidation for the config file. Stop a deployment when generation fails. The HTML's required-config marker makes a missing configuration fail closed without relying on an inline script; your Content Security Policy must permit the deployed external scripts.

In static mode, images and recordings use only the snapshot's published media entries. Unavailable recordings have no play button; unavailable images do not load an empty URL. A failed snapshot refresh retains a previously verified generation and displays its observation time. Automatic local API, administration, Wikipedia and live microphone requests are disabled. Administration and live streaming are separate deployments.

## Run direct recorder-to-CDN synchronization

`run_sync.py` is an optional server-side publisher. It needs Python 3.9+, `boto3` and `botocore`; the normal application has no SDK dependency. Copy `publisher.env.example` to a private file outside the checkout, set permissions to `0600`, and fill in the endpoint, bucket, public CDN origin, explicit bucket credentials, source roots, state directory, database and recorder timezone. A service manager can inject these variables instead. The literal `--env-file` parser does not execute shell code or expand variables.

```sh
python3 avian/cdn/run_sync.py --env-file /etc/avian-cdn/publisher.env --dry-run
python3 avian/cdn/run_sync.py --env-file /etc/avian-cdn/publisher.env --once
# Explicitly opt into a continuous background loop:
python3 avian/cdn/run_sync.py --env-file /etc/avian-cdn/publisher.env --loop
```

The read-only dry run checks configuration and samples a bounded initial directory scan; it does not connect to Cloudflare, create state or certify the whole archive. `AVIAN_SYNC_ENABLED=0` disables synchronization. With the flag absent, all required server settings enable it; absent settings disable it and partial settings fail. Browser configuration remains separate: server credentials are never copied to `public-config.js` or the data cache.

Every cycle joins one exclusive state lock, durable SQLite ledger and SDK attempt guard. The incremental directory catalog resumes archive discovery and revisits recent directories. All eligible MP3 recordings and PNG/JPEG/WebP images beneath the configured roots enter the queue, including old recordings outside the frontend's 500-row detail window. Do not point the artwork root at unrelated generated image directories. Source and state roots must be resolved, absolute and disjoint. Use `AVIAN_SYNC_IMAGE_ROOT` for one artwork directory, or leave it empty and supply `AVIAN_SYNC_IMAGE_ROOTS` as a literal JSON object of up to eight labels and separate absolute directories. Image mappings use the selected root label; public object keys do not expose those labels. Nested mounts and symbolic-link aliases are rejected, so separate roots do not require copying artwork.

New or changed stable files receive content-addressed audio/image keys. A file becomes available to the browser only after a full cloud GET verifies its SHA-256, byte count and content type and its source fingerprint still matches. That verified association remains available after confirmed ordinary local removal; present changed, inaccessible or unsafe sources are withheld until verification succeeds. Unchanged fingerprints avoid repeated hashing and uploads. A failed or uncertain PUT retains its durable object evidence; the next cycle verifies that key before resending. The publisher never removes original files or remote audio/images.

## Bind artwork and cached descriptions

Set `AVIAN_SYNC_IMAGE_MAP_FILE` to a private JSON file that selects artwork for each scientific name. The same file can resolve duplicate recording filenames. These source references are local integration inputs; only public URL/hash/byte fields appear in the browser data.

```json
{
  "images": {
    "Example species": {
      "pose1": {"root": "images", "path": "illustrations/example-species.png", "source": "illustrations"},
      "pose2": {"root": "images", "path": "illustrations/example-species-2.png", "source": "illustrations"}
    }
  },
  "recordings": {
    "example-recording.mp3": {"root": "audio", "path": "2026-01-01/example-recording.mp3"}
  }
}
```

Missing or not-yet-verified artwork has no CDN entry. A duplicate recording name with different verified content requires an explicit mapping rather than an arbitrary selection. The latest available recording is selected from the species' recent rows in chronological order.

Set `AVIAN_SYNC_WIKI_CACHE_FILE` to a local JSON cache shaped as `{"species":{"Example species":{"extract":"Plain-text description","title":"Optional title","url":"https://example.org/bird"}}}}`. The publisher emits only those reviewed description fields as the current `data/descriptions.json`, with no private cache metadata. This file is read locally; the publisher does not scrape Wikipedia. The browser needs `AVIAN_WIKI_INDEX_URL` set to that CDN URL.

## Publish one current frontend cache

The exporter captures the live detections database in a read-only SQLite transaction using one clock and timezone. Stable schema-2 payloads contain the current statistics, picker windows, life list and up to 500 recent rows per species. Verified public media identities are attached without reopening the live database. Unavailable media stays explicitly unavailable; visitors never fall back to the recorder.

The publisher freezes one candidate in SQLite, uploads and GET-verifies its data objects and generation manifest, then replaces `data/snapshot-index.json` last and verifies that exact index. A restart finishes the frozen candidate before starting a newer one. A lost index PUT reply is resolved against the exact durable current/in-flight graph. Cached descriptions have their own bounded pending-value recovery.

Set `AVIAN_SYNC_CLEANUP_CURRENT_CACHE=1` for current-only frontend metadata. After verified index selection, bounded cleanup retires **only publisher-owned** obsolete JSON objects and manifests outside the current/in-flight closure. It preserves audio, images, the current index, cached descriptions and unknown pre-existing bucket objects. DELETE uncertainty is recorded before the request; the old proof cannot be reused until absence is confirmed. There is no historical snapshot-backup retention. An older browser encountering a retired data object refreshes the current index once and adopts a coherent page/bird batch.

Cleanup requires the explicit setting; if it is omitted or set to `0`, the publisher holds at its tracked cache allowance instead of accumulating unlimited history. The default frontend body allowance is 8 MiB, with one candidate limited to 4 MiB and individual JSON objects to 2 MiB. Temporary export directories are removed after freezing; inode-bound cleanup removes owned incomplete exports after an interrupted process. It does not sweep arbitrary directories or media.

For an existing bucket, stop previous publishers and establish exclusive authority before using `--adopt-current-cache`. That one-time command GET-verifies the existing index, matching manifest and every referenced object before adopting only the selected closure. If configured, it separately adopts the current description value. It does not claim unknown old cache objects or import unverified media receipts. Preserve previous state and resolve its uncertain writes before switching publishers; never run two state directories against the same bucket namespace concurrently.

## Optional Seasonality daily series

Set `AVIAN_SYNC_SEASONALITY=1` to publish the schema-3 `seasonality-v1` capability. The browser adds a fourth **Seasonality** tab with a separate daily-occurrence time series for each bird over the last twelve months. Leaving the setting absent or `0` preserves schema 2. Deploy a browser and publisher that both support the capability before enabling it; older strict schema-2 clients cannot adopt a schema-3 index.

The annual window includes the recorder-local current date. Its first date is the day after the date twelve calendar months earlier, clamping a prior February 29 to February 28. Civil-date arithmetic keeps leap days and daylight-saving changes independent of the visitor's timezone. Counts include every valid source observation within the window and captured local time, rather than the species detail view's latest 500 rows. Invalid dates, times, nonexistent spring daylight-saving times and future observations are excluded. Repeated autumn hours preserve their ambiguous local time instead of inventing an offset. The aggregate reports excluded in-window rows and the earliest valid observation; neither detections nor the first observation establish recorder uptime. It identifies unknown coverage and the partial current day. A zero means zero recorded detections, not confirmed absence.

Each species carries sparse `[day_offset, detection_count]` pairs in one compact, content-addressed annual object. The counts include multiple detection rows for the same recording and do not depend on media uploads. The collector reads only dates, local times and bird names, and exports no filenames, confidence or recording identities. The new object belongs to the same verified current graph as the existing application data; it uses the CDN's existing refresh and update signal.

The existing resource allowances remain unchanged: a 4 MiB complete candidate, 8 MiB rolling cache, 2 MiB per JSON object, 512 referenced objects, an 8 MiB allocated temporary tree and a 12 MiB state reservation. The existing 128 MiB shared state allowance, media quotas, SDK request limits, disk reserve and CPU/runtime controls still apply. The annual collector admits at most 200,000 source rows and 500 species. The complete publication must fit its object and byte allowances; exceeding a limit preserves the previously selected graph rather than truncating observations, growing history or requesting data from the recorder. Review a complete export before activating the feature; no provider plan or service is created by enabling it.

## Scheduling, resources and Cloudflare setup

The default cycle limits are 24 media files, 16 MiB combined media, 8 MiB per file, 1,024 discovery entries, 150 actual SDK transport attempts and 120 seconds. The media phase leaves a request/time margin for the data publisher. Large initial data candidates resume across cycles. A shared body reader limits upload payload release to 512 KiB/s; it does not cap total network bytes. Disk checks require 1.5 GiB free by default, and Linux checks require 512 MiB available memory. These checks complement operating-system CPU, memory and runtime controls. The shared state defaults to a conservative 128 MiB peak allowance including allocated files, SQLite journal growth and spooling; catalog ceilings are 100,000 sources, 10,000 directories and 150,000 immutable keys. Remote media admission reserves every pending or verified immutable key body against an 8 GiB default ceiling. Set `AVIAN_SYNC_REMOTE_RESERVED_BYTES` to a verified baseline for pre-existing untracked content; it is fixed in the target identity and cannot be silently lowered. These are publisher resource limits, not whole-account billing measurements.

The default loop waits 15 minutes between successful cycles and uses a configurable backoff after a hold. A shorter interval can accelerate initial backfill; review upload totals and whole-account allowances before selecting it. A bounded batch completes only a portion of a large archive, and the durable catalog resumes the rest. A backlog count from partial discovery is not a complete archive census.

The `templates/` directory contains example systemd service/timer files. Adapt the service user, source visibility, installation paths and private environment file before installing them. Choose either the timer with `--once` or the persistent `--loop`; do not enable both. `MemoryMax` requires an enabled memory controller; `LimitAS`, CPU quota, task count and process time limits are separate controls. No service or timer is installed or activated by this repository.

For R2, use Standard storage and a public custom domain with caching and CORS for your frontend origin. Serve JSON as `application/json`, MP3 as `audio/mpeg`, and artwork with the corresponding image type. Audio must support byte ranges. Immutable media/data objects have long public cache lifetimes; the current index and descriptions use short lifetimes/revalidation. Use credentials scoped to the intended bucket. See [R2 pricing](https://developers.cloudflare.com/r2/pricing/) for shared included allowances and metered overages. Local request/storage limits and budget forecasts are not provider-enforced billing caps.

## Optional live update signal

The optional Worker files `updates_entry.mjs` and `updates_worker.mjs` provide live update hints through a hibernating SQLite Durable Object. They carry a small generation/time signal only; they never serve recordings, artwork or bird-data payloads. Use `templates/wrangler-updates.jsonc.example` as a starting point, keeping its relative entry path correct if you move the configuration. Set your own HTTPS route, allowed frontend origins and namespace. No Worker or namespace is created by installing this repository.

Provision `UPDATES_PUBLISH_TOKEN` as a Worker secret using a random token of 32–256 letters, digits, underscores or hyphens. Keep it on the Worker and recorder only. Set server-side `AVIAN_SYNC_NOTIFY_URL=https://updates.example.net/notify` and `AVIAN_SYNC_NOTIFY_TOKEN` to the same private token, and the browser's `AVIAN_UPDATES_URL=wss://updates.example.net/live`. Both private notification settings are required together; absent settings disable notification. The browser receives only its public WebSocket URL.

After a fully verified current-cache publish, the recorder makes one bounded authenticated notification request. A failed or lost reply leaves one durable latest delivery record for the next admission; a newer selected generation supersedes an older hint. Notifications never roll back the CDN cache or become a prerequisite for synchronization. The endpoint acknowledges the exact generation and time, and sender state hashes its notification target/token identity without storing another plaintext token.

The Worker validates the browser's Origin, accepts at most 256 hibernating sockets by default, stores one latest signal, and suppresses duplicate or older notifications. It bounds payload sizes and sends per connection; no timer or heartbeat keeps it running while idle. Client messages cannot publish updates. The browser coalesces bursts, verifies the current CDN index before adopting data, reconnects with bounded backoff, and disconnects while hidden. Existing periodic CDN refresh remains the fallback for missed messages, disconnected sockets and exhausted free service limits. Allow the chosen `wss://` origin in your Content Security Policy's `connect-src` if you use one.

SQLite Durable Objects are available on Workers Free, with service limits; this feature does not enable a paid plan or promise a billing cap. Review [Durable Object pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/) and [WebSocket hibernation](https://developers.cloudflare.com/durable-objects/best-practices/websockets/) for the current account. R2's own [event notifications](https://developers.cloudflare.com/r2/buckets/event-notifications/) deliver to Queues; they are a separate integration and are unnecessary for the direct publisher signal.

## Export or test offline

The exporter is also usable without the uploader:

```sh
python3 avian/cdn/export_snapshots.py \
  --db /srv/recorder/BirdDB.txt \
  --output /var/lib/avian-cdn/candidate \
  --timezone Europe/London \
  --media-manifest /var/lib/avian-cdn/verified-media.json \
  --media-origin https://media.example.net
```

Its optional manifest is schema 1 with `recordings`, `latest_recording` and `images` maps of already verified public identities; it is not an upload receipt. The standalone exporter keeps its own output quota and does not run cloud synchronization or cleanup. `--as-of` and `--generation` support reproducible exports; existing PHP last-hour behavior is preserved unless `--fix-last-hour` is selected.

Add `--seasonality` to an offline export to include the optional annual daily-occurrence capability.

```sh
cd avian/cdn
python3 -m unittest discover -p 'test_*.py'
node --test test_public_config.mjs test_frontend_cdn.mjs test_updates_worker.mjs
```

The fixtures are synthetic and require no keys, live recordings, private artwork, network access or real database. Node 20 or newer is sufficient for the JavaScript checks. Test SDK/provider compatibility and an actual bounded upload on your intended recorder before scheduling it.
