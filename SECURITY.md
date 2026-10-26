# Security and access rules

- **Deny by default.** Only domains in `permissions.json` (and their subdomains or listed host
  patterns) are contacted, each with a recorded reason and a rate limit (default 1 request/s).
- **robots.txt is honoured** for every permitted host. An unreachable robots.txt (network error or
  5xx) is treated as disallowing the site.
- **HTTPS and public addresses only.** Hosts resolving to private, loopback or reserved addresses
  are refused (local test fixtures need an explicit `allow_private_network` entry). Residual risk:
  DNS can change between the check and the connection.
- **Redirects** are followed manually; each hop is re-checked. Cookies and credentials never cross
  hosts. Responses are size-capped (8 MiB pages, 2 MiB media samples, 32 MiB MP4 index).
- **No circumvention.** No logins, no challenge solving, no paywall or DRM bypass. DRM-protected
  streams (`SAMPLE-AES`, non-identity HLS key formats, DASH `ContentProtection`) are reported as
  `drm`; 401 responses as `auth_required`. Diagnostics never propose repairs for these.
- **Untrusted content.** Website scripts never run except in the optional headless browser, whose
  every request passes the same policy (others are aborted). Generated adapters never execute
  website scripts or `eval` anything. XML with DTDs is refused. Media is decoded by ffmpeg from
  local samples with network protocols disabled.
- **Evidence is redacted.** Token-like query values are removed from logs; MovieHub results are
  stored by host and URL hash, not signed URL.
- **Review UI** listens on loopback only, checks the Host header, and requires a per-process token
  for every state-changing request. Site text is inserted with `textContent`, never as HTML.
- **MovieHub** is never modified: work happens on temporary copies and a local clone branch; the
  output is a patch file for the owner to review.
