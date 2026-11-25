"""Autosave, crash recovery and legacy project files in the running editor (round-2 integration).

* autosave: debounced (~2 s after the last edit), never more than ~30 s behind, atomic, failures shown
* conflicts: a project file changed by another program is never overwritten silently
* crash recovery: a REAL child process (tests/crash_child.py) runs the app through main(), edits, is SIGKILLed;
  a second child must detect the crash and restore the edits
* legacy: Clip Studio v1 files (round-0 / round-1 format and an older minimal one) and the old autosave

Everything is skipped until the editor is integrated with project_store (MainWindow.show_editor exists).
"""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from uihelpers import flush, wait_until

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.append(REPO)


@pytest.fixture(scope="module")
def integrated(cs):
    if not hasattr(cs.MainWindow, "show_editor") or not hasattr(cs, "store"):
        pytest.skip("editor not integrated with project_store yet")
    return cs.store


@pytest.fixture
def store(integrated, tmp_path, monkeypatch):
    """project_store with this test's own projects/config folders."""
    monkeypatch.setenv("PARROT_STUDIO_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("PARROT_STUDIO_PROJECTS_DIR", str(tmp_path / "projects"))
    integrated.take_problems()
    return integrated


@pytest.fixture
def live(cs, qapp, modals, store):
    """An editor window with autosave switched ON (the `win` fixture switches it off)."""
    from conftest import dispose_window
    w = cs.MainWindow()
    w.show_editor()
    w.resize(1400, 900)
    w.show()
    flush(20)
    yield w
    dispose_window(w)


class Counter:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def __call__(self, path, text, *a, **k):
        self.calls.append((time.time(), path, len(json.loads(text).get("clips", []))))
        return self.fn(path, text, *a, **k)


def open_new(w, store, name="Autosave test"):
    p = store.create_project(name)
    assert w.open_project(p) is not False
    flush(50)
    return p


def file_doc(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# =============================================================================== autosave
def test_autosave_is_debounced(live, store, rich_media, monkeypatch):
    w = live
    p = open_new(w, store)
    cnt = Counter(store.save_project_text)
    monkeypatch.setattr(store, "save_project_text", cnt)
    w.add_to_library(rich_media["png"])
    t_first = time.time()
    for i in range(4):                                   # four edits, 0.4 s apart
        w.add_clip(w.lib[0], 1, None)
        flush(400)
    assert cnt.calls == [], "saved while edits were still coming"
    t_last = time.time()
    assert wait_until(lambda: cnt.calls, timeout=6), "no autosave after the edits stopped"
    when = cnt.calls[0][0] - t_last
    assert 1.0 < when < 4.0, f"autosave {when:.2f} s after the last edit (expected about 2 s)"
    assert wait_until(lambda: w.writer.wait_idle(0.01), timeout=5)
    flush(300)
    assert len(cnt.calls) == 1 and cnt.calls[0][2] == 4
    assert len(file_doc(p)["clips"]) == 4 and file_doc(p)["schema"] == 2
    assert t_last - t_first < 3


def test_autosave_max_interval_while_editing(cs, qapp, modals, store, rich_media, monkeypatch):
    from conftest import dispose_window
    monkeypatch.setattr(cs.MainWindow, "SAVE_MAX_MS", 2500)
    w = cs.MainWindow()
    w.show_editor()
    w.show()
    try:
        p = open_new(w, store)
        cnt = Counter(store.save_project_text)
        monkeypatch.setattr(store, "save_project_text", cnt)
        w.add_to_library(rich_media["png"])
        t0 = time.time()
        while time.time() - t0 < 4.5:                    # never pause long enough for the debounce
            w.add_clip(w.lib[0], 1, None)
            flush(500)
        assert cnt.calls, "continuous editing must still be saved every SAVE_MAX_MS"
        assert cnt.calls[0][0] - t0 < 3.5
        assert 0 < len(file_doc(p)["clips"]) < len(w.clips) + 1
    finally:
        dispose_window(w)


def test_autosave_skips_when_nothing_changed(live, store, monkeypatch):
    w = live
    open_new(w, store)
    cnt = Counter(store.save_project_text)
    monkeypatch.setattr(store, "save_project_text", cnt)
    w.set_time(1.0)                                      # seeking, selecting, zooming are not edits
    w.zoom(1.3)
    flush(3000)
    assert cnt.calls == []


def test_canvas_only_change_is_autosaved(live, store):
    w = live
    p = open_new(w, store)
    w.cb_canvas.setCurrentIndex(1)
    assert wait_until(lambda: file_doc(p)["W"] == w.W, timeout=6), "canvas change never reached the file"


def test_autosave_write_failure_keeps_old_file_and_is_shown(live, store, rich_media, monkeypatch):
    w = live
    p = open_new(w, store)
    w.add_to_library(rich_media["png"])
    w.add_clip(w.lib[0], 1, 0.0)
    assert wait_until(lambda: len(file_doc(p)["clips"]) == 1, timeout=6)
    good = open(p, encoding="utf-8").read()
    real_replace = os.replace

    def failing_replace(src, dst):
        if os.path.abspath(dst) == os.path.abspath(p):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", failing_replace)
    w.add_clip(w.lib[0], 1, None)
    assert wait_until(lambda: getattr(w, "save_state", "") == "failed" or
                      "fail" in w.statusBar().currentMessage().lower(), timeout=8), "failure was swallowed"
    assert open(p, encoding="utf-8").read() == good, "the old file must stay intact"
    folder = os.path.dirname(p)
    assert not [n for n in os.listdir(folder) if n.endswith(".tmp")], "temp file left behind"
    # the unsaved edit is not lost: it is in a recovery snapshot
    assert wait_until(lambda: w.writer.wait_idle(0.01), timeout=5)
    info = store.find_recovery(p)
    assert info is not None and len(store.load_recovery(info)["clips"]) == 2
    # once the disk works again the next save goes through
    monkeypatch.setattr(os, "replace", real_replace)
    w.autosave()
    assert wait_until(lambda: len(file_doc(p)["clips"]) == 2, timeout=6)


def test_external_change_is_never_overwritten_silently(live, store, rich_media):
    w = live
    p = open_new(w, store)
    w.add_to_library(rich_media["png"])
    w.add_clip(w.lib[0], 1, 0.0)
    assert wait_until(lambda: len(file_doc(p)["clips"]) == 1, timeout=6)
    assert wait_until(lambda: w.writer.wait_idle(0.01), timeout=5)
    theirs = file_doc(p)
    theirs["clips"] = theirs["clips"] * 3
    for i, c in enumerate(theirs["clips"]):
        c = dict(c, id=f"ext{i}", start=5.0 * i)
        theirs["clips"][i] = c
    with open(p, "w", encoding="utf-8") as f:
        json.dump(theirs, f)
    w.add_clip(w.lib[0], 1, None)                         # our edit -> autosave must notice the conflict
    flush(4000)
    assert len(file_doc(p)["clips"]) == 3, "the other program's version was overwritten"
    # our edit is not lost either
    assert wait_until(lambda: w.writer.wait_idle(0.01), timeout=5)
    info = store.find_recovery(p)
    assert info is not None and len(store.load_recovery(info)["clips"]) == 2


def test_switching_projects_saves_the_first(live, store, rich_media):
    w = live
    p1 = open_new(w, store, "First")
    w.add_to_library(rich_media["png"])
    w.add_clip(w.lib[0], 1, 0.0)
    p2 = store.create_project("Second")
    w.open_project(p2)                                   # immediately, well inside the debounce
    assert len(file_doc(p1)["clips"]) == 1
    assert w.proj_path == os.path.abspath(p2) and w.clips == []


def test_close_saves_pending_changes(cs, qapp, modals, store, rich_media):
    w = cs.MainWindow()
    w.show_editor()
    w.show()
    p = open_new(w, store)
    w.add_to_library(rich_media["png"])
    w.add_clip(w.lib[0], 1, 0.0)
    w.close()                                            # well inside the debounce
    flush(50)
    assert len(file_doc(p)["clips"]) == 1
    w.deleteLater()
    flush()


# =============================================================================== recovery prompts in-process
@pytest.fixture
def pick(cs, monkeypatch):
    """Make QMessageBox.exec() click the button whose text starts with the first matching entry of pick.order."""
    from PySide6.QtWidgets import QMessageBox

    class State:
        order = []
        seen = []
    base = cs.QMessageBox

    class PickBox(base):
        def exec(self):
            State.seen.append((self.windowTitle(), [b.text() for b in self.buttons()]))
            self._clk = None
            for want in State.order:
                for b in self.buttons():
                    if b.text().replace("&", "").startswith(want):
                        self._clk = b
                        break
                if self._clk:
                    break
            return 0

        def clickedButton(self):
            return self._clk
    monkeypatch.setattr(cs, "QMessageBox", PickBox)
    return State


def _make_recovery(store, n):
    p = store.create_project("Has recovery")
    d = store.load_project_file(p)
    d["clips"] = [{"id": f"r{i}", "name": f"img{i}", "path": "", "kind": "image", "track": 1, "start": 5.0 * i,
                   "inp": 0.0, "dur": 5.0} for i in range(n)]
    store.write_recovery(p, d)
    old = time.time() - 120
    os.utime(p, (old, old))
    return p


def test_opening_a_project_with_recovery_offers_and_restores(live, store, pick):
    w = live
    p = _make_recovery(store, 3)
    pick.order = ["Restore"]
    w.open_project(p)
    assert any("ecover" in t for t, _ in pick.seen), pick.seen
    assert len(w.clips) == 3
    w.do_undo()                                          # Ctrl+Z goes back to the saved version
    assert len(w.clips) == 0


def test_recovery_keep_for_later_and_discard(live, store, pick):
    w = live
    p = _make_recovery(store, 2)
    pick.order = ["Keep"]
    w.open_project(p)
    assert w.clips == []
    w.show_dashboard()
    pick.order = ["Discard"]
    pick.seen.clear()
    w.open_project(p)
    assert pick.seen, "a kept recovery must be offered again"
    assert w.clips == [] and store.find_recovery(p) is None


def test_damaged_recovery_is_not_loaded(live, store, pick):
    w = live
    p = store.create_project("Damaged rec")
    rp = store.write_recovery(p, store.load_project_file(p))
    with open(rp, "w", encoding="utf-8") as f:
        f.write('{"clips": [{"id":')
    os.utime(p, (time.time() - 60, time.time() - 60))
    pick.order = ["Restore"]
    assert w.open_project(p) is not False
    assert not any("ecover" in t for t, _ in pick.seen)
    assert w.proj_path == os.path.abspath(p)


# =============================================================================== legacy files in the editor
def legacy_round0(media):
    from test_project_store import v1_doc
    d = v1_doc(2, media=media["video"])
    d["lib"].append({"kind": "image", "dur": 0.0, "w": 200, "h": 100, "hasaudio": False, "hdr": False, "trc": "",
                     "path": media["png"], "name": "pic.png", "pending": False})
    d["clips"].append({**d["clips"][0], "id": "capt0001", "name": "Caption: Hi", "path": "", "kind": "image",
                       "track": 1, "nat": 1, "text": "Hi there", "csize": 72, "cfont": "Arial", "dur": 2.0})
    d["W"], d["H"] = 1920, 1080
    d["substyle"]["size"] = 101
    return d


def test_legacy_v1_project_opens_and_upgrades_without_losing_keys(live, store, media, tmp_path, cs):
    w = live
    d = legacy_round0(media)
    d["unknown_future_key"] = [1, 2, 3]
    p = str(tmp_path / "legacy" / "Old project.clipproj")
    os.makedirs(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f)
    assert w.open_project(p) is not False
    assert len(w.clips) == 3 and (w.W, w.H) == (1920, 1080) and w.sub_style()["size"] == 101
    assert [c.text for c in w.clips if c.nat] == ["Hi there"]
    assert [l["path"] for l in w.lib] == [media["video"], media["png"]]
    w.add_clip(w.lib[1], 0, None)                        # an edit -> autosave upgrades the file
    assert wait_until(lambda: file_doc(p).get("schema") == 2, timeout=6)
    nd = file_doc(p)
    assert nd["unknown_future_key"] == [1, 2, 3] and len(nd["clips"]) == 4
    for a, b in zip(d["clips"], nd["clips"]):
        assert {k: b[k] for k in a} == {**a, "path": b["path"], "name": b["name"]} or a["nat"]


def test_older_minimal_v1_project_opens(live, store, media, tmp_path):
    w = live
    p = str(tmp_path / "older.clipproj")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"W": 1080, "H": 1920, "clips": [{"id": "x1", "name": "clip", "path": media["video"],
                                                     "kind": "video", "track": 2, "start": 0.0, "inp": 0.0,
                                                     "dur": 2.0}]}, f)
    assert w.open_project(p) is not False
    assert len(w.clips) == 1 and w.clips[0].dur == 2.0
    w.set_time(1.0)
    w.update_preview(force=True)
    w.tl.repaint()


