"""project_store.py against the round-2 contract (scratchpad/round2_plan.md, "Project system contract").

Pure-Python tests: no QApplication needed. Every test gets its own config / projects / HOME folders.
Skipped as a whole until project_store.py exists.
"""
import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.append(REPO)
ps = pytest.importorskip("project_store")


# ------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    cfg, proj = tmp_path / "cfg", tmp_path / "projects"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("PARROT_STUDIO_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("PARROT_STUDIO_PROJECTS_DIR", str(proj))
    if hasattr(ps, "take_problems"):
        ps.take_problems()
    return dict(home=str(home), cfg=str(cfg), projects=str(proj), tmp=str(tmp_path))


def v1_doc(n_clips=2, media="/media/clip.mp4"):
    """A Clip Studio (schema-less, v1) project exactly as the round-1 app's project_dict() wrote it."""
    clip = {"id": "a1b2c3d4", "name": "clip.mp4", "path": media, "kind": "video", "track": 2, "start": 0.0,
            "inp": 0.0, "dur": 3.0, "srcdur": 3.0, "w": 1080, "h": 1920, "hasaudio": True, "fin": 0.0, "fout": 0.0,
            "vol": 100, "scale": 100, "x": 0.0, "y": 0.0, "ttype": "None", "tdur": 1.0, "nat": 0, "text": "",
            "csize": 64, "cfont": "Segoe UI", "ccol": "#000000", "cbox": "#ffffff", "cuse": True, "words": [],
            "wdur": 1.0, "cbold": -1}
    clips = []
    for i in range(n_clips):
        c = dict(clip, id=f"id{i:06d}", start=3.0 * i)
        clips.append(c)
    return {"substyle": {"font": "Arial", "bold": True, "size": 78, "color": "#6CB3FF", "color2": "#FFFFFF",
                         "outline_color": "#000000", "outline": 8, "tag_color": "#00FF00", "tag_color2": "#FF3B3B",
                         "ypct": 55.0, "xpct": 50.0, "anim": "Zoom In", "tag_anim": "Same as subtitle", "anim_ms": 200,
                         "upper": False},
            "W": 1080, "H": 1920,
            "lib": [{"kind": "video", "dur": 3.0, "w": 1080, "h": 1920, "hasaudio": True, "hdr": False, "trc": "",
                     "path": media, "name": "clip.mp4", "pending": False}],
            "clips": clips}


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    return path


def touch_later(path, secs=2.0):
    t = time.time() + secs
    os.utime(path, (t, t))


# ------------------------------------------------------------------------------------------- basics
def test_importable_without_qt():
    code = ("import sys; sys.path.insert(0, %r); import project_store; "
            "bad = [m for m in sys.modules if m.startswith('PySide6.QtWidgets') or m.startswith('PySide6.QtGui')]; "
            "print(bad); sys.exit(1 if bad else 0)" % REPO)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_dirs_honour_env_overrides(iso):
    assert os.path.abspath(ps.config_dir()) == iso["cfg"]
    assert os.path.abspath(ps.default_projects_dir()) == iso["projects"]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX defaults")
def test_dirs_defaults_without_overrides(iso, monkeypatch):
    monkeypatch.delenv("PARROT_STUDIO_CONFIG_DIR")
    monkeypatch.delenv("PARROT_STUDIO_PROJECTS_DIR")
    assert ps.config_dir() == os.path.join(iso["home"], ".config", "parrot_studio")
    assert ps.default_projects_dir() == os.path.join(iso["home"], "Videos", "Parrot Studio", "Projects")


def test_projects_dir_is_persisted(iso, tmp_path):
    assert os.path.abspath(ps.get_projects_dir()) == iso["projects"]
    new = str(tmp_path / "elsewhere" / "My Projects")
    ps.set_projects_dir(new)
    assert os.path.abspath(ps.get_projects_dir()) == os.path.abspath(new)
    with open(os.path.join(iso["cfg"], "settings.json"), encoding="utf-8") as f:
        assert os.path.abspath(json.load(f)["projects_dir"]) == os.path.abspath(new)


