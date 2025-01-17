# -*- coding: utf-8 -*-
# Copyright 2004-2005 Joe Wreschnig, Michael Urman
#           2012-2013 Nick Boultbee
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation

# Much of this code is highly optimized, because many of the functions
# are called in tight loops. Don't change things just to make them
# more readable, unless they're also faster.

import os
import shutil
import time
import re
import collections

from ._image import ImageContainer

from quodlibet import const
from quodlibet import util
from quodlibet import config
from quodlibet.util.path import mkdir, fsdecode, mtime, expanduser
from quodlibet.util.path import normalize_path, fsnative, escape_filename
from quodlibet.util.string import encode

from quodlibet.util.uri import URI
from quodlibet.util import human_sort_key as human
from quodlibet.util.dprint import print_d, print_w

from quodlibet.util.cover.manager import cover_plugins

# Used by __init__.py
from quodlibet.util.tags import STANDARD_TAGS as USEFUL_TAGS
from quodlibet.util.tags import MACHINE_TAGS


MIGRATE = frozenset(("~#playcount ~#laststarted ~#lastplayed ~#added "
           "~#skipcount ~#rating ~bookmark").split())

PEOPLE = ("albumartist artist author composer ~performers originalartist "
          "lyricist arranger conductor").split()

TAG_ROLES = {
    "composer": _("Composition"),
    "lyricist": _("Lyrics"),
    "arranger": _("Arrangement"),
    "conductor": _("Conducting")
}

TAG_TO_SORT = {
    "artist": "artistsort",
    "album": "albumsort",
    "albumartist": "albumartistsort",
    "performer": "performersort",
    "~performers": "~performerssort"
}

INTERN_NUM_DEFAULT = frozenset("~#lastplayed ~#laststarted ~#playcount "
    "~#skipcount ~#length ~#bitrate ~#filesize".split())

SORT_TO_TAG = dict([(v, k) for (k, v) in TAG_TO_SORT.iteritems()])

PEOPLE_SORT = [TAG_TO_SORT.get(k, k) for k in PEOPLE]

FILESYSTEM_TAGS = "~filename ~basename ~dirname".split()