def test_legacy_autosave_offered_once(cs, qapp, modals, store, media):
    from conftest import dispose_window
    from test_project_store import v1_doc
    old = cs.MainWindow.AUTOSAVE
    os.makedirs(os.path.dirname(old), exist_ok=True)
    with open(old, "w", encoding="utf-8") as f:
        json.dump(v1_doc(2, media=media["video"]), f)
    w = cs.MainWindow()
    w.show()
    try:
        modals.question_answer = cs.QMessageBox.Yes
        w.offer_restore()
        asked = [c for c in modals.calls if c[0] == "question"]
        assert len(asked) == 1
        assert len(w.clips) == 2 and w.proj_path and os.path.isfile(w.proj_path)
        assert not os.path.exists(old)
        n = len(modals.calls)
        w.offer_restore()
        assert not [c for c in modals.calls[n:] if c[0] == "question"], "offered twice"
    finally:
        dispose_window(w)
        for f in (old, old + ".old"):
            if os.path.exists(f):
                os.remove(f)


# =============================================================================== real crash (SIGKILL)
def _child_env(tmp_path):
    env = dict(os.environ)
    home = tmp_path / "child_home"
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData"),
               PARROT_STUDIO_CONFIG_DIR=str(tmp_path / "child_cfg"),
               PARROT_STUDIO_PROJECTS_DIR=str(tmp_path / "child_projects"), QT_QPA_PLATFORM="offscreen")
    from conftest import MODULE_FILE
    env["CLIP_STUDIO_PATH"] = MODULE_FILE
    return env


