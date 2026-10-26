# Testing

```sh
MOVIEHUB_CHECKOUT=../moviehub python -m unittest discover -s tests -t tests -v
VSA_LIVE=1 MOVIEHUB_CHECKOUT=../moviehub python -m unittest discover -s tests -t tests -p test_live_archive.py -v
```

`MOVIEHUB_PYTHON` selects the interpreter for MovieHub's code (needs `requests==2.32.5`, `six`);
default: the current one. `VSA_CHROME` points at a Chrome binary for the rendering test.
Tests needing ffmpeg, a MovieHub checkout or Chrome skip themselves when those are missing.

| File | Proves |
| --- | --- |
| `test_policy_http.py` | deny-by-default, HTTPS, private addresses, robots.txt, redirect re-checks, size caps, ranges, cache, recording, redaction |
| `test_parsing.py` | search interface detection (SearchAction, OpenSearch, forms), media extraction, HLS/DASH parsing and DRM detection, MP4 box walk, recipe interpreter rules |
| `test_media.py` | verification of real MP4 (index first and last), HLS, DASH; DRM, 401, HTML and 404 reported; cross-site dedup without merging different encodes |
| `test_moviehub.py` | adapter generation and validation, record insertion, inventory updates; contract read from and enforced against the real checkout; archive.org adapter through MovieHub's `query_site()` on synthetic archive-shaped responses |
| `test_end_to_end.py` | full workflow on local fixture sites through MovieHub's real code and test suite, including a patch that `git apply --check`s cleanly; generic form-only site; user recipe site |
| `test_batch_diagnostics.py` | queue persistence, retries, lease expiry, concurrency limit; diagnosis rules; failed repairs keep the previous working recipe |
| `test_web_cli.py` | review UI protection (token, Host), interactive workflow, reviews, batch via API, CLI, browser rendering with policy blocking |
| `test_live_archive.py` | opt-in: the whole workflow against the real archive.org, with MovieHub's query on the real network |

The local fixture sites (`tests/support/fixture_site.py`) serve ffmpeg-generated 125-second media.
They prove the mechanics, not any real site's behaviour. Only the live test touches a real site,
and it covers one title from the runner's network at one time. None of these tests play media on
an Android device; MovieHub's own `run-live-vod-audit.sh` does that for a provider id.
