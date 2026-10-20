#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pruebas offline de sync.py (sin red, sin credenciales).

Ejecutar desde la raíz del proyecto:
    .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sync  # noqa: E402


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def make_track(name="Song", artist="Artist", duration=200, sid="track-1"):
    return sync.Track(sid=sid, name=name, artists=[artist], album="Album", duration_s=duration)


def make_result(title="Song", artist="Artist", duration=200, video_id="vid-1", video_type=""):
    return {
        "title": title,
        "artists": [{"name": artist}],
        "duration_seconds": duration,
        "videoId": video_id,
        "videoType": video_type,
    }


def make_args(**overrides):
    values = dict(
        fresh=False,
        reuse_by_title=True,
        privacy="PRIVATE",
        prune=True,
        force_prune=False,
        order=True,
        covers=True,
        update_metadata=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeYT:
    """YouTube Music de mentira, con consistencia eventual simulada."""

    def __init__(self, video_ids=(), library=None):
        self.items = [
            {"videoId": v, "setVideoId": f"sv{i}"} for i, v in enumerate(video_ids)
        ]
        self._next = len(self.items)
        self._snapshot = [dict(i) for i in self.items]
        self.stale_reads = 0
        self.calls = []
        self.deleted = []
        self.created = []
        self.edits = []
        self.fail_cover_once = False
        self.drop_on_add = set()
        self.cover_forbidden = False
        self.fail_delete = False
        self.requests = []
        self.yt = self
        self.library = list(library or [])

    # --- playlist de trabajo -------------------------------------------------
    def _freeze(self):
        self._snapshot = [dict(i) for i in self.items]

    def get_items(self, playlist_id):
        if self.stale_reads > 0:
            self.stale_reads -= 1
            return [dict(i) for i in self._snapshot]
        return [dict(i) for i in self.items]

    def add_items(self, playlist_id, video_ids, duplicates=False):
        self._freeze()
        for video_id in video_ids:
            if video_id in self.drop_on_add:
                continue
            self.items.append({"videoId": video_id, "setVideoId": f"sv{self._next}"})
            self._next += 1
        self.calls.append(("add", list(video_ids), duplicates))

    def remove_items(self, playlist_id, items):
        self._freeze()
        ids = {item["setVideoId"] for item in items}
        self.items = [item for item in self.items if item["setVideoId"] not in ids]
        self.calls.append(("remove", ids))

    def move_items(self, playlist_id, moves):
        self._freeze()
        self.stale_reads = max(self.stale_reads, 2)  # la API tarda en reflejarlo
        for item_id, successor_id in moves:
            index = next(
                (i for i, item in enumerate(self.items) if item["setVideoId"] == item_id),
                None,
            )
            if index is None:
                continue
            item = self.items.pop(index)
            if successor_id is None:
                self.items.append(item)
                continue
            successor = next(
                (i for i, x in enumerate(self.items) if x["setVideoId"] == successor_id),
                None,
            )
            if successor is None:
                self.items.append(item)
            else:
                self.items.insert(successor, item)
        self.calls.append(("move", list(moves)))

    # --- biblioteca ----------------------------------------------------------
    def call(self, desc, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def library_playlists(self):
        return list(self.library)

    def refresh_library(self):
        pass

    def find_library_playlist(self, title, exclude_ids):
        want = title.strip().casefold()
        for playlist in self.library:
            if playlist["id"] in exclude_ids or not playlist.get("owned"):
                continue
            if playlist["title"].strip().casefold() == want:
                return playlist
        return None

    def create_playlist(self, title, description, privacy):
        playlist_id = f"NEW{len(self.created)}"
        self.created.append((title, description, privacy, playlist_id))
        self.library.append(
            {"id": playlist_id, "title": title, "count": 0, "owned": True}
        )
        return playlist_id

    def delete_playlist(self, playlist_id):
        if self.fail_delete:
            raise RuntimeError("YouTube Music no borró la playlist (sigue en tu biblioteca)")
        self.deleted.append(playlist_id)
        self.library = [p for p in self.library if p["id"] != playlist_id]

    def edit_metadata(self, playlist_id, title=None, description=None, image_path=None):
        self.edits.append(
            {
                "playlist": playlist_id,
                "title": title,
                "description": description,
                "image_path": image_path,
            }
        )
        if image_path is not None and self.fail_cover_once:
            self.fail_cover_once = False
            raise RuntimeError("HTTP 403 caller does not have permission")

    def _send_request(self, endpoint, body):
        self.requests.append((endpoint, body))
        if self.cover_forbidden:
            raise RuntimeError("Server returned HTTP 403: Forbidden. The caller does not have permission")
        return {"status": "STATUS_SUCCEEDED"}


def apply_and_collect(yt, desired, args, dry=False, skip_fetch=False):
    result = sync.apply_tracks(yt, "PL1", desired, args, dry, skip_fetch=skip_fetch)
    return result, [item["videoId"] for item in yt.items]


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
class MatchingTests(unittest.TestCase):
    def test_exact_match_scores_high(self):
        score = sync.score_result(make_track(), make_result())
        self.assertGreater(score, 0.9)

    def test_cover_by_other_artist_is_penalized(self):
        score = sync.score_result(make_track(), make_result(artist="Other Band"))
        self.assertLess(score, 0.65)

    def test_karaoke_is_strongly_penalized(self):
        track = make_track()
        for title in ("Song (Karaoke Version)", "Song カラオケ"):
            with self.subTest(title=title):
                self.assertEqual(sync._variant_penalty(track, make_result(title=title)), 0.55)

    def test_instrumental_singular_and_plural(self):
        track = make_track()
        for title in ("Song (Instrumental)", "Song (Instrumentals)"):
            with self.subTest(title=title):
                self.assertEqual(sync._variant_penalty(track, make_result(title=title)), 0.6)

    def test_remix_live_acoustic_are_soft_penalties(self):
        track = make_track()
        for title in ("Song (Remix)", "Song - Live", "Song (Acoustic)"):
            with self.subTest(title=title):
                self.assertEqual(sync._variant_penalty(track, make_result(title=title)), 0.6)

    def test_different_parts_are_penalized(self):
        track = make_track(name="Song Part 3")
        self.assertEqual(
            sync._variant_penalty(track, make_result(title="Song Part 2")), 0.6
        )
        self.assertEqual(
            sync._variant_penalty(make_track(), make_result(title="Song Part 3")), 0.6
        )

    def test_remaster_is_stripped(self):
        self.assertGreaterEqual(
            sync.similarity("Song", "Song - 2011 Digital Remaster"), 0.99
        )

    def test_cjk_containment_boost(self):
        self.assertGreaterEqual(
            sync.similarity("青春狂騒曲", "青春狂騒曲 - Seishun Kyousoukyoku"), 0.9
        )

    def test_latin_special_letters_are_not_cjk(self):
        self.assertIsNone(sync._NONLATIN_RE.search("cœur"))
        self.assertGreaterEqual(sync.similarity("Cœur", "Coeur"), 0.6)

    def test_short_latin_titles_do_not_match_by_prefix(self):
        self.assertLess(sync.similarity("Stay", "Stay With Me"), 0.65)

    def test_best_match_looks_at_videos_when_song_is_a_variant(self):
        track = make_track()
        yt = SimpleNamespace(
            search=lambda query, flt: (
                [make_result(title="Song (Live)", video_id="live")]
                if flt == "songs"
                else [make_result(video_id="original")]
            )
        )
        video_id, score, _desc, best_id = sync.best_match(yt, track, 0.65)
        self.assertEqual(video_id, "original")
        self.assertEqual(best_id, "original")
        self.assertGreaterEqual(score, 0.65)


# --------------------------------------------------------------------------- #
# Orden y aplicación de canciones
# --------------------------------------------------------------------------- #
class ComputeMovesTests(unittest.TestCase):
    def test_compute_moves_reaches_desired_order(self):
        current = [
            {"videoId": "b", "setVideoId": "s1"},
            {"videoId": "c", "setVideoId": "s2"},
            {"videoId": "a", "setVideoId": "s3"},
        ]
        desired = ["a", "b", "c"]
        moves = sync.compute_moves(current, desired)
        self.assertTrue(moves)
        # Aplicamos los movimientos en memoria para comprobar el resultado.
        items = [dict(i) for i in current]
        for item_id, successor_id in moves:
            index = next(i for i, x in enumerate(items) if x["setVideoId"] == item_id)
            item = items.pop(index)
            successor = next(i for i, x in enumerate(items) if x["setVideoId"] == successor_id)
            items.insert(successor, item)
        self.assertEqual([x["videoId"] for x in items], desired)

    def test_compute_moves_empty_when_already_sorted(self):
        current = [
            {"videoId": "a", "setVideoId": "s1"},
            {"videoId": "b", "setVideoId": "s2"},
        ]
        self.assertEqual(sync.compute_moves(current, ["a", "b"]), [])


class ApplyTracksTests(unittest.TestCase):
    @patch("sync.time.sleep", return_value=None)
    def test_orders_despite_eventual_consistency(self, _sleep):
        yt = FakeYT(["b", "c", "a"])
        result, final = apply_and_collect(yt, ["a", "b", "c"], make_args())
        self.assertGreaterEqual(result["moved"], 1)
        self.assertEqual(final, ["a", "b", "c"])

    @patch("sync.time.sleep", return_value=None)
    def test_adds_then_orders(self, _sleep):
        yt = FakeYT(["b"])
        result, final = apply_and_collect(yt, ["a", "b", "c"], make_args())
        self.assertEqual(result["added"], 2)
        self.assertEqual(final, ["a", "b", "c"])

    def test_prune_safeguard_skips_massive_removal(self):
        yt = FakeYT([f"v{i}" for i in range(20)])
        result, final = apply_and_collect(yt, [f"v{i}" for i in range(5)], make_args())
        self.assertEqual(result["removed"], 0)
        self.assertEqual(len(final), 20)
        self.assertFalse(any(call[0] == "remove" for call in yt.calls))

    @patch("sync.time.sleep", return_value=None)
    def test_force_prune_allows_massive_removal(self, _sleep):
        yt = FakeYT([f"v{i}" for i in range(20)])
        result, final = apply_and_collect(
            yt, [f"v{i}" for i in range(5)], make_args(force_prune=True)
        )
        self.assertEqual(result["removed"], 15)
        self.assertEqual(final, [f"v{i}" for i in range(5)])

    @patch("sync.time.sleep", return_value=None)
    def test_duplicates_are_added_and_ordered(self, _sleep):
        yt = FakeYT([])
        result, final = apply_and_collect(yt, ["a", "a", "b"], make_args())
        self.assertEqual(result["added"], 3)
        adds = [call for call in yt.calls if call[0] == "add"]
        self.assertEqual(len(adds), 1)
        self.assertEqual(sorted(adds[0][1]), ["a", "a", "b"])
        self.assertTrue(adds[0][2])
        self.assertEqual(final, ["a", "a", "b"])

    @patch("sync.time.sleep", return_value=None)
    def test_missing_track_still_orders_present(self, _sleep):
        yt = FakeYT(["b"])
        yt.drop_on_add = {"c"}
        with self.assertLogs("sync", level="WARNING") as logs:
            _result, final = apply_and_collect(yt, ["a", "b", "c"], make_args())
        self.assertEqual(final, ["a", "b"])
        self.assertTrue(any("faltan 1 canciones" in line for line in logs.output))
        self.assertFalse(any("no pude dejar el orden" in line for line in logs.output))

    @patch("sync.time.sleep", return_value=None)
    def test_extra_tracks_do_not_block_order(self, _sleep):
        yt = FakeYT(["b", "x"])
        _result, final = apply_and_collect(yt, ["a", "b"], make_args(prune=False))
        self.assertEqual(final[:2], ["a", "b"])
        self.assertIn("x", final)

    def test_dry_run_changes_nothing(self):
        yt = FakeYT(["b"])
        result, final = apply_and_collect(yt, ["a", "b"], make_args(), dry=True)
        self.assertEqual(result["added"], 1)
        self.assertEqual(final, ["b"])
        self.assertEqual(yt.calls, [])


# --------------------------------------------------------------------------- #
# Purga / --fresh
# --------------------------------------------------------------------------- #
class PurgeTests(unittest.TestCase):
    def library(self):
        return [
            {"id": "LM", "title": "Liked Music", "count": "1,218", "owned": False},
            {"id": "SE", "title": "Episodes for Later", "count": "5", "owned": False},
            {"id": "P1", "title": "Old", "count": "1,218", "owned": True},
            {"id": "P2", "title": "Favorite Songs", "count": 3, "owned": True},
            {"id": "P3", "title": "Followed", "count": 2, "owned": False},
        ]

    def test_purge_keeps_system_and_followed_playlists(self):
        yt = FakeYT(library=self.library())
        deleted = sync.purge_yt_playlists(yt, dry=False)
        self.assertEqual(deleted, 2)
        self.assertEqual(yt.deleted, ["P1", "P2"])
        self.assertEqual([p["id"] for p in yt.library], ["LM", "SE", "P3"])

    def test_purge_dry_run_only_lists(self):
        yt = FakeYT(library=self.library())
        deleted = sync.purge_yt_playlists(yt, dry=True)
        self.assertEqual(deleted, 0)
        self.assertEqual(yt.deleted, [])
        self.assertEqual(len(yt.library), 5)

    def test_count_string_is_normalized(self):
        class LibraryYT:
            def call(self, desc, fn, *args, **kwargs):
                return fn(*args, **kwargs)

            def get_library_playlists(self, limit=None):
                return [
                    {
                        "playlistId": "P1",
                        "title": "Big",
                        "count": "1,218",
                        "owned": True,
                    }
                ]

        target = object.__new__(sync.YTMTarget)
        target._library_cache = None
        target.yt = LibraryYT()
        target.call = LibraryYT().call
        self.assertEqual(target.library_playlists()[0]["count"], 1218)
        self.assertEqual(sync._as_count(None), 0)
        self.assertEqual(sync._as_count("1.218"), 1218)


class EnsureTargetTests(unittest.TestCase):
    def pl(self):
        return {"id": "sp1", "name": "Playlist", "description": "Desc", "image_urls": []}

    def test_fresh_deletes_old_and_creates_new(self):
        yt = FakeYT(
            library=[{"id": "OLD", "title": "Playlist", "count": 1, "owned": True}]
        )
        state = {"playlists": {"sp1": {"ytId": "OLD", "created": True, "name": "Playlist"}}}
        yt_id, created, owned = sync.ensure_target(
            self.pl(), yt, state, make_args(fresh=True), dry=False
        )
        self.assertEqual(yt.deleted, ["OLD"])
        self.assertEqual(yt_id, "NEW0")
        self.assertTrue(created and owned)
        self.assertEqual(state["playlists"]["sp1"]["ytId"], "NEW0")

    def test_fresh_dry_run_does_not_delete_or_create(self):
        yt = FakeYT(
            library=[{"id": "OLD", "title": "Playlist", "count": 1, "owned": True}]
        )
        state = {"playlists": {}}
        yt_id, created, owned = sync.ensure_target(
            self.pl(), yt, state, make_args(fresh=True), dry=True
        )
        self.assertIsNone(yt_id)
        self.assertEqual(yt.deleted, [])
        self.assertEqual(yt.created, [])
        self.assertFalse(created or owned)

    def test_fresh_delete_failure_does_not_create_duplicate(self):
        yt = FakeYT(
            library=[{"id": "OLD", "title": "Playlist", "count": 1, "owned": True}]
        )
        yt.fail_delete = True
        state = {"playlists": {}}
        with self.assertRaises(sync.SyncError):
            sync.ensure_target(
                self.pl(), yt, state, make_args(fresh=True), dry=False
            )
        self.assertEqual(yt.created, [])

    def test_reuses_existing_playlist_by_title(self):
        yt = FakeYT(
            library=[{"id": "OLD", "title": "Playlist", "count": 1, "owned": True}]
        )
        state = {"playlists": {}}
        yt_id, created, owned = sync.ensure_target(
            self.pl(), yt, state, make_args(fresh=False), dry=False
        )
        self.assertEqual(yt_id, "OLD")
        self.assertFalse(created or owned)
        self.assertEqual(state["playlists"]["sp1"]["created"], False)

    def test_creates_when_missing(self):
        yt = FakeYT()
        state = {"playlists": {}}
        yt_id, created, owned = sync.ensure_target(
            self.pl(), yt, state, make_args(fresh=False), dry=False
        )
        self.assertEqual((yt_id, created, owned), ("NEW0", True, True))


class DeletePlaylistTests(unittest.TestCase):
    def target(self, present_after):
        class Fake:
            def __init__(self):
                self.reads = 0
                self.yt = self

            def call(self, desc, fn, *args, **kwargs):
                return fn(*args, **kwargs)

            def delete_playlist(self, playlist_id):
                return {}

            def library_playlists(self):
                self.reads += 1
                if self.reads > present_after:
                    return []
                return [{"id": "X", "title": "P", "count": 0, "owned": True}]

        return Fake()

    @patch("sync.time.sleep", return_value=None)
    def test_dict_response_is_success_when_id_disappears(self, _sleep):
        fake = self.target(present_after=0)
        sync.YTMTarget.delete_playlist(fake, "X")  # no debe lanzar

    @patch("sync.time.sleep", return_value=None)
    def test_deletion_command_in_response_is_immediate_success(self, _sleep):
        class Fake:
            def __init__(self):
                self.reads = 0
                self.yt = self

            def call(self, desc, fn, *args, **kwargs):
                return fn(*args, **kwargs)

            def delete_playlist(self, playlist_id):
                return {
                    "responseContext": {"consistencyTokenJar": {"encryptedTokenJarContents": "SECRET"}},
                    "command": {
                        "commandExecutorCommand": {
                            "commands": [
                                {"handlePlaylistDeletionCommand": {"playlistId": "X"}}
                            ]
                        }
                    },
                }

            def library_playlists(self):
                self.reads += 1
                return [{"id": "X", "title": "P", "count": 0, "owned": True}]

        fake = Fake()
        sync.YTMTarget.delete_playlist(fake, "X")
        self.assertEqual(fake.reads, 0)  # no hizo falta esperar a la biblioteca

    @patch("sync.time.sleep", return_value=None)
    def test_eventual_consistency_gets_retried(self, _sleep):
        fake = self.target(present_after=2)
        sync.YTMTarget.delete_playlist(fake, "X")  # no debe lanzar
        self.assertEqual(fake.reads, 3)

    @patch("sync.time.sleep", return_value=None)
    def test_still_present_raises(self, _sleep):
        fake = self.target(present_after=99)
        with self.assertRaises(sync.SyncError):
            sync.YTMTarget.delete_playlist(fake, "X")


# --------------------------------------------------------------------------- #
# Comprobación de portadas
# --------------------------------------------------------------------------- #
class CheckCoverPermissionTests(unittest.TestCase):
    def test_creates_temp_playlist_uploads_and_deletes_it(self):
        yt = FakeYT()
        with tempfile.TemporaryDirectory() as tmp:
            cover = Path(tmp) / "cover.jpg"
            cover.write_bytes(b"fake-jpg")
            with patch("sync.upload_playlist_image", return_value="blob-1"):
                sync.check_cover_permission(yt, Path(tmp))
        self.assertEqual(len(yt.created), 1)
        self.assertEqual(yt.deleted, ["NEW0"])
        self.assertTrue(yt.requests)
        action = yt.requests[0][1]["actions"][0]
        self.assertEqual(action["action"], "ACTION_SET_CUSTOM_THUMBNAIL")
        self.assertEqual(action["addedCustomThumbnail"]["playlistScottyEncryptedBlobId"], "blob-1")

    def test_403_aborts_and_still_deletes_temp_playlist(self):
        yt = FakeYT()
        yt.cover_forbidden = True
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cover.jpg").write_bytes(b"fake-jpg")
            with patch("sync.upload_playlist_image", return_value="blob-1"):
                with self.assertRaises(sync.SyncError) as ctx:
                    sync.check_cover_permission(yt, Path(tmp))
        self.assertIn("youtube.com/verify", str(ctx.exception))
        self.assertEqual(yt.deleted, ["NEW0"])


# --------------------------------------------------------------------------- #
# Metadatos
# --------------------------------------------------------------------------- #
class SyncMetadataTests(unittest.TestCase):
    def pl(self):
        return {
            "id": "sp1",
            "name": "Playlist",
            "description": "Desc",
            "image_urls": ["https://img/1.jpg"],
        }

    @patch("sync.fetch_cover")
    def test_dry_run_does_not_download_covers(self, fetch_cover):
        yt = FakeYT()
        state = {"playlists": {}}
        sync.sync_metadata(
            self.pl(), yt, state, "PL1", make_args(), Path("/tmp"), created=True, dry=True
        )
        fetch_cover.assert_not_called()
        self.assertEqual(yt.edits, [])

    @patch("sync.fetch_cover", return_value=(Path("/tmp/cover.jpg"), "sha1", "https://img/1.jpg"))
    def test_cover_upload_records_sha_and_url(self, _fetch_cover):
        yt = FakeYT()
        state = {"playlists": {}}
        sync.sync_metadata(
            self.pl(), yt, state, "PL1", make_args(), Path("/tmp"), created=True, dry=False
        )
        entry = state["playlists"]["sp1"]
        self.assertEqual(entry["coverSha"], "sha1")
        self.assertEqual(entry["coverUrl"], "https://img/1.jpg")
        self.assertEqual(yt.edits[0]["image_path"], Path("/tmp/cover.jpg"))

    @patch("sync.fetch_cover", return_value=(Path("/tmp/cover.jpg"), "sha1", "https://img/1.jpg"))
    def test_cover_failure_does_not_abort(self, _fetch_cover):
        yt = FakeYT()
        yt.fail_cover_once = True
        state = {"playlists": {}}
        with self.assertLogs("sync", level="WARNING") as logs:
            sync.sync_metadata(
                self.pl(), yt, state, "PL1", make_args(), Path("/tmp"), created=True, dry=False
            )
        entry = state["playlists"]["sp1"]
        self.assertNotIn("coverSha", entry)
        self.assertNotIn("coverUrl", entry)
        self.assertEqual(len(yt.edits), 2)
        self.assertIsNone(yt.edits[1]["image_path"])
        messages = [record.getMessage() for record in logs.records]
        self.assertTrue(any("youtube.com/verify" in message for message in messages))
        self.assertFalse(
            any("metadatos actualizados" in message and "portada" in message for message in messages)
        )


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #
class ResolverTests(unittest.TestCase):
    def resolver(self, state, **overrides):
        values = dict(
            threshold=0.65,
            retry_unmatched=False,
            limit=0,
            workers=1,
            fallback_best=True,
            fallback_min=0.30,
        )
        values.update(overrides)
        return sync.Resolver(lambda: FakeYT(), state, **values)

    def test_uses_cache_without_searching(self):
        track = make_track()
        state = {
            "tracks": {
                track.sid: {"status": "matched", "videoId": "cached", "score": 0.9}
            }
        }
        with patch("sync.best_match") as best_match:
            out = self.resolver(state).resolve([track])
        best_match.assert_not_called()
        self.assertEqual(out[track.sid], "cached")

    def test_stores_fallback_marked_as_approximate(self):
        track = make_track()
        state = {"tracks": {}}
        with patch(
            "sync.best_match", return_value=(None, 0.4, "Similar — Other (0.40)", "other")
        ):
            resolver = self.resolver(state)
            out = resolver.resolve([track])
        self.assertEqual(out[track.sid], "other")
        self.assertTrue(state["tracks"][track.sid]["fallback"])
        self.assertEqual(resolver.approx, 1)

    def test_below_fallback_min_is_unmatched(self):
        track = make_track()
        state = {"tracks": {}}
        with patch("sync.best_match", return_value=(None, 0.1, "Nada — Nadie", "other")):
            resolver = self.resolver(state)
            out = resolver.resolve([track])
        self.assertIsNone(out[track.sid])
        self.assertEqual(state["tracks"][track.sid]["status"], "unmatched")
        self.assertEqual(resolver.unmatched, 1)

    def test_retry_error_preserves_previous_match(self):
        track = make_track()
        state = {
            "tracks": {
                track.sid: {
                    "status": "matched",
                    "videoId": "previous",
                    "score": 0.8,
                    "fallback": True,
                }
            }
        }
        with patch("sync.best_match", side_effect=RuntimeError("timeout")):
            resolver = self.resolver(state, retry_unmatched=True)
            out = resolver.resolve([track])
        self.assertEqual(out[track.sid], "previous")
        self.assertEqual(state["tracks"][track.sid]["videoId"], "previous")
        self.assertEqual(resolver.errors, 1)

    def test_limit_skips_remaining_tracks(self):
        tracks = [make_track(sid=f"t{i}") for i in range(3)]
        state = {"tracks": {}}
        with patch("sync.best_match", return_value=("v", 0.9, "", "v")):
            resolver = self.resolver(state, limit=1)
            out = resolver.resolve(tracks)
        self.assertEqual(resolver.searched, 1)
        self.assertEqual(resolver.limit_skipped, 2)
        self.assertEqual(sum(1 for value in out.values() if value), 1)


# --------------------------------------------------------------------------- #
# Utilidades varias / parser
# --------------------------------------------------------------------------- #
class UtilityTests(unittest.TestCase):
    @patch("sync.time.sleep", return_value=None)
    def test_call_retries_transient_error(self, _sleep):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("timeout")
            return "ok"

        self.assertEqual(sync.call_retries("x", fn), "ok")
        self.assertEqual(len(calls), 3)

    @patch("sync.time.sleep", return_value=None)
    def test_call_retries_treats_1400ms_as_transient(self, _sleep):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("timeout after 1400ms")
            return "ok"

        self.assertEqual(sync.call_retries("x", fn), "ok")
        self.assertEqual(len(calls), 2)

    def test_call_retries_does_not_retry_404(self):
        calls = []

        def fn():
            calls.append(1)
            raise RuntimeError("HTTP 404")

        with self.assertRaises(RuntimeError):
            sync.call_retries("x", fn)
        self.assertEqual(len(calls), 1)

    @patch("sync.time.sleep", return_value=None)
    def test_call_retries_html_json_decode(self, _sleep):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise json.JSONDecodeError("Expecting value", "", 0)
            return "ok"

        self.assertEqual(sync.call_retries("x", fn), "ok")
        self.assertEqual(len(calls), 3)

    @patch("sync.time.sleep", return_value=None)
    def test_json_decode_gives_up_with_clear_message(self, _sleep):
        calls = []

        def fn():
            calls.append(1)
            raise json.JSONDecodeError("Expecting value", "", 0)

        with self.assertRaises(sync.SyncError) as ctx:
            sync.call_retries("x", fn)
        self.assertIn("HTML", str(ctx.exception))
        self.assertEqual(len(calls), 6)

    def test_missing_playlist_error(self):
        self.assertTrue(sync._missing_playlist_error(RuntimeError("404 Not Found")))
        self.assertTrue(sync._missing_playlist_error(RuntimeError("playlist not found")))
        self.assertFalse(
            sync._missing_playlist_error(RuntimeError("Video is no longer available"))
        )

    def test_parser_defaults(self):
        args = sync.build_parser().parse_args(["sync"])
        self.assertEqual(args.liked_mode, "like")
        self.assertFalse(args.fresh)
        self.assertTrue(args.prune)
        self.assertTrue(args.order)
        self.assertTrue(args.covers)
        self.assertFalse(args.require_covers)
        self.assertFalse(args.no_cover_check)

    def test_setup_refresh_flag(self):
        self.assertFalse(sync.build_parser().parse_args(["setup"]).refresh)
        self.assertTrue(sync.build_parser().parse_args(["setup", "--refresh"]).refresh)

    def test_short_resp_never_leaks_tokens(self):
        response = {
            "error": {"message": "The caller does not have permission"},
            "consistencyTokenJar": {"encryptedTokenJarContents": "SECRET-TOKEN"},
        }
        text = sync._short_resp(response)
        self.assertIn("permission", text)
        self.assertNotIn("SECRET-TOKEN", text)

    def test_deletion_confirmed_only_for_matching_playlist(self):
        response = {
            "command": {
                "commandExecutorCommand": {
                    "commands": [{"handlePlaylistDeletionCommand": {"playlistId": "PL1"}}]
                }
            }
        }
        self.assertTrue(sync._deletion_confirmed(response, "PL1"))
        self.assertFalse(sync._deletion_confirmed(response, "PL2"))
        self.assertFalse(sync._deletion_confirmed({}, "PL1"))


if __name__ == "__main__":
    unittest.main()
