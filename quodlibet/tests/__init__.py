# -*- coding: utf-8 -*-
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation

import os
import sys
import unittest
import tempfile
import shutil
import atexit
import subprocess

try:
    import pytest
except ImportError:
    raise SystemExit("pytest missing: sudo apt-get install python-pytest")

from quodlibet.compat import PY3
from quodlibet.util.path import fsnative, is_fsnative, xdg_get_cache_home
from quodlibet.util.misc import environ
from quodlibet import util

from unittest import TestCase as OrigTestCase


class TestCase(OrigTestCase):

    # silence deprec warnings about useless renames
    failUnless = OrigTestCase.assertTrue
    failIf = OrigTestCase.assertFalse
    failUnlessEqual = OrigTestCase.assertEqual
    failUnlessRaises = OrigTestCase.assertRaises
    failUnlessAlmostEqual = OrigTestCase.assertAlmostEqual
    failIfEqual = OrigTestCase.assertNotEqual
    failIfAlmostEqual = OrigTestCase.assertNotAlmostEqual


skip = unittest.skip
skipUnless = unittest.skipUnless
skipIf = unittest.skipIf

DATA_DIR = os.path.join(util.get_module_dir(), "data")
assert is_fsnative(DATA_DIR)
_TEMP_DIR = None


def _wrap_tempfile(func):
    def wrap(*args, **kwargs):
        if kwargs.get("dir") is None and _TEMP_DIR is not None:
            assert is_fsnative(_TEMP_DIR)
            kwargs["dir"] = _TEMP_DIR
        return func(*args, **kwargs)
    return wrap


NamedTemporaryFile = _wrap_tempfile(tempfile.NamedTemporaryFile)


def mkdtemp(*args, **kwargs):
    path = _wrap_tempfile(tempfile.mkdtemp)(*args, **kwargs)
    assert is_fsnative(path)
    return path


def mkstemp(*args, **kwargs):
    fd, filename = _wrap_tempfile(tempfile.mkstemp)(*args, **kwargs)
    assert is_fsnative(filename)
    return (fd, filename)


def init_fake_app():
    from quodlibet import app

    from quodlibet import browsers
    from quodlibet.player.nullbe import NullPlayer
    from quodlibet.library.libraries import SongFileLibrary
    from quodlibet.library.librarians import SongLibrarian
    from quodlibet.qltk.quodlibetwindow import QuodLibetWindow, PlayerOptions
    from quodlibet.util.cover import CoverManager

    browsers.init()
    app.name = "Quod Libet"
    app.id = "quodlibet"
    app.player = NullPlayer()
    app.library = SongFileLibrary()
    app.library.librarian = SongLibrarian()
    app.cover_manager = CoverManager()
    app.window = QuodLibetWindow(app.library, app.player, headless=True)
    app.player_options = PlayerOptions(app.window)


def destroy_fake_app():
    from quodlibet import app

    app.window.destroy()
    app.library.destroy()
    app.library.librarian.destroy()
    app.player.destroy()

    app.window = app.library = app.player = app.name = app.id = None
    app.cover_manager = None


_BUS_INFO = None


def init_test_environ():
    """This needs to be called before any test can be run.

    Before exiting the process call exit_test_environ() to clean up
    any resources created.
    """

    global _TEMP_DIR, _BUS_INFO

    # create a user dir in /tmp and set env vars
    _TEMP_DIR = tempfile.mkdtemp(prefix=fsnative(u"QL-TEST-"))

    # needed for dbus/dconf
    runtime_dir = tempfile.mkdtemp(prefix=fsnative(u"RUNTIME-"), dir=_TEMP_DIR)
    os.chmod(runtime_dir, 0o700)
    environ["XDG_RUNTIME_DIR"] = runtime_dir

    # force the old cache dir so that GStreamer can re-use the GstRegistry
    # cache file
    environ["XDG_CACHE_HOME"] = xdg_get_cache_home()
    # GStreamer will update the cache if the environment has changed
    # (in Gst.init()). Since it takes 0.5s here and doesn't add much,
    # disable it. If the registry cache is missing it will be created
    # despite this setting.
    environ["GST_REGISTRY_UPDATE"] = fsnative(u"no")

    # set HOME and remove all XDG vars that default to it if not set
    home_dir = tempfile.mkdtemp(prefix=fsnative(u"HOME-"), dir=_TEMP_DIR)
    environ["HOME"] = home_dir

    # set to new default
    environ.pop("XDG_DATA_HOME", None)

    _BUS_INFO = None
    if os.name != "nt" and "DBUS_SESSION_BUS_ADDRESS" in environ:
        try:
            out = subprocess.check_output(["dbus-launch"])
        except (subprocess.CalledProcessError, OSError):
            pass
        else:
            if PY3:
                out = out.decode("ascii")
            _BUS_INFO = dict([l.split("=", 1) for l in out.splitlines()])
            environ.update(_BUS_INFO)

    # Ideally nothing should touch the FS on import, but we do atm..
    # Get rid of all modules so QUODLIBET_USERDIR gets used everywhere.
    for key in list(sys.modules.keys()):
        if key.startswith('quodlibet'):
            del(sys.modules[key])

    import quodlibet
    quodlibet.init(no_translations=True, no_excepthook=True)
    quodlibet.app.name = "QL Tests"


def exit_test_environ():
    """Call after init_test_environ() and all tests are finished"""

    global _TEMP_DIR, _BUS_INFO

    try:
        shutil.rmtree(_TEMP_DIR)
    except EnvironmentError:
        pass

    if _BUS_INFO:
        try:
            subprocess.check_call(
                ["kill", "-9", _BUS_INFO["DBUS_SESSION_BUS_PID"]])
        except (subprocess.CalledProcessError, OSError):
            pass


# we have to do this on import so the tests work with other test runners
# like py.test which don't know about out setup code and just import
init_test_environ()
atexit.register(exit_test_environ)


def unit(run=[], suite=None, strict=False, exitfirst=False, network=True,
         quality=False):
    """Returns 0 if everything passed"""

    # make glib warnings fatal
    if strict:
        from gi.repository import GLib
        GLib.log_set_always_fatal(
            GLib.LogLevelFlags.LEVEL_CRITICAL |
            GLib.LogLevelFlags.LEVEL_ERROR |
            GLib.LogLevelFlags.LEVEL_WARNING)

    args = []

    if run:
        args.append("-k")
        args.append(" or ".join(run))

    skip_markers = []

    if not quality:
        skip_markers.append("quality")

    if not network:
        skip_markers.append("network")

    if skip_markers:
        args.append("-m")
        args.append(" and ".join(["not %s" % m for m in skip_markers]))

    if exitfirst:
        args.append("-x")

    if suite is None:
        args.append("tests")
    else:
        args.append(os.path.join("tests", suite))

    pytest.main(args=args)
