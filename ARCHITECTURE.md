# Architecture

## MovieHub contract (why Python, and what is generated)

MovieHub plays films from websites through Python bundled into the Android app by Chaquopy
(Python 3.11, `requests`, `six`). The JavaScript `provider-engine` serves Live TV channels only.
A film website is a module in `android-tv-launcher/app/src/main/python/moviehub_native/`:

| MovieHub expects (read from its code)                                    | Generated adapter                                   |
| ------------------------------------------------------------------------ | --------------------------------------------------- |
| `PROVIDERS` record `dict(id, module, family='native', name, site, movies, series)` | `provider_record()`, inserted in MovieHub's layout |
| `source.movie(imdb, tmdb, title, localtitle, aliases, year)` → key       | JSON key with title, year, aliases                  |
| `source.sources(key, hosts)` → rows from `current_hosts.direct(url, referer, provider, quality)` | one row per distinct media file |
| `source.resolve(url)`                                                    | identity (rows are direct)                          |
| All HTTP through `moviehub_compat.client` (HTTPS, public addresses, limits, cancellation) | `client.request`, `client.safe`, `client.check` only |
| `moviehub_vod.playable()` verifies media type, size, duration ≥ 120 s, no DRM | not duplicated: MovieHub's own check runs in the harness |

`vsa/moviehub/contract.py` reads these facts from a checkout with `ast` (no import) and refuses to
generate if any of them changed.

## Components

```
vsa/
  contracts.py       typed data (profiles, results, candidates, reports, evidence)
  policy.py          permissions, robots.txt, public-address and HTTPS rules, rate limits
  http.py            bounded fetcher: every hop policy-checked, logged (redacted), cached, recordable
  capabilities/      site-independent extraction: html.py (metadata, JSON-LD, nav, forms, media),
                     opensearch.py, render.py (optional headless Chrome, policy-filtered)
  plugins/           base.py (SitePlugin protocol, registry, entry points "vsa.plugins"),
                     recipe.py (any site described by a recipe), generic.py (unknown sites)
  recipes/           built-in recipes (archive_org.json)
  recipe_core.py     the recipe interpreter, shared verbatim by the analyser and generated adapters
  media/             manifest.py (HLS/DASH), mp4.py (box walk), verify.py (bounded sampling + ffprobe/ffmpeg)
  dedup.py           content-evidence media identity across sites
  engine.py          analyse / search / discover / verify / deduplicate, knows no website
  pipeline.py        resumable workflow with an evidence bundle per run
  moviehub/          contract.py, generate.py, harness.py, replay.py, integrate.py
  diagnostics.py     findings, targeted repairs, versioned recipes
  batch.py           persistent SQLite queue: leases, retries with backoff, concurrency
  store.py           SQLite: HTTP cache, runs, reviews, recipe versions, queue
  workspace.py       data directory and the services the CLI and UI share
  web/               loopback JSON API and the review page
```

## Recipes: one description, two runtimes

A recipe is JSON: `json_api` (search endpoint → result list → detail endpoint → file list) or
`html` (search page → result links under a path prefix → detail page → `<video>/<source>`,
JSON-LD `contentUrl`, literal media URLs). The analyser interprets it with `recipe_core.py`; the
generator copies `recipe_core.py` verbatim into the adapter with the recipe as a constant. The
code the analyser verified is therefore the code MovieHub runs. A new site needs either a recipe
file (no code) or, for unknown sites, the generic plugin infers an `html` recipe from the page's
search interface (schema.org SearchAction, OpenSearch or a search form) and its result links.
Sites needing site-specific logic add a plugin; the engine does not change.

Media found only by running a site's JavaScript can be analysed, but no adapter is generated for
it: MovieHub adapters never execute website scripts.

## Verification

Only samples are downloaded: a file's first 2 MiB plus its MP4 index if stored at the end (written
into a sparse file at its real offset), or a playlist with its first segment. ffprobe reads
streams; ffmpeg decodes two seconds. Both run with `-protocol_whitelist file,crypto`, so they
cannot make network requests of their own.

## Deduplication

Sources are the same media only on content evidence: an identical declared SHA-1/MD5, or identical
first 64 KiB and total size, or an identical first segment. Hosts, URLs and titles never merge
anything. Same-length different encodes are listed as related.

## Harness

`harness.stage()` copies MovieHub's Python and tests to a temporary folder, adds the adapter and
record, and applies the inventory count updates. `run_query()` executes MovieHub's real
`moviehub_vod.query_site()` in a separate interpreter: `live` patches nothing; `replay` replaces
only `requests.Session.request` and DNS with recorded exchanges (MovieHub's own test technique).
`run_suites()` runs MovieHub's `test_free_vod.py` (as its CI does) and the generated test.
