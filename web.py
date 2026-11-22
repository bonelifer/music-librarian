"""Review web page for music-librarian (plain HTML forms, no JavaScript).

Every page works with a keyboard and a screen reader: real headings,
labelled form controls, a skip link and visible focus. Forms that change
files carry a per-process token so other sites cannot submit them.
"""

import html
import re
import secrets
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import deezer
import lyrics
import store
import suggest
import tags

PAGE_SIZE = 100
BULK_LIMIT = 200
MAX_GENRE_LENGTH = 60
FILTERS = {
    "pending": "Waiting for review",
    "changed": "Changed since approval",
    "nosuggest": "No suggestions",
    "applied": "Applied",
    "skipped": "Skipped",
    "ignored": "Never ask again",
}
ORDER = "albumartist COLLATE NOCASE, album COLLATE NOCASE, id"
# Lists whose albums can be ticked and approved together.
APPROVABLE = ("pending", "changed", "skipped")
# Theme choices: "auto" follows the system's light/dark setting.
THEMES = {"auto": "Auto", "light": "Light", "dark": "Dark"}
THEME_TEXT = {"auto": "Auto (follows your system)", "light": "Light",
              "dark": "Dark"}
# The theme button steps through the choices in this order.
NEXT_THEME = {"auto": "light", "light": "dark", "dark": "auto"}

# Colors follow myMPD's look: its gray panels and the green highlight
# (#28a745). Links use a lighter or darker green so text stays readable,
# and solid buttons use #198754 so white text on them is readable too.
DARK_COLORS = """color-scheme: dark;
  --bg: #212529; --surface: #343a40; --stripe: #2b3035; --fg: #f8f9fa;
  --muted: #adb5bd; --line: #495057; --accent: #28a745; --link: #45c867;
  --button: #198754; --on-button: #fff; --header: #343a40;
  --picked: #1e3a2a; --warn: #ea868f; --ok: #75b798; --ok-bg: #051b11;
  --warn-bg: #2c0b0e; --info-bg: #2b3035;"""

STYLE = """
:root { color-scheme: light;
  --bg: #f8f9fa; --surface: #e9ecef; --stripe: #f1f3f5; --fg: #212529;
  --muted: #495057; --line: #ced4da; --accent: #198754; --link: #157347;
  --button: #198754; --on-button: #fff; --header: #dee2e6;
  --picked: #d1e7dd; --warn: #a52834; --ok: #0f5132; --ok-bg: #d1e7dd;
  --warn-bg: #f8d7da; --info-bg: #e9ecef; --radius: 8px; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme=light]) { """ + DARK_COLORS + """ } }
:root[data-theme=dark] { """ + DARK_COLORS + """ }
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); margin: 0;
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--link); text-underline-offset: .15em; }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.skip { position: absolute; left: -999px; }
.skip:focus { left: 1rem; top: .5rem; z-index: 10; background: var(--bg);
  padding: .5rem .75rem; border-radius: var(--radius); }
header { border-bottom: 1px solid var(--line); background: var(--header); }
header nav, main { max-width: 64rem; margin: 0 auto; padding: 0 1rem; }
nav ul { list-style: none; margin: 0; padding: 0; display: flex;
  flex-wrap: wrap; gap: .25rem; align-items: center; }
nav .brand { font-weight: 700; margin-right: auto; padding: .7rem 0; }
nav a { display: inline-block; padding: .7rem .75rem; text-decoration: none;
  border-bottom: 3px solid transparent; color: var(--fg); }
nav a:hover { border-bottom-color: var(--line); }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip-path: inset(50%); white-space: nowrap; }
/* Theme button: a half white, half black circle. The tooltip shows on
   hover and on keyboard focus (CSS only; the page has no JavaScript). */
.theme { position: relative; margin: 0; padding: .3rem 0 .3rem .25rem; }
.theme button { width: 2.5rem; height: 2.5rem; min-height: 0; padding: 0;
  justify-content: center; border-radius: 50%; background: transparent;
  border-color: transparent; }
.theme button:hover { border-color: var(--line); }
.theme-icon { width: 1.5rem; height: 1.5rem; border-radius: 50%;
  border: 2px solid var(--fg);
  background: linear-gradient(90deg, #fff 50%, #000 50%); }
.theme .tip { position: absolute; right: 0; top: calc(100% + .1rem);
  z-index: 5; width: max-content; max-width: 16rem; padding: .3rem .6rem;
  border-radius: var(--radius); background: var(--fg); color: var(--bg);
  font-size: .85rem; line-height: 1.3; visibility: hidden; }
.theme:hover .tip, .theme button:focus-visible + .tip { visibility: visible; }
nav a[aria-current=page] { border-bottom-color: var(--accent);
  color: var(--fg); font-weight: 600; }
main { padding-bottom: 3rem; }
h1 { font-size: 1.6rem; line-height: 1.25; margin: 1.5rem 0 .75rem; }
h2 { font-size: 1.15rem; margin: 1.75rem 0 .5rem; }
.muted { color: var(--muted); }
.error { color: var(--warn); }
.notice { background: var(--info-bg); padding: .75rem 1rem;
  border-radius: var(--radius); border-left: 4px solid var(--accent); }
.panel { background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: .75rem 1rem; }
.panel ul { margin: .25rem 0; padding-left: 1.25rem; }
.cards { list-style: none; padding: 0; margin: 1rem 0; display: grid;
  gap: .75rem; grid-template-columns: repeat(auto-fill, minmax(11rem, 1fr)); }
.card { display: flex; flex-direction: column; gap: .15rem; height: 100%;
  padding: .85rem 1rem; border: 1px solid var(--line);
  border-radius: var(--radius); background: var(--surface);
  text-decoration: none; color: var(--fg); }
.card:hover { border-color: var(--accent); }
.card .count { font-size: 1.75rem; font-weight: 700; line-height: 1.1; }
.card .label { color: var(--muted); }
.btn, button { font: inherit; display: inline-flex; align-items: center;
  gap: .4rem; min-height: 2.5rem; padding: .45rem 1rem; cursor: pointer;
  border: 1px solid var(--line); border-radius: var(--radius);
  background: var(--surface); color: var(--fg); text-decoration: none; }
.btn:hover, button:hover { border-color: var(--accent); }
.primary { background: var(--button); border-color: var(--button);
  color: var(--on-button); font-weight: 600; }
.primary:hover { filter: brightness(1.08); }
.toolbar { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center;
  margin: 1rem 0; }
.toolbar form { margin: 0; }
.badge { display: inline-block; padding: .1rem .6rem; border-radius: 999px;
  font-size: .85rem; font-weight: 600; background: var(--surface);
  border: 1px solid var(--line); }
.badge.applied { background: var(--ok-bg); color: var(--ok);
  border-color: transparent; }
.badge.changed, .badge.missing { background: var(--warn-bg);
  color: var(--warn); border-color: transparent; }
.badge.pending { background: var(--info-bg); border-color: transparent; }
code { font-size: .9em; overflow-wrap: anywhere; }
fieldset { border: 1px solid var(--line); border-radius: var(--radius);
  margin: 1rem 0; padding: .75rem 1rem 1rem; }
legend { font-weight: 600; padding: 0 .35rem; }
input[type=text] { font: inherit; width: min(32rem, 100%); padding: .45rem .6rem;
  border: 1px solid var(--line); border-radius: var(--radius);
  background: var(--bg); color: var(--fg); }
input[type=checkbox] { width: 1.3rem; height: 1.3rem; margin: 0;
  accent-color: var(--accent); cursor: pointer; vertical-align: middle; }
label { cursor: pointer; }
.chips { list-style: none; padding: 0; margin: .25rem 0 .75rem;
  display: flex; flex-wrap: wrap; gap: .5rem; }
.chip { display: inline-flex; align-items: center; gap: .5rem;
  padding: .4rem .8rem .4rem .6rem; border: 1px solid var(--line);
  border-radius: 999px; background: var(--bg); }
.chip:has(input:checked) { background: var(--picked);
  border-color: var(--accent); }
.chip .score { color: var(--muted); font-size: .85rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .5rem .6rem; vertical-align: top;
  border-bottom: 1px solid var(--line); }
/* Long unbroken values (M4A field names, AcoustID fingerprints) wrap
   instead of pushing the table wider than the window. */
.list td { overflow-wrap: anywhere; }
.list tbody tr:nth-child(even) { background: var(--stripe); }
.list tbody tr:hover { background: var(--surface); }
.list tbody tr:has(input:checked) { background: var(--picked); }
.list thead th { position: sticky; top: 0; z-index: 1; background: var(--bg);
  box-shadow: inset 0 -2px var(--line); border-bottom: 0; }
.list td.pick, .list th.pick { width: 3rem; text-align: center; }
.list td.pick { padding: 0; }
.list .fill { display: block; margin: -.5rem -.6rem; padding: .5rem .6rem;
  min-height: 100%; }
.list td.pick .fill { margin: 0; padding: .65rem .6rem; }
.actions { position: sticky; bottom: 0; z-index: 2; background: var(--bg);
  display: flex; flex-wrap: wrap; gap: .5rem 1rem; align-items: center;
  padding: .75rem 0; border-top: 1px solid var(--line); margin-top: .5rem; }
.pager { display: flex; gap: 1rem; margin: 1rem 0; }
.filters { list-style: none; padding: 0; margin: 1rem 0; display: flex;
  flex-wrap: wrap; gap: .5rem; }
.filters a { display: inline-block; padding: .3rem .8rem;
  border: 1px solid var(--line); border-radius: 999px;
  text-decoration: none; color: var(--fg); background: var(--surface); }
.filters a:hover { border-color: var(--accent); }
.filters a[aria-current=page] { background: var(--picked);
  border-color: var(--accent); font-weight: 600; }
details { margin: 1rem 0; }
summary { cursor: pointer; font-weight: 600; padding: .25rem 0; }
@media (max-width: 40rem) {
  h1 { font-size: 1.35rem; }
  .list thead { position: absolute; width: 1px; height: 1px; overflow: hidden;
    clip-path: inset(50%); }
  .list, .list tbody { display: block; }
  .list tr { display: grid; grid-template-columns: 1fr; gap: .15rem;
    padding: .6rem .5rem; border-bottom: 1px solid var(--line); }
  .list.pickable tr { grid-template-columns: 2.75rem 1fr; }
  .list td { display: block; border: 0; padding: 0; }
  .list.pickable td:not(.pick) { grid-column: 2; }
  .list td.pick { grid-row: 1 / span 3; width: auto; }
  .list td[data-label]::before { content: attr(data-label) ": ";
    color: var(--muted); font-size: .85rem; }
  .list .fill { display: inline; margin: 0; padding: 0; }
  nav .brand { flex-basis: 100%; padding-bottom: 0; }
  nav a { padding: .45rem .55rem; }
}
"""