class AudioFile(dict, ImageContainer):
    """An audio file. It looks like a dict, but implements synthetic
    and tied tags via __call__ rather than __getitem__. This means
    __getitem__, get, and so on can be used for efficiency.

    If you need to sort many AudioFiles, you can use their sort_key
    attribute as a decoration."""

    # New tags received from the backend will update the song
    fill_metadata = False
    # Container for multiple songs, while played new songs can start/end
    multisong = False
    # Part of a multisong
    streamsong = False
    # Can be added to the queue, playlists
    can_add = True
    # Is a real file
    is_file = True
    # Multiple tags for the same tag possible
    multiple_values = True

    format = "Unknown Audio File"
    mimes = []

    @util.cached_property
    def __song_key(self):
        return (self("~#disc"), self("~#track"),
            human(self("artistsort")),
            self.get("musicbrainz_artistid", ""),
            human(self.get("title", "")),
            self.get("~filename"))

    @util.cached_property
    def album_key(self):
        return (human(self("albumsort", "")),
                human(self("albumartistsort", "")),
                self.get("album_grouping_key") or self.get("labelid") or
                self.get("musicbrainz_albumid") or "")

    @util.cached_property
    def sort_key(self):
        return [self.album_key, self.__song_key]

    @staticmethod
    def sort_by_func(tag):
        """Returns a fast sort function for a specific tag (or pattern).
        Some keys are already in the sort cache, so we can use them."""
        def artist_sort(song):
            return song.sort_key[1][2]

        if callable(tag):
            return lambda song: human(tag(song))
        elif tag == "artistsort":
            return artist_sort
        elif tag in FILESYSTEM_TAGS:
            return lambda song: fsdecode(song(tag), note=False)
        elif tag.startswith("~#") and "~" not in tag[2:]:
            return lambda song: song(tag)
        return lambda song: human(song(tag))

    def __getstate__(self):
        """Don't pickle anything from __dict__"""
        pass

    def __setstate__(self, state):
        """Needed because we have defined getstate"""
        pass

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        if not self.__dict__:
            return
        pop = self.__dict__.pop
        pop("album_key", None)
        pop("sort_key", None)
        pop("__song_key", None)

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        if not self.__dict__:
            return
        pop = self.__dict__.pop
        pop("album_key", None)
        pop("sort_key", None)
        pop("__song_key", None)

    @property
    def key(self):
        return self["~filename"]

    @property
    def mountpoint(self):
        return self["~mountpoint"]

    def __cmp__(self, other):
        if not other:
            return -1
        try:
            return cmp(self.sort_key, other.sort_key)
        except AttributeError:
            return -1

    def __hash__(self):
        # Dicts aren't hashable by default, so we need a hash
        # function. Previously this used ~filename. That created a
        # situation when an object could end up in two buckets by
        # renaming files. So now it uses identity.
        return hash(id(self))

    def __eq__(self, other):
        # And to preserve Python hash rules, we need a strict __eq__.
        return self is other

    def __ne__(self, other):
        return self is not other

    def reload(self):
        """Reload an audio file from disk. The caller is responsible for
        handling any errors."""

        fn = self["~filename"]
        saved = {}
        for key in self:
            if key in MIGRATE:
                saved[key] = self[key]
        self.clear()
        self["~filename"] = fn
        self.__init__(fn)
        self.update(saved)

    def realkeys(self):
        """Returns a list of keys that are not internal, i.e. they don't
        have '~' in them."""

        return filter(lambda s: s[:1] != "~", self.keys())

    def __call__(self, key, default=u"", connector=" - "):
        """Return a key, synthesizing it if necessary. A default value
        may be given (like dict.get); the default default is an empty
        unicode string (even if the tag is numeric).

        If a tied tag ('a~b') is requested, the 'connector' keyword
        argument may be used to specify what it is tied with.

        For details on tied tags, see the documentation for util.tagsplit."""

        if key[:1] == "~":
            key = key[1:]
            if "~" in key:
                # FIXME: decode ~filename etc.
                if not isinstance(default, basestring):
                    return default
                return connector.join(
                    filter(None,
                    map(lambda x: isinstance(x, basestring) and x or str(x),
                    map(lambda x: (isinstance(x, float) and "%.2f" % x) or x,
                    map(self.__call__, util.tagsplit("~" + key)))))) or default
            elif key == "#track":
                try:
                    return int(self["tracknumber"].split("/")[0])
                except (ValueError, TypeError, KeyError):
                    return default
            elif key == "#disc":
                try:
                    return int(self["discnumber"].split("/")[0])
                except (ValueError, TypeError, KeyError):
                    return default
            elif key == "length":
                length = self.get("~#length")
                if length is None:
                    return default
                else:
                    return util.format_time(length)
            elif key == "#rating":
                return dict.get(self, "~" + key, config.RATINGS.default)
            elif key == "rating":
                return util.format_rating(self("~#rating"))
            elif key == "people" or key == "people:roles":
                return (self._role_call(key, PEOPLE, "performer", True)
                        or default)
            elif key == "peoplesort" or key == "peoplesort:roles":
                # Ignores non-sort tags if there are any sort tags (e.g. just
                # returns "B" for {artist=A, performersort=B}).
                # TODO: figure out the "correct" behavior for mixed sort tags
                return (self._role_call(key, PEOPLE_SORT, "performersort",
                                        True)
                        or self("~" + key.replace("sort", ""),
                                default, connector))
            elif (key == "performers" or key == "performer" or
                    key == "performers:roles" or key == "performer:roles"):
                return (self._role_call(key,
                                        prefixed("performer", self.keys()),
                                        "performer")
                        or default)
            elif (key == "performerssort" or key == "performersort" or
                  key == "performerssort:roles" or
                  key == "performersort:roles"):
                return (self._role_call(key,
                                        prefixed("performersort", self.keys()),
                                        "performersort")
                        or self("~" + key.replace("sort", ""), default,
                                connector))
            elif key == "basename":
                return os.path.basename(self["~filename"]) or self["~filename"]
            elif key == "dirname":
                return os.path.dirname(self["~filename"]) or self["~filename"]
            elif key == "uri":
                try:
                    return self["~uri"]
                except KeyError:
                    return URI.frompath(self["~filename"])
            elif key == "format":
                return self.get("~format", self.format)
            elif key == "year":
                return self.get("date", default)[:4]
            elif key == "#year":
                try:
                    return int(self.get("date", default)[:4])
                except (ValueError, TypeError, KeyError):
                    return default
            elif key == "originalyear":
                return self.get("originaldate", default)[:4]
            elif key == "#originalyear":
                try:
                    return int(self.get("originaldate", default)[:4])
                except (ValueError, TypeError, KeyError):
                    return default
            elif key == "#tracks":
                try:
                    return int(self["tracknumber"].split("/")[1])
                except (ValueError, IndexError, TypeError, KeyError):
                    return default
            elif key == "#discs":
                try:
                    return int(self["discnumber"].split("/")[1])
                except (ValueError, IndexError, TypeError, KeyError):
                    return default
            elif key == "lyrics":
                try:
                    fileobj = file(self.lyric_filename, "rU")
                except EnvironmentError:
                    return default
                else:
                    return fileobj.read().decode("utf-8", "replace")
            elif key == "filesize":
                return util.format_size(self("~#filesize", 0))
            elif key == "playlists":
                # See Issue 876
                # Avoid circular references from formats/__init__.py
                from quodlibet.util.collection import Playlist
                playlists = Playlist.playlists_featuring(self)
                return "\n".join([s.name for s in playlists]) or default
            elif key.startswith("#replaygain_"):
                try:
                    val = self.get(key[1:], default)
                    return round(float(val.split(" ")[0]), 2)
                except (ValueError, TypeError, AttributeError):
                    return default
            elif key[:1] == "#":
                key = "~" + key
                if key in self:
                    return self[key]
                elif key in INTERN_NUM_DEFAULT:
                    return dict.get(self, key, 0)
                else:
                    try:
                        val = self[key[2:]]
                    except KeyError:
                        return default
                    try:
                        return int(val)
                    except ValueError:
                        try:
                            return float(val)
                        except ValueError:
                            return default
            else:
                return dict.get(self, "~" + key, default)

        elif key == "title":
            title = dict.get(self, "title")
            if title is None:
                basename = self("~basename")
                basename = basename.decode(const.FSCODING, "replace")
                return "%s [%s]" % (basename, _("Unknown"))
            else:
                return title
        elif key in SORT_TO_TAG:
            try:
                return self[key]
            except KeyError:
                key = SORT_TO_TAG[key]
        return dict.get(self, key, default)

    def _roles(self, tag):
        """Returns a defaultdict of name => [role, ...]."""
        roles = collections.defaultdict(list)
        for key in self.keys():
            if key.startswith(tag + ":"):
                role = util.title(key[1 + len(tag):])
                for name in self.list(key):
                    roles[name].append(role)
        for name in self.list(tag):
            roles[name]
        return roles

    def _role_call(self, key, sub_keys, role_tag, use_pseudo_roles=False):
        """Used to implement __call__ for a synthetic key with role support."""
        names = []
        names_seen = set()
        for sub_key in sub_keys:
            for value in self.list(sub_key):
                if value not in names_seen:
                    names_seen.add(value)
                    names.append(value)

        not_role_tag = lambda t: t != role_tag and t != '~' + role_tag
        if key.endswith(":roles"):
            roles = self._roles(role_tag)
            if use_pseudo_roles:
                for tag in filter(not_role_tag, sub_keys):
                    for name in self.list(tag):
                        if tag in TAG_ROLES:
                            roles[name].append(TAG_ROLES[tag])
                        else:
                            roles[name]
            return "\n".join(role_desc(n, roles[n]) for n in names)
        else:
            return "\n".join(names)

    @property
    def lyric_filename(self):
        """Returns the (potential) lyrics filename for this file"""

        filename = self.comma("title").replace(u'/', u'')[:128] + u'.lyric'
        sub_dir = ((self.comma("lyricist") or self.comma("artist"))
                  .replace(u'/', u'')[:128])

        if os.name == "nt":
            # this was added at a later point. only use escape_filename here
            # to keep the linux case the same as before
            filename = escape_filename(filename)
            sub_dir = escape_filename(sub_dir)
        else:
            filename = fsnative(filename)
            sub_dir = fsnative(sub_dir)

        path = os.path.join(
            expanduser(fsnative(u"~/.lyrics")), sub_dir, filename)
        return path

    def comma(self, key):
        """Get all values of a tag, separated by commas. Synthetic
        tags are supported, but will be slower. If the value is
        numeric, that is returned rather than a list."""

        if "~" in key or key == "title":
            v = self(key, u"")
        else:
            v = self.get(key, u"")
        if isinstance(v, (int, long, float)):
            return v
        else:
            return v.replace("\n", ", ")

    def list(self, key):
        """Get all values of a tag, as a list. Synthetic tags are supported,
        but will be slower. Numeric tags are not supported.

        An empty synthetic tag cannot be distinguished from a non-existent
        synthetic tag; both result in []."""

        if "~" in key or key == "title":
            v = self(key, connector="\n")
            if v == "":
                return []
            else:
                return v.split("\n")
        else:
            v = self.get(key)
            return [] if v is None else v.split("\n")

    def list_separate(self, key, connector=" - "):
        """Similar to list, but will return a list of all combinations
        for tied tags instead of one comma separated string"""
        if key[:1] == "~" and "~" in key[1:]:
            vals = \
                filter(None,
                map(lambda x: isinstance(x, basestring) and x or str(x),
                map(lambda x: (isinstance(x, float) and "%.2f" % x) or x,
                (self(tag) for tag in util.tagsplit(key)))))
            vals = (val.split("\n") for val in vals)
            r = [[]]
            for x in vals:
                r = [i + [y] for y in x for i in r]
            return map(connector.join, r)
        else:
            return self.list(key)

    def as_lowercased(self):
        """Returns a new AudioFile with all keys lowercased / values merged.

        Useful for tag writing for case insensitive tagging formats like
        APEv2 or VorbisComment.
        """

        merged = AudioFile()
        text = {}
        for key, value in self.iteritems():
            lower = key.lower()
            if key.startswith("~#"):
                merged[lower] = value
            else:
                text.setdefault(lower, []).extend(value.split("\n"))
        for key, values in text.items():
            merged[key] = "\n".join(values)
        return merged

    def exists(self):
        """Return true if the file still exists (or we can't tell)."""

        return os.path.exists(self["~filename"])

    def valid(self):
        """Return true if the file cache is up-to-date (checked via
        mtime), or we can't tell."""
        return (bool(self.get("~#mtime", 0)) and
                self["~#mtime"] == mtime(self["~filename"]))

    def mounted(self):
        """Return true if the disk the file is on is mounted, or
        the file is not on a disk."""
        return os.path.ismount(self.get("~mountpoint", "/"))

    def can_change(self, k=None):
        """See if this file supports changing the given tag. This may
        be a limitation of the file type, or the file may not be
        writable.

        If no arguments are given, return a list of tags that can be
        changed, or True if 'any' tags can be changed (specific tags
        should be checked before adding)."""

        if k is None:
            if os.access(self["~filename"], os.W_OK):
                return True
            else:
                return []

        try:
            if isinstance(k, unicode):
                k = k.encode("ascii")
            else:
                k.decode("ascii")
        except UnicodeError:
            return False

        return (k and "=" not in k and "~" not in k
                and os.access(self["~filename"], os.W_OK))

    def rename(self, newname):
        """Rename a file. Errors are not handled. This shouldn't be used
        directly; use library.rename instead."""

        if os.path.isabs(newname):
            mkdir(os.path.dirname(newname))
        else:
            newname = os.path.join(self('~dirname'), newname)

        if not os.path.exists(newname):
            shutil.move(self['~filename'], newname)
        elif normalize_path(newname, canonicalise=True) != self['~filename']:
            raise ValueError

        self.sanitize(newname)

    def website(self):
        """Look for a URL in the audio metadata, or a Google search
        if no URL can be found."""

        if "website" in self:
            return self.list("website")[0]
        for cont in self.list("contact") + self.list("comment"):
            c = cont.lower()
            if (c.startswith("http://") or c.startswith("https://") or
                    c.startswith("www.")):
                return cont
            elif c.startswith("//www."):
                return "http:" + cont
        else:
            text = "http://www.google.com/search?q="
            esc = lambda c: ord(c) > 127 and '%%%x' % ord(c) or c
            if "labelid" in self:
                text += ''.join(map(esc, self["labelid"]))
            else:
                artist = util.escape("+".join(self("artist").split()))
                album = util.escape("+".join(self("album").split()))
                artist = encode(artist)
                album = encode(album)
                artist = "%22" + ''.join(map(esc, artist)) + "%22"
                album = "%22" + ''.join(map(esc, album)) + "%22"
                text += artist + "+" + album
            text += "&ie=UTF8"
            return text

    def sanitize(self, filename=None):
        """Fill in metadata defaults. Find ~mountpoint, ~#mtime, ~#filesize
        and ~#added. Check for null bytes in tags."""

        # Replace nulls with newlines, trimming zero-length segments
        for key, val in self.items():
            if isinstance(val, basestring) and '\0' in val:
                self[key] = '\n'.join(filter(lambda s: s, val.split('\0')))
            # Remove unnecessary defaults
            if key in INTERN_NUM_DEFAULT and val == 0:
                del self[key]

        if filename:
            self["~filename"] = filename
        elif "~filename" not in self:
            raise ValueError("Unknown filename!")
        if self.is_file:
            self["~filename"] = normalize_path(
                self["~filename"], canonicalise=True)
            # Find mount point (terminating at "/" if necessary)
            head = self["~filename"]
            while "~mountpoint" not in self:
                head, tail = os.path.split(head)
                # Prevent infinite loop without a fully-qualified filename
                # (the unit tests use these).
                head = head or "/"
                if os.path.ismount(head):
                    self["~mountpoint"] = head
        else:
            self["~mountpoint"] = "/"

        # Fill in necessary values.
        self.setdefault("~#added", int(time.time()))

        # For efficiency, do a single stat here. See Issue 504
        try:
            stat = os.stat(self['~filename'])
            self["~#mtime"] = stat.st_mtime
            self["~#filesize"] = stat.st_size

            # Issue 342. This is a horrible approximation (due to headers) but
            # on FLACs, the most common case, this should be close enough
            if "~#bitrate" not in self:
                try:
                    # kbps = bytes * 8 / seconds / 1000
                    self["~#bitrate"] = int(stat.st_size /
                                            (self["~#length"] * (1000 / 8)))
                except (KeyError, ZeroDivisionError):
                    pass
        except OSError:
            self["~#mtime"] = 0

    def to_dump(self):
        """A string of 'key=value' lines, similar to vorbiscomment output."""
        s = []
        for k in self.keys():
            k = str(k)
            if isinstance(self[k], int) or isinstance(self[k], long):
                s.append("%s=%d" % (k, self[k]))
            elif isinstance(self[k], float):
                s.append("%s=%f" % (k, self[k]))
            else:
                for v2 in self.list(k):
                    if isinstance(v2, str):
                        s.append("%s=%s" % (k, v2))
                    else:
                        s.append("%s=%s" % (k, encode(v2)))
        for k in (INTERN_NUM_DEFAULT - set(self.keys())):
            s.append("%s=%d" % (k, self.get(k, 0)))
        if "~#rating" not in self:
            s.append("~#rating=%f" % self("~#rating"))
        s.append("~format=%s" % self.format)
        s.append("")
        return "\n".join(s)

    def from_dump(self, text):
        """Parses the text created with to_dump and adds the found tags."""
        for line in text.split("\n"):
            if not line:
                continue
            parts = line.split("=")
            key = parts[0]
            val = "=".join(parts[1:])
            if key == "~format":
                pass
            elif key.startswith("~#"):
                try:
                    self.add(key, int(val))
                except ValueError:
                    try:
                        self.add(key, float(val))
                    except ValueError:
                        pass
            else:
                self.add(key, val)

    def change(self, key, old_value, new_value):
        """Change 'old_value' to 'new_value' for the given metadata key.
        If the old value is not found, set the key to the new value."""
        try:
            parts = self.list(key)
            try:
                parts[parts.index(old_value)] = new_value
            except ValueError:
                self[key] = new_value
            else:
                self[key] = "\n".join(parts)
        except KeyError:
            self[key] = new_value

    def add(self, key, value):
        """Add a value for the given metadata key."""
        if key not in self:
            self[key] = value
        else:
            self[key] += "\n" + value

    def remove(self, key, value):
        """Remove a value from the given key; if the value is not found,
        remove all values for that key."""
        if key not in self:
            return
        elif self[key] == value:
            del(self[key])
        else:
            try:
                parts = self.list(key)
                parts.remove(value)
                self[key] = "\n".join(parts)
            except ValueError:
                if key in self:
                    del(self[key])

    def find_cover(self):
        """Return a file-like containing cover image data, or None if
        no cover is available."""
        return cover_plugins.acquire_cover_sync(self)

    def replay_gain(self, profiles, pre_amp_gain=0, fallback_gain=0):
        """Return the computed Replay Gain scale factor.

        profiles is a list of Replay Gain profile names ('album',
        'track') to try before giving up. The special profile name
        'none' will cause no scaling to occur. pre_amp_gain will be
        applied before checking for clipping. fallback_gain will be
        used when the song does not have replaygain information.
        """
        for profile in profiles:
            if profile is "none":
                return 1.0
            try:
                db = float(self["replaygain_%s_gain" % profile].split()[0])
                peak = float(self.get("replaygain_%s_peak" % profile, 1))
            except (KeyError, ValueError, IndexError):
                continue
            else:
                db += pre_amp_gain
                scale = 10. ** (db / 20)
                if scale * peak > 1:
                    scale = 1.0 / peak  # don't clip
                return min(15, scale)
        else:
            scale = 10. ** ((fallback_gain + pre_amp_gain) / 20)
            if scale > 1:
                scale = 1.0  # don't clip
            return min(15, scale)

    def write(self):
        """Write metadata back to the file."""
        raise NotImplementedError

    def __get_bookmarks(self):
        marks = []
        invalid = []
        for line in self.list("~bookmark"):
            try:
                time, mark = line.split(" ", 1)
            except:
                invalid.append((-1, line))
            else:
                try:
                    time = util.parse_time(time, None)
                except:
                    invalid.append((-1, line))
                else:
                    if time >= 0:
                        marks.append((time, mark))
                    else:
                        invalid.append((-1, line))
        marks.sort()
        marks.extend(invalid)
        return marks

    def __set_bookmarks(self, marks):
        result = []
        for time_, mark in marks:
            if time_ < 0:
                raise ValueError("mark times must be positive")
            result.append(u"%s %s" % (util.format_time(time_), mark))
        result = u"\n".join(result)
        if result:
            self["~bookmark"] = result
        elif "~bookmark" in self:
            del(self["~bookmark"])

    bookmarks = property(
        __get_bookmarks, __set_bookmarks,
        doc="""Parse and return song position bookmarks, or set them.
        Accessing this returns a copy, so song.bookmarks.append(...)
        will not work; you need to do
           marks = song.bookmarks
           marks.append(...)
           song.bookmarks = marks""")


# Looks like the real thing.
DUMMY_SONG = AudioFile({
    '~#length': 234, '~filename': '/dev/null',
    'artist': 'The Artist', 'album': 'An Example Album',
    'title': 'First Track', 'tracknumber': 1,
    'date': '2010-12-31',
})


def role_desc(name, roles):
    return name if not roles else "%s (%s)" % (name, ", ".join(sorted(roles)))


def prefixed(prefix, strings):
    return filter(lambda s: s == prefix or s.startswith(prefix + ":"), strings)
