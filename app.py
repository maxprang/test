#!/usr/bin/env python3
"""Open Directory Checker - a defensive security tool.

Serves a small web app that checks whether a single, user-supplied URL exposes
an open directory listing (Apache "Index of", nginx autoindex, etc.) and, if so,
lists its entries. It is meant for authorized self-assessment: check sites you
own or are permitted to test.

Runs on the Python standard library only - no third-party dependencies.
"""

import html
import ipaddress
import json
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

HOST = "0.0.0.0"
PORT = 8000

# Cap on how much of a response body we read, to avoid pulling huge files.
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB
REQUEST_TIMEOUT = 10  # seconds
USER_AGENT = "OpenDirectoryChecker/1.0 (+authorized-assessment)"

# Signatures that indicate a server-generated directory listing.
LISTING_SIGNATURES = (
    "index of /",
    "<title>index of",
    "directory listing for",
    'id="files"',            # some autoindex themes
    "parent directory</a>",
)


def resolve_is_public(hostname):
    """Return (ok, reason). ok=False blocks private/internal/loopback targets.

    This is the SSRF guard: it stops the checker from being pointed at internal
    infrastructure, cloud metadata endpoints, or the loopback interface.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False, "Hostname konnte nicht aufgelöst werden."

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, f"Ungültige Adresse: {addr}"
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False, (
                "Ziel zeigt auf eine private/interne Adresse und wurde "
                "aus Sicherheitsgründen blockiert."
            )
    return True, ""


def validate_url(raw):
    """Return (parsed_url_str, error). Only http/https to public hosts allowed."""
    raw = (raw or "").strip()
    if not raw:
        return None, "Bitte eine URL angeben."
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return None, "Nur http:// und https:// werden unterstützt."
    if not parsed.hostname:
        return None, "Ungültige URL: kein Host gefunden."
    ok, reason = resolve_is_public(parsed.hostname)
    if not ok:
        return None, reason
    return raw, None


def parse_listing(base_url, body):
    """Extract entries from a directory-listing HTML page.

    Returns a list of dicts: {name, href, is_dir}. Best-effort across the common
    Apache/nginx/lighttpd listing formats by walking anchor tags.
    """
    entries = []
    seen = set()
    for match in re.finditer(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.IGNORECASE | re.DOTALL):
        href = match.group(1).strip()
        text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        text = html.unescape(text)

        # Skip sorting links (?C=N;O=D) and anchors that leave the directory.
        if href.startswith("?") or href.startswith("#"):
            continue
        low_text = text.lower()
        if low_text in ("parent directory", "..", "../"):
            continue
        if href in ("../", "..", "/"):
            continue
        if href in seen:
            continue
        seen.add(href)

        name = text or href
        is_dir = href.endswith("/")
        entries.append(
            {
                "name": name,
                "href": urljoin(base_url, href),
                "is_dir": is_dir,
            }
        )
    return entries


def check_url(raw_url):
    """Fetch the URL and decide whether it is an open directory listing."""
    url, err = validate_url(raw_url)
    if err:
        return {"ok": False, "error": err}

    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            final_url = resp.geturl()
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
            server = resp.headers.get("Server", "unbekannt")
            body = resp.read(MAX_BODY_BYTES).decode("utf-8", errors="replace")
    except HTTPError as e:
        return {"ok": False, "error": f"HTTP-Fehler {e.code}: {e.reason}"}
    except URLError as e:
        return {"ok": False, "error": f"Verbindung fehlgeschlagen: {e.reason}"}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "error": "Zeitüberschreitung beim Abruf."}
    except Exception as e:  # noqa: BLE001 - surface anything else cleanly
        return {"ok": False, "error": f"Unerwarteter Fehler: {e}"}

    low = body.lower()
    is_listing = any(sig in low for sig in LISTING_SIGNATURES)
    entries = parse_listing(final_url, body) if is_listing else []

    return {
        "ok": True,
        "url": final_url,
        "status": status,
        "server": server,
        "content_type": ctype,
        "is_open_directory": is_listing,
        "entry_count": len(entries),
        "entries": entries[:500],  # cap payload size
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Open Directory Checker</title>
<style>
  :root {
    --bg: #0a0e14; --panel: #111823; --border: #1e2a3a;
    --text: #c8d3e0; --muted: #6b7a8f; --accent: #39d98a; --accent-dim: #1f7a52;
    --warn: #f0b429; --danger: #f2545b; --link: #4aa3ff;
    --mono: "SF Mono", "JetBrains Mono", "Fira Code", Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: radial-gradient(1200px 600px at 70% -10%, #10202e 0%, var(--bg) 55%);
    color: var(--text); font-family: var(--mono); line-height: 1.5;
    min-height: 100vh;
  }
  .wrap { max-width: 880px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
  header h1 { font-size: 1.6rem; margin: 0 0 .25rem; letter-spacing: .5px; }
  header h1 .prompt { color: var(--accent); }
  header p.sub { color: var(--muted); margin: 0 0 1.5rem; font-size: .9rem; }
  .banner {
    border: 1px solid var(--accent-dim); background: rgba(57,217,138,.07);
    border-left: 3px solid var(--accent); border-radius: 6px;
    padding: .75rem 1rem; margin-bottom: 1.5rem; font-size: .82rem; color: #a9c9b8;
  }
  .banner strong { color: var(--accent); }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 1.25rem; margin-bottom: 1.25rem;
  }
  form { display: flex; gap: .6rem; flex-wrap: wrap; }
  input[type=text] {
    flex: 1 1 320px; background: #0b111a; border: 1px solid var(--border);
    color: var(--text); padding: .7rem .85rem; border-radius: 6px;
    font-family: var(--mono); font-size: .95rem;
  }
  input[type=text]:focus { outline: none; border-color: var(--accent-dim); }
  button {
    background: var(--accent); color: #05130c; border: 0; border-radius: 6px;
    padding: .7rem 1.3rem; font-family: var(--mono); font-weight: 700;
    font-size: .9rem; cursor: pointer; letter-spacing: .3px;
  }
  button:disabled { opacity: .5; cursor: wait; }
  .meta { display: flex; flex-wrap: wrap; gap: .5rem 1.5rem; font-size: .8rem;
    color: var(--muted); margin: .25rem 0 1rem; }
  .meta b { color: var(--text); font-weight: 600; }
  .verdict { font-size: 1.05rem; font-weight: 700; margin: .25rem 0 1rem;
    display: flex; align-items: center; gap: .5rem; }
  .verdict.open { color: var(--danger); }
  .verdict.closed { color: var(--accent); }
  ul.entries { list-style: none; margin: 0; padding: 0; max-height: 420px;
    overflow-y: auto; border: 1px solid var(--border); border-radius: 6px; }
  ul.entries li { padding: .45rem .8rem; border-bottom: 1px solid #0e1621;
    display: flex; gap: .6rem; align-items: center; font-size: .85rem; }
  ul.entries li:last-child { border-bottom: 0; }
  ul.entries li .ico { width: 1.1rem; text-align: center; }
  ul.entries li a { color: var(--link); text-decoration: none; word-break: break-all; }
  ul.entries li a:hover { text-decoration: underline; }
  li.dir a { color: var(--accent); }
  .err { color: var(--danger); font-size: .9rem; }
  .hint { color: var(--muted); font-size: .82rem; }
  .edu h2 { font-size: 1rem; color: var(--accent); margin: 0 0 .6rem; }
  .edu h3 { font-size: .9rem; margin: 1rem 0 .35rem; color: var(--text); }
  .edu p, .edu li { font-size: .85rem; color: #9fb0c3; }
  .edu code { background: #0b111a; padding: .1rem .35rem; border-radius: 4px;
    color: var(--accent); font-size: .82rem; }
  .edu pre { background: #0b111a; border: 1px solid var(--border); border-radius: 6px;
    padding: .8rem; overflow-x: auto; font-size: .8rem; color: #b9c7d6; }
  footer { color: var(--muted); font-size: .75rem; text-align: center; margin-top: 2rem; }
  .spinner { color: var(--muted); font-size: .85rem; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span class="prompt">$</span> Open Directory Checker</h1>
    <p class="sub">Prüft eine einzelne URL auf ein offen zugängliches Directory Listing.</p>
  </header>

  <div class="banner">
    <strong>Nur autorisierte Nutzung.</strong> Prüfe ausschließlich Systeme, die dir
    gehören oder für die du eine ausdrückliche Testerlaubnis hast. Dieses Tool sendet
    eine einzelne HTTP-Anfrage an das angegebene Ziel &mdash; kein Massen-Scan.
  </div>

  <div class="card">
    <form id="form">
      <input type="text" id="url" placeholder="z. B. example.com/files/  oder  https://ziel.tld/backup/"
             autocomplete="off" spellcheck="false">
      <button type="submit" id="btn">Prüfen</button>
    </form>
    <p class="hint" id="hint">Gibt zurück, ob das Ziel ein Directory Listing zeigt und listet die Einträge.</p>
    <div id="result"></div>
  </div>

  <div class="card edu">
    <h2>Was ist ein offenes Verzeichnis?</h2>
    <p>
      Wenn ein Webserver <b>Directory Listing</b> aktiviert hat und in einem Ordner keine
      Index-Datei (z.&nbsp;B. <code>index.html</code>) liegt, generiert er eine HTML-Liste
      aller Dateien und Unterordner &mdash; das bekannte <code>Index of /</code>. Häufig
      landen so versehentlich Backups, Logs, Datenbank-Dumps oder Konfigurationen mit
      Zugangsdaten öffentlich im Netz.
    </p>
    <h3>Gegenmaßnahmen (Härtung)</h3>
    <p><b>Apache</b> &mdash; Directory Listing global oder pro Verzeichnis abschalten:</p>
    <pre>Options -Indexes</pre>
    <p><b>nginx</b> &mdash; autoindex ist standardmäßig aus; sicherstellen, dass es nicht aktiviert ist:</p>
    <pre>autoindex off;</pre>
    <p>Zusätzlich: sensible Verzeichnisse per Auth schützen, unnötige Dateien nicht im
      Web-Root ablegen und regelmäßig prüfen, was öffentlich erreichbar ist.</p>
  </div>

  <footer>Defensives Security-Werkzeug &middot; nur für autorisierte Prüfungen</footer>
</div>

<script>
const form = document.getElementById('form');
const btn = document.getElementById('btn');
const result = document.getElementById('result');

function esc(s){ return (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = document.getElementById('url').value.trim();
  if (!url) return;
  btn.disabled = true;
  result.innerHTML = '<p class="spinner">&#9889; Prüfe Ziel &hellip;</p>';
  try {
    const res = await fetch('/api/check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });
    const data = await res.json();
    render(data);
  } catch (err) {
    result.innerHTML = '<p class="err">Anfrage fehlgeschlagen: ' + esc(String(err)) + '</p>';
  } finally {
    btn.disabled = false;
  }
});

function render(d){
  if (!d.ok){
    result.innerHTML = '<p class="err">&#10007; ' + esc(d.error) + '</p>';
    return;
  }
  let h = '';
  h += '<div class="meta">'
     + '<span><b>URL:</b> ' + esc(d.url) + '</span>'
     + '<span><b>Status:</b> ' + esc(String(d.status)) + '</span>'
     + '<span><b>Server:</b> ' + esc(d.server) + '</span>'
     + '</div>';
  if (d.is_open_directory){
    h += '<div class="verdict open">&#9888; Offenes Verzeichnis erkannt &mdash; ' + d.entry_count + ' Eintr&auml;ge</div>';
    if (d.entries.length){
      h += '<ul class="entries">';
      for (const e of d.entries){
        const ico = e.is_dir ? '&#128193;' : '&#128196;';
        const cls = e.is_dir ? 'dir' : 'file';
        h += '<li class="' + cls + '"><span class="ico">' + ico + '</span>'
           + '<a href="' + esc(e.href) + '" target="_blank" rel="noopener noreferrer">'
           + esc(e.name) + '</a></li>';
      }
      h += '</ul>';
    }
  } else {
    h += '<div class="verdict closed">&#10004; Kein offenes Directory Listing gefunden.</div>';
  }
  result.innerHTML = h;
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenDirChecker/1.0"

    def log_message(self, fmt, *args):  # quieter logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML)
        else:
            self._send(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/check":
            self._send(404, json.dumps({"ok": False, "error": "Not Found"}),
                       "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw or "{}")
            url = payload.get("url", "")
        except (ValueError, json.JSONDecodeError):
            self._send(400, json.dumps({"ok": False, "error": "Ungültige Anfrage."}),
                       "application/json")
            return
        result = check_url(url)
        self._send(200, json.dumps(result), "application/json")


def main():
    port = PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    server = ThreadingHTTPServer((HOST, port), Handler)
    print(f"Open Directory Checker läuft auf http://{HOST}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBeende ...")
        server.shutdown()


if __name__ == "__main__":
    main()
