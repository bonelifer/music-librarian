"""Offline tests for music-librarian (no network; Last.fm and MPD are faked)."""

import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mutagen  # noqa: E402
from mutagen.easyid3 import EasyID3  # noqa: E402
from mutagen.flac import FLAC  # noqa: E402
from mutagen.id3 import APIC, ID3, TPUB, TXXX  # noqa: E402

import deezer  # noqa: E402
import music_librarian  # noqa: E402
import lastfm  # noqa: E402
import library  # noqa: E402
import lyrics  # noqa: E402
import mpd  # noqa: E402
import store  # noqa: E402
import suggest  # noqa: E402
import tags  # noqa: E402
import web  # noqa: E402


def make_mp3(path: Path, artist="Artist", album="Album", genre=None):
    """Write a tiny valid MP3 (three silent MPEG-1 Layer III frames)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = b"\xff\xfb\x90\x64" + bytes(413)
    path.write_bytes(frame * 3)
    tags = EasyID3()
    tags["artist"] = artist
    tags["albumartist"] = artist
    if album:
        tags["album"] = album
    if genre:
        tags["genre"] = genre
    tags.save(path, v2_version=3)


def make_flac(path: Path, artist="Artist", album="Album", genre=None):
    """Write a tiny FLAC file (STREAMINFO only) with Vorbis comments."""
    path.parent.mkdir(parents=True, exist_ok=True)
    info = struct.pack(">HH", 4096, 4096) + bytes(6)
    info += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big")
    info += bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + info)
    audio = FLAC(path)
    audio["artist"] = artist
    audio["albumartist"] = artist
    audio["album"] = album
    if genre:
        audio["genre"] = genre
    audio.save()


def genres_of(path: Path):
    """Genre values as stored in the file."""
    return list(mutagen.File(path, easy=True).tags.get("genre", []))


class TimeFormatTests(unittest.TestCase):
    """Times are stored in UTC and shown as MM/DD/YYYY h:mm AM/PM in TZ."""

    def setUp(self):
        self.old_tz = os.environ.get("TZ")

    def tearDown(self):
        if self.old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def use_zone(self, zone):
        os.environ["TZ"] = zone
        time.tzset()

    def test_display_time_in_utc(self):
        from datetime import datetime, timezone
        self.use_zone("UTC")
        self.assertEqual(store.display_time(datetime(
            2026, 11, 16, 9, 5, tzinfo=timezone.utc)), "11/16/2026 9:05 AM UTC")
        self.assertEqual(store.display_time(datetime(
            2026, 11, 16, 0, 30, tzinfo=timezone.utc)), "11/16/2026 12:30 AM UTC")
        self.assertEqual(web.human_time("2026-11-16T21:56:45+00:00"),
                         "11/16/2026 9:56 PM UTC")

    def test_display_time_in_local_zone(self):
        from datetime import datetime, timezone
        self.use_zone("America/Chicago")
        self.assertEqual(web.human_time("2026-11-16T21:56:45+00:00"),
                         "11/16/2026 3:56 PM CST")
        # Daylight saving time, and a stored time that crosses midnight.
        self.assertEqual(store.display_time(datetime(
            2026, 7, 2, 3, 0, tzinfo=timezone.utc)), "07/01/2026 10:00 PM CDT")
        # Stored values stay UTC whatever the display zone is.
        self.assertTrue(store.now_iso().endswith("+00:00"))


class SuggestTests(unittest.TestCase):
    """Curated-list matching and ranking."""

    def setUp(self):
        self.whitelist = suggest.load_whitelist()

    def test_names_are_matched_loosely_and_prettified(self):
        self.assertEqual(self.whitelist[suggest.lookup_key("HIP-HOP")],
                         "Hip Hop")
        self.assertEqual(self.whitelist[suggest.lookup_key("indie rock")],
                         "Indie Rock")
        self.assertEqual(self.whitelist[suggest.lookup_key("R&B")],
                         self.whitelist[suggest.lookup_key(
                             "rhythm and blues")])
        self.assertEqual(suggest.pretty_name("j-pop"), "J-Pop")
        self.assertEqual(suggest.pretty_name("rock and roll"),
                         "Rock and Roll")

    def test_junk_tags_are_dropped_and_sources_combined(self):
        found = suggest.build_suggestions(
            [("seen live", 100), ("Indie Rock", 80), ("favorites", 70)],
            [("indie rock", 50), ("Rock", 40)],
            ["Alternative/Rock pop"],
            self.whitelist)
        names = [item["name"] for item in found]
        self.assertNotIn("Seen Live", names)
        self.assertEqual(found[0]["name"], "Indie Rock")
        self.assertEqual(found[0]["sources"], ["album", "artist"])
        self.assertEqual(found[0]["weight"], 90)
        self.assertEqual(found[0]["album_weight"], 80)
        self.assertIn("Alternative", names)
        self.assertNotIn("Rock Pop", names)

    def test_split_genre_values(self):
        self.assertEqual(suggest.split_genre_values(
            ["Rock/Pop; Folk", "Pop"]), ["Rock", "Pop", "Folk"])

    def test_preselected(self):
        found = [{"name": "A", "weight": 90}, {"name": "B", "weight": 30},
                 {"name": "C", "weight": 26}, {"name": "D", "weight": 25}]
        self.assertEqual(suggest.preselected(found, 3), ["A", "B", "C"])
        self.assertEqual(suggest.preselected(
            [{"name": "Z", "weight": 10}], 3), ["Z"])
        self.assertEqual(suggest.preselected([], 3), [])


class LibraryTests(unittest.TestCase):
    """Scanning, writing and reverting against real (tiny) audio files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "library"
        self.root.mkdir()
        self.db = store.connect(Path(self.temp.name) / "state" / "db.sqlite")
        self.messages = []

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def scan(self):
        return library.scan(self.db, self.root, log=self.messages.append)

    def album(self, name):
        return self.db.execute("SELECT * FROM albums WHERE album = ?",
                               (name,)).fetchone()

    def test_scan_groups_by_album_across_disc_folders(self):
        make_mp3(self.root / "A/Album/CD 1/01.mp3", genre=["Rock/Pop"])
        make_mp3(self.root / "A/Album/CD 2/01.mp3", genre=["Folk"])
        make_flac(self.root / "B/Other/01.flac", artist="B", album="Other")
        result = self.scan()
        self.assertEqual((result["files"], result["albums"],
                          result["new_albums"]), (3, 2, 2))
        album = self.album("Album")
        self.assertEqual(album["folder"], "A/Album")
        self.assertEqual(store.loads(album["current_genres"], []),
                         ["Rock/Pop", "Folk"])

    def test_files_without_album_tag_group_by_folder(self):
        make_mp3(self.root / "Loose/x.mp3", album="")
        make_mp3(self.root / "Loose/y.mp3", album="")
        self.scan()
        rows = self.db.execute("SELECT album_key, album FROM albums").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["album"], "Loose")

    def test_untagged_disc_folders_are_named_from_parent_folders(self):
        make_mp3(self.root / "Eagles/The Long Run (1979)/CD 1/01.mp3",
                 artist="", album="")
        make_mp3(self.root / "Eagles/The Long Run (1979)/CD 2/01.mp3",
                 artist="", album="")
        self.scan()
        rows = self.db.execute("SELECT albumartist, album, folder FROM "
                               "albums").fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [("Eagles", "The Long Run",
                           "Eagles/The Long Run (1979)")])

    def test_unchanged_files_are_not_read_again(self):
        make_mp3(self.root / "A/Album/01.mp3")
        self.scan()
        self.assertEqual(self.scan()["read"], 0)

    def test_unreadable_file_is_reported_not_fatal(self):
        make_mp3(self.root / "A/Album/01.mp3")
        (self.root / "A/Album/broken.mp3").write_bytes(b"not audio")
        result = self.scan()
        self.assertEqual(result["errors"], 1)
        self.assertTrue(self.messages)

    def test_apply_writes_separate_values_as_id3v24_and_revert(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track, genre=["Rock/Pop"])
        self.scan()
        album_id = self.album("Album")["id"]
        result = library.apply_album(self.db, self.root, album_id,
                                     ["Indie Rock", "Alternative"])
        self.assertEqual(result, {"written": 1, "errors": []})
        tags = ID3(track)
        self.assertEqual(tags.version[:2], (2, 4))
        self.assertEqual(tags["TCON"].text, ["Indie Rock", "Alternative"])
        self.assertEqual(tags["TPE1"].text, ["Artist"])
        self.assertEqual(self.album("Album")["status"], "applied")
        # A second apply keeps the very first genres for Revert.
        library.apply_album(self.db, self.root, album_id, ["Rock"])
        library.revert_album(self.db, self.root, album_id)
        self.assertEqual(genres_of(track), ["Rock/Pop"])
        album = self.album("Album")
        self.assertEqual((album["status"], album["applied_genres"]),
                         ("pending", None))

    def test_flac_gets_one_field_per_genre(self):
        track = self.root / "B/Other/01.flac"
        make_flac(track, album="Other")
        self.scan()
        library.apply_album(self.db, self.root, self.album("Other")["id"],
                            ["Jazz", "Swing"])
        self.assertEqual(FLAC(track)["genre"], ["Jazz", "Swing"])

    def test_rescan_after_apply_stays_applied_until_files_change(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track)
        self.scan()
        library.apply_album(self.db, self.root, self.album("Album")["id"],
                            ["Rock"])
        self.scan()
        self.assertEqual(self.album("Album")["status"], "applied")
        tags = EasyID3(track)
        tags["genre"] = ["Pop"]
        tags.save()
        os.utime(track, ns=(1, 1))
        self.scan()
        self.assertEqual(self.album("Album")["status"], "changed")

    def test_removed_album_becomes_missing_and_returns_pending(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track)
        make_mp3(self.root / "B/Other/01.mp3", artist="B", album="Other")
        self.scan()
        backup = track.read_bytes()
        track.unlink()
        self.scan()
        self.assertEqual(self.album("Album")["status"], "missing")
        track.write_bytes(backup)
        self.scan()
        self.assertEqual(self.album("Album")["status"], "pending")

    def test_safe_path_refuses_escapes_and_symlinks(self):
        outside = Path(self.temp.name) / "outside.mp3"
        make_mp3(outside)
        (self.root / "link.mp3").symlink_to(outside)
        with self.assertRaises(library.TagError):
            library.safe_path(self.root, "../outside.mp3")
        with self.assertRaises(library.TagError):
            library.safe_path(self.root, "link.mp3")
        self.scan()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM tracks").fetchone()[0], 0)

    def test_unreadable_track_keeps_its_revert_backup(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track, genre=["Folk"])
        self.scan()
        album_id = self.album("Album")["id"]
        library.apply_album(self.db, self.root, album_id, ["Rock"])
        good = track.read_bytes()
        track.write_bytes(b"temporarily broken")
        self.scan()
        backup = self.db.execute("SELECT original_genres FROM tracks"
                                 ).fetchone()[0]
        self.assertEqual(store.loads(backup, None), ["Folk"])
        track.write_bytes(good)
        self.scan()
        library.revert_album(self.db, self.root, album_id)
        self.assertEqual(genres_of(track), ["Folk"])

    def test_file_that_cannot_be_checked_is_not_treated_as_deleted(self):
        keep = self.root / "A/Album/01.mp3"
        make_mp3(keep)
        make_mp3(self.root / "B/Other/01.mp3", artist="B", album="Other")
        self.scan()
        real_stat = Path.stat

        def failing_stat(path, *args, **kwargs):
            if path.name == "01.mp3" and path.parent.name == "Album":
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        Path.stat = failing_stat
        try:
            self.scan()
        finally:
            Path.stat = real_stat
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM tracks").fetchone()[0], 2)

    def test_empty_library_does_not_wipe_tracks(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track)
        self.scan()
        track.unlink()
        self.scan()
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM tracks").fetchone()[0], 1)

    def test_change_on_one_track_marks_album_changed(self):
        first = self.root / "A/Album/01.mp3"
        second = self.root / "A/Album/02.mp3"
        make_mp3(first)
        make_mp3(second)
        self.scan()
        library.apply_album(self.db, self.root, self.album("Album")["id"],
                            ["Rock", "Pop"])
        tags = EasyID3(second)
        tags["genre"] = ["Rock"]
        tags.save()
        os.utime(second, ns=(1, 1))
        self.scan()
        self.assertEqual(self.album("Album")["status"], "changed")

    def test_backup_comes_from_the_file_not_the_last_scan(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track, genre=["Folk"])
        self.scan()
        tags = EasyID3(track)
        tags["genre"] = ["Edited Later"]
        tags.save()
        album_id = self.album("Album")["id"]
        library.apply_album(self.db, self.root, album_id, ["Rock"])
        library.revert_album(self.db, self.root, album_id)
        self.assertEqual(genres_of(track), ["Edited Later"])

    def test_failed_revert_stays_revertible(self):
        first = self.root / "A/Album/01.mp3"
        second = self.root / "A/Album/02.mp3"
        make_mp3(first, genre=["Folk"])
        make_mp3(second, genre=["Folk"])
        self.scan()
        album_id = self.album("Album")["id"]
        library.apply_album(self.db, self.root, album_id, ["Rock"])
        saved = second.read_bytes()
        second.unlink()
        result = library.revert_album(self.db, self.root, album_id)
        self.assertEqual(len(result["errors"]), 1)
        album = self.album("Album")
        self.assertEqual(album["status"], "changed")
        self.assertIsNotNone(album["applied_genres"])
        second.write_bytes(saved)
        self.assertEqual(library.revert_album(
            self.db, self.root, album_id)["errors"], [])
        self.assertEqual(genres_of(second), ["Folk"])

    def test_backup_is_saved_before_the_file_is_written(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track, genre=["Folk"])
        self.scan()
        original_write = library.write_genres

        def write_then_crash(path, genres):
            original_write(path, genres)
            raise library.TagError("stopped after writing")

        library.write_genres = write_then_crash
        try:
            library.apply_album(self.db, self.root,
                                self.album("Album")["id"], ["Rock"])
        finally:
            library.write_genres = original_write
        backup = self.db.execute("SELECT original_genres FROM tracks"
                                 ).fetchone()[0]
        self.assertEqual(store.loads(backup, None), ["Folk"])

    def test_revert_leaves_never_written_tracks_alone(self):
        first = self.root / "A/Album/01.mp3"
        second = self.root / "A/Album/02.mp3"
        make_mp3(first, genre=["Folk"])
        make_mp3(second, genre=["Folk"])
        self.scan()
        album_id = self.album("Album")["id"]
        saved = second.read_bytes()
        second.unlink()
        library.apply_album(self.db, self.root, album_id, ["Rock"])
        second.write_bytes(saved)
        tags = EasyID3(second)
        tags["genre"] = ["Edited Elsewhere"]
        tags.save()
        result = library.revert_album(self.db, self.root, album_id)
        self.assertEqual(result, {"written": 1, "errors": []})
        self.assertEqual(genres_of(first), ["Folk"])
        self.assertEqual(genres_of(second), ["Edited Elsewhere"])

    def test_missing_file_is_reported_on_apply(self):
        track = self.root / "A/Album/01.mp3"
        make_mp3(track)
        self.scan()
        track.unlink()
        result = library.apply_album(self.db, self.root,
                                     self.album("Album")["id"], ["Rock"])
        self.assertEqual(result["written"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.album("Album")["status"], "changed")


class FakeResponse(io.BytesIO):
    """Context-manager response for the fake opener."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class LastfmTests(unittest.TestCase):
    """Retries, not-found handling, errors and the cache."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = store.connect(Path(self.temp.name) / "db.sqlite")
        self.calls = []
        self.sleeps = []

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def client(self, replies):
        replies = list(replies)

        def opener(request, timeout):
            self.calls.append(request.full_url)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return FakeResponse(json.dumps(reply).encode())

        return lastfm.Lastfm("key", self.db, opener=opener,
                             sleep=self.sleeps.append)

    def test_tags_are_parsed_and_cached(self):
        client = self.client([{"toptags": {"tag": [
            {"name": "Indie Rock", "count": 100},
            {"name": "", "count": 5}, {"name": "pop", "count": "x"}]}}])
        expected = [("Indie Rock", 100), ("pop", 0)]
        self.assertEqual(client.album_tags("A", "B"), expected)
        self.assertEqual(client.album_tags("a ", "b"), expected)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("autocorrect=1", self.calls[0])

    def test_single_tag_object_and_not_found(self):
        client = self.client([
            {"toptags": {"tag": {"name": "Jazz", "count": 7}}},
            {"error": 6, "message": "not found"}])
        self.assertEqual(client.artist_tags("A"), [("Jazz", 7)])
        self.assertEqual(client.artist_tags("Nobody"), [])

    def test_transient_errors_retry_with_backoff(self):
        client = self.client([
            {"error": 29, "message": "rate limit"},
            OSError("reset"),
            {"toptags": {"tag": []}}])
        self.assertEqual(client.artist_tags("A"), [])
        self.assertEqual(len(self.calls), 3)
        self.assertIn(2, self.sleeps)
        self.assertIn(4, self.sleeps)

    def test_permanent_error_raises_and_is_not_cached(self):
        client = self.client([{"error": 10, "message": "Invalid API key"},
                              {"toptags": {"tag": []}}])
        with self.assertRaises(lastfm.LastfmError):
            client.artist_tags("A")
        self.assertEqual(client.artist_tags("A"), [])

    def test_http_4xx_without_body_is_permanent(self):
        error = HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))
        client = self.client([error])
        with self.assertRaises(lastfm.LastfmError):
            client.artist_tags("A")


