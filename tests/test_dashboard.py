"""Project dashboard: deleting a project moves it to the Recycle Bin / Trash (dashboard.move_to_trash), never
touches imported media, and falls back to a 'Delete permanently?' prompt when the bin is unavailable.
Skipped until dashboard.py exists."""
import os
import sys

import pytest

from uihelpers import flush

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.append(REPO)


@pytest.fixture
def env(tmp_path, monkeypatch, cs):
    if not hasattr(cs, "store") or "dashboard" not in sys.modules:
        pytest.skip("no dashboard yet")
    monkeypatch.setenv("PARROT_STUDIO_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("PARROT_STUDIO_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))        # the freedesktop trash goes here
    return dict(store=cs.store, dash=sys.modules["dashboard"], trash=tmp_path / "xdg_data" / "Trash",
                tmp=tmp_path)


@pytest.fixture
def answers(cs, monkeypatch):
    """QMessageBox.exec() clicks the button whose text EXACTLY matches the next entry of answers.queue
    (records every box as (title, [buttons]))."""
    class State:
        queue = []
        seen = []

    class PickBox(cs.QMessageBox):
        def exec(self):
            State.seen.append((self.windowTitle(), [b.text() for b in self.buttons()]))
            want = State.queue.pop(0) if State.queue else None
            self._clk = next((b for b in self.buttons() if b.text().replace("&", "") == want), None)
            return 0

        def clickedButton(self):
            return self._clk
    monkeypatch.setattr(cs, "QMessageBox", PickBox)
    return State


def _project_with_media(env, name="Doomed"):
    store = env["store"]
    media = env["tmp"] / "media" / "clip.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"not really a video")
    p = store.create_project(name)
    d = store.load_project_file(p)
    d["lib"] = [{"kind": "video", "dur": 1.0, "w": 10, "h": 10, "hasaudio": False, "hdr": False, "trc": "",
                 "path": str(media), "name": "clip.mp4", "pending": False}]
    store.save_project_file(p, d)
    return p, str(media)


def _info(store, p):
    return next(i for i in store.list_projects() if os.path.abspath(i.path) == os.path.abspath(p))


def _in_trash(env, name):
    files = env["trash"] / "files"
    return files.is_dir() and any(n == name or n.startswith(name) for n in os.listdir(files))


def test_move_to_trash_function(env):
    d = env["tmp"] / "some folder"
    d.mkdir()
    (d / "a.txt").write_text("x")
    assert env["dash"].move_to_trash(str(d)) is True
    assert not d.exists() and _in_trash(env, "some folder")
    infos = os.listdir(env["trash"] / "info")
    assert any(n.endswith(".trashinfo") for n in infos)


@pytest.fixture
def dash_win(cs, qapp, modals, env, answers):
    from conftest import dispose_window
    w = cs.MainWindow()
    w.show()
    w.show_dashboard()
    flush(20)
    yield w
    dispose_window(w)


def test_dashboard_delete_moves_project_to_trash_and_keeps_media(dash_win, env, answers):
    store = env["store"]
    p, media = _project_with_media(env)
    folder = os.path.dirname(p)
    answers.queue = ["Delete"]
    assert dash_win.dashboard.delete(_info(store, p)) is True
    assert answers.seen and answers.seen[0][0].startswith("Delete project")
    assert not os.path.exists(folder), "project folder still there"
    assert _in_trash(env, os.path.basename(folder)), "project folder not in the Trash"
    assert os.path.isfile(media), "imported media must never be touched"
    assert all(os.path.abspath(i.path) != os.path.abspath(p) for i in store.list_projects())


def test_dashboard_delete_cancel_keeps_project(dash_win, env, answers):
    p, _ = _project_with_media(env, "Keep me")
    answers.queue = ["Cancel"]
    assert dash_win.dashboard.delete(_info(env["store"], p)) is False
    assert os.path.isfile(p)


def test_dashboard_delete_falls_back_to_permanent_delete(dash_win, env, answers, monkeypatch):
    store, dash = env["store"], env["dash"]
    monkeypatch.setattr(dash, "move_to_trash", lambda path: False)       # no Recycle Bin available
    p, media = _project_with_media(env)
    answers.queue = ["Delete", "Delete permanently"]
    assert dash_win.dashboard.delete(_info(store, p)) is True
    assert [t for t, _ in answers.seen] == ["Delete project?", "Delete permanently?"]
    assert not os.path.exists(p) and os.path.isfile(media)
    assert not _in_trash(env, os.path.basename(os.path.dirname(p)))


def test_dashboard_delete_fallback_can_be_cancelled(dash_win, env, answers, monkeypatch):
    store, dash = env["store"], env["dash"]
    monkeypatch.setattr(dash, "move_to_trash", lambda path: False)
    p, media = _project_with_media(env, "Survivor")
    answers.queue = ["Delete", "Cancel"]
    assert dash_win.dashboard.delete(_info(store, p)) is False
    assert len(answers.seen) == 2
    assert os.path.isfile(p) and os.path.isfile(media), "a failed trash move must delete nothing"
    assert store.load_project_file(p)["lib"][0]["path"] == media


def test_store_delete_with_trash_failure_deletes_nothing(env):
    from conftest import CS  # noqa: F401  (app module already imported)
    store = env["store"]
    p, media = _project_with_media(env, "Atomic")
    with pytest.raises(store.ProjectError):
        store.delete_project(p, trash=lambda path: False)
    assert os.path.isfile(p) and os.path.isfile(media)
    with pytest.raises(store.ProjectError):
        store.delete_project(p, trash=lambda path: (_ for _ in ()).throw(OSError("bin is full")))
    assert os.path.isfile(p)
