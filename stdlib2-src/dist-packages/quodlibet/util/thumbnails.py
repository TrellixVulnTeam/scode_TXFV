# -*- coding: utf-8 -*-
# Copyright 2009-2014 Christoph Reiter
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation

import os
import tempfile
import hashlib

from gi.repository import GdkPixbuf, GLib

from quodlibet.const import USERDIR
from quodlibet.util.path import mtime, mkdir, pathname2url, \
    xdg_get_cache_home, is_fsnative


def add_border(pixbuf, val, round=False, width=1):
    """Add a 1px border to the pixbuf and round of the edges if needed.
    val is the border brightness from 0 to 255.

    The resulting pixbuf will be width * 2px higher and wider.

    Can not fail.
    """

    # XXX: borders don't overlap in all places
    # .. port to cairo?
    for i in range(width):
        pixbuf = _add_border(pixbuf, val, round)
    return pixbuf


def _add_border(pixbuf, val, round=False):
    if not 0 <= val <= 255:
        raise ValueError

    c = (val << 24) | (val << 16) | (val << 8) | 0xFF

    w, h = pixbuf.get_width(), pixbuf.get_height()
    rgb = GdkPixbuf.Colorspace.RGB
    newpb = GdkPixbuf.Pixbuf.new(rgb, True, 8, w + 2, h + 2)
    newpb.fill(c)
    pixbuf.copy_area(0, 0, w, h, newpb, 1, 1)

    if round:
        m = (c & 0xFFFFFF00) | 0xDD
        p = (c & 0xFFFFFF00) | 0xBB
        o = (c & 0xFFFFFF00) | 0x70
        n = (c & 0xFFFFFF00) | 0x40
        l = -1
        e = -2

        mask = (
            (0, 0, n, p),
            (0, o, m, l),
            (n, m, e, e),
            (p, l, e, e)
        )

        overlay = GdkPixbuf.Pixbuf.new(rgb, True, 8, 1, 1)
        overlay.fill(m)

        for y, row in enumerate(mask):
            for x, pix in enumerate(row):
                for xn, yn in [(x, y), (w + 1 - x, y), (w + 1 - x, h + 1 - y),
                               (x, h + 1 - y)]:
                    if pix == l:
                        overlay.composite(newpb, xn, yn, 1, 1, 0, 0, 1, 1,
                                          GdkPixbuf.InterpType.NEAREST, 70)
                    elif pix != e:
                        newpb.new_subpixbuf(xn, yn, 1, 1).fill(pix)
    return newpb


def calc_scale_size(boundary, size, scale_up=True):
    """Returns the biggest possible size to fit into the boundary,
    respecting the aspect ratio.

    If `scale_up` is True the result can be larger than size.

    All sizes have to be > 0.
    """

    bwidth, bheight = boundary
    iwidth, iheight = size

    if bwidth <= 0 or bheight <= 0 or iwidth <= 0 or iheight <= 0:
        raise ValueError

    scale_w, scale_h = iwidth, iheight

    if iwidth > bwidth or iheight > bheight or scale_up:
        bratio = float(bwidth) / bheight
        iratio = float(iwidth) / iheight

        if iratio > bratio:
            scale_w = bwidth
            scale_h = int(bwidth / iratio)
        else:
            scale_w = int(bheight * iratio)
            scale_h = bheight

    return scale_w, scale_h


def scale(pixbuf, boundary, scale_up=True, force_copy=False):
    """Scale a pixbuf so it fits into the boundary.
    (preserves image aspect ratio)

    If `scale_up` is True, the resulting pixbuf can be larger than
    the original one.

    If `force_copy` is False the resulting pixbuf might be the passed one.

    Can not fail.
    """

    size = pixbuf.get_width(), pixbuf.get_height()

    scale_w, scale_h = calc_scale_size(boundary, size, scale_up)

    if (scale_w, scale_h) == size:
        if force_copy:
            return pixbuf.copy()
        return pixbuf

    return pixbuf.scale_simple(scale_w, scale_h, GdkPixbuf.InterpType.BILINEAR)


