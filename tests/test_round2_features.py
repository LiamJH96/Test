"""Round-2 fixes from scratchpad/workflow_report.md, driven through the UI:
relink ("Locate file…"), offline library items kept and saved, Delete in the Library, Esc cancelling a library
drag, the empty inspector state, and "Load font file..." in the Subtitles section.
Each test skips on an app without the feature."""
import glob
import json
import os
import shutil

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton

from uihelpers import activate, click, clip_center, flush, goto_left_tab, left_tabs, tl_point


@pytest.fixture
def ed(win, modals):
    win.resize(1500, 920)
    activate(win)
    return win


def need(w, attr):
    if not hasattr(w, attr):
        pytest.skip(f"app has no {attr}")


def _project_with_two_files(w, modals, rich_media, tmp_path):
    """Clip A on the timeline + B only in the library, both in one folder that then moves away."""
    src = tmp_path / "shoot"
    src.mkdir()
    a, b = str(src / "a.mp4"), str(src / "b.mp4")
    shutil.copy(rich_media["horiz"], a)
    shutil.copy(rich_media["vert"], b)
    w.add_to_library(a)
    w.add_to_library(b)
    w.add_clip(w.lib[0], None, None)
    w.add_clip(w.lib[0], None, None)                 # two clips use A
    proj = str(tmp_path / "relink.clipproj")
    modals.save_path = proj
    assert w.save_project()
    moved = tmp_path / "moved shoot"
    shutil.move(str(src), str(moved))
    return proj, a, b, str(moved / "a.mp4"), str(moved / "b.mp4")


def test_offline_library_items_are_shown_and_saved(ed, modals, rich_media, tmp_path):
    w = ed
    need(w, "add_offline_lib")
    proj, a, b, a2, b2 = _project_with_two_files(w, modals, rich_media, tmp_path)
    w.open_project(proj)
    assert [l["path"] for l in w.lib] == [a, b] and all(l.get("missing") for l in w.lib)
    texts = [w.libview.item(i).text() for i in range(w.libview.count())]
    assert all("offline" in t.lower() for t in texts), texts
    assert any(c[0] in ("warning", "msgbox") and "b.mp4" in str(c[1]) for c in modals.calls)
    w.add_caption("an edit")                          # any edit + save keeps the offline entries in the file
    assert w.save_project()
    d = json.load(open(proj, encoding="utf-8"))
    assert [l["path"] for l in d["lib"]] == [a, b]
    assert all(set(l) >= {"kind", "dur", "w", "h", "path", "name"} for l in d["lib"])
    # files come back -> they are online again on the next open
    shutil.move(os.path.dirname(a2), os.path.dirname(a))
    w.open_project(proj)
    assert [l["path"] for l in w.lib] == [a, b] and not any(l.get("missing") for l in w.lib)
    assert "offline" not in w.libview.item(1).text().lower()


def test_locate_file_relinks_clips_and_library_and_offers_same_folder(ed, modals, rich_media, tmp_path):
    w = ed
    need(w, "locate_file")
    proj, a, b, a2, b2 = _project_with_two_files(w, modals, rich_media, tmp_path)
    w.open_project(proj)
    # the warning has a "Locate files…" button (round 2)
    box = [c for c in modals.calls if c[0] == "msgbox" and "missing" in c[1][0].lower()]
    assert box, modals.calls
    # right-click the offline item -> Locate file… -> pick the moved A; B in the same folder is offered too
    lv = w.libview
    pos = lv.visualItemRect(lv.item(0)).center()
    modals.menu_pick = "Locate file…"
    modals.open_path = a2
    modals.question_answer = __import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.Yes
    n_q = sum(1 for c in modals.calls if c[0] == "question")
    w.lib_menu(pos)
    flush(50)
    assert any(t.startswith("Locate file") for t in modals.menus[-1][1]), modals.menus[-1][1]
    assert sum(1 for c in modals.calls if c[0] == "question") == n_q + 1, "same-folder fix was not offered"
    assert [c.path for c in w.clips if not c.nat] == [a2, a2]
    assert [l["path"] for l in w.lib] == [a2, b2] and not any(l.get("missing") for l in w.lib)
    assert all("offline" not in lv.item(i).text().lower() for i in range(lv.count()))
    assert w.missing_paths() == []
    w.save_project()
    d = json.load(open(proj, encoding="utf-8"))
    assert {c["path"] for c in d["clips"]} == {a2} and [l["path"] for l in d["lib"]] == [a2, b2]
    # export no longer refuses
    n = len(modals.calls)
    modals.save_path = ""                             # cancel at the file dialog
    w.export()
    assert all(c[0] != "warning" for c in modals.calls[n:])