def _run_edit_child(tmp_path, media, wait_for):
    mf = tmp_path / "media.json"
    mf.write_text(json.dumps({"video": media["video"], "png": media["png"]}))
    env = _child_env(tmp_path)
    env["CRASH_WAIT_FOR"] = wait_for
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "crash_child.py"), "edit", str(mf)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    lines, t0 = [], time.time()
    try:
        for line in p.stdout:
            lines.append(line.split())
            if lines[-1][0] in ("RECOVERY_WRITTEN", "SAVED", "TIMEOUT", "NO_INTEGRATION", "CREATE_FAILED"):
                break
            if time.time() - t0 > 60:
                break
    finally:
        os.kill(p.pid, signal.SIGKILL)                   # the crash
        p.wait(10)
    assert p.returncode == -signal.SIGKILL or lines[-1][0] == "NO_INTEGRATION"
    return lines, mf, env


def _run_check_child(mf, env):
    r = subprocess.run([sys.executable, os.path.join(HERE, "crash_child.py"), "check", str(mf)], env=env,
                       capture_output=True, text=True, timeout=120)
    res = [l for l in r.stdout.splitlines() if l.startswith("RESULT ")]
    assert res, r.stdout[-2000:] + r.stderr[-2000:]
    return json.loads(res[0][7:])