class MpdTests(unittest.TestCase):
    """The update command reaches MPD, quoted, with the password first."""

    def serve(self, replies):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        received = []

        def run():
            conn, _ = listener.accept()
            with conn:
                stream = conn.makefile("rwb")
                stream.write(b"OK MPD 0.24.0\n")
                stream.flush()
                for reply in replies:
                    received.append(stream.readline().decode().strip())
                    stream.write(reply)
                    stream.flush()
                received.append(stream.readline().decode().strip())
            listener.close()

        thread = threading.Thread(target=run)
        thread.start()
        return listener.getsockname()[1], received, thread

    def test_update_with_password(self):
        port, received, thread = self.serve([b"OK\n", b"updating_db: 1\nOK\n"])
        mpd.update("127.0.0.1", port, 'library/A "B"', "secret")
        thread.join(5)
        self.assertEqual(received, ['password "secret"',
                                    'update "library/A \\"B\\""', "close"])

    def test_ack_raises(self):
        port, _, thread = self.serve([b"ACK [50@0] {update} Malformed\n"])
        with self.assertRaises(mpd.MpdError):
            mpd.update("127.0.0.1", port, "x")
        thread.join(5)

    def test_unreachable_raises(self):
        with self.assertRaises(mpd.MpdError):
            mpd.update("127.0.0.1", 1, "x")


