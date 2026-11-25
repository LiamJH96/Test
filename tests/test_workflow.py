"""End-to-end workflow tests driven like a first-time user would: real mouse/keyboard events (QTest) on real
widgets, realistic media, a real export checked with ffprobe and pixel samples.

Tests marked xfail document CONFIRMED bugs from scratchpad/workflow_report.md (strict=False, so a fix shows up as
XPASS instead of breaking the run). Remove the marker when the fix lands.

Run:  QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_workflow.py
"""
import json
import os
import shutil
import time

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QSlider

from uihelpers import (activate, button, click, clip_center, clips_state, count_pixels, ffprobe, flush, frame_at,
                       goto_left_tab, key, lib_double_click, lib_drag_to, tl_drag, tl_point, trans_drag_to,
                       type_into_spin, wait_until)


# =============================================================================== fixtures
@pytest.fixture
def ed(win, modals):
    """The editor window, active, so application shortcuts and focus behave as on a desktop."""
    win.resize(1500, 920)
    activate(win)
    flush(20)
    return win


def import_all(w, modals, paths):
    modals.open_paths = list(paths)
    click(button(w, "Import media"))
    return [l["path"] for l in w.lib]


def fit(w):
    key(w, Qt.Key_0, Qt.ControlModifier)


# =============================================================================== import + add
def test_import_button_and_add_by_double_click_and_drag(ed, modals, rich_media):
    w = ed
    paths = [rich_media[k] for k in ("vert", "horiz", "mp3", "png")]
    assert import_all(w, modals, paths) == paths
    assert ("opens" in modals.kinds())
    kinds = [l["kind"] for l in w.lib]
    assert kinds == ["video", "video", "audio", "image"]
    v = w.lib[0]
    assert (v["w"], v["h"]) == (1080, 1920) and v["hasaudio"] and abs(v["dur"] - 4.0) < 0.1
    # double-click adds the vertical clip to the main Video track at 0
    lib_double_click(w, 0)
    assert len(w.clips) == 1 and w.clips[0].track == 2 and w.clips[0].start == 0.0
    flush(50)
    fit(w)
    # drag the horizontal clip onto the Video row right after it, the mp3 onto Music, the png onto Overlay 1
    hint = lib_drag_to(w, 1, 4.0, 2)
    assert hint is not None and hint[0] == 2
    hint = lib_drag_to(w, 2, 0.0, 4)
    assert hint[0] == 4
    lib_drag_to(w, 3, 1.0, 1)
    by = {c.name: c for c in w.clips}
    assert by["horizontal.mp4"].track == 2 and abs(by["horizontal.mp4"].start - 4.0) < 0.1
    assert by["music.mp3"].track == 4 and by["music.mp3"].start < 0.1
    assert by["overlay.png"].track == 1 and abs(by["overlay.png"].start - 1.0) < 0.1
    # importing the same file again does not duplicate the library entry
    import_all(w, modals, [rich_media["vert"]])
    assert len(w.lib) == 4