class ReviewServer(ThreadingHTTPServer):
    """HTTP server that carries the Reviewer and the form token."""

    daemon_threads = True

    def __init__(self, address, reviewer):
        super().__init__(address, Handler)
        self.reviewer = reviewer
        self.token = secrets.token_urlsafe(24)


def serve(reviewer) -> None:
    """Run the review page until interrupted."""
    settings = reviewer.settings
    server = ReviewServer((settings.listen, settings.port), reviewer)
    print(f"music-librarian is listening on http://{settings.listen}:"
          f"{settings.port}/", flush=True)
    server.serve_forever()


def get_theme(connection) -> str:
    """The saved theme choice ("auto", "light" or "dark")."""
    row = connection.execute(
        "SELECT value FROM settings WHERE key = 'theme'").fetchone()
    return row[0] if row and row[0] in THEMES else "auto"


def set_theme(connection, theme: str) -> None:
    """Save the theme choice; unknown values fall back to "auto"."""
    if theme not in THEMES:
        theme = "auto"
    with store.transaction(connection):
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES "
            "('theme', ?, ?) ON CONFLICT (key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (theme, store.now_iso()))


def review_counts(connection) -> dict:
    """Albums per status, plus the review sub-counts.

    Besides every status, the result has "nosuggest" (pending or changed
    albums whose Last.fm lookup found nothing) and "waiting" (pending or
    changed albums not looked up on Last.fm yet).
    """
    counts = store.album_counts(connection)
    counts["nosuggest"] = connection.execute(
        "SELECT COUNT(*) FROM albums WHERE status IN ('pending', "
        "'changed') AND suggestions IS NOT NULL AND suggestions = '[]'"
    ).fetchone()[0]
    counts["waiting"] = connection.execute(
        "SELECT COUNT(*) FROM albums WHERE status IN ('pending', "
        "'changed') AND suggestions IS NULL").fetchone()[0]
    return counts


def esc(value) -> str:
    """HTML-escape any value."""
    return html.escape(str(value), quote=True)


def human_time(stamp) -> str:
    """Stored ISO UTC timestamp in local display format ('' if missing)."""
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return esc(stamp)
    return store.display_time(moment)


def genre_list(genres) -> str:
    """Comma-separated genres, or a muted 'none'."""
    return esc(", ".join(genres)) if genres else \
        '<span class="muted">none</span>'


def default_genres(status: str, suggestions, applied, limit: int) -> list:
    """Genres ticked by default for an album (album page and multi-approve).

    An album approved before keeps its approved genres; otherwise the best
    suggestions are used.
    """
    if status in ("applied", "changed") and applied:
        return list(applied)
    return suggest.preselected(suggestions or [], limit)


def clean_genres(values) -> list:
    """Validate submitted genre names; raises ValueError with a reason."""
    names = []
    for value in values:
        for part in value.split(","):
            name = " ".join(part.split())
            if not name:
                continue
            if len(name) > MAX_GENRE_LENGTH:
                raise ValueError(f"'{name[:20]}…' is longer than "
                                 f"{MAX_GENRE_LENGTH} characters")
            if re.search(r"[\x00-\x1f\x7f]", name):
                raise ValueError("Genre names cannot contain control "
                                 "characters")
            if name.casefold() not in (item.casefold() for item in names):
                names.append(name)
    if len(names) > 10:
        raise ValueError("Choose at most 10 genres")
    return names


