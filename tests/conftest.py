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
        "sine=frequency=440:duration=3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", vid)
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", vid2)               # no audio track
    _ff("-f", "lavfi", "-i", "sine=frequency=660:duration=2", wav)
    _ff("-f", "lavfi", "-i", "color=c=red:size=200x100", "-frames:v", "1", png)
    return dict(video=vid, video_noaudio=vid2, wav=wav, png=png, dir=str(d))


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
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QColorDialog, QFileDialog, QInputDialog, QMenu, QMessageBox
    md = Modals()
    md.question_answer = QMessageBox.Yes
    md.color = QColor("#123456")

    def rec(kind, ret):
        def f(*a, **k):
            md.calls.append((kind, a[1:4] if len(a) > 1 else a))
            return ret() if callable(ret) else ret
        return staticmethod(f)

    monkeypatch.setattr(QMessageBox, "information", rec("information", QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "warning", rec("warning", QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "critical", rec("critical", QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "question", rec("question", lambda: md.question_answer))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: md.calls.append(("msgbox", self.text())) or 0)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", rec("save", lambda: (md.save_path, "")))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", rec("open", lambda: (md.open_path, "")))
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", rec("opens", lambda: (list(md.open_paths), "")))
    monkeypatch.setattr(QInputDialog, "getDouble", rec("getDouble", lambda: md.gap))
    monkeypatch.setattr(QColorDialog, "getColor", rec("color", lambda: md.color))

    def menu_exec(self, *a, **k):
        md.menus.append((self, _menu_texts(self)))
        if md.menu_pick:
            act = _find_action(self, md.menu_pick)
            md.menu_pick = None
            if act:
                act.trigger()
                return act
        return None
    monkeypatch.setattr(QMenu, "exec", menu_exec)
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


@pytest.fixture
def win(cs, qapp, modals):
    """A fresh, shown MainWindow. Never leaves modal dialogs or autosave files behind."""
    from PySide6.QtCore import QEvent
    w = cs.MainWindow()
    w.auto_timer.stop()
    w.show()
    _flush(qapp)
    yield w
    w.playing = False
    w.timer.stop()
    w._saved = w.snapshot()       # not dirty -> closeEvent does not ask to save
    w.close()
    w.deleteLater()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    _flush(qapp)
    try:
        os.remove(w.AUTOSAVE)
    except OSError:
        pass