def test_move_trim_split_with_real_mouse_and_keys(ed, modals, rich_media):
    w = ed
    import_all(w, modals, [rich_media["vert"], rich_media["png"]])
    lib_double_click(w, 0)
    flush(50)
    w.add_clip(w.lib[1], 1, 0.5)
    img = w.sel
    fit(w)
    tl = w.tl
    w.ck_snap.setChecked(False)
    # move the overlay 1 s to the right
    p0 = clip_center(w, img)
    tl_drag(w, p0, p0 + QPoint(int(round(tl.pps * 1.0)), 0))
    assert abs(img.start - 1.5) < 2 / tl.pps
    fit(w)                                       # bring the new right edge back into view
    # trim its right edge 1 s shorter (grab near the top: the middle of a join is the "+" button)
    r = tl.clip_rect(img)
    p = QPoint(int(r.right()) - 2, int(r.top()) + 5)
    tl_drag(w, p, p - QPoint(int(round(tl.pps)), 0))
    assert abs(img.dur - 4.0) < 2 / tl.pps
    # click the ruler at 2 s, click the video clip, press S
    v = next(c for c in w.clips if c.track == 2)
    click(tl, QPoint(int(tl.tx(2.0)), 5))
    assert abs(w.t - 2.0) < 2 / tl.pps
    click(tl, clip_center(w, v))
    assert w.sel is v
    n_undo = len(w.undo)
    key(w, Qt.Key_S)
    parts = sorted((c for c in w.clips if c.track == 2), key=lambda c: c.start)
    assert len(parts) == 2 and len(w.undo) == n_undo + 1
    assert abs(parts[0].end - parts[1].start) < 1e-6 and abs(parts[1].inp - parts[0].dur) < 1e-6
    # Esc during a trim puts it back
    r = tl.clip_rect(parts[1])
    p = QPoint(int(r.right()) - 2, int(r.top()) + 5)
    d0 = parts[1].dur
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, p)
    QTest.mouseMove(tl, p - QPoint(40, 0))
    flush()
    assert tl.mode == "trimR"
    QTest.keyClick(tl, Qt.Key_Escape)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, p - QPoint(40, 0))
    flush()
    tl._auto.stop()
    assert abs(next(c for c in w.clips if c.id == parts[1].id).dur - d0) < 1e-9


def test_snapping_accuracy(ed, modals, rich_media):
    w = ed
    import_all(w, modals, [rich_media["vert"], rich_media["png"]])
    w.add_clip(w.lib[0], None, None)
    flush(50)
    fit(w)
    w.set_time(3.0)
    w.add_clip(w.lib[1], 1, 6.0)
    img = w.sel
    img.dur = 2.0
    w.after_edit()
    fit(w)
    tl = w.tl
    assert w.ck_snap.isChecked() and tl.clip_rect(img).right() < tl.width()

    def drag_start_to(x_target):
        p0 = clip_center(w, img)
        dx = int(round(x_target - tl.clip_rect(img).left()))
        tl_drag(w, p0, p0 + QPoint(dx, 0))

    drag_start_to(tl.tx(3.0) + 4)                  # 4 px off the playhead: snaps exactly
    assert img.start == pytest.approx(3.0, abs=1e-9)
    drag_start_to(tl.tx(4.0) + 3)                  # 3 px off the video clip's end (4.0 s): snaps
    assert img.start == pytest.approx(4.0, abs=1e-9)
    drag_start_to(tl.tx(5.0) + 12)                 # 12 px from anything: no snap
    assert abs(img.start - (5.0 + 12 / tl.pps)) < 1.5 / tl.pps
    w.ck_snap.setChecked(False)                    # Snap off: free placement
    drag_start_to(tl.tx(3.0) + 4)
    assert abs(img.start - (3.0 + 4 / tl.pps)) < 1.5 / tl.pps


def test_playback_and_seek_keep_preview_and_timeline_in_sync(ed, modals, rich_media):
    w = ed
    import_all(w, modals, [rich_media["vert"]])
    w.add_clip(w.lib[0], None, None)
    flush(50)
    w.set_time(0.0)
    key(w, Qt.Key_Space)
    assert w.playing
    t0 = time.perf_counter()
    flush(600)
    el = time.perf_counter() - t0
    assert abs(w.t - el) < 0.15                    # wall-clock based playhead
    assert abs(w.tl._phx - w.tl.tx(w.t)) < 2 * w.tl.pps * 0.05 + 1
    assert w.lbl_time.text() == w.time_html()
    # seek by clicking the ruler while playing: playback continues from there
    click(w.tl, QPoint(int(w.tl.tx(3.0)), 5))
    flush(200)
    assert 3.1 < w.t < 3.6
    key(w, Qt.Key_Space)
    assert not w.playing
    # playing to the end stops there
    key(w, Qt.Key_End)
    assert w.t == pytest.approx(w.total())
    key(w, Qt.Key_Space)                           # at the end: restarts from 0
    assert w.playing and w.t < 0.2
    key(w, Qt.Key_Space)


