# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Open Directory Checker — a small defensive security web app. A user submits a
single URL; the server fetches it, decides whether the response is a directory
listing (Apache/nginx/lighttpd autoindex), and returns the parsed entries. It is
scoped to authorized, single-target self-assessment — deliberately **not** an
internet-wide crawler or mass scanner.

## Commands

```bash
python3 app.py           # serve on http://0.0.0.0:8000
python3 app.py 8123      # serve on a custom port
```

There is no external dependency (Python 3.8+ standard library only), no build
step, and no test runner configured. Logic is exercised directly:

```bash
# parser + SSRF guard sanity check
python3 -c "import app; print(app.parse_listing('http://x/', '<a href=\"a.zip\">a.zip</a>'))"
```

## Architecture

Everything lives in `app.py`, a single stdlib `ThreadingHTTPServer`:

- **`Handler`** — `GET /` serves the inline HTML page (`INDEX_HTML`, a string
  constant with all CSS/JS inline); `POST /api/check` takes `{"url": ...}` and
  returns the JSON check result. The frontend is intentionally embedded in the
  Python file rather than served from separate static assets.
- **`check_url`** — the pipeline: `validate_url` → `urlopen` (with timeout and a
  2 MiB read cap) → signature match against `LISTING_SIGNATURES` → `parse_listing`.
- **`validate_url` / `resolve_is_public`** — the security boundary. Only
  `http`/`https` are allowed, and every resolved IP is checked; private,
  loopback, link-local, reserved, multicast and unspecified targets are rejected.
  This is the SSRF guard that keeps the fetcher from being pointed at internal
  infrastructure or cloud metadata endpoints.
- **`parse_listing`** — best-effort anchor-tag extraction across listing formats;
  it drops sort links (`?C=...`) and parent-directory links, and marks entries
  whose href ends in `/` as directories.

### Important invariants

- The SSRF guard in `resolve_is_public` must run before any server-side fetch.
  If you add new fetch paths, route them through `validate_url` too.
- Output is capped (500 entries, 2 MiB body) to bound payload size — preserve
  these caps when changing the response shape.
- Keep the tool single-target. Do not add crawling, recursion into found
  directories, IP-range enumeration, or bulk fetching — that changes it from an
  authorized self-assessment tool into a mass scanner.