class WebTests(unittest.TestCase):
    """The review page end to end, with Last.fm and MPD left out."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "library"
        self.track = self.root / "A/Album/01.mp3"
        make_mp3(self.track, genre=["Rock/Pop"])
        settings = music_librarian.Settings(
            library_dir=self.root, state_dir=base / "state",
            lastfm_api_key="", listen="127.0.0.1", port=0,
            scan_interval_minutes=0, max_genres=3, bulk_min_weight=60,
            mpd_host="127.0.0.1", mpd_port=1, mpd_password="",
            mpd_path_prefix="library")
        self.reviewer = music_librarian.Reviewer(settings, log=lambda _: None)
        self.reviewer.scan()
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET suggestions = ?", (store.dumps([
            {"name": "Indie Rock", "weight": 80, "sources": ["album"],
             "album_weight": 80},
            {"name": "Rock", "weight": 30, "sources": ["artist"],
             "album_weight": 0}]),))
        db.close()
        self.server = web.ReviewServer(("127.0.0.1", 0), self.reviewer)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read().decode()

    def post(self, path, fields):
        request = Request(self.base + path, urlencode(fields, doseq=True)
                          .encode(), method="POST")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.url
        except HTTPError as error:
            return error.code, ""

    def test_overview_and_album_page(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("LASTFM_API_KEY is not set", body)
        self.assertIn("looks after the genre tags in your library", body)
        status, body = self.get("/album/1")
        self.assertIn('value="Indie Rock" checked', body)
        self.assertIn('value="Rock" checked', body)
        self.assertIn("1 track in", body)

    def test_theme_button_steps_through_choices(self):
        _, body = self.get("/")
        self.assertIn('<html lang="en">', body)  # auto: no data-theme
        self.assertIn('name="theme" value="light"', body)
        self.assertIn("Theme: Auto (follows your system). Click for Light.",
                      body)
        status, url = self.post("/theme", {"token": self.server.token,
                                           "theme": "light",
                                           "back": "/tags?msg=x"})
        self.assertEqual(status, 200)
        self.assertTrue(url.endswith("/tags?msg=x"))
        _, body = self.get("/")
        self.assertIn('<html lang="en" data-theme="light">', body)
        self.assertIn('name="theme" value="dark"', body)
        self.post("/theme", {"token": self.server.token, "theme": "dark"})
        _, body = self.get("/")
        self.assertIn('<html lang="en" data-theme="dark">', body)
        self.assertIn('name="theme" value="auto"', body)
        # Unknown values fall back to auto; outside addresses go home.
        status, url = self.post("/theme", {"token": self.server.token,
                                           "theme": "neon",
                                           "back": "//example.com/"})
        self.assertEqual(url, self.base + "/")
        _, body = self.get("/")
        self.assertIn('<html lang="en">', body)
        status, _ = self.post("/theme", {"token": "wrong", "theme": "dark"})
        self.assertEqual(status, 403)

    def test_apply_needs_the_token(self):
        status, _ = self.post("/album/1/apply", {"token": "wrong",
                                                 "genre": "Jazz"})
        self.assertEqual(status, 403)
        self.assertEqual(genres_of(self.track), ["Rock/Pop"])

    def test_apply_writes_and_reports_mpd_failure(self):
        status, url = self.post("/album/1/apply", {
            "token": self.server.token, "genre": ["Indie Rock"],
            "extra": "Shoegaze, indie rock"})
        self.assertEqual(status, 200)
        self.assertIn("MPD+update+failed", url)
        self.assertEqual(genres_of(self.track), ["Indie Rock", "Shoegaze"])

    def test_empty_choice_writes_nothing(self):
        self.post("/album/1/apply", {"token": self.server.token})
        self.assertEqual(genres_of(self.track), ["Rock/Pop"])

    def test_bulk_skips_artist_only_suggestions(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET suggestions = ?", (store.dumps([
            {"name": "Blues", "weight": 90, "sources": ["artist"]}]),))
        db.close()
        _, body = self.get("/bulk")
        self.assertIn("No waiting album", body)

    def test_bulk_reports_mpd_failures(self):
        _, body = self.get("/bulk")
        self.assertIn('name="album" value="1"', body)
        status, url = self.post("/bulk", {"token": self.server.token,
                                          "album": "1"})
        self.assertEqual(status, 200)
        self.assertIn("Wrote+genres+for+1+albums", url)
        self.assertIn("MPD+was+not+told+about+1+albums", url)
        self.assertEqual(genres_of(self.track), ["Indie Rock", "Rock"])

    def test_bulk_needs_strong_album_tags_not_combined_score(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET suggestions = ?", (store.dumps([
            {"name": "Blues", "weight": 70, "sources": ["album", "artist"],
             "album_weight": 1}]),))
        db.close()
        _, body = self.get("/bulk")
        self.assertIn("No waiting album", body)

    def test_old_suggestions_are_looked_up_again(self):
        self.reviewer.settings.lastfm_api_key = "key"
        looked_up = []

        def fake(client, row):
            looked_up.append(row["id"])
            return []

        self.reviewer.suggestions_for = fake
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET suggested_at = ?", (store.now_iso(),))
        db.close()
        self.assertEqual(self.reviewer.suggest_missing(), 0)
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET suggested_at = '2000-01-01T00:00:00"
                   "+00:00'")
        db.close()
        self.assertEqual(self.reviewer.suggest_missing(), 1)
        self.assertEqual(looked_up, [1])

    def test_next_prefers_albums_that_were_looked_up(self):
        make_mp3(self.root / "0/First/01.mp3", artist="0", album="First")
        self.reviewer.scan()
        with urlopen(self.base + "/next", timeout=10) as response:
            self.assertTrue(response.url.endswith("/album/1"))

    def test_next_only_offers_pending_albums(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET status = 'changed'")
        db.close()
        with urlopen(self.base + "/next", timeout=10) as response:
            self.assertIn("Nothing+is+waiting+for+review", response.url)

    def test_nav_groups_the_genre_pages(self):
        def current(path):
            _, body = self.get(path)
            nav = body.split("</nav>")[0]
            return [label for label in ("Overview", "Genres", "Tags", "Lyrics")
                    if f'aria-current="page">{label}</a>' in nav]

        self.assertEqual(current("/"), ["Overview"])
        for path in ("/albums?filter=pending", "/albums?filter=applied",
                     "/bulk", "/album/1"):
            self.assertEqual(current(path), ["Genres"], path)
        self.assertEqual(current("/tags"), ["Tags"])
        self.assertEqual(current("/tags/field?name=TPUB"), ["Tags"])
        self.assertEqual(current("/lyrics"), ["Lyrics"])

    def test_genres_page_has_filters_and_review_buttons(self):
        _, body = self.get("/albums?filter=applied")
        self.assertIn("<h1>Genres</h1>", body)
        self.assertIn('href="/albums?filter=pending">Waiting for review (1)',
                      body)
        self.assertIn('href="/albums?filter=applied" aria-current="page">'
                      "Applied (0)", body)
        self.assertIn('href="/next">Review one at a time', body)
        self.assertIn('href="/bulk">Bulk accept', body)

    def test_review_list_offers_checkboxes_with_default_genres(self):
        _, body = self.get("/albums?filter=pending")
        self.assertIn('name="album" value="1"', body)
        self.assertNotIn('value="1" id="a1" checked', body)
        self.assertIn("Indie Rock, Rock", body)
        _, body = self.get("/albums?filter=pending&tick=all")
        self.assertIn('value="1" id="a1" checked', body)
        _, body = self.get("/albums?filter=applied")
        self.assertNotIn('name="album"', body)

    def test_approve_ticked_writes_default_genres(self):
        status, url = self.post("/approve", {
            "token": self.server.token, "filter": "pending", "page": "1",
            "album": "1"})
        self.assertEqual(status, 200)
        self.assertIn("filter=pending", url)
        self.assertIn("Wrote+genres+for+1+albums", url)
        self.assertEqual(genres_of(self.track), ["Indie Rock", "Rock"])

    def test_approve_refuses_albums_no_longer_in_review(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET status = 'ignored'")
        db.close()
        _, url = self.post("/approve", {"token": self.server.token,
                                        "album": "1"})
        self.assertIn("Nothing+written", url)
        self.assertEqual(genres_of(self.track), ["Rock/Pop"])

    def test_approve_changed_album_keeps_its_approved_genres(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET status = 'changed', applied_genres = ?",
                   (store.dumps(["Shoegaze"]),))
        db.close()
        self.post("/approve", {"token": self.server.token,
                               "filter": "changed", "album": "1"})
        self.assertEqual(genres_of(self.track), ["Shoegaze"])

    def test_ignored_list_can_ask_again(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET status = 'ignored'")
        db.close()
        _, body = self.get("/albums?filter=ignored")
        self.assertIn('name="album" value="1"', body)
        self.assertIn('action="/unignore"', body)
        self.assertIn("Scan the library now", body)
        _, body = self.get("/albums?filter=ignored&tick=all")
        self.assertIn('value="1" id="a1" checked', body)
        status, url = self.post("/unignore", {"token": "wrong", "album": "1"})
        self.assertEqual(status, 403)
        status, url = self.post("/unignore", {
            "token": self.server.token, "page": "1", "album": "1"})
        self.assertEqual(status, 200)
        self.assertIn("filter=ignored", url)
        self.assertIn("1+album+is+back+under+Waiting+for+review", url)
        db = self.reviewer.connect()
        status = db.execute("SELECT status FROM albums").fetchone()[0]
        db.close()
        self.assertEqual(status, "pending")

    def test_ask_again_leaves_other_albums_alone(self):
        db = self.reviewer.connect()
        db.execute("UPDATE albums SET status = 'applied'")
        db.close()
        _, url = self.post("/unignore", {"token": self.server.token,
                                         "album": "1"})
        self.assertIn("Nothing+changed", url)
        db = self.reviewer.connect()
        status = db.execute("SELECT status FROM albums").fetchone()[0]
        db.close()
        self.assertEqual(status, "applied")

    def test_scan_returns_to_the_page_that_asked(self):
        self.reviewer.scan = lambda: None
        self.reviewer.auto_clean = lambda: None
        self.reviewer.suggest_missing = lambda: None
        _, url = self.post("/scan", {"token": self.server.token,
                                     "back": "/albums?filter=ignored"})
        self.assertIn("/albums?filter=ignored&msg=Scan+started", url)
        _, url = self.post("/scan", {"token": self.server.token,
                                     "back": "//example.com/"})
        self.assertTrue(url.startswith(self.base + "/?msg=Scan+started"))
        _, url = self.post("/scan", {"token": self.server.token})
        self.assertTrue(url.startswith(self.base + "/?msg=Scan+started"))

    def test_clean_genres(self):
        self.assertEqual(web.clean_genres(["Rock,  Pop ", "rock"]),
                         ["Rock", "Pop"])
        with self.assertRaises(ValueError):
            web.clean_genres(["x" * 61])
        with self.assertRaises(ValueError):
            web.clean_genres(["bad\x07name"])


def add_clutter(path: Path):
    """Give an MP3 (ID3v2.3) a label, a barcode, ReplayGain and a cover."""
    id3 = ID3(path)
    id3.add(TPUB(encoding=3, text=["Some Label"]))
    id3.add(TXXX(encoding=3, desc="BARCODE", text=["0123456789"]))
    id3.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN",
                 text=["-7.00 dB"]))
    id3.add(APIC(encoding=3, mime="image/png", type=3, desc="",
                 data=b"\x89PNG fake"))
    id3.save(path, v2_version=3)


class TagTests(unittest.TestCase):
    """Inventory, removal with backups, restore, and the schema upgrade."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "library"
        self.track = self.root / "A/Album/01.mp3"
        make_mp3(self.track, genre=["Rock"])
        add_clutter(self.track)
        self.db = store.connect(Path(self.temp.name) / "db.sqlite")
        library.scan(self.db, self.root, log=lambda _: None)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def frames(self):
        return {tags.field_key(frame) for frame in ID3(self.track).values()}

    def test_inventory_counts_examples_and_protection(self):
        found = {item["field"]: item for item in tags.inventory(self.db)}
        self.assertEqual(found["TPUB"]["files"], 1)
        self.assertEqual(found["TPUB"]["examples"], ["Some Label"])
        self.assertFalse(found["TPUB"]["protected"])
        for field in ("TIT2", "TCON", "APIC:", "TXXX:REPLAYGAIN_TRACK_GAIN"):
            if field in found:
                self.assertTrue(found[field]["protected"], field)
        self.assertTrue(tags.is_protected("TXXX:MusicBrainz Album Id"))
        self.assertIn("TPUB", tags.describe("TPUB"))
        self.assertEqual(tags.describe("TXXX:BARCODE"),
                         "BARCODE (custom text, TXXX)")

    def test_rules_refuse_protected_fields(self):
        tags.save_rules(self.db, ["TPUB", "TIT2", "APIC:"])
        self.assertEqual(tags.rule_fields(self.db), {"TPUB"})

    def test_apply_removes_only_rule_fields_and_keeps_version(self):
        tags.save_rules(self.db, ["TPUB", "TXXX:BARCODE"])
        result = tags.apply_rules(self.db, self.root)
        self.assertEqual((result["files"], result["errors"]), (1, []))
        left = self.frames()
        self.assertNotIn("TPUB", left)
        self.assertNotIn("TXXX:BARCODE", left)
        self.assertIn("TXXX:REPLAYGAIN_TRACK_GAIN", left)
        self.assertIn("APIC:", left)
        self.assertEqual(ID3(self.track).version[:2], (2, 3))
        stored = self.db.execute("SELECT fields FROM tracks").fetchone()[0]
        self.assertNotIn("TPUB", store.loads(stored, {}))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM removed_tags").fetchone()[0], 2)

    def test_restore_field_puts_back_the_exact_frame(self):
        tags.save_rules(self.db, ["TPUB", "TXXX:BARCODE"])
        tags.apply_rules(self.db, self.root)
        result = tags.restore(self.db, self.root, field="TPUB")
        self.assertEqual(result["files"], 1)
        id3 = ID3(self.track)
        self.assertEqual(id3["TPUB"].text, ["Some Label"])
        self.assertNotIn("TXXX:BARCODE", self.frames())
        tags.restore(self.db, self.root, album_id=1)
        self.assertEqual(ID3(self.track)["TXXX:BARCODE"].text,
                         ["0123456789"])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM removed_tags WHERE restored_at IS NULL"
        ).fetchone()[0], 0)

    def test_backup_is_stored_before_the_file_changes(self):
        tags.save_rules(self.db, ["TPUB"])
        original = tags.write_removal

        def crash(path, fields, *rest):
            raise tags.TagError("disk full")

        tags.write_removal = crash
        try:
            result = tags.apply_rules(self.db, self.root)
        finally:
            tags.write_removal = original
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("TPUB", self.frames())
        self.assertEqual(self.db.execute(
            "SELECT field FROM removed_tags").fetchone()[0], "TPUB")

    def test_flac_files_are_never_changed(self):
        flac = self.root / "B/Other/01.flac"
        make_flac(flac, album="Other")
        library.scan(self.db, self.root, log=lambda _: None)
        tags.save_rules(self.db, ["album", "ALBUM"])
        self.assertEqual(tags.affected(self.db, tags.rule_fields(self.db)),
                         [])

    def test_genre_write_makes_the_scan_reread_fields(self):
        library.apply_album(self.db, self.root, 1, ["Pop"])
        self.assertIsNone(self.db.execute(
            "SELECT fields FROM tracks").fetchone()[0])
        self.assertEqual(library.scan(self.db, self.root,
                                      log=lambda _: None)["read"], 1)

    def test_cached_inventory_follows_changes(self):
        def tpub_files():
            found = {item["field"]: item
                     for item in tags.cached_inventory(self.db)}
            return found.get("TPUB", {}).get("files", 0)

        self.assertEqual(tpub_files(), 1)
        tags.save_rules(self.db, ["TPUB"])
        self.assertTrue({item["field"]: item for item in
                         tags.cached_inventory(self.db)}["TPUB"]["rule"])
        self.assertEqual(len(tags.cached_affected(self.db, {"TPUB"})), 1)
        tags.apply_rules(self.db, self.root)
        self.assertEqual(tpub_files(), 0)
        self.assertEqual(tags.cached_affected(self.db, {"TPUB"}), [])
        tags.restore(self.db, self.root, field="TPUB")
        self.assertEqual(tpub_files(), 1)

    def add_v1(self, path=None):
        """Give the test MP3 (or path) an ID3v1 block; return its bytes."""
        path = path or self.track
        id3 = ID3(path)
        id3.save(path, v1=2, v2_version=3)
        block = tags.read_v1(path)
        self.assertEqual(len(block), 128)
        library.scan(self.db, self.root, log=lambda _: None)
        return block

    def test_id3v1_is_listed_removed_and_restored_exactly(self):
        self.add_v1()
        original = self.track.read_bytes()
        found = {item["field"]: item for item in tags.inventory(self.db)}
        self.assertEqual(found["ID3v1"]["files"], 1)
        self.assertFalse(found["ID3v1"]["protected"])
        self.assertIn("ID3v1", tags.describe("ID3v1"))
        tags.save_rules(self.db, ["ID3v1"])
        result = tags.apply_rules(self.db, self.root)
        self.assertEqual((result["files"], result["errors"]), (1, []))
        self.assertEqual(tags.read_v1(self.track), b"")
        self.assertEqual(self.track.read_bytes(), original[:-128])
        self.assertIn("TPUB", self.frames())
        tags.restore(self.db, self.root, field="ID3v1")
        self.assertEqual(self.track.read_bytes(), original)

    def test_id3v1_and_frames_removed_together(self):
        block = self.add_v1()
        tags.save_rules(self.db, ["ID3v1", "TPUB"])
        tags.apply_rules(self.db, self.root)
        self.assertEqual(tags.read_v1(self.track), b"")
        self.assertNotIn("TPUB", self.frames())
        self.assertEqual(ID3(self.track).version[:2], (2, 3))
        tags.restore(self.db, self.root, album_id=1)
        self.assertEqual(tags.read_v1(self.track), block)
        self.assertEqual(ID3(self.track)["TPUB"].text, ["Some Label"])

    def test_file_with_only_id3v1(self):
        bare = self.root / "B/Bare/01.mp3"
        bare.parent.mkdir(parents=True)
        bare.write_bytes(b"\xff\xfb\x90\x64" + bytes(413)
                         + b"TAG" + b"Old Title".ljust(30, b"\0")
                         + b"Old Artist".ljust(30, b"\0") + bytes(65))
        self.assertEqual(tags.read_fields(bare),
                         {"ID3v1": "Old Title / Old Artist"})
        packed = tags.remove_from_file(bare, {"ID3v1", "TPUB"})
        self.assertEqual(set(packed), {"ID3v1"})
        tags.write_removal(bare, set(packed), packed["ID3v1"])
        self.assertEqual(bare.stat().st_size, 417)

    def test_v1_is_not_cut_when_the_file_changed(self):
        block = self.add_v1()
        tags.write_removal(self.track, {"ID3v1"}, b"TAG" + bytes(125))
        self.assertEqual(tags.read_v1(self.track), block)

    def test_sources_name_known_fields_only(self):
        self.assertTrue(tags.source("TXXX:ITUNESADVISORY")[0]
                        .startswith("deemix"))
        self.assertEqual(tags.source("TXXX:SCRIPT")[0], "Picard")
        self.assertTrue(tags.source("ID3v1")[0].startswith("deemix"))
        self.assertIn("Keep", tags.source("TBPM")[1])
        self.assertIsNone(tags.source("TXXX:SOMETHING ELSE"))

    def test_schema_3_upgrade_rereads_fields(self):
        path = Path(self.temp.name) / "v3.sqlite"
        old = store.sqlite3.connect(path)
        for script in (store.SCHEMA, store.MIGRATION_2, store.MIGRATION_3):
            for statement in script.split(";"):
                if statement.strip():
                    old.execute(statement)
        old.execute("INSERT INTO albums (album_key, albumartist, album, "
                    "folder, updated_at) VALUES ('k', 'A', 'B', 'f', 'x')")
        old.execute("INSERT INTO tracks (path, album_id, album_key, "
                    "albumartist, album, mtime_ns, size, genres, fields) "
                    "VALUES ('a.mp3', 1, 'k', 'A', 'B', 1, 1, '[]', '{}')")
        old.execute("PRAGMA user_version=3")
        old.commit()
        old.close()
        upgraded = store.connect(path)
        try:
            self.assertIsNone(upgraded.execute(
                "SELECT fields FROM tracks").fetchone()[0])
            self.assertFalse(tags.auto_clean_enabled(upgraded))
        finally:
            upgraded.close()

    def test_schema_1_database_is_upgraded(self):
        path = Path(self.temp.name) / "old.sqlite"
        old = store.sqlite3.connect(path)
        for statement in store.SCHEMA.split(";"):
            if statement.strip():
                old.execute(statement)
        old.execute("PRAGMA user_version=1")
        old.commit()
        old.close()
        upgraded = store.connect(path)
        try:
            self.assertEqual(upgraded.execute(
                "PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
            upgraded.execute("SELECT COUNT(*) FROM lyrics")
            columns = {row[1] for row in upgraded.execute(
                "PRAGMA table_info(tracks)")}
            self.assertIn("fields", columns)
            upgraded.execute("SELECT COUNT(*) FROM removed_tags")
        finally:
            upgraded.close()


class TagPageTests(unittest.TestCase):
    """The Tags pages end to end."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "library"
        self.track = self.root / "A/Album/01.mp3"
        make_mp3(self.track)
        add_clutter(self.track)
        settings = music_librarian.Settings(
            library_dir=self.root, state_dir=base / "state",
            lastfm_api_key="", listen="127.0.0.1", port=0,
            scan_interval_minutes=0, max_genres=3, bulk_min_weight=60,
            mpd_host="127.0.0.1", mpd_port=1, mpd_password="",
            mpd_path_prefix="")
        self.reviewer = music_librarian.Reviewer(settings, log=lambda _: None)
        self.reviewer.scan()
        self.server = web.ReviewServer(("127.0.0.1", 0), self.reviewer)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=10) as response:
            return response.read().decode()

    def post(self, path, fields):
        request = Request(self.base + path, urlencode(fields, doseq=True)
                          .encode(), method="POST")
        with urlopen(request, timeout=10) as response:
            return response.url

    def test_tags_page_lists_fields_and_protects_core_ones(self):
        body = self.get("/tags")
        self.assertIn('value="TPUB"', body)
        self.assertNotIn('value="TIT2"', body)
        self.assertIn(">kept<", body)
        # Long field names and examples wrap inside the table cells.
        self.assertIn(".list td { overflow-wrap: anywhere; }", body)
        self.assertIn("Artist - Album", self.get("/tags/field?name=TPUB"))

    def test_save_rules_remove_and_restore_through_the_page(self):
        token = self.server.token
        url = self.post("/tags/rules", {"token": token, "field": ["TPUB"]})
        self.assertIn("1+fields+marked", url)
        self.assertIn("1 MP3 files", self.get("/tags"))
        self.post("/tags/apply", {"token": token})
        self.reviewer.tag_job.join(10)
        self.assertNotIn("TPUB", ID3(self.track))
        self.assertIn("Removed tags from 1 files",
                      self.reviewer.status["tags"])
        body = self.get("/album/1")
        self.assertIn("Tags removed by the cleanup", body)
        url = self.post("/album/1/restore-tags", {"token": token})
        self.assertIn("Restored+tags+in+1+files", url)
        self.assertEqual(ID3(self.track)["TPUB"].text, ["Some Label"])

    def test_page_shows_sources_and_auto_switch(self):
        body = self.get("/tags")
        self.assertIn(">Source<", body)
        self.assertIn("deemix (label)", body)
        self.assertIn('name="enabled" value="1">', body)
        url = self.post("/tags/auto", {"token": self.server.token,
                                       "enabled": "1"})
        self.assertIn("Automatic+cleanup+is+on", url)
        self.assertIn('value="1" checked>', self.get("/tags"))
        url = self.post("/tags/auto", {"token": self.server.token})
        self.assertIn("Automatic+cleanup+is+off", url)

    def test_auto_clean_after_scan_handles_new_files(self):
        token = self.server.token
        self.post("/tags/rules", {"token": token, "field": ["TPUB"]})
        self.post("/tags/apply", {"token": token})
        self.reviewer.tag_job.join(10)
        # Removed everywhere, the field can still be kept as a rule.
        self.assertIn('value="TPUB" checked', self.get("/tags"))
        newer = self.root / "B/Newer/01.mp3"
        make_mp3(newer, artist="B", album="Newer")
        add_clutter(newer)
        self.reviewer.scan()
        self.reviewer.auto_clean()
        self.assertIn("TPUB", ID3(newer))
        self.post("/tags/auto", {"token": token, "enabled": "1"})
        self.reviewer.auto_clean()
        self.assertNotIn("TPUB", ID3(newer))
        self.assertIn("Auto-cleaned 1 files in 1 albums",
                      self.reviewer.status["tags"])
        stored = self.reviewer.connect()
        try:
            self.assertEqual(stored.execute(
                "SELECT COUNT(*) FROM removed_tags WHERE path LIKE 'B/%'"
            ).fetchone()[0], 1)
        finally:
            stored.close()

    def test_tag_forms_need_the_token(self):
        request = Request(self.base + "/tags/apply", b"token=wrong",
                          method="POST")
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 403)


class FakeLrclib:
    """Answers LRCLIB requests from a dict: (endpoint, title) -> reply."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def opener(self, request, timeout):
        from urllib.parse import parse_qs, urlsplit
        url = urlsplit(request.full_url)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        endpoint = url.path.rsplit("/", 1)[1]
        self.calls.append((endpoint, params))
        self.agent = request.get_header("User-agent")
        reply = self.replies.get((endpoint, params.get("track_name")))
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            raise HTTPError(request.full_url, 404, "Not Found", {},
                            io.BytesIO(b"{}"))
        return FakeResponse(json.dumps(reply).encode())

    def client(self):
        return lyrics.Lrclib(opener=self.opener, sleep=lambda _: None)


SYNCED = {"id": 1, "duration": 0, "syncedLyrics": "[00:01.00] la\r\n",
          "plainLyrics": "la", "instrumental": False}


class LyricsTests(unittest.TestCase):
    """LRCLIB lookups, sidecar files, coverage and undo."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "library"
        self.db = store.connect(Path(self.temp.name) / "db.sqlite")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def track(self, name, title, artist="Artist", album="Album"):
        path = self.root / "A" / name
        make_mp3(path, artist=artist, album=album)
        tags_ = EasyID3(path)
        tags_["title"] = title
        tags_.save()
        return path

    def scan(self):
        library.scan(self.db, self.root, log=lambda _: None)

    def test_name_cleaning(self):
        self.assertEqual(lyrics.clean_name("Song (2013 Remaster)"), "Song")
        self.assertEqual(lyrics.clean_name("Song - Remastered 2011"), "Song")
        self.assertEqual(lyrics.clean_name("Song (Live)"), "Song (Live)")
        self.assertEqual(lyrics.clean_name("Song (feat. X)"), "Song")
        self.assertEqual(lyrics.primary_artist("A/B"), "A")
        self.assertEqual(lyrics.primary_artist("A feat. B"), "A")

    def test_fill_writes_synced_plain_and_records_the_rest(self):
        one = self.track("01.mp3", "One")
        two = self.track("02.mp3", "Two")
        self.track("03.mp3", "Three")
        four = self.track("04.mp3", "Four")
        self.scan()
        fake = FakeLrclib({
            ("get", "One"): SYNCED,
            ("get", "Two"): {"id": 2, "syncedLyrics": None,
                             "plainLyrics": "words", "instrumental": False},
            ("get", "Four"): {"id": 4, "instrumental": True,
                              "syncedLyrics": None, "plainLyrics": None},
        })
        paths = lyrics.due(self.db, self.root)
        self.assertEqual(len(paths), 4)
        counts = lyrics.fill(self.db, self.root, paths, client=fake.client())
        self.assertEqual((counts["synced"], counts["plain"],
                          counts["instrumental"], counts["notfound"]),
                         (1, 1, 1, 1))
        self.assertEqual(one.with_suffix(".lrc").read_text(),
                         "[00:01.00] la\n")
        self.assertEqual(two.with_suffix(".txt").read_text(), "words\n")
        self.assertFalse(four.with_suffix(".lrc").exists())
        self.assertIn("music-librarian", fake.agent)
        totals = lyrics.coverage(self.db, self.root)["totals"]
        self.assertEqual(totals, {"synced-file": 1, "plain-file": 1,
                                  "not-found": 1, "instrumental": 1})
        self.assertEqual(lyrics.due(self.db, self.root), [])

    def test_existing_lyrics_are_never_replaced_or_looked_up(self):
        one = self.track("01.mp3", "One")
        one.with_suffix(".txt").write_text("mine")
        self.scan()
        self.assertEqual(lyrics.due(self.db, self.root), [])
        with self.assertRaises(FileExistsError):
            lyrics.write_new(one.with_suffix(".txt"), "theirs")
        self.assertEqual(one.with_suffix(".txt").read_text(), "mine")

    def test_embedded_lyrics_count_as_present(self):
        from mutagen.id3 import USLT
        one = self.track("01.mp3", "One")
        id3 = ID3(one)
        id3.add(USLT(encoding=3, lang="eng", desc="", text="la"))
        id3.save(one)
        self.scan()
        self.assertEqual(lyrics.coverage(self.db, self.root)["totals"],
                         {"embedded": 1})

    def test_fallbacks_clean_names_then_search_by_duration(self):
        self.track("01.mp3", "Song (2013 Remaster)", artist="A/B")
        self.scan()
        fake = FakeLrclib({("search", "Song"): [
            {"id": 7, "duration": 999, "syncedLyrics": "[00:01.00] far"},
            {"id": 8, "duration": 0, "syncedLyrics": None,
             "plainLyrics": "near"},
            {"id": 9, "duration": 1, "syncedLyrics": "[00:01.00] best"}]})
        lyrics.fill(self.db, self.root, lyrics.due(self.db, self.root),
                    client=fake.client())
        self.assertEqual([call[0] for call in fake.calls],
                         ["get", "get", "search"])
        self.assertEqual(fake.calls[1][1]["track_name"], "Song")
        self.assertEqual(fake.calls[1][1]["artist_name"], "A")
        written = (self.root / "A/01.lrc").read_text()
        self.assertEqual(written, "[00:01.00] best\n")

    def test_live_take_gets_plain_lyrics_from_another_length(self):
        self.track("01.mp3", "Song (Live at Anaheim, 1975)")
        self.scan()
        fake = FakeLrclib({("search", "Song"): [
            {"id": 3, "duration": 250, "instrumental": False,
             "syncedLyrics": "[00:10.00] first line\n[00:12.00] second",
             "plainLyrics": None}]})
        counts = lyrics.fill(self.db, self.root,
                             lyrics.due(self.db, self.root),
                             client=fake.client())
        self.assertEqual(counts["plain"], 1)
        self.assertEqual([c[1]["track_name"] for c in fake.calls[-2:]],
                         ["Song (Live at Anaheim, 1975)", "Song"])
        self.assertEqual((self.root / "A/01.txt").read_text(),
                         "first line\nsecond\n")
        self.assertFalse((self.root / "A/01.lrc").exists())

    def test_untagged_file_uses_its_file_name(self):
        path = self.root / "A/1.01 Eagles - The Long Run (2013 Remaster).mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes((b"\xff\xfb\x90\x64" + bytes(413)) * 3)
        info = lyrics.track_info(path)
        self.assertEqual((info["artist"], info["title"]),
                         ("Eagles", "The Long Run (2013 Remaster)"))

    def test_rate_limit_stops_the_fill(self):
        self.track("01.mp3", "One")
        self.scan()
        busy = HTTPError("u", 429, "Too Many", {"Retry-After": "1"},
                         io.BytesIO(b""))
        fake = FakeLrclib({("get", "One"): busy})
        counts = lyrics.fill(self.db, self.root,
                             lyrics.due(self.db, self.root),
                             client=fake.client())
        self.assertIn("429", counts["reason"])
        self.assertEqual(len(fake.calls), lyrics.ATTEMPTS)

    def test_not_found_is_retried_only_after_the_waiting_period(self):
        self.track("01.mp3", "One")
        self.scan()
        lyrics.fill(self.db, self.root, lyrics.due(self.db, self.root),
                    client=FakeLrclib({}).client())
        self.assertEqual(lyrics.due(self.db, self.root), [])
        self.db.execute("UPDATE lyrics SET checked_at = '2000-01-01'")
        self.assertEqual(len(lyrics.due(self.db, self.root)), 1)
        self.assertEqual(lyrics.due(self.db, self.root, only_new=True), [])

    def test_deleted_written_file_is_fetched_again_on_a_manual_fill(self):
        one = self.track("01.mp3", "One")
        self.scan()
        fake = FakeLrclib({("get", "One"): SYNCED})
        lyrics.fill(self.db, self.root, lyrics.due(self.db, self.root),
                    client=fake.client())
        one.with_suffix(".lrc").unlink()
        self.assertEqual(lyrics.due(self.db, self.root, only_new=True), [])
        self.assertEqual(len(lyrics.due(self.db, self.root)), 1)

    def test_remove_written_keeps_edited_files(self):
        one = self.track("01.mp3", "One")
        two = self.track("02.mp3", "Two")
        self.scan()
        fake = FakeLrclib({("get", "One"): SYNCED, ("get", "Two"): SYNCED})
        lyrics.fill(self.db, self.root, lyrics.due(self.db, self.root),
                    client=fake.client())
        two.with_suffix(".lrc").write_text("edited by hand")
        result = lyrics.remove_written(self.db, self.root)
        self.assertEqual(result, {"removed": 1, "kept": 1})
        self.assertFalse(one.with_suffix(".lrc").exists())
        self.assertTrue(two.with_suffix(".lrc").exists())


class LyricsPageBase(unittest.TestCase):
    """A running page with one track and LRCLIB faked (no tests here)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "library"
        self.track = self.root / "A/Album/01.mp3"
        make_mp3(self.track)
        tags_ = EasyID3(self.track)
        tags_["title"] = "One"
        tags_.save()
        settings = music_librarian.Settings(
            library_dir=self.root, state_dir=base / "state",
            lastfm_api_key="", listen="127.0.0.1", port=0,
            scan_interval_minutes=0, max_genres=3, bulk_min_weight=60,
            mpd_host="127.0.0.1", mpd_port=1, mpd_password="",
            mpd_path_prefix="")
        self.reviewer = music_librarian.Reviewer(settings, log=lambda _: None)
        self.reviewer.scan()
        self.fake = FakeLrclib({("get", "One"): SYNCED})
        self.original = lyrics.Lrclib
        lyrics.Lrclib = lambda: self.original(opener=self.fake.opener,
                                              sleep=lambda _: None)
        self.server = web.ReviewServer(("127.0.0.1", 0), self.reviewer)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        lyrics.Lrclib = self.original
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=10) as response:
            return response.read().decode()

    def post(self, path, fields):
        request = Request(self.base + path, urlencode(fields).encode(),
                          method="POST")
        with urlopen(request, timeout=10) as response:
            return response.url


class LyricsPageTests(LyricsPageBase):
    """The Lyrics page with LRCLIB faked."""

    def test_fill_and_remove_through_the_page(self):
        body = self.get("/lyrics")
        self.assertIn("Fill missing lyrics (1 tracks)", body)
        self.post("/lyrics/fill", {"token": self.server.token})
        self.reviewer.lyrics_job.join(10)
        self.assertTrue(self.track.with_suffix(".lrc").exists())
        self.assertIn("1 synced", self.reviewer.status["lyrics"])
        self.assertRegex(self.reviewer.status["lyrics"],
                         r"^\d\d/\d\d/\d{4} \d{1,2}:\d\d [AP]M [A-Z+0-9-]+: ")
        url = self.post("/lyrics/remove", {"token": self.server.token})
        self.assertIn("tick+the+box", url)
        self.assertTrue(self.track.with_suffix(".lrc").exists())
        url = self.post("/lyrics/remove", {"token": self.server.token,
                                           "confirm": "yes"})
        self.assertIn("Removed+1+lyrics+files", url)
        self.assertFalse(self.track.with_suffix(".lrc").exists())


class OldDatabaseTests(unittest.TestCase):
    """A database from the genre-review name is picked up on first start."""

    def settings(self, base):
        return music_librarian.Settings(
            library_dir=base, state_dir=base / "state",
            lastfm_api_key="", listen="127.0.0.1", port=0,
            scan_interval_minutes=0, max_genres=3, bulk_min_weight=60,
            mpd_host="127.0.0.1", mpd_port=1, mpd_password="",
            mpd_path_prefix="library")

    def test_old_database_is_renamed_with_its_data(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = self.settings(Path(folder))
            settings.state_dir.mkdir()
            old = settings.state_dir / music_librarian.OLD_DB_NAME
            connection = store.connect(old)
            connection.execute(
                "INSERT INTO tag_rules (field, updated_at) VALUES ('X', 'now')")
            connection.commit()
            connection.close()
            music_librarian.adopt_old_database(settings, log=lambda _: None)
            self.assertFalse(old.exists())
            connection = store.connect(settings.db_path)
            self.assertEqual(connection.execute(
                "SELECT field FROM tag_rules").fetchall()[0][0], "X")
            connection.close()

    def test_existing_new_database_is_left_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = self.settings(Path(folder))
            settings.state_dir.mkdir()
            old = settings.state_dir / music_librarian.OLD_DB_NAME
            old.write_bytes(b"old")
            settings.db_path.write_bytes(b"new")
            music_librarian.adopt_old_database(settings, log=lambda _: None)
            self.assertEqual(old.read_bytes(), b"old")
            self.assertEqual(settings.db_path.read_bytes(), b"new")


class CliTests(unittest.TestCase):
    """No command prints help; settings are validated."""

    def test_no_arguments_prints_help(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(music_librarian.main([]), 0)
        self.assertIn("usage:", output.getvalue())

    def test_missing_library_is_a_clear_error(self):
        os.environ.pop("LIBRARY_DIR", None)
        with self.assertRaises(SystemExit) as caught:
            music_librarian.main(["scan"])
        self.assertIn("LIBRARY_DIR", str(caught.exception))

    def test_flag_setting(self):
        os.environ["LYRICS_AUTO_FILL"] = "maybe"
        try:
            with self.assertRaises(SystemExit):
                music_librarian.env_flag("LYRICS_AUTO_FILL")
            os.environ["LYRICS_AUTO_FILL"] = "yes"
            self.assertTrue(music_librarian.env_flag("LYRICS_AUTO_FILL"))
        finally:
            del os.environ["LYRICS_AUTO_FILL"]

    def test_bad_number_setting(self):
        os.environ["MAX_GENRES"] = "lots"
        try:
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(SystemExit) as caught:
                    music_librarian.main(["-L", folder, "-S", folder, "scan"])
            self.assertIn("MAX_GENRES", str(caught.exception))
        finally:
            del os.environ["MAX_GENRES"]


class FakeDeezerServer:
    """Answers Deezer requests: login, search and lyrics, from dicts."""

    def __init__(self, tracks=None, lyrics_by_id=None, jwt="token"):
        self.tracks = tracks or {}          # search text -> [hits]
        self.lyrics_by_id = lyrics_by_id or {}
        self.jwt = jwt
        self.calls = []
        self.reject_tokens = set()

    def opener(self, request, timeout):
        url = request.full_url
        if url.startswith(deezer.AUTH_URL.split("?")[0]):
            self.calls.append("login")
            self.cookie = request.get_header("Cookie")
            return FakeResponse(json.dumps(
                {"jwt": self.jwt} if self.jwt else {}).encode())
        if url.startswith(deezer.SEARCH_URL):
            from urllib.parse import parse_qs, urlsplit
            text = parse_qs(urlsplit(url).query)["q"][0]
            self.calls.append(("search", text))
            return FakeResponse(json.dumps(
                {"data": self.tracks.get(text, [])}).encode())
        body = json.loads(request.data)
        token = request.get_header("Authorization").split()[-1]
        self.calls.append(("lyrics", body["variables"]["id"], token))
        if token in self.reject_tokens:
            return FakeResponse(json.dumps({"errors": [
                {"type": "JwtTokenInvalidError"}], "data": {"track": None}})
                .encode())
        return FakeResponse(json.dumps({"data": {"track": {
            "lyrics": self.lyrics_by_id.get(body["variables"]["id"])}}})
            .encode())

    def client(self, clock=None):
        options = {"opener": self.opener, "sleep": lambda _: None}
        if clock:
            options["clock"] = clock
        return deezer.Deezer("secret-arl", **options)


DEEZER_SYNCED = {"text": "la la", "synchronizedLines": [
    {"lrcTimestamp": "[00:01.00]", "line": "la"},
    {"lrcTimestamp": "[00:02.50]", "line": "la"}]}
DEEZER_PLAIN = {"text": "words only", "synchronizedLines": None}


class DeezerClientTests(unittest.TestCase):
    """The Deezer client: matching, LRC text, tokens and errors."""

    def test_read_arl_prefers_the_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".arl"
            path.write_text("from-file\n")
            self.assertEqual(deezer.read_arl(str(path), "from-env"),
                             "from-file")
            path.write_text("")
            self.assertEqual(deezer.read_arl(str(path), "from-env"),
                             "from-env")
            self.assertEqual(deezer.read_arl(str(Path(temp) / "gone"), ""),
                             "")
        with self.assertRaises(deezer.DeezerError):
            deezer.Deezer("")

    def test_artist_matching_and_lrc(self):
        self.assertTrue(deezer.same_artist(
            "Gene Autry with Carl Cotner & His Orchestra", "Gene Autry"))
        self.assertTrue(deezer.same_artist(
            "Austin Meade/Treaty Oak Revival", "Austin Meade"))
        self.assertTrue(deezer.same_artist("Beyoncé", "Beyonce"))
        self.assertFalse(deezer.same_artist("Vince Gill", "Reba McEntire"))
        self.assertEqual(deezer.to_lrc(DEEZER_SYNCED["synchronizedLines"]),
                         "[00:01.00]la\n[00:02.50]la")
        self.assertEqual(deezer.to_lrc(None), "")

    def test_find_needs_artist_and_length_to_match(self):
        fake = FakeDeezerServer(tracks={"Vince Gill Her Heart": [
            {"id": 1, "duration": 300, "artist": {"name": "Vince Gill"}},
            {"id": 2, "duration": 224, "artist": {"name": "Someone Else"}},
            {"id": 3, "duration": 225, "artist": {"name": "Vince Gill"}}]})
        client = fake.client()
        hit = client.find({"title": "Her Heart", "artist": "Vince Gill",
                           "duration": 223.4})
        self.assertEqual(hit["id"], 3)
        self.assertIsNone(client.find({"title": "Her Heart",
                                       "artist": "Vince Gill",
                                       "duration": 100}))
        self.assertIsNone(client.find({"title": "", "artist": "X",
                                       "duration": 100}))

    def test_token_is_renewed_when_old_or_rejected(self):
        now = [0.0]
        fake = FakeDeezerServer(lyrics_by_id={"7": DEEZER_SYNCED})
        client = fake.client(clock=lambda: now[0])
        self.assertEqual(client.lyrics(7)["synced"],
                         "[00:01.00]la\n[00:02.50]la")
        self.assertEqual(fake.cookie, "arl=secret-arl")
        client.lyrics(7)
        self.assertEqual(fake.calls.count("login"), 1)
        now[0] += deezer.TOKEN_SECONDS + 1  # token too old: log in again
        client.lyrics(7)
        self.assertEqual(fake.calls.count("login"), 2)
        fake.reject_tokens.add("token")      # rejected early: once more
        fake.jwt = "fresh"
        self.assertEqual(client.lyrics(7)["plain"], "la la")
        self.assertEqual(fake.calls.count("login"), 3)

    def test_refused_login_and_missing_lyrics(self):
        fake = FakeDeezerServer(jwt="")
        with self.assertRaises(deezer.DeezerError):
            fake.client().lyrics(1)
        fake = FakeDeezerServer()
        self.assertEqual(fake.client().lyrics(1), {"synced": "",
                                                   "plain": ""})

    def test_http_401_stops_with_a_clear_error(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 401, "Unauthorized", {},
                            io.BytesIO(b""))
        client = deezer.Deezer("arl", opener=opener, sleep=lambda _: None)
        with self.assertRaisesRegex(deezer.DeezerError, "expired"):
            client.lyrics(1)


class FakeDeezerClient:
    """Stands in for deezer.Deezer: title -> (track id, lyrics)."""

    def __init__(self, answers, error=None):
        self.answers = answers
        self.error = error
        self.asked = []

    def find(self, info):
        self.asked.append(info.get("title"))
        if self.error:
            raise self.error
        answer = self.answers.get(info.get("title"))
        return {"id": answer[0]} if answer else None

    def lyrics(self, track_id):
        for found_id, found in self.answers.values():
            if found_id == track_id:
                return found
        return {"synced": "", "plain": ""}


class DeezerFillTests(LyricsTests):
    """The Deezer pass after LRCLIB (reuses the LRCLIB test helpers)."""

    def lrclib_pass(self, replies):
        self.scan()
        fake = FakeLrclib(replies)
        lyrics.fill(self.db, self.root, lyrics.due(self.db, self.root),
                    client=fake.client())

    def test_deezer_fills_missing_and_upgrades_plain(self):
        missing = self.track("01.mp3", "Missing")
        plain = self.track("02.mp3", "Plain")
        self.track("03.mp3", "Nowhere")
        synced = self.track("04.mp3", "Synced")
        words = self.track("05.mp3", "Words")
        self.lrclib_pass({
            ("get", "Plain"): {"id": 2, "syncedLyrics": None,
                               "plainLyrics": "plain", "instrumental": False},
            ("get", "Synced"): SYNCED,
        })
        due = dict(lyrics.deezer_due(self.db, self.root))
        self.assertEqual(due, {"A/01.mp3": "missing", "A/02.mp3": "upgrade",
                               "A/03.mp3": "missing",
                               "A/05.mp3": "missing"})
        client = FakeDeezerClient({
            "Missing": (11, {"synced": "[00:01.00]m", "plain": "m"}),
            "Plain": (12, {"synced": "[00:01.00]p", "plain": "p"}),
            "Words": (15, {"synced": "", "plain": "just words"}),
        })
        result = lyrics.deezer_fill(self.db, self.root,
                                    lyrics.deezer_due(self.db, self.root),
                                    client)
        self.assertEqual((result["synced"], result["plain"],
                          result["upgraded"], result["nomatch"]),
                         (1, 1, 1, 1))
        self.assertEqual(missing.with_suffix(".lrc").read_text(),
                         "[00:01.00]m\n")
        self.assertTrue(plain.with_suffix(".lrc").is_file())
        self.assertFalse(plain.with_suffix(".txt").exists())
        self.assertEqual(words.with_suffix(".txt").read_text(),
                         "just words\n")
        self.assertFalse(synced.with_suffix(".txt").exists())
        rows = {row["path"]: row for row in self.db.execute(
            "SELECT * FROM lyrics")}
        self.assertEqual(rows["A/02.mp3"]["source"], "deezer")
        self.assertEqual(rows["A/02.mp3"]["status"], "synced")
        self.assertEqual(rows["A/04.mp3"]["source"], "lrclib")
        self.assertEqual(rows["A/03.mp3"]["deezer_status"], "nomatch")
        # Nothing is due again until the retry time has passed.
        self.assertEqual(lyrics.deezer_due(self.db, self.root), [])
        # Undo removes Deezer's files like LRCLIB's.
        removed = lyrics.remove_written(self.db, self.root)
        self.assertEqual(removed["removed"], 4)
        self.assertFalse(plain.with_suffix(".lrc").exists())

    def test_changed_plain_file_is_not_upgraded(self):
        plain = self.track("01.mp3", "Plain")
        self.lrclib_pass({("get", "Plain"): {
            "id": 2, "syncedLyrics": None, "plainLyrics": "plain",
            "instrumental": False}})
        plain.with_suffix(".txt").write_text("my own edit\n")
        client = FakeDeezerClient({"Plain": (12, {"synced": "[00:01.00]p",
                                                  "plain": "p"})})
        result = lyrics.deezer_fill(self.db, self.root,
                                    lyrics.deezer_due(self.db, self.root),
                                    client)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(client.asked, [])
        self.assertFalse(plain.with_suffix(".lrc").exists())
        self.assertEqual(plain.with_suffix(".txt").read_text(),
                         "my own edit\n")

    def test_refused_login_stops_the_pass(self):
        self.track("01.mp3", "One")
        self.track("02.mp3", "Two")
        self.lrclib_pass({})
        client = FakeDeezerClient({}, error=deezer.DeezerError("expired"))
        result = lyrics.deezer_fill(self.db, self.root,
                                    lyrics.deezer_due(self.db, self.root),
                                    client)
        self.assertEqual(result["reason"], "expired")
        self.assertEqual(client.asked, ["One"])
        self.assertEqual(len(lyrics.deezer_due(self.db, self.root)), 2)

    def test_lrclib_recheck_keeps_deezer_columns(self):
        self.track("01.mp3", "One")
        self.lrclib_pass({})
        lyrics.deezer_fill(self.db, self.root,
                           lyrics.deezer_due(self.db, self.root),
                           FakeDeezerClient({}))
        fake = FakeLrclib({})
        lyrics.fill(self.db, self.root, ["A/01.mp3"], client=fake.client())
        row = self.db.execute("SELECT deezer_status, deezer_checked_at "
                              "FROM lyrics").fetchone()
        self.assertEqual(row["deezer_status"], "nomatch")
        self.assertIsNotNone(row["deezer_checked_at"])

    def test_schema_4_upgrade_marks_written_files_as_lrclib(self):
        path = Path(self.temp.name) / "v4.sqlite"
        old = store.sqlite3.connect(path)
        for script in (store.SCHEMA, store.MIGRATION_2, store.MIGRATION_3,
                       store.MIGRATION_4):
            for statement in script.split(";"):
                if statement.strip():
                    old.execute(statement)
        old.execute("INSERT INTO lyrics (path, status, written_file, "
                    "written_sha256, checked_at) VALUES ('a.mp3', 'plain', "
                    "'a.txt', 'x', 'now'), ('b.mp3', 'notfound', NULL, NULL, "
                    "'now')")
        old.execute("PRAGMA user_version=4")
        old.commit()
        old.close()
        upgraded = store.connect(path)
        try:
            rows = dict(upgraded.execute("SELECT path, source FROM lyrics"))
            self.assertEqual(rows, {"a.mp3": "lrclib", "b.mp3": None})
        finally:
            upgraded.close()


class DeezerPageTests(LyricsPageBase):
    """The Lyrics page and fill job with Deezer turned on."""

    def setUp(self):
        super().setUp()
        self.fake.replies.clear()  # LRCLIB has nothing
        self.reviewer.settings.deezer_lyrics = True
        self.client = FakeDeezerClient({"One": (
            9, {"synced": "[00:01.00]one", "plain": "one"})})
        self.reviewer.deezer_client = lambda: self.client

    def test_fill_runs_deezer_after_lrclib(self):
        result = self.reviewer.fill_lyrics()
        self.assertEqual(result["notfound"], 1)
        self.assertEqual(result["deezer"]["synced"], 1)
        self.assertTrue(self.track.with_suffix(".lrc").is_file())
        self.assertIn("1 synced", self.reviewer.status["deezer"])
        body = self.get("/lyrics")
        self.assertIn("Last Deezer pass:", body)
        self.assertIn("1 from Deezer", body)

    def test_page_offers_the_deezer_lookups(self):
        connection = self.reviewer.connect()
        lyrics.fill(connection, self.root, ["A/Album/01.mp3"],
                    client=self.original(opener=self.fake.opener,
                                         sleep=lambda _: None))
        connection.close()
        body = self.get("/lyrics")
        self.assertIn("Fill missing lyrics (1 on Deezer)", body)
        self.assertIn("Deezer is asked next", body)

    def test_missing_arl_is_reported(self):
        self.reviewer.deezer_client = lambda: deezer.Deezer("")
        self.reviewer.fill_lyrics()
        self.assertIn("no Deezer ARL", self.reviewer.status["deezer"])


if __name__ == "__main__":
    unittest.main()