def test_corrupt_settings_file_falls_back_to_defaults(iso):
    write(os.path.join(iso["cfg"], "settings.json"), "{ not json")
    assert os.path.abspath(ps.get_projects_dir()) == iso["projects"]
    ps.add_recent(os.path.join(iso["tmp"], "x.clipproj"))     # and it can be written again afterwards
    with open(os.path.join(iso["cfg"], "settings.json"), encoding="utf-8") as f:
        json.load(f)


# ------------------------------------------------------------------------------------------- atomic writes
def test_atomic_write_json_roundtrip(tmp_path):
    p = str(tmp_path / "sub" / "a.json")
    ps.atomic_write_json(p, {"x": [1, 2, "é"]})
    with open(p, encoding="utf-8") as f:
        assert json.load(f) == {"x": [1, 2, "é"]}
    assert os.listdir(tmp_path / "sub") == ["a.json"]


def _assert_untouched(p, tmp_path, original):
    with open(p, encoding="utf-8") as f:
        assert json.load(f) == original
    left = [n for n in os.listdir(tmp_path) if n != os.path.basename(p)]
    assert left == [], f"temporary files left behind: {left}"


def test_atomic_write_failure_at_replace_keeps_old_file(tmp_path, monkeypatch):
    tmp_path = tmp_path / "w"
    tmp_path.mkdir()
    p = str(tmp_path / "proj.clipproj")
    ps.atomic_write_json(p, {"v": 1})

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ps.atomic_write_json(p, {"v": 2})
    monkeypatch.undo()
    _assert_untouched(p, tmp_path, {"v": 1})


def test_atomic_write_failure_mid_write_keeps_old_file(tmp_path, monkeypatch):
    tmp_path = tmp_path / "w"
    tmp_path.mkdir()
    p = str(tmp_path / "proj.clipproj")
    ps.atomic_write_json(p, {"v": 1})

    def boom(fd):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        ps.atomic_write_json(p, {"v": 2, "big": "x" * 100000})
    monkeypatch.undo()
    _assert_untouched(p, tmp_path, {"v": 1})


def test_atomic_write_unserialisable_data_keeps_old_file(tmp_path):
    tmp_path = tmp_path / "w"
    tmp_path.mkdir()
    p = str(tmp_path / "proj.clipproj")
    ps.atomic_write_json(p, {"v": 1})
    with pytest.raises((TypeError, ValueError)):
        ps.atomic_write_json(p, {"v": object()})
    _assert_untouched(p, tmp_path, {"v": 1})


# ------------------------------------------------------------------------------------------- schema / load
def test_schema_version_and_migrate_v1_keeps_every_key():
    assert ps.SCHEMA_VERSION == 2
    d1 = v1_doc()
    d1["some_future_key"] = {"keep": True}
    snap = json.dumps(d1, sort_keys=True)
    d2 = ps.migrate(d1)
    assert json.dumps(d1, sort_keys=True) == snap, "migrate must not modify its argument"
    assert d2["schema"] == 2 and isinstance(d2.get("meta"), dict)
    for k, v in d1.items():
        assert d2[k] == v, f"migrate dropped or changed {k!r}"
    assert ps.migrate(d2) == d2                                 # idempotent on current files


def test_migrate_refuses_newer_schema():
    with pytest.raises(ps.ProjectError):
        ps.migrate(dict(v1_doc(), schema=ps.SCHEMA_VERSION + 1))


def test_load_legacy_v1_files(tmp_path):
    # round-0 / round-1 format (identical keys)
    p = write(str(tmp_path / "old.clipproj"), v1_doc(3))
    d = ps.load_project_file(p)
    assert d["schema"] == 2 and len(d["clips"]) == 3 and d["W"] == 1080 and d["lib"][0]["path"] == "/media/clip.mp4"
    assert d["clips"] == v1_doc(3)["clips"]
    # an older, minimal Clip Studio file: no substyle, no lib, clips without the newer fields
    old = {"W": 1920, "H": 1080, "clips": [{"id": "x", "name": "a.mp4", "path": "/a.mp4", "kind": "video",
                                               "track": 2, "start": 0.0, "inp": 0.0, "dur": 2.0}]}
    d = ps.load_project_file(write(str(tmp_path / "older.clipproj"), old))
    assert d["W"] == 1920 and d["clips"][0]["dur"] == 2.0 and isinstance(d.get("lib", []), list)
    # UTF-8 with BOM (some editors write it)
    p = str(tmp_path / "bom.clipproj")
    with open(p, "w", encoding="utf-8-sig") as f:
        json.dump(v1_doc(1), f)
    assert len(ps.load_project_file(p)["clips"]) == 1