def get_thumbnail_folder():
    """Returns a path to the thumbnail folder.

    The returned path might not exist.
    """

    if os.name == "nt":
        thumb_folder = os.path.join(USERDIR, "thumbnails")
    else:
        cache_folder = os.path.join(xdg_get_cache_home(), "thumbnails")
        thumb_folder = os.path.expanduser('~/.thumbnails')
        if os.path.exists(cache_folder) or not os.path.exists(thumb_folder):
            thumb_folder = cache_folder

    return thumb_folder


def get_cache_info(path, boundary):
    """For an image at `path` return (cache_path, thumb_size)

    cache_path points to a potential cache file
    thumb size is either 128 or 256
    """

    assert is_fsnative(path)

    width, height = boundary

    if width <= 128 and height <= 128:
        size_name = "normal"
        thumb_size = 128
    else:
        size_name = "large"
        thumb_size = 256

    thumb_folder = get_thumbnail_folder()
    cache_dir = os.path.join(thumb_folder, size_name)

    uri = "file://" + pathname2url(path)
    thumb_name = hashlib.md5(uri).hexdigest() + ".png"
    thumb_path = os.path.join(cache_dir, thumb_name)

    return (thumb_path, thumb_size)


def get_thumbnail_from_file(fileobj, boundary):
    """Like get_thumbnail() but works with files that can't be reopened.

    This is needed on Windows where NamedTemporaryFile can't be reopened.

    Can raise GLib.GError or return None.
    """

    assert fileobj

    try:
        path = fileobj.name
        assert is_fsnative(path), path
        return get_thumbnail(path, boundary)
    except GLib.GError:
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(fileobj.read())
            loader.close()
            fileobj.seek(0, 0)
            # can return None in case of partial data
            pixbuf = loader.get_pixbuf()
        except EnvironmentError:
            pass
        else:
            if pixbuf is not None:
                return scale(pixbuf, boundary)


def get_thumbnail(path, boundary):
    """Get a thumbnail pixbuf of an image at `path`.

    Will create/use a thumbnail in the user's thumbnail directory if possible.
    Follows the Free Desktop specification:

    http://specifications.freedesktop.org/thumbnail-spec/

    Can raise GLib.GError.
    """

    width, height = boundary

    # embedded thumbnails come from /tmp/
    # and too big thumbnails make no sense
    if path.startswith(tempfile.gettempdir()) or \
            width > 256 or height > 256 or mtime(path) == 0:
        return GdkPixbuf.Pixbuf.new_from_file_at_size(path, width, height)

    thumb_path, thumb_size = get_cache_info(path, boundary)
    cache_dir = os.path.dirname(thumb_path)
    try:
        mkdir(cache_dir, 0700)
    except OSError:
        return GdkPixbuf.Pixbuf.new_from_file_at_size(path, width, height)

    pb = meta_mtime = None
    if os.path.exists(thumb_path):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(thumb_path)
        except GLib.GError:
            # in case it fails to load, we recreate it
            pass
        else:
            meta_mtime = pb.get_option("tEXt::Thumb::MTime")
            if meta_mtime:
                try:
                    meta_mtime = int(meta_mtime)
                except ValueError:
                    pass

    if not pb or meta_mtime != int(mtime(path)):
        pb = GdkPixbuf.Pixbuf.new_from_file(path)

        #Too small picture, no thumbnail needed
        if pb.get_width() < thumb_size and pb.get_height() < thumb_size:
            return scale(pb, boundary)

        info = GdkPixbuf.Pixbuf.get_file_info(path)[0]
        uri = "file://" + pathname2url(path)
        mime = info.get_mime_types()[0]
        options = {
            "tEXt::Thumb::Image::Width": str(pb.get_width()),
            "tEXt::Thumb::Image::Height": str(pb.get_height()),
            "tEXt::Thumb::URI": uri,
            "tEXt::Thumb::MTime": str(int(mtime(path))),
            "tEXt::Thumb::Size": str(os.path.getsize(path)),
            "tEXt::Thumb::Mimetype": mime,
            "tEXt::Software": "QuodLibet"
        }

        pb = scale(pb, (thumb_size, thumb_size))
        pb.savev(thumb_path, "png", options.keys(), options.values())
        try:
            os.chmod(thumb_path, 0600)
        except OSError:
            pass

    return scale(pb, boundary)
