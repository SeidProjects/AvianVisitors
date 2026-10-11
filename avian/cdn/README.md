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

Only four public browser globals are emitted: `AVIAN_PUBLIC_STATIC_MODE`, `AVIAN_MEDIA_ORIGIN`, `AVIAN_SNAPSHOT_INDEX_URL` and `AVIAN_WIKI_INDEX_URL`. These URLs are public. Keep S3 access keys, account identifiers, source paths and publisher configuration on the publisher host; never insert credentials into a browser configuration or public URL.

| Build settings | Result |
| --- | --- |
| No flag and no public URLs | Local mode |
| `AVIAN_CDN_ENABLED=0` | Local mode; stale CDN URLs cleared |
| Both required URLs, flag absent or `1` | Static CDN mode |
| Partial settings, invalid flag or unsafe URLs | Build fails before replacing the previous config |

URLs must be HTTPS without credentials, query strings or fragments. The snapshot and optional description URLs must use the media origin. Deploy configuration and the matching HTML/JS together; use versioned releases or revalidation for the config file. Stop a deployment when generation fails. The HTML's required-config marker makes a missing configuration fail closed without relying on an inline script; your Content Security Policy must permit the deployed external scripts.

In static mode, images and recordings use only the snapshot's published media entries. Unavailable recordings have no play button; unavailable images do not load an empty URL. A failed snapshot refresh retains a previously verified generation and displays its observation time. Automatic local API, administration, Wikipedia and live microphone requests are disabled. Administration and live streaming are separate deployments.

## Produce bird-data snapshots

`export_snapshots.py` reads the BirdNET-Pi detections database through a read-only SQLite transaction. It writes schema-2 content-addressed objects and a snapshot index to a separate output directory; it makes no network calls and never edits the database or removes recordings.

```sh
python3 avian/cdn/export_snapshots.py \
  --db /srv/birdnet/BirdDB.txt \
  --output /srv/birds-publisher/candidate \
  --timezone Europe/London \
  --media-manifest /srv/birds-publisher/verified-media.json \
  --media-origin https://media.example.net
```

Set the timezone to the recorder's actual timezone. `--as-of` and `--generation` support reproducible exports. The existing PHP last-hour behavior is preserved unless `--fix-last-hour` is explicitly selected. Each species detail snapshot exposes up to 500 recent recordings while retaining its full detection count.

The private media manifest must describe already uploaded and verified objects:

```json
{
  "schema_version": 1,
  "generation": "media-release-1",
  "recordings": {
    "example-recording.mp3": {
      "url": "https://media.example.net/audio/example-recording.mp3",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "bytes": 1234
    }
  },
  "latest_recording": {
    "Example species": {"file": "example-recording.mp3"}
  },
  "images": {}
}
```

This is a format example, not an upload-verification receipt. Use actual byte counts and SHA-256 values from your publisher. Image maps are keyed by scientific name and contain `pose1` and optional `pose2` entries with the same URL/hash/byte fields. An omitted media entry is explicitly unavailable; the frontend does not fetch it from the Pi.

## Connect a publisher and Cloudflare

Environment settings choose where visitors read. A separate server-side publisher performs synchronization: discover stable new or changed media, upload and verify those files, capture the verified media manifest, run the exporter, then upload data objects and the generation manifest. Publish `data/snapshot-index.json` **last**, only after its entire generation is readable and verified. Retain prior immutable objects for visitors using an older generation. Configure the publisher's scheduling, lock, limits, retry recovery and retention in your private deployment repository.

This contribution includes frontend/configuration support and the read-only exporter. It does **not** install an R2 uploader, timer, credentials, bucket, domain, retention job or deletion policy. CDN synchronization does not automatically free Pi storage: delete original recordings only through a separately reviewed retention policy after verified offload.

For R2, use Standard storage and a public custom domain with caching and CORS for your frontend origin. Serve JSON as `application/json`, MP3 as `audio/mpeg` and PNG as `image/png`. Audio must support byte ranges. Immutable objects should have long public cache lifetimes; the current index and public config need short lifetimes/revalidation. R2 has monthly included allowances and metered overages; consult the [current R2 pricing](https://developers.cloudflare.com/r2/pricing/) for your whole account.

The exporter defaults to a 256 MiB output-directory quota, a 32 MiB free-disk reserve and a 32 MiB candidate cap. Its lock and quota cover the single output directory, including retained objects and manifests. They do not bound sibling directories or run a cleanup job. Your scheduler must bound aggregate temporary storage and cloud history separately, preserve uncertain publication evidence, and stop before its resource limits.

## Validate without a Pi or Cloudflare account

```sh
cd avian/cdn
python3 -m unittest -v test_generate_public_config.py test_export_snapshots.py
node --test test_public_config.mjs test_frontend_cdn.mjs
```

The fixtures are synthetic and require no API keys, recordings, artwork, network access or live database. Node 20 or newer is sufficient for the JavaScript checks.
