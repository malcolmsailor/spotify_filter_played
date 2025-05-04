#!/usr/bin/env python


# TODO: (Malcolm 2023-12-05) allow to remove playlists
import argparse
import csv
import datetime
import json
import logging
import os
import pdb
import pickle
import re
import sys
import textwrap
import traceback
from pathlib import Path
from urllib.request import urlopen

import backoff
import httpx
import pydantic
import pytz
import tekore as tk

DATA_DIR = os.getenv(
    "SPOTIFY_FILTER_DIR",
    os.path.join(os.path.expanduser("~"), ".spotify_filter_played"),
)


def internet_on():
    try:
        urlopen("https://www.google.com/", timeout=10)
        return True
    except Exception:  # pylint: disable=broad-except
        return False


if not internet_on():
    sys.exit()


def custom_excepthook(exc_type, exc_value, exc_traceback):
    traceback.print_exception(exc_type, exc_value, exc_traceback, file=sys.stdout)
    pdb.post_mortem(exc_traceback)


if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

PLAYLIST_DIR = os.path.join(DATA_DIR, "playlists")
AUTH_CONFIG = os.path.join(DATA_DIR, "auth.cfg")

LAST_SUCCESS = os.path.join(DATA_DIR, "last_success")
LAST_COMPLETED_RUN = os.path.join(DATA_DIR, "last_completed_run")
PLAYLIST_JSON = os.path.join(DATA_DIR, "playlist_util.json")
PLAYLIST_MEM_DIR = os.path.join(DATA_DIR, "playlist_mem")
REMOVED_FROM_SRC = os.path.join(DATA_DIR, "removed_from_src.csv")

if not os.path.exists(PLAYLIST_MEM_DIR):
    os.makedirs(PLAYLIST_MEM_DIR)

NUM_ATTEMPTS = 3
WAIT_BETWEEN_ATTEMPTS = 60
MAX_INTERVAL_IN_SECS_BETWEEN_SUCCESSES = 7200

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False  # prevent messages from appearing in root logger as well

# Create a console handler and set level to INFO
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)

# We send INFO and lower messages to stdout
stdout_handler.addFilter(lambda record: record.levelno <= logging.INFO)

# Warning and higher to stderr
stderr_handler = logging.StreamHandler()
stderr_handler.setLevel(logging.WARNING)


# Create formatter and add it to the handler
formatter = logging.Formatter(
    "%(pathname)s:%(levelname)s:%(message)s",
)
stdout_handler.setFormatter(formatter)
stderr_handler.setFormatter(formatter)

# Add the handler to logger
LOGGER.addHandler(stdout_handler)
LOGGER.addHandler(stderr_handler)


def init_auth(reauthenticate):
    user_token = None
    if not reauthenticate and os.path.exists(AUTH_CONFIG):
        (
            client_id,
            client_secret,
            redirect_uri,
            user_refresh,
        ) = tk.config_from_file(AUTH_CONFIG, return_refresh=True)
        user_token = tk.refresh_user_token(client_id, client_secret, user_refresh)
    if user_token is None:
        client_id = input("Paste client id: ")
        client_secret = input("Paste client secret: ")
        redirect_uri = "https://example.com/callback"
        user_token = tk.prompt_for_user_token(
            client_id,
            client_secret,
            redirect_uri,
            scope=tk.scope.every,  # type:ignore
        )
        conf = (
            client_id,
            client_secret,
            redirect_uri,
            user_token.refresh_token,
        )
        tk.config_to_file(AUTH_CONFIG, conf)

    return client_id, client_secret, user_token  # type:ignore


def add_new_playlist(s):
    while True:
        source_link = input(
            "Enter new source playlist id or url (or leave blank to cancel): \n"
            "(https://open.spotify.com/playlist/<PLAYLIST_ID>?<OTHER_QUERIES>)\n"
        )
        if not source_link:
            return None
        m = re.match(
            r"(?:https://open.spotify.com/playlist/)(?P<id>[^?]+)(?:\?.*)$",
            source_link,
        )
        if not m:
            print("Error parsing playlist url")
            continue
        source_id = m.group("id")
        try:
            source_playlist = s.playlist(source_id)
        except Exception:
            print("Error reading source playlist")
        else:
            while True:
                dest_link = input(
                    "Enter new destination playlist id (or leave blank to cancel): \n"
                    "(https://open.spotify.com/playlist/<PLAYLIST_ID>?<OTHER_QUERIES>)\n"
                )
                if not dest_link:
                    return None
                m = re.match(
                    r"(?:https://open.spotify.com/playlist/)(?P<id>[^?]+)(?:\?.*)$",
                    dest_link,
                )
                if not m:
                    print("Error parsing playlist url")
                    continue
                dest_id = m.group("id")
                try:
                    dest_playlist = s.playlist(dest_id)
                except Exception:
                    print("Error reading destination playlist")
                else:
                    return (
                        source_playlist.name,
                        source_id,
                        dest_playlist.name,
                        dest_id,
                    )


