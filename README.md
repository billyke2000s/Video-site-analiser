# Video Site Analyser

A plugin-based tool that analyses permitted video websites, verifies their media, and generates
website adapters for [MovieHub](https://github.com/billyke2000s/moviehub) that run on MovieHub's
own provider code.

```
URL → analyse → search → select results → discover media → verify playback
    → generate MovieHub adapter → run it through MovieHub's moviehub_vod.query_site()
    → MovieHub's test suite → local integration patch (never pushed)
```

## What it produces

For a site, the tool writes a `moviehub_native/<id>.py` module in the same form as MovieHub's
existing native adapters (`vixsrc.py`, `fourkhdhub.py`), its `PROVIDERS` record, a MovieHub-style
offline test with recorded fixtures, and a `git diff` patch against a local MovieHub branch that
also updates MovieHub's pinned provider counts. Nothing is pushed to MovieHub.

## Setup

Python 3.11 (MovieHub's Chaquopy runtime), `ffmpeg`/`ffprobe`, and optionally Chrome for rendering.

```sh
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[moviehub]"          # add ",render" for JavaScript rendering (Playwright)
git clone https://github.com/billyke2000s/moviehub ../moviehub   # integration target, read-only
```

## Use

```sh
vsa contract ../moviehub                       # show the provider contract read from MovieHub
vsa run https://archive.org/ --query "Night of the Living Dead" --year 1968 \
    --imdb tt0063350 --tmdb 10331 --moviehub ../moviehub          # whole workflow, evidence in vsa-data/runs/
vsa serve --moviehub ../moviehub               # review UI on http://127.0.0.1:8765/
vsa permit example.org --reason "why this site may be analysed"
vsa batch add sites.tsv && vsa batch run --concurrency 2
vsa diagnose vsa-data/runs/<run>               # explain a failed run
```

`--mode live` runs MovieHub's query on the real network; the default `replay` runs it on the
exchanges recorded during verification. Only sites in `vsa-data/permissions.json` are contacted
(Internet Archive is listed by default).

The review UI shows the site profile, search results with selection, every media source with a
preview and its technical evidence, deduplicated media identities, **Working** / **Something's
wrong** controls, diagnosis with proposed repairs, adapter generation, the batch queue and the
permission list.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): components, plugin and recipe model, MovieHub contract mapping
- [SECURITY.md](SECURITY.md): access policy and handling of untrusted sites
- [TESTING.md](TESTING.md): what each test proves, and how to run them
- [STATUS.md](STATUS.md): what works, what was verified, what remains
