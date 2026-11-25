"""Child process for the crash-recovery tests (run by tests/test_recovery.py, not collected by pytest).

    python crash_child.py edit   <media.json>   start the app through main(), make a project, edit it, report
                                                progress on stdout, then idle until the parent SIGKILLs it
    python crash_child.py check  <media.json>   start the app again through main(), accept every recovery offer,
                                                print what was restored as one JSON line, close cleanly

The parent sets HOME / PARROT_STUDIO_CONFIG_DIR / PARROT_STUDIO_PROJECTS_DIR to a temp folder, so the real
session-lock / recovery machinery runs exactly as for a user, but isolated.
Env CRASH_WAIT_FOR: "recovery" (default) = report once a recovery snapshot exists (kill before autosave);
"saved" = report once the project file itself has the edits.
"""
import importlib.util
import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.environ.get("CLIP_STUDIO_PATH", os.path.dirname(HERE))
if os.path.isdir(APP):
    APP = os.path.join(APP, "clip_studio.py")
sys.path.insert(0, os.path.dirname(APP))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.append(REPO)
spec = importlib.util.spec_from_file_location("clip_studio", APP)
cs = importlib.util.module_from_spec(spec)
sys.modules["clip_studio"] = cs
spec.loader.exec_module(cs)
cs.EMO_STATE["offline"] = True

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

mode, media_file = sys.argv[1], sys.argv[2]
MEDIA = json.load(open(media_file, encoding="utf-8"))
ASKED = []


def say(*a):
    print(*a, flush=True)


class AutoBox(QMessageBox):
    """Every prompt is answered 'yes / restore' and recorded."""

    @staticmethod
    def question(*a, **k):
        ASKED.append(("question", a[1], a[2]))
        return QMessageBox.Yes

    @staticmethod
    def warning(*a, **k):
        ASKED.append(("warning", a[1], a[2]))
        return QMessageBox.Ok

    @staticmethod
    def information(*a, **k):
        ASKED.append(("information", a[1], a[2]))
        return QMessageBox.Ok

    def exec(self):
        ASKED.append(("box", self.windowTitle(), self.text()))
        self._pick = None
        for b in self.buttons():
            if self.buttonRole(b) == QMessageBox.AcceptRole:
                self._pick = b
                break
        return 0

    def clickedButton(self):
        return getattr(self, "_pick", None)


cs.QMessageBox = AutoBox
if not hasattr(cs.MainWindow, "show_editor") or not hasattr(cs, "store"):
    say("NO_INTEGRATION")
    sys.exit(3)
store = cs.store
orig_offer = cs.MainWindow.offer_restore


def file_clips(p):
    try:
        with open(p, encoding="utf-8") as f:
            return len(json.load(f).get("clips", []))
    except Exception:
        return -1


def edit_session(w):
    orig_offer(w)
    if not w.create_and_open("Crash test"):
        say("CREATE_FAILED")
        return
    p = w.proj_path
    say("PROJECT", p)
    for k in ("video", "png"):
        w.add_to_library(MEDIA[k])
    w.add_clip(w.lib[0], None, None)
    w.add_clip(w.lib[1], 1, 0.5)
    w.set_time(1.0)
    w.add_caption("survives a crash")
    w.clips[0].fin = 0.5
    w.after_edit()
    n = len(w.clips)
    say("EDITED", n, json.dumps(sorted(c.name for c in w.clips)))
    want = os.environ.get("CRASH_WAIT_FOR", "recovery")
    t0 = time.time()

    def poll():
        if want == "recovery":
            info = store.find_recovery(p)
            if info is not None:
                say("RECOVERY_WRITTEN", round(time.time() - t0, 3), file_clips(p))
                return
        elif file_clips(p) == n:
            say("SAVED", round(time.time() - t0, 3))
            return
        if time.time() - t0 > 20:
            say("TIMEOUT", file_clips(p))
            return
        QTimer.singleShot(25, poll)
    poll()


def check_session(w):
    orig_offer(w)
    p = w.proj_path
    res = dict(asked=ASKED, proj_path=p, clips=len(w.clips), names=sorted(c.name for c in w.clips),
               fin=[c.fin for c in w.clips if c.kind == "video"], file_clips=file_clips(p) if p else None)
    # the restored edits must reach the project file by autosave, too
    t0 = time.time()

    def wait_file():
        if p and file_clips(p) == res["clips"] or time.time() - t0 > 10:
            res["file_clips_after"] = file_clips(p) if p else None
            res["recovery_left"] = bool(p and store.find_recovery(p))
            say("RESULT", json.dumps(res))
            w.close()
            QApplication.instance().quit()
            return
        QTimer.singleShot(50, wait_file)
    wait_file()


cs.MainWindow.offer_restore = edit_session if mode == "edit" else check_session
cs.main()