# =============================================================================== undo / redo
def _build_chain(w, modals, rich_media):
    """15 different edits through the UI. Returns [(label, snapshot)] including the starting state.
    w.last_push is reset before property edits so time-based coalescing does not merge them (see the xfail test
    below for what happens when it does)."""
    states = [("empty", w.snapshot())]

    def rec(label):
        states.append((label, w.snapshot()))

    import_all(w, modals, [rich_media[k] for k in ("vert", "horiz", "mp3", "png")])
    lib_double_click(w, 0)
    rec("add vertical")
    flush(50)
    lib_double_click(w, 1)
    rec("add horizontal")
    lib_double_click(w, 2)
    rec("add music")
    lib_double_click(w, 3)
    rec("add png")
    fit(w)
    tl = w.tl
    img = next(c for c in w.clips if c.kind == "image")
    v = next(c for c in w.clips if c.name == "gameplay_vertical.mp4")
    h = next(c for c in w.clips if c.name == "horizontal.mp4")
    p0 = clip_center(w, img)
    tl_drag(w, p0, p0 + QPoint(int(tl.pps * 2), 0))
    rec("move png")
    r = tl.clip_rect(h)
    p = QPoint(int(r.right()) - 2, int(r.top()) + 5)
    tl_drag(w, p, p - QPoint(int(tl.pps * 0.5), 0))
    rec("trim horizontal")
    click(tl, QPoint(int(tl.tx(1.5)), 5))
    click(tl, clip_center(w, v))
    key(w, Qt.Key_S)
    rec("split vertical")
    goto_left_tab(w, "Captions")
    w.new_cap.setFocus()
    QTest.keyClicks(w.new_cap, "HELLO PARROT")
    click(button(w, "Add caption to timeline"))
    rec("add caption")
    goto_left_tab(w, "Transitions")
    trans_drag_to(w, "Crossfade", h)
    rec("crossfade")
    click(tl, clip_center(w, h))
    w.last_push = 0.0
    w.goto_tab("Fade")
    type_into_spin(w.p_fin, "0.5")
    rec("fade in")
    w.last_push = 0.0
    w.goto_tab("Audio")
    type_into_spin(w.p_vol, "40")
    rec("volume")
    w.last_push = 0.0
    w.goto_tab("Adjust")
    for val in (110, 120, 130):                   # one quick slider-like gesture = one undo step
        w.p_scale.setValue(val)
        flush()
    rec("scale")
    click(button(w, "Fill"))
    rec("fill")
    click(tl, clip_center(w, img))
    key(w, Qt.Key_Delete)
    rec("delete png")
    cap = next(c for c in w.clips if c.nat)
    click(tl, clip_center(w, cap))
    key(w, Qt.Key_D, Qt.ControlModifier)
    rec("duplicate caption")
    return states


def test_undo_redo_chain_restores_every_state_exactly(ed, modals, rich_media):
    w = ed
    states = _build_chain(w, modals, rich_media)
    assert len(states) == 16
    assert len({s for _, s in states}) == 16, "every step must change the project"
    for i in range(len(states) - 1, 0, -1):
        key(w, Qt.Key_Z, Qt.ControlModifier)
        assert json.loads(w.snapshot()) == json.loads(states[i - 1][1]), f"undo of '{states[i][0]}'"
    key(w, Qt.Key_Z, Qt.ControlModifier)          # nothing left: harmless
    assert json.loads(w.snapshot()) == []
    for i in range(1, len(states)):
        key(w, Qt.Key_Y, Qt.ControlModifier)
        assert json.loads(w.snapshot()) == json.loads(states[i][1]), f"redo of '{states[i][0]}'"
    # a new edit after undo clears redo
    key(w, Qt.Key_Z, Qt.ControlModifier)
    key(w, Qt.Key_Z, Qt.ControlModifier)
    w.add_caption("new branch")
    n = len(w.snapshot())
    key(w, Qt.Key_Y, Qt.ControlModifier)
    assert len(w.snapshot()) == n


@pytest.mark.xfail(strict=False, reason="P1-1 workflow_report: a property edit within 0.8 s of ANY other undo step "
                                        "is merged into that step (apply_props time-based coalescing)")
