# Status (26 October 2026)

## Working and verified

- Live end-to-end against archive.org (CI run 40355736273, evidence on branch
  `evidence/live-40355736273`): "Night of the Living Dead" (1968) → 5 matching items → 25 media
  files, all 25 verified by ffprobe + ffmpeg decode → generated `moviehub_native/archive_org.py` →
  MovieHub's real `moviehub_vod.query_site()` on the live network returned `complete` with 23
  sources, all 23 matched to analyser-verified files. The 2 not accepted are 10-second clips, which
  MovieHub rejects by design (< 120 s). MovieHub's `test_free_vod.py` (58 tests) and the generated
  test (3 tests) passed; the integration patch was produced.
- The first live run (CI 40350400029 / 40355007931) found a real bug: long films with a
  multi-megabyte index at the start failed verification (6/25 playable). Fixed, with a regression test.
- 50 offline/local tests pass locally and in CI (Python 3.11), see TESTING.md.

## Limitations and open items

- Only one real site has been exercised. The generic plugin has been tested on local fixture sites
  only; real sites will expose cases it does not handle.
- No Android or TV playback was tested. MovieHub's `run-live-vod-audit.sh 0 1 native:archive_org`
  on an emulator is the next check.
- The generated archive.org patch is about 2.7 MB because its offline fixture keeps 64 KiB of every
  file it verified; it should be trimmed to a few files before proposing it to MovieHub.
- The patch updates MovieHub's pinned test counts; documentation counts (TESTING.md,
  ARCHITECTURE.md, docs/FREE_VOD_AUDIT.md, CHANGELOG.md) are left for the owner.
- Series (episode mapping) are not supported by the generator.
- Diagnostics have rule-based repairs for timeouts and over-strict search matching only.
- Media previews in the review UI play directly from the site in the browser; HLS/DASH previews load
  hls.js/dash.js from cdnjs. Some hosts refuse browser playback; the technical evidence still applies.
- `evidence/live-40355007133` is a partial branch from a cancelled duplicate run; it could not be
  deleted from this session and can be removed.