@pytest.mark.parametrize("text", [
    "", "   \n", '{"W": 1080, "H": 19', "\x00\x01garbage", "[1, 2]", "null",
    json.dumps({"clips": 5, "W": 1080, "H": 1920}),
    json.dumps({"clips": [], "lib": []}),
    json.dumps({"clips": [], "W": "wide", "H": 1920}),
    json.dumps({"clips": [1, 2], "W": 1080, "H": 1920}),
])
def test_load_corrupt_raises_project_error(tmp_path, text):
    p = write(str(tmp_path / "bad.clipproj"), text)
    with pytest.raises(ps.ProjectError) as ei:
        ps.load_project_file(p)
    assert str(ei.value).strip(), "the error needs a readable message"


def test_load_missing_or_unreadable_raises_project_error(tmp_path):
    with pytest.raises(ps.ProjectError):
        ps.load_project_file(str(tmp_path / "nope.clipproj"))
    with pytest.raises(ps.ProjectError):
        ps.load_project_file(str(tmp_path))                  # a folder, not a file


def test_load_corrupt_points_at_good_backup(tmp_path):
    if not hasattr(ps, "save_project_file"):
        pytest.skip("no save_project_file")
    p = str(tmp_path / "p.clipproj")
    ps.save_project_file(p, v1_doc(1))
    ps.save_project_file(p, v1_doc(2))                       # the first version becomes the .bak
    write(p, '{"clips": [')
    with pytest.raises(ps.ProjectError) as ei:
        ps.load_project_file(p)
    assert ei.value.backup and len(ps.load_project_file(ei.value.backup)["clips"]) == 1


# ------------------------------------------------------------------------------------------- projects
def test_create_project_layout_and_unique_names(iso):
    p1 = ps.create_project("My Video")
    folder = os.path.dirname(p1)
    assert os.path.dirname(folder) == iso["projects"]
    assert os.path.basename(folder) == "My Video" and os.path.basename(p1) == "My Video.clipproj"
    d = ps.load_project_file(p1)
    assert d["schema"] == 2 and d["clips"] == [] and (d["W"], d["H"]) == (1080, 1920)
    assert os.path.isdir(os.path.join(folder, "recovery"))
    p2 = ps.create_project("My Video")
    p3 = ps.create_project("my video")                     # case-insensitive clash (Windows / macOS)
    assert os.path.basename(os.path.dirname(p2)) == "My Video (2)"
    assert os.path.basename(os.path.dirname(p3)).endswith("(3)")
    assert len({p1, p2, p3}) == 3 and all(os.path.isfile(p) for p in (p1, p2, p3))


@pytest.mark.parametrize("name", ['a/b\\c:d*e?"f<g>h|i', "  ..  ", "CON", "x" * 300, "Ünïcødé 🦜"])
def test_create_project_with_awkward_names(name, iso):
    p = ps.create_project(name)
    assert os.path.isfile(p)
    base = os.path.basename(os.path.dirname(p))
    assert base and not any(ch in base for ch in '/\\:*?"<>|') and len(base) <= 120
    assert base.upper() != "CON"


def test_create_project_in_explicit_parent(tmp_path):
    p = ps.create_project("Elsewhere", parent=str(tmp_path / "custom"))
    assert os.path.dirname(os.path.dirname(p)) == str(tmp_path / "custom")