def test_locate_file_declining_same_folder_relinks_only_one(ed, modals, rich_media, tmp_path):
    w = ed
    need(w, "locate_file")
    proj, a, b, a2, b2 = _project_with_two_files(w, modals, rich_media, tmp_path)
    w.open_project(proj)
    modals.open_path = a2
    modals.question_answer = __import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.No
    assert w.locate_file(a)
    assert [l["path"] for l in w.lib] == [a2, b] and w.lib[1].get("missing")
    assert w.missing_paths() == [b]


def test_delete_key_in_library_removes_only_unused_items(ed, modals, rich_media):
    w = ed
    need(w, "delete_lib_selected")
    w.add_to_library(rich_media["vert"])
    w.add_to_library(rich_media["png"])
    w.add_clip(w.lib[0], None, None)
    lv = w.libview
    lv.setFocus()
    lv.setCurrentRow(0)                               # in use on the timeline: stays
    QTest.keyClick(lv, Qt.Key_Delete)
    flush()
    assert len(w.lib) == 2 and len(w.clips) == 1
    assert "timeline" in w.statusBar().currentMessage().lower()
    lv.setCurrentRow(1)                               # unused: removed from the library, timeline untouched
    QTest.keyClick(lv, Qt.Key_Backspace)
    flush()
    assert [l["path"] for l in w.lib] == [rich_media["vert"]] and len(w.clips) == 1 and lv.count() == 1


def test_escape_cancels_a_library_drag(ed, modals, rich_media):
    w = ed
    w.add_to_library(rich_media["vert"])
    flush()
    lv = w.libview
    p = lv.visualItemRect(lv.item(0)).center()
    QTest.mousePress(lv.viewport(), Qt.LeftButton, Qt.NoModifier, p)
    QTest.mouseMove(lv.viewport(), p + QPoint(12, 0))
    flush()
    lp = lv.viewport().mapFromGlobal(w.tl.mapToGlobal(tl_point(w, 1.0, 2)))
    QTest.mouseMove(lv.viewport(), lp)
    flush()
    assert lv._drag is not None and w.tl.drop_hint is not None
    QTest.keyClick(lv, Qt.Key_Escape)
    flush()
    assert lv._drag is None and w.tl.drop_hint is None
    QTest.mouseRelease(lv.viewport(), Qt.LeftButton, Qt.NoModifier, lp)
    flush()
    w.tl._auto.stop()
    assert w.clips == [], "the cancelled drag still added a clip"


def test_inspector_empty_state_and_tabs_follow_selection(ed, modals, rich_media):
    w = ed
    need(w, "p_empty")
    assert w.p_empty.isVisibleTo(w) and not w.ptabs.isVisibleTo(w)
    w.add_to_library(rich_media["vert"])
    w.add_to_library(rich_media["mp3"])
    w.add_clip(w.lib[0], None, None)
    w.add_clip(w.lib[1], 4, 0.0)
    v, m = w.clips
    click(w.tl, clip_center(w, v))
    assert w.sel is v and not w.p_empty.isVisibleTo(w) and w.ptabs.isVisibleTo(w)

    def tabs():
        return {w.ptabs.tabText(i) for i in range(w.ptabs.count()) if w.ptabs.isTabVisible(i)}
    assert {"Fade", "Adjust", "Audio"} <= tabs() and "Subtitle" not in tabs()
    click(w.tl, clip_center(w, m))
    assert tabs() == {"Fade", "Audio"}
    click(w.tl, QPoint(int(w.tl.tx(w.total() + 3)), w.tl.height() - 10))      # empty timeline space
    assert w.sel is None and w.p_empty.isVisibleTo(w) and not w.ptabs.isVisibleTo(w)


def test_load_font_from_subtitles_section(ed, modals):
    w = ed
    goto_left_tab(w, "Subtitles")
    page = left_tabs(w).currentWidget()
    btns = [b for b in page.findChildren(QAbstractButton) if b.text().startswith("Load font file")]
    assert btns, "no 'Load font file...' in the Subtitles section"
    fonts = sorted(glob.glob("/usr/share/fonts/**/*.ttf", recursive=True))
    modals.open_paths = fonts[:1]
    click(btns[0])
    assert ("opens" in modals.kinds())
    if fonts:                                         # no caption selected: the status bar says what happened
        assert "Loaded font" in w.statusBar().currentMessage()
