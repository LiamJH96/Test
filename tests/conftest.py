"""Shared fixtures for the Clip Studio regression suite.

Environment variables
---------------------
CLIP_STUDIO_PATH      directory containing clip_studio.py, or the .py file itself (default: the repo root,
                      i.e. the parent of this tests/ folder).
CLIP_STUDIO_ORIGINAL  optional path to an untouched copy of the original clip_studio.py; when present, a few
                      extra source-level comparisons are run against it (the essential baseline is embedded in
                      test_regression.py, so the suite never depends on this file existing).

Run:  QT_QPA_PLATFORM=offscreen python -m pytest -q tests
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------- must happen BEFORE clip_studio is imported:
# the module computes ~/.clip_studio paths (emoji cache, captions, sub defaults, autosave) at import time.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_FAKE_HOME = tempfile.mkdtemp(prefix="cs_test_home_")
os.environ["HOME"] = _FAKE_HOME
os.environ["USERPROFILE"] = _FAKE_HOME
os.environ.setdefault("XDG_RUNTIME_DIR", tempfile.mkdtemp(prefix="cs_xdg_"))
os.environ["APPDATA"] = os.path.join(_FAKE_HOME, "AppData", "Roaming")
# Parrot Studio (round 2) keeps settings / projects here; tests must never touch the real ones.
os.environ["PARROT_STUDIO_CONFIG_DIR"] = os.path.join(_FAKE_HOME, "parrot_config")
os.environ["PARROT_STUDIO_PROJECTS_DIR"] = os.path.join(_FAKE_HOME, "parrot_projects")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")

import pytest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.dirname(HERE)


def _module_file():
    p = os.environ.get("CLIP_STUDIO_PATH", DEFAULT_DIR)
    if os.path.isdir(p):
        p = os.path.join(p, "clip_studio.py")
    return os.path.abspath(p)


MODULE_FILE = _module_file()


def _load():
    sys.path.insert(0, os.path.dirname(MODULE_FILE))
    spec = importlib.util.spec_from_file_location("clip_studio", MODULE_FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clip_studio"] = mod
    spec.loader.exec_module(mod)
    return mod


CS = _load()
CS.EMO_STATE["offline"] = True      # never hit the network for emoji pictures during tests


def pytest_report_header(config):
    return [f"clip_studio under test: {MODULE_FILE}", f"fake HOME: {_FAKE_HOME}"]


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_FAKE_HOME, ignore_errors=True)


@pytest.fixture(scope="session")
def cs():
    return CS


@pytest.fixture(scope="session")
def fake_home():
    return _FAKE_HOME


@pytest.fixture(scope="session")
def qapp(cs):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    cs.dark_theme(app)
    return app


# ---------------------------------------------------------------- test media (generated once per session)
def _ff(*args):
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    vid = str(d / "clip_a.mp4")
    vid2 = str(d / "clip_b.mp4")
    wav = str(d / "tone.wav")
    png = str(d / "pic.png")
    _ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=3", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=3", "-c:v", "libx264", "-g", "15", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", vid)
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2", "-c:v", "libx264", "-g", "15", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", vid2)               # no audio track
    _ff("-f", "lavfi", "-i", "sine=frequency=660:duration=2", wav)
    _ff("-f", "lavfi", "-i", "color=c=red:size=200x100", "-frames:v", "1", png)
    hdr = str(d / "hdr_clip.mp4")             # 10-bit PQ / BT.2020 tagged -> detected as HDR
    _ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=1",
        "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc,format=yuv420p10le",
        "-c:v", "libx264", "-preset", "ultrafast", "-color_primaries", "bt2020", "-color_trc", "smpte2084",
        "-colorspace", "bt2020nc", hdr)
    return dict(video=vid, video_noaudio=vid2, wav=wav, png=png, hdr=hdr, dir=str(d))


@pytest.fixture(scope="session")
def rich_media(tmp_path_factory):
    """Realistic samples for the workflow tests: vertical 1080x1920 60 fps gameplay-like clip with stereo audio,
    a horizontal 1920x1080 clip, an mp3 music bed, a PNG overlay and a solid-blue vertical clip (for pixel checks)."""
    d = tmp_path_factory.mktemp("rich_media")
    out = dict(dir=str(d), vert=str(d / "gameplay_vertical.mp4"), horiz=str(d / "horizontal.mp4"),
               mp3=str(d / "music.mp3"), png=str(d / "overlay.png"), blue=str(d / "blue_vertical.mp4"))
    _ff("-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=60:duration=4", "-f", "lavfi", "-i",
        "sine=frequency=330:duration=4", "-f", "lavfi", "-i", "sine=frequency=550:duration=4",
        "-filter_complex", "[1][2]amerge=inputs=2[a]", "-map", "0:v", "-map", "[a]", "-c:v", "libx264",
        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", out["vert"])
    _ff("-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=3", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", out["horiz"])
    _ff("-f", "lavfi", "-i", "sine=frequency=220:duration=8", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k",
        out["mp3"])
    _ff("-f", "lavfi", "-i", "color=c=0x00ff00:size=400x200,format=rgba", "-frames:v", "1", out["png"])
    _ff("-f", "lavfi", "-i", "color=c=0x0000ff:size=540x960:rate=30:duration=4", "-f", "lavfi", "-i",
        "sine=frequency=500:duration=4", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-shortest", out["blue"])
    return out


# ---------------------------------------------------------------- modal dialogs never block
class Modals:
    """Records every dialog the app tries to show and answers it."""

    def __init__(self):
        self.calls = []
        self.question_answer = None          # filled in by the fixture (QMessageBox.Yes)
        self.save_path = ""
        self.open_path = ""
        self.open_paths = []
        self.gap = (1.0, True)
        self.color = None
        self.menus = []                      # [(QMenu, [action texts]), ...]
        self.menu_pick = None                # text of an action to trigger when a menu is exec'd

    def kinds(self):
        return [c[0] for c in self.calls]


def _menu_texts(m):
    out = []
    for a in m.actions():
        if a.isSeparator():
            continue
        out.append(a.text())
        if a.menu() is not None:
            out += ["> " + t for t in _menu_texts(a.menu())]
    return out


def _find_action(m, text):
    for a in m.actions():
        if a.menu() is not None:
            r = _find_action(a.menu(), text)
            if r:
                return r
        elif a.text().replace("&", "").strip() == text:
            return a
    return None


@pytest.fixture
def modals(monkeypatch, cs, qapp):
    """Replace the dialog classes *inside the clip_studio module* with non-blocking Python subclasses.

    (Patching attributes on the Shiboken classes themselves is not reliable, so the module globals are swapped.)
    """
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QColorDialog, QFileDialog, QInputDialog, QMenu, QMessageBox
    md = Modals()
    md.question_answer = QMessageBox.Yes
    md.color = QColor("#123456")

    def rec(kind, args, ret):
        md.calls.append((kind, args))
        return ret

    class FakeMessageBox(QMessageBox):
        @staticmethod
        def information(*a, **k):
            return rec("information", a[1:], QMessageBox.Ok)

        @staticmethod
        def warning(*a, **k):
            return rec("warning", a[1:], QMessageBox.Ok)

        @staticmethod
        def critical(*a, **k):
            return rec("critical", a[1:], QMessageBox.Ok)

        @staticmethod
        def question(*a, **k):
            return rec("question", a[1:], md.question_answer)

        def exec(self):
            md.calls.append(("msgbox", (self.windowTitle(), self.text())))
            return 0

        def clickedButton(self):
            return None

    class FakeFileDialog(QFileDialog):
        @staticmethod
        def getSaveFileName(*a, **k):
            return rec("save", a[1:], (md.save_path, ""))

        @staticmethod
        def getOpenFileName(*a, **k):
            return rec("open", a[1:], (md.open_path, ""))

        @staticmethod
        def getOpenFileNames(*a, **k):
            return rec("opens", a[1:], (list(md.open_paths), ""))

    class FakeInputDialog(QInputDialog):
        @staticmethod
        def getDouble(*a, **k):
            return rec("getDouble", a[1:], md.gap)

    class FakeColorDialog(QColorDialog):
        @staticmethod
        def getColor(*a, **k):
            return rec("color", a[1:], md.color)

    class FakeMenu(QMenu):
        def exec(self, *a, **k):
            md.menus.append((self, _menu_texts(self)))
            if md.menu_pick:
                act = _find_action(self, md.menu_pick)
                md.menu_pick = None
                if act:
                    act.trigger()
                    return act
            return None

    for name, fake in (("QMessageBox", FakeMessageBox), ("QFileDialog", FakeFileDialog),
                       ("QInputDialog", FakeInputDialog), ("QColorDialog", FakeColorDialog), ("QMenu", FakeMenu)):
        monkeypatch.setattr(cs, name, fake)
    return md


def _flush(app, ms=0):
    import time
    t_end = time.time() + ms / 1000.0
    while True:
        app.processEvents()
        if time.time() >= t_end:
            break
        time.sleep(0.005)


@pytest.fixture
def flush(qapp):
    return lambda ms=0: _flush(qapp, ms)


def dispose_window(w):
    """Close a MainWindow without prompts and tear it down so late background signals cannot reach it."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    try:
        w.playing = False
        w.timer.stop()
        stop_autosave(w)
        w._saved = w.snapshot()       # not dirty -> closeEvent does not ask to save
        if hasattr(w, "is_dirty"):
            try:
                w.is_dirty = lambda: False
            except Exception:
                pass
        w.close()
    except RuntimeError:
        return
    for sig, slot in ((CS.BUS.wave, w.tl._invalidate), (CS.BUS.prog, w.on_prog), (CS.BUS.done, w.on_export_done),
                      (CS.BUS.conv, w.on_conv_prog), (CS.BUS.convdone, w.on_conv_done)):
        try:
            sig.disconnect(slot)
        except (RuntimeError, TypeError):
            pass
    w.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    _flush(app)


def stop_autosave(w):
    """Stop every autosave-ish timer (works for the round-1 auto_timer and round-2 debounced timers)."""
    from PySide6.QtCore import QTimer
    for name in ("auto_timer", "save_timer", "save_max_timer", "rec_timer", "autosave_timer", "_autosave_timer"):
        t = getattr(w, name, None)
        if isinstance(t, QTimer):
            t.stop()


def show_editor(w):
    """Round 2: the main window starts on the project dashboard. Switch to the editor when that exists."""
    if hasattr(w, "show_editor"):
        w.show_editor()


@pytest.fixture
def win(cs, qapp, modals):
    """A fresh, shown MainWindow on the editor page. Never leaves modal dialogs or autosave files behind."""
    w = cs.MainWindow()
    stop_autosave(w)
    show_editor(w)
    w.show()
    _flush(qapp)
    yield w
    dispose_window(w)
    try:
        os.remove(getattr(cs.MainWindow, "AUTOSAVE", os.path.join(_FAKE_HOME, ".clip_studio", "autosave.clipproj")))
    except OSError:
        pass