def test_list_projects_newest_first_with_recents_and_missing(iso, tmp_path):
    a = ps.create_project("Alpha")
    b = ps.create_project("Beta")
    outside = write(str(tmp_path / "outside" / "Loose.clipproj"), v1_doc(1))
    gone = write(str(tmp_path / "outside" / "Gone.clipproj"), v1_doc(1))
    ps.add_recent(outside)
    ps.add_recent(gone)
    os.remove(gone)
    now = time.time()
    os.utime(a, (now + 30, now + 30))
    os.utime(b, (now + 10, now + 10))
    os.utime(outside, (now + 20, now + 20))
    infos = ps.list_projects()
    paths = [os.path.abspath(i.path) for i in infos]
    live = [p for p, i in zip(paths, infos) if not getattr(i, "missing", False)]
    assert live[:3] == [os.path.abspath(a), os.path.abspath(outside), os.path.abspath(b)]
    gi = [i for i in infos if os.path.abspath(i.path) == os.path.abspath(gone)]
    assert not gi or gi[0].missing, "a missing recent project must be skipped or flagged"
    i0 = infos[0]
    assert i0.name == "Alpha" and os.path.abspath(i0.folder) == os.path.dirname(os.path.abspath(a))
    assert i0.modified == pytest.approx(now + 30, abs=1)
    assert i0.thumbnail is None or os.path.isfile(i0.thumbnail)
    assert len(paths) == len(set(paths)), "a project must not be listed twice"


def test_recent_add_remove(iso, tmp_path):
    p = write(str(tmp_path / "r" / "Recent.clipproj"), v1_doc(1))
    ps.add_recent(p)
    ps.add_recent(p)
    assert sum(os.path.abspath(i.path) == os.path.abspath(p) for i in ps.list_projects()) == 1
    ps.remove_recent(p)
    assert all(os.path.abspath(i.path) != os.path.abspath(p) for i in ps.list_projects())
    assert os.path.isfile(p), "remove_recent must not delete the file"


def test_rename_project_keeps_content_and_recents(iso):
    p = ps.create_project("Old Name")
    if hasattr(ps, "save_project_file"):
        ps.save_project_file(p, dict(v1_doc(2), schema=2))
    else:
        ps.atomic_write_json(p, v1_doc(2))
    n = ps.rename_project(p, "New Name")
    assert os.path.isfile(n) and not os.path.exists(p)
    assert os.path.basename(n) == "New Name.clipproj"
    assert len(ps.load_project_file(n)["clips"]) == 2
    listed = [os.path.abspath(i.path) for i in ps.list_projects()]
    assert os.path.abspath(n) in listed and os.path.abspath(p) not in listed
    other = ps.create_project("Taken")
    n2 = ps.rename_project(n, "Taken")                      # never clobbers another project
    assert os.path.isfile(other) and os.path.isfile(n2) and n2 != other


def test_rename_unmanaged_project_stays_in_its_folder(tmp_path):
    p = write(str(tmp_path / "mine" / "Loose.clipproj"), v1_doc(1))
    sibling = write(str(tmp_path / "mine" / "notes.txt"), "keep me")
    n = ps.rename_project(p, "Tidy")
    assert os.path.dirname(n) == str(tmp_path / "mine") and os.path.isfile(sibling)


def test_delete_project_never_touches_media(iso, tmp_path):
    media = write(str(tmp_path / "media" / "clip.mp4"), "not really a video")
    p = ps.create_project("Doomed")
    ps.atomic_write_json(p, dict(v1_doc(1, media=media), schema=2))
    ps.add_recent(p)
    ps.delete_project(p)
    assert not os.path.exists(p)
    assert os.path.isfile(media)
    assert all(os.path.abspath(i.path) != os.path.abspath(p) for i in ps.list_projects())


def test_delete_unmanaged_project_removes_only_the_project_file(tmp_path):
    folder = tmp_path / "my stuff"
    p = write(str(folder / "Loose.clipproj"), v1_doc(1))
    media = write(str(folder / "clip.mp4"), "x")
    other = write(str(folder / "other.clipproj"), v1_doc(1))
    ps.delete_project(p)
    assert not os.path.exists(p) and os.path.isfile(media) and os.path.isfile(other) and folder.is_dir()


# ------------------------------------------------------------------------------------------- recovery
def test_recovery_newer_than_project_is_found_and_discarded(iso):
    p = ps.create_project("Rec")
    assert ps.find_recovery(p) is None
    data = dict(v1_doc(4), schema=2)
    ps.write_recovery(p, data)
    os.utime(p, (time.time() - 60, time.time() - 60))
    info = ps.find_recovery(p)
    assert info is not None
    rp = getattr(info, "path", info)
    d = ps.load_project_file(rp) if not hasattr(ps, "load_recovery") else ps.load_recovery(info)
    assert len(d["clips"]) == 4
    ps.discard_recovery(p)
    assert ps.find_recovery(p) is None