class Handler(BaseHTTPRequestHandler):
    """Routes for the review page."""

    server_version = "music-librarian"

    def log_message(self, format, *args):  # noqa: A002 (stdlib signature)
        """Keep request logging quiet; errors are shown on the page."""

    # --- helpers ---------------------------------------------------------

    @property
    def reviewer(self):
        """The shared Reviewer."""
        return self.server.reviewer

    def send_page(self, title: str, body: str,
                  status: HTTPStatus = HTTPStatus.OK) -> None:
        """Wrap body in the page layout and send it."""
        connection = self.db()
        try:
            theme = get_theme(connection)
        finally:
            connection.close()
        # "auto" leaves data-theme off so the system setting decides.
        theme_attr = "" if theme == "auto" else f' data-theme="{theme}"'
        page = (
            f"<!doctype html><html lang=\"en\"{theme_attr}><head>"
            "<meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, "
            f"initial-scale=1\"><title>{esc(title)} - Music librarian</title>"
            f"<style>{STYLE}</style></head><body>"
            "<a class=\"skip\" href=\"#main\">Skip to content</a>"
            f"<header><nav aria-label=\"Main\"><ul>{self.nav_links()}"
            f"<li>{self.theme_form(theme)}</li></ul></nav></header>"
            f"<main id=\"main\">{body}</main></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(page)

    def nav_links(self) -> str:
        """Main navigation, marking the current page for screen readers."""
        path = urlsplit(self.path).path
        links = [
            ("/", "Overview", path == "/"),
            # The genre review pages: the filtered lists, one album, bulk.
            ("/albums?filter=pending", "Genres",
             path in ("/albums", "/bulk") or path.startswith("/album/")),
            ("/tags", "Tags", path == "/tags" or path.startswith("/tags/")),
            ("/lyrics", "Lyrics", path == "/lyrics"),
        ]
        items = ["<li class=\"brand\">Music librarian</li>"]
        for href, label, is_current in links:
            current = ' aria-current="page"' if is_current else ""
            items.append(f"<li><a href=\"{href}\"{current}>{label}</a></li>")
        return "".join(items)

    def theme_form(self, theme: str) -> str:
        """Header button that steps through Auto, Light and Dark colors.

        Each click saves the next choice. Screen readers hear the current
        choice and what the click does; the tooltip shows the same to
        mouse and keyboard users.
        """
        current = THEME_TEXT[theme]
        following = NEXT_THEME[theme]
        action = f"Click for {THEMES[following]}."
        return (f"<form class=\"theme\" method=\"post\" action=\"/theme\">"
                f"{self.token_field()}<input type=\"hidden\" name=\"back\" "
                f"value=\"{esc(self.path)}\"><button type=\"submit\" "
                f"name=\"theme\" value=\"{following}\">"
                "<span class=\"theme-icon\" aria-hidden=\"true\"></span>"
                f"<span class=\"sr-only\">Theme: {current}. {action}</span>"
                f"</button><span class=\"tip\" aria-hidden=\"true\">Theme: "
                f"{current}. {action}</span></form>")

    def redirect(self, location: str) -> None:
        """303 redirect after a form post."""
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def not_found(self) -> None:
        """404 page."""
        self.send_page("Not found", "<h1>Not found</h1>",
                       HTTPStatus.NOT_FOUND)

    def token_field(self) -> str:
        """Hidden form token."""
        return (f"<input type=\"hidden\" name=\"token\" "
                f"value=\"{esc(self.server.token)}\">")

    def read_form(self):
        """Parse a POSTed form; None if it is too big or the token is wrong."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return None
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"),
                        keep_blank_values=True)
        token = form.get("token", [""])[0]
        if not secrets.compare_digest(token, self.server.token):
            return None
        return form

    def db(self):
        """A short-lived DB connection for this request."""
        return self.reviewer.connect()

    # --- routing ---------------------------------------------------------

    def do_GET(self):  # noqa: N802 (stdlib name)
        """Read-only pages."""
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            return self.overview(query.get("msg", [""])[0])
        if url.path == "/albums":
            return self.album_list(query.get("filter", ["pending"])[0],
                                   query.get("page", ["1"])[0],
                                   query.get("tick", [""])[0] == "all",
                                   query.get("msg", [""])[0])
        if url.path == "/next":
            return self.go_next(None)
        if url.path == "/bulk":
            return self.bulk_preview()
        if url.path == "/tags":
            return self.tags_page(query.get("msg", [""])[0])
        if url.path == "/lyrics":
            return self.lyrics_page(query.get("msg", [""])[0])
        if url.path == "/tags/field":
            return self.tag_field_page(query.get("name", [""])[0])
        match = re.fullmatch(r"/album/(\d+)", url.path)
        if match:
            return self.album_page(int(match.group(1)),
                                   query.get("msg", [""])[0])
        return self.not_found()

    def do_POST(self):  # noqa: N802 (stdlib name)
        """Actions that change state or files."""
        form = self.read_form()
        if form is None:
            return self.send_page(
                "Expired form", "<h1>That form has expired</h1><p>Reload the "
                "page and try again.</p>", HTTPStatus.FORBIDDEN)
        path = urlsplit(self.path).path
        if path == "/theme":
            connection = self.db()
            try:
                set_theme(connection, form.get("theme", [""])[0])
            finally:
                connection.close()
            back = form.get("back", ["/"])[0]
            # Only return to a page on this site.
            if not back.startswith("/") or back.startswith("//"):
                back = "/"
            return self.redirect(back)
        if path == "/scan":
            return self.start_scan()
        if path == "/bulk":
            return self.bulk_apply(form)
        if path == "/approve":
            return self.approve_ticked(form)
        if path in ("/lyrics/fill", "/lyrics/stop", "/lyrics/remove"):
            return self.lyrics_action(path.rsplit("/", 1)[1], form)
        if path in ("/tags/rules", "/tags/apply", "/tags/restore",
                    "/tags/auto"):
            return self.tag_action(path.rsplit("/", 1)[1], form)
        match = re.fullmatch(r"/album/(\d+)/restore-tags", path)
        if match:
            result = self.reviewer.restore_album_tags(int(match.group(1)))
            text = f"Restored tags in {result['files']} files."
            if result["errors"]:
                text += " Failed: " + "; ".join(result["errors"][:3])
            if result.get("mpd"):
                text += " " + result["mpd"]
            return self.redirect(f"/album/{match.group(1)}?"
                                 + urlencode({"msg": text}))
        match = re.fullmatch(r"/album/(\d+)/(apply|skip|ignore|revert|"
                             r"unignore)", path)
        if not match:
            return self.not_found()
        album_id, action = int(match.group(1)), match.group(2)
        return self.album_action(album_id, action, form)

    # --- pages -----------------------------------------------------------

    def overview(self, message: str) -> None:
        """Counts per status, background status and actions."""
        connection = self.db()
        try:
            counts = review_counts(connection)
        finally:
            connection.close()
        cards = "".join(
            f"<li><a class=\"card\" href=\"/albums?filter={key}\">"
            f"<span class=\"count\">{counts.get(key, 0)}</span>"
            f"<span class=\"label\">{esc(label)}</span></a></li>"
            for key, label in FILTERS.items())
        status = self.reviewer.status
        notice = f"<p class=\"notice\" role=\"status\">{esc(message)}</p>" \
            if message else ""
        key_note = "" if self.reviewer.settings.lastfm_api_key else (
            "<p class=\"error\">LASTFM_API_KEY is not set, so no "
            "suggestions can be fetched.</p>")
        self.send_page("Overview", (
            f"<h1>Overview</h1>{notice}{key_note}"
            "<p>Music librarian looks after the genre tags in your library. "
            "It scans the library into albums, asks Last.fm which genres "
            "each album is known for, and suggests them. On the "
            "<a href=\"/albums?filter=pending\">Genres</a> page you pick the "
            "ones you want, and only the genre tag is written into the "
            "files; every album can be reverted. <a href=\"/tags\">Tags</a> "
            "lists every tag field in the library so you can remove the "
            "ones you don't want, with undo. <a href=\"/lyrics\">Lyrics</a> "
            "fills in missing lyrics from LRCLIB. The library is rescanned "
            "on a schedule, or right away with <em>Scan the library "
            "now</em>. The cards below count albums by status; click one "
            "to see those albums.</p>"
            "<h2 id=\"by-status\">Albums by status</h2>"
            f"<ul class=\"cards\" aria-labelledby=\"by-status\">{cards}</ul>"
            "<div class=\"toolbar\">"
            f"<form method=\"post\" action=\"/scan\">{self.token_field()}"
            "<button type=\"submit\">Scan the library now</button></form>"
            "</div><h2>Background work</h2><div class=\"panel\"><ul>"
            f"<li>Last scan: {esc(status['scan'])}</li><li>Last.fm lookups: "
            f"{esc(status['suggest'])}</li><li>Albums still waiting for a "
            f"Last.fm lookup: {counts['waiting']}</li></ul></div>"))

    def album_list(self, name: str, page_text: str, tick_all: bool = False,
                   message: str = "") -> None:
        """Paged list of albums in one filter.

        In the review lists every album with suggestions gets a checkbox,
        so several can be approved at once with their default genres.
        """
        if name not in FILTERS:
            return self.not_found()
        page = max(1, int(page_text)) if page_text.isdigit() else 1
        where, params = self.filter_sql(name)
        connection = self.db()
        try:
            counts = review_counts(connection)
            total = connection.execute(
                f"SELECT COUNT(*) FROM albums WHERE {where}",
                params).fetchone()[0]
            rows = connection.execute(
                f"SELECT id, albumartist, album, status, current_genres, "
                f"suggestions, applied_genres FROM albums WHERE {where} "
                f"ORDER BY {ORDER} LIMIT ? OFFSET ?",
                (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
        finally:
            connection.close()
        approvable = name in APPROVABLE
        limit = self.reviewer.settings.max_genres
        items = []
        for row in rows:
            title = (f"{esc(row['albumartist'] or 'Unknown artist')} - "
                     f"{esc(row['album'])}")
            now = genre_list(store.loads(row["current_genres"], []))
            if not approvable:
                items.append(
                    f"<tr><td><a href=\"/album/{row['id']}\">{title}</a></td>"
                    f"<td data-label=\"Now\">{now}</td></tr>")
                continue
            genres = default_genres(
                row["status"], store.loads(row["suggestions"], None),
                store.loads(row["applied_genres"], None), limit)
            items.append(self.pick_row(
                row["id"], title, now, genres, tick_all,
                "Albums without suggestions need their own page"))
        columns = 4 if approvable else 2
        body_rows = "".join(items) or (
            f"<tr><td colspan=\"{columns}\">No albums here.</td></tr>")
        pages = ""
        if page > 1:
            pages += (f"<a href=\"/albums?{urlencode({'filter': name, 'page': page - 1})}\">"
                      "Previous page</a> ")
        if page * PAGE_SIZE < total:
            pages += (f"<a href=\"/albums?{urlencode({'filter': name, 'page': page + 1})}\">"
                      "Next page</a>")
        notice = f"<p class=\"notice\" role=\"status\">{esc(message)}</p>" \
            if message else ""
        if approvable:
            toggle = {"filter": name, "page": page}
            if not tick_all:
                toggle["tick"] = "all"
            toggle_link = (f"<a href=\"/albums?{urlencode(toggle)}\">"
                           f"{'Untick all' if tick_all else 'Tick all on this page'}"
                           "</a>")
            content = (
                "<p>Tick albums to approve them with the genres shown under "
                "<em>Will become</em> (the ones their album page would "
                "tick). Click anywhere on a row except the album name to "
                "tick it. Albums without suggestions need their own page.</p>"
                f"<p>{toggle_link}</p>"
                f"<form method=\"post\" action=\"/approve\">"
                f"{self.token_field()}"
                f"<input type=\"hidden\" name=\"filter\" value=\"{esc(name)}\">"
                f"<input type=\"hidden\" name=\"page\" value=\"{page}\">"
                + self.pick_table(body_rows, "Genres in the files")
                + "<div class=\"actions\"><button type=\"submit\" "
                "class=\"primary\">Write genres for the ticked albums</button>"
                f"{toggle_link}</div></form>")
        else:
            content = (
                "<table class=\"list\"><thead><tr><th scope=\"col\">Album"
                "</th><th scope=\"col\">Genres in the files</th></tr></thead>"
                f"<tbody>{body_rows}</tbody></table>")
        filter_links = "".join(
            f"<li><a href=\"/albums?filter={key}\"{current}>"
            f"{esc(label)} ({counts.get(key, 0)})</a></li>"
            for key, label in FILTERS.items()
            for current in (' aria-current="page"' if key == name else "",))
        self.send_page(f"Genres: {FILTERS[name]}", (
            "<h1>Genres</h1>"
            "<ul class=\"filters\" aria-label=\"Album filters\">"
            f"{filter_links}</ul>"
            "<div class=\"toolbar\"><a class=\"btn primary\" "
            "href=\"/next\">Review one at a time</a>"
            "<a class=\"btn\" href=\"/bulk\">Bulk accept</a></div>"
            f"{notice}<h2>{esc(FILTERS[name])}</h2><p>{total} albums.</p>"
            f"{content}<p class=\"pager\">{pages}</p>"))

    @staticmethod
    def pick_row(album_id: int, title: str, now: str, genres, ticked: bool,
                 no_pick: str) -> str:
        """One tickable list row.

        Every cell except the album link is a label for the checkbox, so a
        click anywhere on the row ticks it (no JavaScript needed).
        """
        if not genres:
            return (f"<tr><td class=\"pick\"><span class=\"muted\" "
                    f"title=\"{esc(no_pick)}\">-</span></td>"
                    f"<td data-label=\"Album\"><a href=\"/album/{album_id}\">"
                    f"{title}</a></td><td data-label=\"Now\">{now}</td>"
                    "<td data-label=\"Will become\"><span class=\"muted\">"
                    "needs its own page</span></td></tr>")
        box_id = f"a{album_id}"
        return (
            f"<tr><td class=\"pick\"><label class=\"fill\" for=\"{box_id}\">"
            f"<input type=\"checkbox\" name=\"album\" value=\"{album_id}\" "
            f"id=\"{box_id}\"{' checked' if ticked else ''} aria-label=\""
            f"Approve {title}\"></label></td>"
            f"<td data-label=\"Album\"><a href=\"/album/{album_id}\">{title}"
            f"</a></td><td data-label=\"Now\"><label class=\"fill\" "
            f"for=\"{box_id}\">{now}</label></td>"
            f"<td data-label=\"Will become\"><label class=\"fill\" "
            f"for=\"{box_id}\">{genre_list(genres)}</label></td></tr>")

    @staticmethod
    def pick_table(body_rows: str, now_heading: str) -> str:
        """Table wrapper for pick_row rows."""
        return (
            "<table class=\"list pickable\"><thead><tr>"
            "<th scope=\"col\" class=\"pick\">Approve</th>"
            "<th scope=\"col\">Album</th>"
            f"<th scope=\"col\">{esc(now_heading)}</th>"
            "<th scope=\"col\">Will become</th></tr></thead>"
            f"<tbody>{body_rows}</tbody></table>")

    @staticmethod
    def filter_sql(name: str):
        """WHERE clause and parameters for a list filter."""
        if name == "nosuggest":
            return ("status IN ('pending', 'changed') AND suggestions = '[]'",
                    ())
        return ("status = ?", (name,))

    def go_next(self, after_id, message: str = ""):
        """Redirect to the next album waiting for review, in list order.

        message, when given, is shown on the page it lands on.
        """
        connection = self.db()
        try:
            row = None
            # Albums whose Last.fm lookup is done come first.
            for ready in ("AND suggestions IS NOT NULL", ""):
                row = self.next_row(connection, after_id, ready)
                if row is not None:
                    break
        finally:
            connection.close()
        if row is None:
            return self.redirect("/?" + urlencode(
                {"msg": (message + " " if message else "")
                 + "Nothing is waiting for review."}))
        if message:
            return self.redirect(f"/album/{row['id']}?"
                                 + urlencode({"msg": message}))
        return self.redirect(f"/album/{row['id']}")

    @staticmethod
    def next_row(connection, after_id, ready: str):
        """Next pending album after after_id (wrapping), or None.

        ready is an extra SQL condition (a constant, never user input).
        """
        base = f"status = 'pending' {ready} AND id != ?"
        if after_id is not None:
            current = connection.execute(
                "SELECT albumartist, album FROM albums WHERE id = ?",
                (after_id,)).fetchone()
            if current is not None:
                row = connection.execute(
                    f"SELECT id FROM albums WHERE {base} AND (albumartist "
                    "COLLATE NOCASE, album COLLATE NOCASE, id) > (?, ?, ?) "
                    f"ORDER BY {ORDER} LIMIT 1",
                    (after_id, current["albumartist"], current["album"],
                     after_id)).fetchone()
                if row is not None:
                    return row
        return connection.execute(
            f"SELECT id FROM albums WHERE {base} ORDER BY {ORDER} LIMIT 1",
            (after_id or 0,)).fetchone()

    def album_page(self, album_id: int, message: str) -> None:
        """One album: current genres, suggestions, and the review form."""
        connection = self.db()
        try:
            album = connection.execute("SELECT * FROM albums WHERE id = ?",
                                       (album_id,)).fetchone()
            tracks = connection.execute(
                "SELECT path, genres FROM tracks WHERE album_id = ? "
                "ORDER BY path", (album_id,)).fetchall()
        finally:
            connection.close()
        if album is None:
            return self.not_found()
        settings = self.reviewer.settings
        current = store.loads(album["current_genres"], [])
        suggestions = store.loads(album["suggestions"], None)
        applied = store.loads(album["applied_genres"], None)
        ticked = default_genres(album["status"], suggestions, applied,
                                settings.max_genres)
        choices = "".join(
            f"<li><label class=\"chip\"><input type=\"checkbox\" "
            f"name=\"genre\" value=\"{esc(item['name'])}\""
            f"{' checked' if item['name'] in ticked else ''}> "
            f"<span>{esc(item['name'])}</span> <span class=\"score\">"
            f"score {item['weight']}, from "
            f"{esc(', '.join(item['sources']))}</span></label></li>"
            for item in suggestions or [])
        listed = {item["name"] for item in suggestions or []}
        extra = ", ".join(name for name in ticked if name not in listed)
        if suggestions is None:
            lookup = ("<p class=\"error\">Last.fm lookup failed: "
                      f"{esc(album['suggestions_error'])}. It is retried on "
                      "the next scan.</p>" if album["suggestions_error"]
                      else "<p class=\"muted\">Not looked up on Last.fm "
                      "yet.</p>")
        elif not suggestions:
            lookup = ("<p class=\"muted\">Last.fm has no usable genres for "
                      "this album or artist. Type your own below.</p>")
        else:
            lookup = ""
        notice = f"<p class=\"notice\" role=\"status\">{esc(message)}</p>" \
            if message else ""
        track_rows = "".join(
            f"<tr><td>{esc(row['path'].rsplit('/', 1)[-1])}</td>"
            f"<td>{genre_list(store.loads(row['genres'], []))}</td></tr>"
            for row in tracks)
        token = self.token_field()
        status_text = {
            "pending": "Waiting for review",
            "applied": "Applied " + human_time(album["applied_at"]),
            "skipped": "Skipped", "ignored": "Never ask again",
            "changed": "Changed since approval (or some files could not "
                       "be written)",
            "missing": "No tracks left in the library",
        }[album["status"]]
        revert = (f"<form method=\"post\" action=\"/album/{album_id}/revert\">"
                  f"{token}<button type=\"submit\">Revert to the genres from "
                  "before</button></form>") \
            if album["status"] in ("applied", "changed") else ""
        connection = self.db()
        try:
            removed = tags.removed_for_album(connection, album_id)
        finally:
            connection.close()
        removed_section = ""
        if removed:
            items = "".join(
                f"<li>{esc(tags.describe(field))} <span class=\"muted\">"
                f"({count} {'file' if count == 1 else 'files'})</span></li>"
                for field, count in removed)
            removed_section = (
                "<h2>Tags removed by the cleanup</h2><div class=\"panel\">"
                f"<ul>{items}</ul><form method=\"post\" action=\"/album/"
                f"{album_id}/restore-tags\">{token}<button type=\"submit\">"
                "Restore these tags</button></form></div>")
        ignore_action = "unignore" if album["status"] == "ignored" \
            else "ignore"
        ignore_label = "Ask about this album again" \
            if album["status"] == "ignored" else "Never ask again"
        self.send_page(f"{album['albumartist']} - {album['album']}", (
            f"<h1>{esc(album['albumartist'] or 'Unknown artist')} - "
            f"{esc(album['album'])}</h1>{notice}"
            f"<p><span class=\"badge {album['status']}\">{status_text}</span>"
            f" <span class=\"muted\">{len(tracks)} "
            f"{'track' if len(tracks) == 1 else 'tracks'} in</span> <code>"
            f"{esc(album['folder'] or '(library root)')}</code></p>"
            f"<p>Genres in the files now: {genre_list(current)}</p>"
            f"<form method=\"post\" action=\"/album/{album_id}/apply\">"
            f"{token}<fieldset><legend>Genres to write (up to "
            f"{settings.max_genres} are ticked by default)</legend>{lookup}"
            f"<ul class=\"chips\">{choices}</ul>"
            "<p><label for=\"extra\">Other genres, separated by commas:"
            "</label><br><input type=\"text\" id=\"extra\" name=\"extra\" "
            f"value=\"{esc(extra)}\" autocomplete=\"off\"></p></fieldset>"
            "<button type=\"submit\" class=\"primary\">Write these genres"
            "</button></form><div class=\"toolbar\">"
            f"<form method=\"post\" action=\"/album/{album_id}/skip\">{token}"
            "<button type=\"submit\">Skip for now</button></form>"
            f"<form method=\"post\" action=\"/album/{album_id}/"
            f"{ignore_action}\">{token}<button type=\"submit\">"
            f"{ignore_label}</button></form>{revert}</div>{removed_section}"
            "<details><summary>Tracks</summary><table class=\"list\"><thead><tr>"
            "<th scope=\"col\">File</th><th scope=\"col\">Genres</th></tr>"
            f"</thead><tbody>{track_rows}</tbody></table></details>"))

    # --- actions ---------------------------------------------------------

    def album_action(self, album_id: int, action: str, form) -> None:
        """Apply, skip, ignore, unignore or revert one album."""
        connection = self.db()
        try:
            album = connection.execute(
                "SELECT id, status FROM albums WHERE id = ?",
                (album_id,)).fetchone()
            if album is None:
                return self.not_found()
            if action in ("skip", "ignore", "unignore"):
                status = {"skip": "skipped", "ignore": "ignored",
                          "unignore": "pending"}[action]
                connection.execute(
                    "UPDATE albums SET status = ?, updated_at = ? "
                    "WHERE id = ?", (status, store.now_iso(), album_id))
                return self.go_next(album_id) if action != "unignore" \
                    else self.redirect(f"/album/{album_id}")
        finally:
            connection.close()
        if action == "revert":
            result = self.reviewer.revert(album_id)
            return self.redirect(f"/album/{album_id}?" + urlencode(
                {"msg": self.describe(result, "Reverted")}))
        try:
            genres = clean_genres(form.get("genre", []) +
                                  form.get("extra", []))
        except ValueError as error:
            return self.redirect(f"/album/{album_id}?" + urlencode(
                {"msg": f"Nothing written: {error}."}))
        if not genres:
            return self.redirect(f"/album/{album_id}?" + urlencode(
                {"msg": "Nothing written: choose or type at least one "
                        "genre."}))
        result = self.reviewer.apply(album_id, genres)
        if result["errors"]:
            return self.redirect(f"/album/{album_id}?" + urlencode(
                {"msg": self.describe(result, "Wrote")}))
        return self.go_next(album_id, result.get("mpd", ""))

    @staticmethod
    def describe(result: dict, verb: str) -> str:
        """One-line summary of a write result."""
        count = result["written"]
        text = f"{verb} {count} {'track' if count == 1 else 'tracks'}."
        if result["errors"]:
            text += (f" {len(result['errors'])} failed: "
                     + "; ".join(result["errors"][:3]))
        if result.get("mpd"):
            text += " " + result["mpd"]
        return text

    def start_scan(self) -> None:
        """Run a scan and a suggestion pass in the background."""
        import threading

        def work():
            try:
                self.reviewer.scan()
                self.reviewer.auto_clean()
                self.reviewer.suggest_missing()
            except Exception as error:  # shown on the overview
                self.reviewer.status["scan"] = f"failed: {error}"

        threading.Thread(target=work, daemon=True).start()
        self.redirect("/?" + urlencode({"msg": "Scan started."}))

    def bulk_candidates(self):
        """Pending albums whose best suggestion scores at least the minimum."""
        settings = self.reviewer.settings
        connection = self.db()
        try:
            rows = connection.execute(
                "SELECT id, albumartist, album, current_genres, suggestions "
                "FROM albums WHERE status = 'pending' AND suggestions IS NOT "
                f"NULL AND suggestions != '[]' ORDER BY {ORDER}").fetchall()
        finally:
            connection.close()
        candidates = []
        for row in rows:
            found = store.loads(row["suggestions"], [])
            # Only the album's own tags are trusted for bulk writes;
            # artist-level tags often miss what a single album is.
            if found and found[0].get("album_weight", 0) \
                    >= settings.bulk_min_weight:
                candidates.append((row, suggest.preselected(
                    found, settings.max_genres)))
            if len(candidates) >= BULK_LIMIT:
                break
        return candidates

    def bulk_preview(self) -> None:
        """List what bulk accept would write; nothing changes yet."""
        settings = self.reviewer.settings
        candidates = self.bulk_candidates()
        rows = "".join(
            self.pick_row(
                row["id"],
                f"{esc(row['albumartist'] or 'Unknown artist')} - "
                f"{esc(row['album'])}",
                genre_list(store.loads(row["current_genres"], [])),
                genres, True, "")
            for row, genres in candidates)
        if not candidates:
            body = ("<h1>Bulk accept</h1><p>No waiting album has a "
                    "suggestion from its own Last.fm tags scoring "
                    f"{settings.bulk_min_weight} or more.</p>")
        else:
            body = (
                "<h1>Bulk accept</h1><p>These albums have a suggestion "
                "from the album's own Last.fm tags scoring at least "
                f"{settings.bulk_min_weight}. "
                "Untick any you want to review by hand. Nothing is written "
                f"until you press the button. At most {BULK_LIMIT} albums "
                "are shown at a time.</p>"
                f"<form method=\"post\" action=\"/bulk\">{self.token_field()}"
                + self.pick_table(rows, "Now")
                + "<div class=\"actions\"><button type=\"submit\" "
                "class=\"primary\">Write genres for the ticked albums"
                "</button></div></form>")
        self.send_page("Bulk accept", body)

    def bulk_apply(self, form) -> None:
        """Write the preselected genres for each ticked album."""
        chosen = {int(value) for value in form.get("album", [])
                  if value.isdigit()}
        pairs = [(row["id"], genres) for row, genres in self.bulk_candidates()
                 if row["id"] in chosen and genres]
        self.redirect("/?" + urlencode({"msg": self.apply_many(pairs)}))

    def approve_ticked(self, form) -> None:
        """Approve the albums ticked on a review list with default genres.

        Each album is re-checked here: it must still be in a review list
        and have genres to write, so a stale page cannot write anything
        unexpected.
        """
        name = form.get("filter", ["pending"])[0]
        name = name if name in APPROVABLE else "pending"
        page = form.get("page", ["1"])[0]
        page = page if page.isdigit() else "1"
        chosen = sorted({int(value) for value in form.get("album", [])
                         if value.isdigit()})
        limit = self.reviewer.settings.max_genres
        pairs = []
        connection = self.db()
        try:
            for album_id in chosen:
                row = connection.execute(
                    "SELECT status, suggestions, applied_genres FROM albums "
                    "WHERE id = ?", (album_id,)).fetchone()
                if row is None or row["status"] not in APPROVABLE:
                    continue
                genres = default_genres(
                    row["status"], store.loads(row["suggestions"], None),
                    store.loads(row["applied_genres"], None), limit)
                if genres:
                    pairs.append((album_id, genres))
        finally:
            connection.close()
        message = self.apply_many(pairs) if pairs else \
            "Nothing written: no albums with genres were ticked."
        self.redirect("/albums?" + urlencode(
            {"filter": name, "page": page, "msg": message}))

    def apply_many(self, pairs) -> str:
        """Apply (album id, genres) pairs; returns a one-line summary."""
        written = failed = mpd_failed = 0
        mpd_message = ""
        for album_id, genres in pairs:
            result = self.reviewer.apply(album_id, genres)
            if result["errors"]:
                failed += 1
            else:
                written += 1
            if result.get("mpd"):
                mpd_failed += 1
                mpd_message = result["mpd"]
        message = f"Wrote genres for {written} albums."
        if failed:
            message += (f" {failed} albums had files that could not be "
                        "written; see 'Changed since approval'.")
        if mpd_failed:
            message += (f" MPD was not told about {mpd_failed} albums "
                        f"({mpd_message}); run an MPD library update.")
        return message

    # --- tag inventory and cleanup -----------------------------------------

    def tags_page(self, message: str) -> None:
        """Every tag field in the library, removal rules and preview."""
        connection = self.db()
        try:
            fields = tags.cached_inventory(connection)
            rules = tags.rule_fields(connection)
            targets = tags.cached_affected(connection, rules)
            auto = tags.auto_clean_enabled(connection)
            pending = connection.execute(
                "SELECT COUNT(*) FROM tracks WHERE fields IS NULL"
            ).fetchone()[0]
        finally:
            connection.close()
        token = self.token_field()
        notice = f"<p class=\"notice\" role=\"status\">{esc(message)}</p>" \
            if message else ""
        scanning = (f"<p class=\"muted\">{pending} files have not been "
                    "read for tags yet; the next scan adds them.</p>"
                    if pending else "")
        rows = []
        for item in fields:
            field = item["field"]
            # Fields already removed everywhere stay listed, so they can be
            # kept as rules for new downloads.
            if item["files"] == 0 and not item["removed"]:
                continue
            if item["protected"]:
                pick = "<span class=\"badge\" title=\"Never removed\">kept</span>"
            elif not item["mp3"] and not item["removed"]:
                pick = ("<span class=\"muted\" title=\"Only MP3 files can "
                        "be cleaned\">-</span>")
            else:
                box_id = f"f{len(rows)}"
                pick = (f"<label class=\"fill\" for=\"{box_id}\"><input "
                        f"type=\"checkbox\" name=\"field\" id=\"{box_id}\" "
                        f"value=\"{esc(field)}\"{' checked' if item['rule'] else ''}"
                        f" aria-label=\"Remove {esc(item['name'])}\"></label>")
            mp3_note = "" if item["mp3"] == item["files"] else (
                f" <span class=\"muted\">({item['mp3']} MP3)</span>")
            examples = esc("; ".join(item["examples"])) or \
                "<span class=\"muted\">-</span>"
            origin = tags.source(field)
            origin_cell = (f"{esc(origin[0])}<br><span class=\"muted\">"
                           f"{esc(origin[1])}</span>" if origin else
                           "<span class=\"muted\">-</span>")
            rows.append(
                f"<tr><td class=\"pick\">{pick}</td>"
                f"<td data-label=\"Field\">{esc(item['name'])}<br>"
                f"<code class=\"muted\">{esc(field)}</code></td>"
                f"<td data-label=\"Source\">{origin_cell}</td>"
                f"<td data-label=\"Files\">{item['files']}{mp3_note}</td>"
                f"<td data-label=\"Albums\"><a href=\"/tags/field?"
                f"{urlencode({'name': field})}\">{item['albums']}</a></td>"
                f"<td data-label=\"Examples\">{examples}</td></tr>")
        table = (
            "<table class=\"list pickable\"><thead><tr>"
            "<th scope=\"col\" class=\"pick\">Remove</th>"
            "<th scope=\"col\">Field</th><th scope=\"col\">Source</th>"
            "<th scope=\"col\">Files</th>"
            "<th scope=\"col\">Albums</th><th scope=\"col\">Examples</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table>")
        auto_form = (
            f"<form method=\"post\" action=\"/tags/auto\">{token}"
            "<p><label><input type=\"checkbox\" name=\"enabled\" value=\"1\""
            f"{' checked' if auto else ''}> Clean new and changed files "
            "automatically after each scan</label></p>"
            "<p class=\"muted\">Uses the fields marked for removal below, "
            "with the same backups, so anything removed can be restored."
            "</p><button type=\"submit\">Save</button></form>")
        album_count = len({album for _, album in targets})
        if rules and targets:
            preview = (
                f"<p>{len(rules)} {'field is' if len(rules) == 1 else 'fields are'}"
                f" marked for removal. They are in <strong>{len(targets)} MP3 "
                f"files</strong> across <strong>{album_count} albums</strong>."
                " Every removed tag is saved, so it can be restored.</p>"
                f"<form method=\"post\" action=\"/tags/apply\">{token}"
                "<button type=\"submit\" class=\"primary\">Remove these "
                "tags from the files</button></form>")
        elif rules:
            preview = ("<p>The fields marked for removal are not in any MP3 "
                       "file right now.</p>")
        else:
            preview = ("<p>No fields are marked for removal. Tick fields "
                       "below and save.</p>")
        removed = [item for item in fields if item["removed"]]
        restore_rows = "".join(
            f"<tr><td>{esc(item['name'])}<br><code class=\"muted\">"
            f"{esc(item['field'])}</code></td><td>{item['removed']}</td><td>"
            f"<form method=\"post\" action=\"/tags/restore\">{token}"
            f"<input type=\"hidden\" name=\"field\" value=\"{esc(item['field'])}\">"
            f"<button type=\"submit\">Restore</button></form></td></tr>"
            for item in removed)
        restore = (
            "<h2>Removed tags</h2><table class=\"list\"><thead><tr>"
            "<th scope=\"col\">Field</th><th scope=\"col\">Files</th>"
            "<th scope=\"col\">Undo</th></tr></thead>"
            f"<tbody>{restore_rows}</tbody></table>") if removed else ""
        self.send_page("Tags", (
            f"<h1>Tags</h1>{notice}"
            "<p>Every tag field found in the library. Tick the ones you want "
            "gone and save; nothing changes in the files until you press "
            "<em>Remove these tags from the files</em>. Fields marked "
            "<em>kept</em> (titles, artists, album, track numbers, dates, "
            "genres, cover art, ReplayGain and MusicBrainz IDs) can't be "
            "removed. Only MP3 files are cleaned. The <em>ID3v1 tag</em> "
            "row is the old-style copy of the tag some downloaders add at "
            "the end of the file.</p>"
            f"<p class=\"muted\">Last tag job: "
            f"{esc(self.reviewer.status['tags'])}</p>{scanning}"
            f"<div class=\"panel\">{preview}</div>"
            f"<h2>Automatic cleanup</h2><div class=\"panel\">{auto_form}</div>"
            f"<h2>Fields</h2><form method=\"post\" action=\"/tags/rules\">"
            f"{token}{table}<div class=\"actions\"><button type=\"submit\" "
            "class=\"primary\">Save removal choices</button></div></form>"
            f"{restore}"))

    def tag_field_page(self, field: str) -> None:
        """Albums carrying one tag field."""
        if not field or len(field) > 300:
            return self.not_found()
        connection = self.db()
        try:
            rows = tags.albums_with(connection, field)
        finally:
            connection.close()
        items = "".join(
            f"<tr><td><a href=\"/album/{row['id']}\">"
            f"{esc(row['albumartist'] or 'Unknown artist')} - "
            f"{esc(row['album'])}</a></td><td data-label=\"Tracks\">"
            f"{row['tracks']}</td></tr>" for row in rows) or (
            "<tr><td colspan=\"2\">No album has this field now.</td></tr>")
        self.send_page(tags.describe(field), (
            f"<h1>{esc(tags.describe(field))}</h1><p><code>{esc(field)}"
            f"</code> is in {len(rows)} albums. <a href=\"/tags\">Back to "
            "Tags</a></p><table class=\"list\"><thead><tr><th scope=\"col\">"
            "Album</th><th scope=\"col\">Tracks with it</th></tr></thead>"
            f"<tbody>{items}</tbody></table>"))

    def tag_action(self, action: str, form) -> None:
        """Save rules, start removal, or start a field restore."""
        if action == "rules":
            connection = self.db()
            try:
                chosen = [value for value in form.get("field", [])
                          if 0 < len(value) <= 300]
                tags.save_rules(connection, chosen)
                count = len(tags.rule_fields(connection))
            finally:
                connection.close()
            message = f"Saved: {count} fields marked for removal."
        elif action == "auto":
            enabled = form.get("enabled", [""])[0] == "1"
            connection = self.db()
            try:
                tags.set_auto_clean(connection, enabled)
            finally:
                connection.close()
            message = ("Automatic cleanup is on: new and changed files are "
                       "cleaned after each scan." if enabled else
                       "Automatic cleanup is off.")
        elif action == "apply":
            message = self.reviewer.start_tag_job("apply")
        else:
            field = form.get("field", [""])[0]
            if not field:
                return self.redirect("/tags")
            message = self.reviewer.start_tag_job("restore", field)
        self.redirect("/tags?" + urlencode({"msg": message}))

    # --- lyrics ---------------------------------------------------------------

    LYRIC_STATES = (
        ("synced-file", "Synced lyrics file (.lrc)"),
        ("plain-file", "Plain lyrics file (.txt)"),
        ("embedded", "Lyrics in the tags"),
        ("instrumental", "Instrumental"),
        ("not-found", "No lyrics found"),
        ("error", "Lookup failed"),
        ("unchecked", "Not looked up yet"),
    )

    def lyrics_page(self, message: str) -> None:
        """Lyrics coverage, the fill job and albums still missing lyrics."""
        root = self.reviewer.settings.library_dir
        connection = self.db()
        try:
            cover = lyrics.coverage(connection, root)
            due = len(lyrics.due(connection, root, cover=cover))
            use_deezer = self.reviewer.settings.deezer_lyrics
            deezer_due = len(lyrics.deezer_due(connection, root, cover=cover)) \
                if use_deezer else 0
            sources = dict(connection.execute(
                "SELECT source, COUNT(*) FROM lyrics WHERE written_file IS "
                "NOT NULL GROUP BY source").fetchall())
            written = sum(sources.values())
            albums = {row["path"]: row for row in connection.execute(
                "SELECT tracks.path, albums.id, albums.albumartist, "
                "albums.album FROM tracks JOIN albums ON albums.id = "
                "tracks.album_id")}
        finally:
            connection.close()
        totals = cover["totals"]
        cards = "".join(
            f"<li><div class=\"card\"><span class=\"count\">"
            f"{totals.get(key, 0)}</span><span class=\"label\">"
            f"{esc(label)}</span></div></li>"
            for key, label in self.LYRIC_STATES)
        missing = {}
        for path, state in cover["tracks"].items():
            if state in ("not-found", "error", "unchecked") and path in albums:
                row = albums[path]
                entry = missing.setdefault(row["id"], [row, 0])
                entry[1] += 1
        worst = sorted(missing.values(), key=lambda item: (
            -item[1], (item[0]["albumartist"] or "").casefold()))[:100]
        album_rows = "".join(
            f"<tr><td><a href=\"/album/{row['id']}\">"
            f"{esc(row['albumartist'] or 'Unknown artist')} - "
            f"{esc(row['album'])}</a></td><td data-label=\"Missing\">"
            f"{count}</td></tr>" for row, count in worst) or (
            "<tr><td colspan=\"2\">Every album has lyrics.</td></tr>")
        token = self.token_field()
        running = self.reviewer.lyrics_lock.locked()
        # LRCLIB: about 1.5 requests per track; Deezer: 2 at 1 per second.
        minutes = round((due * lyrics.PAUSE * 1.5
                         + deezer_due * deezer.PAUSE * 2) / 60)
        if running:
            control = (f"<form method=\"post\" action=\"/lyrics/stop\">"
                       f"{token}<button type=\"submit\">Stop the fill"
                       "</button></form>")
        elif due or deezer_due:
            if not deezer_due:
                label = f"{due} tracks"
            else:
                label = (f"{due} on LRCLIB, then " if due else "") \
                    + f"{deezer_due} on Deezer"
            control = (f"<form method=\"post\" action=\"/lyrics/fill\">"
                       f"{token}<button type=\"submit\" class=\"primary\">"
                       f"Fill missing lyrics ({label})</button></form>")
        else:
            control = "<p>Nothing is due for a lookup right now.</p>"
        remove = "" if not written else (
            f"<h2>Undo</h2><form method=\"post\" action=\"/lyrics/remove\">"
            f"{token}<p>music-librarian has written {written} lyrics files. "
            "Removing them deletes only files that are still exactly as "
            "written.</p><p><label><input type=\"checkbox\" name=\"confirm\" "
            "value=\"yes\"> Yes, delete the lyrics files music-librarian wrote"
            "</label></p><button type=\"submit\">Remove written lyrics files"
            "</button></form>")
        auto = ("New tracks are looked up automatically after each scan (up "
                f"to {self.reviewer.settings.lyrics_auto_limit} per scan)."
                if self.reviewer.settings.lyrics_auto_fill else
                "New tracks are only looked up when you press the button "
                "(set LYRICS_AUTO_FILL=1 to do it after each scan).")
        notice = f"<p class=\"notice\" role=\"status\">{esc(message)}</p>" \
            if message else ""
        if use_deezer:
            deezer_text = (
                "<p>When LRCLIB has nothing, or only plain lyrics, Deezer is "
                "asked next. A synced <code>.lrc</code> from Deezer replaces "
                "a plain <code>.txt</code> that music-librarian wrote, if "
                "that file is unchanged. Deezer's lyrics service is not a "
                "published API, so it can stop working without notice.</p>")
            deezer_status = (f"<p>Last Deezer pass: "
                             f"{esc(self.reviewer.status['deezer'])}</p>")
        else:
            deezer_text = ("<p class=\"muted\">Deezer, a second source, is "
                           "off (set DEEZER_LYRICS=1 to use it).</p>")
            deezer_status = ""
        source_text = ""
        if written:
            parts = [f"{count} from {'Deezer' if name == 'deezer' else 'LRCLIB'}"
                     for name, count in sorted(sources.items(),
                                               key=lambda item: item[0] or "")]
            source_text = (f"<p class=\"muted\">Lyrics files written by "
                           f"music-librarian: {', '.join(parts)}.</p>")
        self.send_page("Lyrics", (
            f"<h1>Lyrics</h1>{notice}"
            "<p>Tracks without lyrics are looked up on <a href=\"https://"
            "lrclib.net/\">LRCLIB</a>. Synced lyrics are saved as a "
            "<code>.lrc</code> file beside the track, plain lyrics as "
            "<code>.txt</code>, which is where myMPD and most players look. "
            "Existing lyrics are never replaced. Tracks LRCLIB doesn't have "
            f"are tried again after {lyrics.NOT_FOUND_RETRY_DAYS} days.</p>"
            f"{deezer_text}"
            f"<h2 id=\"coverage\">Coverage</h2><ul class=\"cards\" "
            f"aria-labelledby=\"coverage\">{cards}</ul>{source_text}"
            f"<div class=\"panel\"><p>Last fill: "
            f"{esc(self.reviewer.status['lyrics'])}</p>{deezer_status}"
            + (f"<p class=\"muted\">Lookups due: {due + deezer_due}. At "
               f"the sources' requested pace that takes about {minutes} "
               "minutes.</p>"
               if (due or deezer_due) and not running else "")
            + f"<p class=\"muted\">{esc(auto)}</p>"
            f"<div class=\"toolbar\">{control}</div></div>"
            "<h2>Albums still missing lyrics</h2><table class=\"list\">"
            "<thead><tr><th scope=\"col\">Album</th><th scope=\"col\">"
            f"Tracks without lyrics</th></tr></thead><tbody>{album_rows}"
            f"</tbody></table>{remove}"))

    def lyrics_action(self, action: str, form) -> None:
        """Start or stop the fill, or remove written files."""
        if action == "fill":
            message = self.reviewer.start_lyrics_job()
        elif action == "stop":
            message = self.reviewer.stop_lyrics_job()
        elif form.get("confirm", [""])[0] != "yes":
            message = ("Nothing removed: tick the box to confirm deleting "
                       "the lyrics files.")
        else:
            result = self.reviewer.remove_lyrics_files()
            message = result.get("error") or (
                f"Removed {result['removed']} lyrics files."
                + (f" Kept {result['kept']} that were changed since."
                   if result["kept"] else ""))
        self.redirect("/lyrics?" + urlencode({"msg": message}))
