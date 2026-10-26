# Run 20261026-160138-de2ecb

- URL: https://archive.org/
- Query: Night of the Living Dead 1968
- Result: complete

| Step | Status | Details |
| --- | --- | --- |
| analyse | ok | {"plugin": "recipe:archive_org", "search_interfaces": 1, "media_on_page": 0} |
| search | ok | {} |
| select | ok | {"selected": ["Night.Of.The.Living.Dead_1080p", "night_of_the_living_dead_dvd", "Night_Of_The_Living_Dead_raw_HD_WS", "NightOfTheLivingDead-MPEG", "PhantasmagoriaTheater-NightOfTheLivingDead1968321"]} |
| discover | ok | {"candidates": 25} |
| verify | ok | {"playable": 6, "total": 25, "distinct_media": 25, "statuses": ["failed", "playable"]} |
| generate | ok | {"files": {"adapter": "adapter/android-tv-launcher/app/src/main/python/moviehub_native/archive_org.py", "record": "adapter/provider_record.json", "recipe": "adapter/recipe.json"}} |
| moviehub_query | ok | {"mode": "live", "moviehub_status": "complete", "comparison": {"analyser_playable": 6, "moviehub_sources": 23, "matched": 4}} |
| moviehub_tests | ok | {} |
| integration_patch | ok | {"branch": "vsa/add-archive-org", "patch": "moviehub-add-archive_org.patch", "applied": ["added moviehub_native.archive_org and its PROVIDERS record", "FreeVodProvidersTest validate() 70 -> 71", "FreeVodProvidersTest ids() 36 -> 37", "self.assertEqual(len(everything), 65) -> 66", "self.assertEqual(l |