def test_recovery_older_than_project_is_ignored(iso):
    p = ps.create_project("Saved after")
    ps.write_recovery(p, dict(v1_doc(4), schema=2))
    touch_later(p, 5)                                      # the project was saved after the snapshot
    assert ps.find_recovery(p) is None


def test_damaged_recovery_is_ignored_and_reported(iso):
    p = ps.create_project("Damaged")
    rp = ps.write_recovery(p, dict(v1_doc(1), schema=2))
    rp = rp if isinstance(rp, str) and os.path.isfile(rp) else None
    if rp is None:
        rec = os.path.join(os.path.dirname(p), "recovery")
        rp = next(os.path.join(rec, n) for n in os.listdir(rec))
    with open(rp, "w", encoding="utf-8") as f:
        f.write('{"clips": [ {"id": ')                    # cut off mid-write
    os.utime(p, (time.time() - 60, time.time() - 60))
    assert ps.find_recovery(p) is None                     # never loaded blindly, never raises
    if hasattr(ps, "take_problems"):
        assert ps.take_problems(), "a skipped damaged recovery file should be reported"


def test_recovery_for_untitled_project(iso):
    ps.write_recovery(None, dict(v1_doc(2), schema=2))
    info = ps.find_recovery(None)
    assert info is not None
    ps.discard_recovery(None)
    assert ps.find_recovery(None) is None


def test_write_recovery_failure_keeps_previous_snapshot(iso, monkeypatch):
    p = ps.create_project("Keep")
    ps.write_recovery(p, dict(v1_doc(1), schema=2))

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ps.write_recovery(p, dict(v1_doc(5), schema=2))
    monkeypatch.undo()
    os.utime(p, (time.time() - 60, time.time() - 60))
    info = ps.find_recovery(p)
    assert info is not None and len(ps.load_project_file(info.path)["clips"]) == 1


def test_session_lock_detects_unclean_shutdown(iso):
    if not hasattr(ps, "acquire_session_lock"):
        pytest.skip("no session lock helpers")
    assert ps.acquire_session_lock() == []
    ps.update_session_lock("/some/project.clipproj")
    ps.release_session_lock()
    assert ps.acquire_session_lock() == []                 # clean exit: nothing to report
    # a session that was killed: its lock file stays behind with a pid that is gone
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    sess = os.path.join(iso["cfg"], "sessions")
    write(os.path.join(sess, f"{dead.pid}.lock"),
          {"pid": dead.pid, "started": time.time() - 100, "project": "/x/Crashed.clipproj"})
    ps.release_session_lock()
    unclean = ps.acquire_session_lock()
    assert [u.get("project") for u in unclean] == ["/x/Crashed.clipproj"]
    assert ps.acquire_session_lock() == []                 # reported once
    ps.release_session_lock()


def test_legacy_autosave_location(iso):
    if not hasattr(ps, "legacy_autosave_path"):
        pytest.skip("no legacy_autosave_path helper")
    p = ps.legacy_autosave_path()
    assert p == os.path.join(iso["home"], ".clip_studio", "autosave.clipproj")
    write(p, v1_doc(2))
    assert len(ps.load_project_file(p)["clips"]) == 2


def test_save_project_file_conflict_detection(tmp_path):
    if not (hasattr(ps, "save_project_file") and hasattr(ps, "file_signature")):
        pytest.skip("no conflict-aware save")
    p = str(tmp_path / "c.clipproj")
    sig = ps.save_project_file(p, v1_doc(1))
    write(p, dict(v1_doc(3), schema=2))                   # someone else changed it
    t = time.time() + 3
    os.utime(p, (t, t))
    with pytest.raises(ps.ProjectError):
        ps.save_project_file(p, v1_doc(2), expect=sig)
    assert len(ps.load_project_file(p)["clips"]) == 3     # their version was not overwritten