@pytest.mark.skipif(sys.platform.startswith("win"), reason="SIGKILL")
def test_crash_after_recovery_write_is_recovered_on_relaunch(integrated, tmp_path, media):
    lines, mf, env = _run_edit_child(tmp_path, media, "recovery")
    kinds = [l[0] for l in lines]
    if "NO_INTEGRATION" in kinds:
        pytest.skip("app not integrated")
    assert "RECOVERY_WRITTEN" in kinds, lines
    proj = " ".join(next(l for l in lines if l[0] == "PROJECT")[1:])
    n_edit = int(next(l for l in lines if l[0] == "EDITED")[1])
    rec_line = next(l for l in lines if l[0] == "RECOVERY_WRITTEN")
    assert float(rec_line[1]) < 2.0, "recovery snapshot should be written well before the 2 s autosave"
    # the kill came before the autosave: the project file is still the empty new project
    assert len(file_doc(proj)["clips"]) < n_edit
    res = _run_check_child(mf, env)
    assert res["proj_path"] and os.path.abspath(res["proj_path"]) == os.path.abspath(proj), res
    assert res["clips"] == n_edit and res["fin"] == [0.5], res
    assert any("unexpected" in (a[1] + a[2]).lower() or "recover" in (a[1] + a[2]).lower() for a in res["asked"])
    assert res["file_clips_after"] == n_edit, "the restored edits should be autosaved into the project"
    assert not res["recovery_left"]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="SIGKILL")
def test_crash_after_autosave_loses_nothing(integrated, tmp_path, media):
    lines, mf, env = _run_edit_child(tmp_path, media, "saved")
    kinds = [l[0] for l in lines]
    if "NO_INTEGRATION" in kinds:
        pytest.skip("app not integrated")
    assert "SAVED" in kinds, lines
    proj = " ".join(next(l for l in lines if l[0] == "PROJECT")[1:])
    n_edit = int(next(l for l in lines if l[0] == "EDITED")[1])
    assert len(file_doc(proj)["clips"]) == n_edit
    # the dead session's lock is left behind and is reported exactly once
    env_cfg = env["PARROT_STUDIO_CONFIG_DIR"]
    sess = os.path.join(env_cfg, "sessions")
    assert [n for n in os.listdir(sess) if n.endswith(".lock")], "no session lock left by the killed app"
    res = _run_check_child(mf, env)
    assert not [a for a in res["asked"] if a[0] == "warning"], res["asked"]
    assert len(file_doc(proj)["clips"]) == n_edit