def test_property_edit_right_after_another_action_is_its_own_undo_step(ed, modals, rich_media):
    w = ed
    import_all(w, modals, [rich_media["vert"], rich_media["horiz"]])
    w.add_clip(w.lib[0], None, None)
    w.add_clip(w.lib[1], None, None)
    h = w.clips[1]
    w.sel = h
    w.load_props()
    w.set_trans("Crossfade")                     # one action...
    after_trans = w.snapshot()
    w.goto_tab("Fade")
    type_into_spin(w.p_fin, "0.75")             # ...and, straight after, a property edit
    assert h.fin == 0.75
    w.do_undo()
    assert json.loads(w.snapshot()) == json.loads(after_trans), "undo must only take back the fade"


@pytest.mark.xfail(strict=False, reason="P2 workflow_report: undo/redo always clears the selection")
def test_undo_keeps_the_selection(ed, modals, rich_media):
    w = ed
    import_all(w, modals, [rich_media["vert"]])
    w.add_clip(w.lib[0], None, None)
    c = w.clips[0]
    click(w.tl, clip_center(w, c))
    w.quick_fade(1.0, None)
    w.do_undo()
    assert w.sel is not None and w.sel.id == c.id


@pytest.mark.xfail(strict=False, reason="P2 workflow_report: canvas size is not part of undo / dirty state")
def test_canvas_change_is_undoable_and_marks_project_dirty(ed, modals, rich_media):
    w = ed
    import_all(w, modals, [rich_media["vert"]])
    w.add_clip(w.lib[0], None, None)
    w._saved = w.snapshot()
    w.update_title()
    n = len(w.clips)
    w.cb_canvas.setCurrentIndex(1)
    assert (w.W, w.H) != (1080, 1920)
    assert w.is_dirty()
    key(w, Qt.Key_Z, Qt.ControlModifier)
    assert len(w.clips) == n, "Ctrl+Z after a canvas change must not remove a clip"
    assert (w.W, w.H) == (1080, 1920)


# =============================================================================== keyboard focus
@pytest.mark.xfail(strict=False, reason="P2 workflow_report: Delete pressed in the Library deletes the selected "
                                        "TIMELINE clip (ApplicationShortcut, typing() only guards text inputs)")
def test_delete_in_library_does_not_delete_timeline_clip(ed, modals, rich_media):
    w = ed
    import_all(w, modals, [rich_media["vert"]])
    w.add_clip(w.lib[0], None, None)
    w.libview.setFocus()
    w.libview.setCurrentRow(0)
    QTest.keyClick(w.libview, Qt.Key_Delete)
    flush()
    assert len(w.clips) == 1


@pytest.mark.xfail(strict=False, reason="P2 workflow_report: arrow keys on a focused slider move the playhead, "
                                        "not the slider")
def test_arrow_keys_on_a_slider_move_the_slider(ed):
    w = ed
    sl = next(s for s in w.findChildren(QSlider) if s.isVisibleTo(w) and s.isEnabled())
    sl.setFocus()
    v0, t0 = sl.value(), w.t
    QTest.keyClick(sl, Qt.Key_Right)
    flush()
    assert sl.value() != v0 and w.t == t0


@pytest.mark.xfail(strict=False, reason="P3 workflow_report: Space on a focused combo box toggles playback")
def test_space_on_combo_box_does_not_play(ed, modals, rich_media):
    w = ed
    import_all(w, modals, [rich_media["vert"]])
    w.add_clip(w.lib[0], None, None)
    w.cb_fps.setFocus()
    QTest.keyClick(w.cb_fps, Qt.Key_Space)
    flush()
    try:
        assert not w.playing
    finally:
        w.playing = False
        pop = QApplication.activePopupWidget()
        if pop:
            pop.hide()