def get_tz_offset():
    return datetime.datetime.now(datetime.UTC).astimezone().utcoffset().total_seconds()


def read_playlists(s, add_new) -> list[tuple[str, str, str, str]]:
    if os.path.exists(PLAYLIST_JSON):
        with open(PLAYLIST_JSON, "r", encoding="utf-8") as inf:
            playlists = json.load(inf)
    else:
        playlists = []
    if not playlists or add_new:
        result = add_new_playlist(s)
        if result is not None:
            new_src_name, new_src_id, new_dst_name, new_dst_id = result
            playlists.append([new_src_name, new_src_id, new_dst_name, new_dst_id])
        with open(PLAYLIST_JSON, "w", encoding="utf-8") as outf:
            json.dump(playlists, outf)
    return playlists


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reauthenticate",
        "-r",
        action="store_true",
        help="Force re-authentication",
    )
    parser.add_argument(
        "--new-playlist",
        "-n",
        action="store_true",
        help="add new playlist",
    )
    parser.add_argument(
        "--delete-playlist",
        "-d",
        action="store_true",
        help="delete playlist",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    return args


def get_mem_path(id_):
    return os.path.join(PLAYLIST_MEM_DIR, id_ + ".json")


def read_mem(id_):
    path = get_mem_path(id_)
    if not os.path.exists(path):
        timestamp, memory = datetime.datetime.fromtimestamp(0), []
    else:
        with open(path, "r", encoding="utf-8") as inf:
            mem = json.load(inf)
        if isinstance(
            mem, list
        ):  # for backwards-compatibility with earlier version of script
            timestamp, memory = datetime.datetime.fromtimestamp(0), mem
        else:
            timestamp, memory = (
                datetime.datetime.fromisoformat(mem["reinit_time"]),
                mem["contents"],
            )
    if timestamp.tzinfo is None:
        timestamp = pytz.utc.localize(timestamp)
    return timestamp, memory


def write_mem(id_, contents, reinit_time):
    path = get_mem_path(id_)

    with open(path, "w", encoding="utf-8") as outf:
        json.dump(
            {"reinit_time": reinit_time.isoformat(), "contents": contents},
            outf,
        )


def get_recent_tracks(s) -> dict[str, tuple[tk.model.FullTrack, datetime.datetime]]:
    play_history: tk.model.PlayHistoryPaging = s.playback_recently_played(limit=50)
    play_history_list: list[tk.model.PlayHistory] = play_history.items
    recent_tracks: list[tuple[str, tk.model.FullTrack, datetime.datetime]] = [
        (item.track.id, item.track, item.played_at)
        for item in play_history_list
        # item.played_at is a datetime that tells us when item was played
    ]
    # Sort by playback time
    recent_tracks.sort(key=lambda x: x[2])
    recent_tracks_dict = {
        id_: (track, played_at) for id_, track, played_at in recent_tracks
    }
    return recent_tracks_dict


def full_playlist_track_to_info(track: tk.model.FullPlaylistTrack):
    track_id = track.id
    artists = [artist.name for artist in track.artists]
    album_name = track.album.name
    track_name = track.name
    return track_id, artists, album_name, track_name


def log_manually_removed_tracks(tracks: list[tk.model.FullPlaylistTrack]):
    if not os.path.exists(REMOVED_FROM_SRC):
        with open(REMOVED_FROM_SRC, "w") as outf:
            csvwriter = csv.writer(outf)
            csvwriter.writerow(("track_id", "artists", "album_name", "track_name"))
    with open(REMOVED_FROM_SRC, "a") as outf:
        csvwriter = csv.writer(outf)
        for track in tracks:
            row = full_playlist_track_to_info(track)
            csvwriter.writerow(row)


def get_playlist_tracks(
    s: tk.Spotify, playlist
) -> dict[str, tuple[tk.model.FullPlaylistTrack, str]]:
    os.makedirs(PLAYLIST_DIR, exist_ok=True)

    playlist_data_path = os.path.join(PLAYLIST_DIR, f"{playlist.id}.pickle")

    if os.path.exists(playlist_data_path):
        with open(playlist_data_path, "rb") as inf:
            playlist_data = pickle.load(inf)
        if playlist_data["snapshot_id"] == playlist.snapshot_id:
            return playlist_data["all_tracks"]

    # The track ids stored in the playlist can be different from those that actually
    # playback, for mysterious market-related reasons. For example,
    # >>> s.track("0ZxWG91nxZcCsEUd6ykc6i", market="CA").id
    # '7d4NqW0bkFTYXinZYMNvMg'
    # Therefore, first we need to retrieve the ids stored in the playlist; then we need
    # to get the tracks in the current market corresponding to those ids
    raw_track_ids: list[str] = [
        t.track.id  # type:ignore
        for t in s.all_items(playlist.tracks)
        if t.track is not None  # type:ignore
    ]

    with s.chunked(True):
        tracks = s.tracks(raw_track_ids, market="from_token")

    all_tracks: dict[str, tuple[tk.model.FullPlaylistTrack, str]] = {
        t.id: (t, raw_id)
        for t, raw_id in zip(tracks, raw_track_ids)  # type:ignore
    }

    playlist_data = {"snapshot_id": playlist.snapshot_id, "all_tracks": all_tracks}
    with open(playlist_data_path, "wb") as outf:
        pickle.dump(playlist_data, outf)
    return playlist_data["all_tracks"]


@backoff.on_exception(
    backoff.expo,
    (
        tk.InternalServerError,
        tk.BadGateway,
        httpx.RemoteProtocolError,
        httpx.ProxyError,
        httpx.ConnectError,
        httpx.ReadTimeout,
        pydantic.ValidationError,
        tk.ServiceUnavailable,
    ),
    max_tries=3,
)
def process(
    s: tk.Spotify,
    recent_tracks: dict[str, tuple[tk.model.FullTrack, datetime.datetime]],
    src_id,
    src_name,
    dst_id,
    dst_name,
):
    LOGGER.info(f"Processing src={src_name} dst={dst_name}")
    try:
        src_playlist = s.playlist(src_id)
    except Exception as e:
        LOGGER.error(f"Error reading source playlist {src_name}: {e}")
        return
    try:
        dst_playlist = s.playlist(dst_id)
    except Exception as e:
        LOGGER.error(f"Error reading destination playlist {dst_name}: {e}")
        return

    assert isinstance(src_playlist, tk.model.FullPlaylist)
    assert isinstance(dst_playlist, tk.model.FullPlaylist)
    reinit_time, prev_dst_ids = read_mem(dst_id)

    src_tracks = get_playlist_tracks(s, src_playlist)
    dst_tracks = get_playlist_tracks(s, dst_playlist)

    # I'm removing this functionality because there are bugs where it will try to remove
    #   many songs at once and it's hard to debug. It's annoying to remove tracks from
    #   source and filtered playlists but it's better than having tracks randomly vanish
    #   from the source playlists.

    # Remove tracks that have been manually removed from dst
    #  manually_removed: list[tk.model.FullPlaylistTrack] = []
    #  for id_ in prev_dst_ids:
    #      if id_ not in dst_tracks and id_ in src_tracks:
    #          manually_removed.append(src_tracks[id_])
    #  if manually_removed:
    #      print(
    #          f"Removing {len(manually_removed)} tracks from source playlist "
    #          f"{src_name}"
    #      )
    #      if len(manually_removed) > 10:
    #          # This has happened before with an apparent but intermittent bug where
    #          # it tries to remove entire playlist contents. We definitely don't want
    #          # that. Pending further investigation we raise a ValueError here.
    #          raise ValueError("Too many songs manually removed, maybe there is a bug?")
    #      s.playlist_remove(src_id, [t.uri for t in manually_removed])
    #      log_manually_removed_tracks(manually_removed)
    #      src_playlist = s.playlist(src_id)
    #      src_tracks = {t.track.id: t.track for t in s.all_items(src_playlist.tracks)}

    to_remove = []
    deleted = []
    to_save = []
    prev_play_count = 0
    for id_ in dst_tracks:
        if id_ in recent_tracks:
            track, played_at = recent_tracks[id_]
            if played_at < reinit_time:
                to_save.append(id_)
                prev_play_count += 1
            else:
                to_remove.append(dst_tracks[id_][1])

        elif id_ not in src_tracks:
            deleted.append(dst_tracks[id_][1])
        else:
            to_save.append(id_)
    if to_remove:
        LOGGER.info(
            f"{len(to_remove)} recently played tracks "
            f"({prev_play_count} tracks older than reinit_time "
            f"of {reinit_time.isoformat()})"
        )
    if deleted:
        LOGGER.info(f"{len(deleted)} deleted tracks to remove")
    to_remove += deleted
    if to_remove:
        LOGGER.info(
            f"Removing {len(to_remove)} tracks from destination playlist {dst_name}"
        )
        s.playlist_remove(dst_id, [f"spotify:track:{id_}" for id_ in to_remove])

    # If dst is now empty, rebuild it
    if len(dst_tracks) - len(to_remove) == 0:
        LOGGER.info(f"Re-initializing {dst_playlist.name}")
        src_uris = [
            t.track.uri
            for t in s.all_items(src_playlist.tracks)  # type:ignore
        ]
        for i in range(0, len(src_uris), 100):
            s.playlist_add(dst_id, src_uris[i : i + 100])
        to_save = list(src_tracks.keys())
        # I believe the timestamps of recently played tracks are utc
        # They're definitely not my local time zone, but tk.model.Timestamp.tzname()
        # and related methods return None
        reinit_time = datetime.datetime.now(datetime.UTC)
        # This seems to have changed in more recent version of tekore
        # reinit_time = datetime.datetime.now()

    # write dst memory
    write_mem(dst_id, to_save, reinit_time)


def delete_playlist():
    if os.path.exists(PLAYLIST_JSON):
        with open(PLAYLIST_JSON, "r", encoding="utf-8") as inf:
            playlists = json.load(inf)
    else:
        print("No playlists to delete")
        return

    for i, (src_name, src_id, dst_name, dst_id) in enumerate(playlists, start=1):
        print(
            "\n".join(
                textwrap.wrap(
                    f" {i:2d}: Source: {src_name} -> Destination: {dst_name}",
                    width=os.get_terminal_size().columns,
                    subsequent_indent="    ",
                )
            )
        )
    while True:
        selected_i = input(
            "Enter the number of the playlist to delete (or leave blank to cancel): "
        )
        if selected_i.strip() == "":
            return
        try:
            selected_i = int(selected_i)
        except ValueError:
            print("Invalid input, please enter a number")
            continue
        if 1 <= selected_i <= len(playlists):
            break
        else:
            print("Invalid input, please enter a number within the range")

    playlists = playlists[: selected_i - 1] + playlists[selected_i:]
    with open(PLAYLIST_JSON, "w", encoding="utf-8") as outf:
        json.dump(playlists, outf)


def _get_last(path):
    if not os.path.exists(path):
        return float("-inf")
    else:
        return os.stat(path).st_mtime


def get_last_success():
    return _get_last(LAST_SUCCESS)


def get_last_completed_run():
    return _get_last(LAST_COMPLETED_RUN)


@backoff.on_exception(
    backoff.expo,
    (
        tk.InternalServerError,
        tk.BadGateway,
        httpx.RemoteProtocolError,
        httpx.ConnectError,
        httpx.ReadTimeout,
    ),
    max_tries=3,
)
def main(args):
    if args.debug:
        sys.excepthook = custom_excepthook

    if args.delete_playlist:
        delete_playlist()
        return

    client_id, client_secret, user_token = init_auth(args.reauthenticate)
    app_token = tk.request_client_token(client_id, client_secret)
    s = tk.Spotify(app_token)
    s.token = user_token
    playlists = read_playlists(s, args.new_playlist)
    recent_tracks = get_recent_tracks(s)
    last_success = get_last_success()
    if not recent_tracks:
        if not args.debug:
            LOGGER.info("No recently-played tracks, exiting")
            return
    most_recent_timestamp = list(recent_tracks.values())[-1][1].timestamp()
    # most recent timestamp is utc; last_success is local time
    # if most_recent_timestamp + get_tz_offset() < last_success:
    # Actually now most_recent_timestamp seems to be local time. Not sure if this
    #   is an update in Tekore or what.
    if most_recent_timestamp < last_success:
        if not args.debug:
            LOGGER.info(
                "Most recently-played track is older than last time script ran successfully, exiting"
            )
            return
    LOGGER.info(f"Found {len(playlists)} playlist pairs")
    for src_name, src_id, dst_name, dst_id in playlists:
        process(s, recent_tracks, src_id, src_name, dst_id, dst_name)
    Path(LAST_SUCCESS).touch()


if __name__ == "__main__":
    args = args = get_args()
    try:
        main(args)
    except Exception as exc:
        import time

        last_completed_run = get_last_completed_run()
        now = time.time()
        max_days_without_completed_run = 1
        days_without_completed_run = (now - last_completed_run) / (24 * 60 * 60)
        if days_without_completed_run > max_days_without_completed_run:
            LOGGER.error(f"{str(type(exc))}: {str(exc)}")
            LOGGER.error(
                f"Script has not completed in {days_without_completed_run:.1f} days"
            )
            raise
        else:
            LOGGER.info(f"{str(type(exc))}: {str(exc)}")
            LOGGER.info(
                f"Script has completed in the last {days_without_completed_run:.1f} days"
            )
    else:
        Path(LAST_COMPLETED_RUN).touch()