def test_text_box_keeps_its_own_undo_and_typing_keys(ed, modals, rich_media):
    w = ed
    import_all(w, modals, [rich_media["vert"]])
    w.add_clip(w.lib[0], None, None)
    goto_left_tab(w, "Captions")
    w.new_cap.setFocus()
    n_undo, n_clips = len(w.undo), len(w.clips)
    QTest.keyClicks(w.new_cap, "s s")             # 's' and space are typed, not split / play
    QTest.keyClick(w.new_cap, Qt.Key_Z, Qt.ControlModifier)
    flush()
    assert len(w.undo) == n_undo and len(w.clips) == n_clips and not w.playing


def test_editor_shortcuts_do_not_fire_on_dashboard(win, modals, rich_media):
    if not hasattr(win, "show_dashboard"):
        pytest.skip("no dashboard yet")
    w = win
    w.add_to_library(rich_media["vert"])
    w.add_clip(w.lib[0], None, None)
    w.sel = w.clips[0]
    w.set_time(1.0)
    w.show_dashboard()
    activate(w)
    before = w.snapshot()
    focus = QApplication.focusWidget() or w
    for k, m in ((Qt.Key_S, Qt.NoModifier), (Qt.Key_Delete, Qt.NoModifier), (Qt.Key_Space, Qt.NoModifier),
                 (Qt.Key_D, Qt.ControlModifier), (Qt.Key_Z, Qt.ControlModifier), (Qt.Key_Right, Qt.NoModifier)):
        QTest.keyClick(focus, k, m)
        flush()
    assert w.snapshot() == before and not w.playing and w.t == 1.0
    w.show_editor()


# =============================================================================== save / reopen / export
def _project_with_text(w, rich_media, cs):
    w.add_to_library(rich_media["blue"])
    w.add_to_library(rich_media["mp3"])
    w.add_clip(w.lib[0], None, None)                # 0-4 s solid blue, with audio
    w.add_clip(w.lib[1], 4, 0.0)                    # music bed
    w.clips[-1].dur = 4.0
    w.set_time(0.5)
    w.add_caption("BIG WHITE BOX")
    cap = w.sel
    cap.y, cap.dur = 0.0, 1.5                       # centred, 0.5-2.0 s
    w.rerender_caption(cap)
    sub = w.make_sub([["hello", 0.0, 0.5], ["world", 0.5, 1.0]], 2.5, 3.5)
    if sub not in w.clips:
        w.clips.append(sub)
    w.after_edit()
    return cap, sub


def test_save_with_ctrl_s_and_reopen_with_ctrl_o(ed, modals, rich_media, tmp_path, cs, qapp):
    from conftest import dispose_window, show_editor, stop_autosave
    w = ed
    _project_with_text(w, rich_media, cs)
    w.cb_canvas.setCurrentIndex(0)
    proj = str(tmp_path / "roundtrip.clipproj")
    modals.save_path = proj
    key(w, Qt.Key_S, Qt.ControlModifier)
    assert os.path.isfile(proj) and not w.is_dirty()
    d = json.load(open(proj, encoding="utf-8"))
    clips, lib, sub = d["clips"], d["lib"], d["substyle"]
    assert "roundtrip" in w.windowTitle()
    dispose_window(w)                           # quit, start again, Ctrl+O the file
    w2 = cs.MainWindow()
    try:
        stop_autosave(w2)
        show_editor(w2)
        w2.show()
        activate(w2)
        modals.open_path = proj
        key(w2, Qt.Key_O, Qt.ControlModifier)
        flush(50)
        d2 = w2.project_dict()
        assert d2["clips"] == clips and d2["lib"] == lib and d2["substyle"] == sub
        assert (w2.W, w2.H) == (d["W"], d["H"]) and not w2.is_dirty()
        assert "roundtrip" in w2.windowTitle()
    finally:
        dispose_window(w2)


def test_export_real_video_with_burned_captions_and_subtitles(ed, modals, rich_media, tmp_path, cs):
    w = ed
    cap, sub = _project_with_text(w, rich_media, cs)
    out = str(tmp_path / "final.mp4")
    modals.save_path = out
    done = []
    cs.BUS.done.connect(lambda e: done.append(e))
    key(w, Qt.Key_E, Qt.ControlModifier)
    assert wait_until(lambda: done, timeout=180), "export did not finish"
    assert done[0] == "", done[0]
    info = ffprobe(out)
    v = [s for s in info["streams"] if s["codec_type"] == "video"]
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert (v[0]["width"], v[0]["height"]) == (1080, 1920)
    assert a and a[0]["channels"] == 2
    assert abs(float(info["format"]["duration"]) - w.total()) < 0.15
    white = lambda c: c.red() > 235 and c.green() > 235 and c.blue() > 235        # noqa: E731
    not_blue = lambda c: c.red() > 90 or c.green() > 90                               # noqa: E731
    band = (0, 760, 1080, 1160)                                                       # centre of the frame
    before = frame_at(out, 0.2, str(tmp_path / "f0.png"))
    during = frame_at(out, 1.2, str(tmp_path / "f1.png"))
    assert count_pixels(before, white, band) == 0
    assert count_pixels(during, white, band) > 200, "caption box not burned in"
    sy = int(1920 * w.sub_style()["ypct"] / 100)
    sband = (0, max(0, sy - 120), 1080, min(1920, sy + 120))
    subf = frame_at(out, 3.0, str(tmp_path / "f2.png"))
    assert count_pixels(before, not_blue, sband) == 0
    assert count_pixels(subf, not_blue, sband) > 100, "subtitle not burned in"


def test_empty_project_export_is_refused_politely(ed, modals):
    key(ed, Qt.Key_E, Qt.ControlModifier)
    assert modals.calls and modals.calls[-1][0] == "information"
    assert "empty" in modals.calls[-1][1][1].lower()


# =============================================================================== missing / corrupt files
def _save_with_media(w, modals, rich_media, tmp_path):
    mv = str(tmp_path / "will_move.mp4")
    shutil.copy(rich_media["horiz"], mv)
    w.add_to_library(mv)
    w.add_to_library(rich_media["png"])
    w.add_clip(w.lib[0], None, None)
    proj = str(tmp_path / "missing.clipproj")
    modals.save_path = proj
    assert w.save_project()
    return mv, proj


def test_missing_media_is_reported_on_open_and_blocks_export(ed, modals, rich_media, tmp_path):
    w = ed
    mv, proj = _save_with_media(w, modals, rich_media, tmp_path)
    os.rename(mv, mv + ".away")
    w._saved = w.snapshot()
    w.open_project(proj)
    warn = [c for c in modals.calls if c[0] == "warning"]
    assert warn and "will_move.mp4" in warn[-1][1][1]
    assert len(w.clips) == 1 and w.clips[0].path == mv       # the clip is kept (blank), not dropped
    n = len(modals.calls)
    w.export()
    assert modals.calls[n][0] == "warning" and "will_move.mp4" in modals.calls[n][1][1]


@pytest.mark.xfail(strict=False, reason="P1-2 workflow_report: opening a project while a library file is offline "
                                        "drops it from the library; the next save deletes it from the project")
def test_offline_library_entry_survives_open_and_save(ed, modals, rich_media, tmp_path):
    w = ed
    mv, proj = _save_with_media(w, modals, rich_media, tmp_path)
    os.rename(mv, mv + ".away")
    w._saved = w.snapshot()
    w.open_project(proj)
    w.save_project()
    os.rename(mv + ".away", mv)
    w.open_project(proj)
    assert any(l["path"] == mv for l in w.lib)


CORRUPT = {
    "truncated": '{"substyle": {}, "W": 1080, "H": 19',
    "empty": "",
    "binary": "\x00\x01\x02garbage",
    "list_root": "[1, 2, 3]",
    "clips_not_list": json.dumps({"clips": 5, "W": 1080, "H": 1920}),
    # the ones below get past json.load and fail half-way through open_project
    "no_size": json.dumps({"clips": [], "lib": []}),
    "size_is_text": json.dumps({"clips": [], "W": "wide", "H": 1920}),
    "clip_with_bad_types": json.dumps({"clips": [{"start": "abc", "dur": None, "track": 9}], "W": 1080, "H": 1920}),
}
HALF_LOADS = {"clip_with_bad_types"}    # no_size / size_is_text / lib entry: fixed in round 2


@pytest.mark.parametrize("case", list(CORRUPT))
def test_corrupt_project_file_leaves_current_project_untouched(ed, modals, rich_media, tmp_path, case, request):
    if case in HALF_LOADS:
        request.node.add_marker(pytest.mark.xfail(strict=False, reason="P1-3 workflow_report: open_project validates "
                                                  "too little; the editor is torn down half-way, no error shown"))
    w = ed
    w.add_to_library(rich_media["png"])
    w.add_clip(w.lib[0], None, None)
    good = str(tmp_path / "good.clipproj")
    modals.save_path = good
    w.save_project()
    before = (w.snapshot(), json.dumps(w.lib, sort_keys=True), w.proj_path, w.W, w.H)
    bad = str(tmp_path / f"{case}.clipproj")
    with open(bad, "w", encoding="utf-8") as f:
        f.write(CORRUPT[case])
    n = len(modals.calls)
    errors = []
    old_hook = __import__("sys").excepthook
    try:
        w.open_project(bad)
    except Exception as e:                       # in the app this would go to sys.excepthook: nothing shown
        errors.append(e)
    finally:
        __import__("sys").excepthook = old_hook
    assert not errors, f"exception escaped open_project: {errors[0]!r}"
    assert any(c[0] in ("warning", "critical", "msgbox") for c in modals.calls[n:]), "no error shown to the user"
    assert (w.snapshot(), json.dumps(w.lib, sort_keys=True), w.proj_path, w.W, w.H) == before


def test_project_with_a_damaged_library_entry_still_opens(ed, modals, tmp_path):
    """A library entry without a path is skipped; the rest of the project opens (no exception, no half state)."""
    p = str(tmp_path / "badlib.clipproj")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"clips": [], "lib": [{"name": "x"}, 5], "W": 1920, "H": 1080}, f)
    assert ed.open_project(p) is not False
    assert os.path.abspath(ed.proj_path) == os.path.abspath(p) and (ed.W, ed.H) == (1920, 1080) and ed.lib == []


# =============================================================================== responsiveness
def test_import_does_not_freeze_for_seconds(ed, rich_media):
    t0 = time.perf_counter()
    ed.add_to_library(rich_media["vert"])
    dt = time.perf_counter() - t0
    assert dt < 3.0, f"importing one clip blocked the GUI thread for {dt:.2f} s"


@pytest.mark.xfail(strict=False, reason="P2 workflow_report: probe + library icon run ffmpeg synchronously on the "
                                        "GUI thread (~250 ms per 1080p clip, seconds for a multi-file import / open)")
def test_import_blocks_gui_less_than_100ms(ed, rich_media):
    t0 = time.perf_counter()
    ed.add_to_library(rich_media["horiz"])
    assert time.perf_counter() - t0 < 0.1


def test_double_actions_are_harmless(ed, modals, rich_media, tmp_path):
    w = ed
    import_all(w, modals, [rich_media["vert"]])
    w.add_clip(w.lib[0], None, None)
    c = w.clips[0]
    click(w.tl, clip_center(w, c))
    click(w.tl, QPoint(int(w.tl.tx(2.0)), 5))
    click(w.tl, clip_center(w, c))
    key(w, Qt.Key_S)
    key(w, Qt.Key_S)                                # playhead now sits on the cut: second S does nothing
    assert len(w.clips) == 2
    key(w, Qt.Key_Delete)
    key(w, Qt.Key_Delete)                           # nothing selected any more
    assert len(w.clips) == 1
    proj = str(tmp_path / "twice.clipproj")
    modals.save_path = proj
    key(w, Qt.Key_S, Qt.ControlModifier)
    key(w, Qt.Key_S, Qt.ControlModifier)
    assert [k for k in modals.kinds() if k == "save"] == ["save"]   # asked for a path only once
    assert json.load(open(proj, encoding="utf-8"))["clips"] == clips_state(w)
