"""Regression suite for clip_studio.py (UI redesign guard).

Every test must pass against the ORIGINAL clip_studio.py; a redesign that only changes visuals must keep them green.

Run (default target is ../clip_studio.py):
    cd /home/user/Test && QT_QPA_PLATFORM=offscreen python -m pytest -q tests
Run against another copy:
    CLIP_STUDIO_PATH=/path/to/dir_or_file.py python -m pytest -q tests
Extra source-level comparison with the untouched original (optional):
    CLIP_STUDIO_ORIGINAL=/path/to/clip_studio_original.py python -m pytest -q tests
"""
import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time

import pytest
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import (QAction, QContextMenuEvent, QDropEvent, QKeyEvent, QKeySequence, QMouseEvent, QShortcut,
                           QWheelEvent, QDragMoveEvent)
from PySide6.QtWidgets import QAbstractButton, QApplication, QGraphicsSceneMouseEvent, QWidget

from conftest import wait_imports, MODULE_FILE, dispose_window

SCRATCH_ORIGINAL = ("/tmp/claude-0/-home-user-Test/6869cefe-e397-54ef-8db1-e6317537ce5b/scratchpad/"
                    "clip_studio_original.py")
ORIGINAL_FILE = os.environ.get("CLIP_STUDIO_ORIGINAL", SCRATCH_ORIGINAL)

# =============================================================================== baseline captured from the original
BASE_SHORTCUTS = ['Space', 'S', 'Delete', 'Shift+Delete', 'Ctrl+Z', 'Ctrl+Y', 'Ctrl+S', 'Left', 'Right', 'Home', 'End',
                  'Ctrl+Shift+Z', 'Ctrl+D', 'Ctrl+=', 'Ctrl++', 'Ctrl+-', 'Ctrl+0', 'Backspace', 'Ctrl+A', 'Z',
                  'Ctrl+O', 'Ctrl+E']

BASE_CLASSES = {
    'Bus': [],
    'Clip': ['__init__', 'end', 'to_dict'],
    'ColorButton': ['__init__', 'paint', 'pick', 'set_color'],
    'LibraryList': ['__init__', '_lib_for', 'mouseMoveEvent', 'mousePressEvent', 'mouseReleaseEvent', 'paintEvent',
                    'startDrag'],
    'MainWindow': ['__init__', 'active', 'add_caption', 'add_clip', 'add_lib_row', 'add_selected_lib',
                   'add_to_library', 'after_drag', 'after_edit', 'after_subs', 'apply_caption', 'apply_props',
                   'apply_sub_style', 'apply_sub_text', 'apply_subs', 'autosave', 'build_canvas',
                   'build_caption_panel', 'build_props', 'build_sub_left', 'build_sub_panel', 'cancel_conv',
                   'clip_at_preview', 'closeEvent', 'convert_hdr', 'delete_sel', 'detach_audio', 'do_redo', 'do_undo',
                   'dragEnterEvent', 'dropEvent', 'duplicate', 'emoji_row', 'export', 'generate_subtitles', 'goto_tab',
                   'import_media', 'insert_gap', 'is_dirty', 'lib_entry', 'lib_menu', 'load_font', 'load_props',
                   'load_sub_defaults', 'make_controls', 'make_icon', 'make_sub', 'next_conv', 'offer_restore',
                   'on_conv_done', 'on_conv_prog', 'on_export_done', 'on_hscroll', 'on_prog', 'open_project',
                   'pick_cap_colour', 'project_dict', 'push_undo', 'quick_fade', 'remove_lib', 'rerender_caption',
                   'reset_sub_defaults', 'resizeEvent', 'restore', 'ripple_delete', 'run_convert', 'run_export',
                   'save_project', 'save_sub_defaults', 'sel', 'select_after', 'select_all', 'selected',
                   'set_canvas', 'set_fit', 'set_item_pending', 'set_selection', 'set_time', 'set_trans', 'showEvent',
                   'snapshot', 'speech_sources', 'split', 'split_sub', 'start_bg', 'status', 'sub_abs_words',
                   'sub_active', 'sub_caps', 'sub_changed', 'sub_factory', 'sub_style', 'sync_scroll', 'tick',
                   'toggle_play', 'total', 'typing', 'update_label', 'update_preview', 'update_title', 'wrap_sub',
                   'zoom', 'zoom_clip', 'zoom_fit'],
    'PreviewView': ['__init__', '_label', 'fit', 'mouseMoveEvent', 'mousePressEvent', 'mouseReleaseEvent', 'reset',
                    'wheelEvent', 'zoom_to'],
    'SliderSpin': ['__init__', '_sp', 'setEnabled', 'setValue', 'value'],
    'Slot': ['__init__', 'release', 'set_clip_path', 'show_clip'],
    'SubItem': ['__init__', 'boundingRect', 'corner', 'handle', 'hoverMoveEvent', 'mouseMoveEvent',
                'mousePressEvent', 'mouseReleaseEvent', 'paint', 'set_time', 'shape'],
    'Timeline': ['__init__', '_auto_tick', '_cache_key', '_invalidate', 'apply_trans_drop', 'clip_at', 'clip_rect',
                 'commit_move', 'commit_multi', 'contextMenuEvent', 'dragEnterEvent', 'dragLeaveEvent',
                 'dragMoveEvent', 'drag_update', 'dropEvent', 'drop_target', 'ext_drop', 'ext_hint', 'free_mode',
                 'junction_at', 'junction_btn', 'keyPressEvent', 'leaveEvent', 'mouseMoveEvent', 'mousePressEvent',
                 'mouseReleaseEvent', 'overlaps', 'paintEvent', 'paint_static', 'playhead_update', 'push_chain',
                 'resolve', 'row_at', 'set_trans_on', 'snap', 'trans_menu', 'tx', 'wheelEvent', 'xt'],
    'TransList': ['__init__', 'mouseMoveEvent', 'mousePressEvent', 'mouseReleaseEvent'],
    'TransformBox': ['__init__', 'boundingRect', 'corner_at', 'corners', 'geom', 'hit', 'hoverMoveEvent', 'hs',
                     'mouseMoveEvent', 'mousePressEvent', 'mouseReleaseEvent', 'paint', 'rect', 'set_clip'],
}
BASE_FUNCS = ['amix_has_normalize', 'ass_col', 'ass_time', 'build_export', 'build_sub_ass', 'caption_layout',
              'chunk_words', 'compute_thumbs', 'compute_wave', 'dark_theme', 'default_font', 'dip_white', 'eff_bold',
              'eff_fades', 'find_ffmpeg', 'fitf', 'fmt_t', 'get_emoji_img', 'has_zscale', 'hdr_filter', 'main',
              'mk_icon', 'motion_exprs', 'next_of', 'paren_flags', 'pix_of', 'prep_fonts', 'prev_of', 'probe',
              'project_len', 'regular_only', 'render_caption', 'run', 'set_times', 'trans_info', 'wipe_alpha']
BASE_CONSTS = ['ANIMS', 'AUD_EXT', 'BIG', 'BUS', 'CANVAS', 'CAP_DIR', 'COL', 'EMOJI_PICK', 'EMOJI_RE', 'EMO_DIR',
               'EMO_MEM', 'EMO_STATE', 'FFMPEG', 'IMG_EXT', 'KIND_TRACK', 'LBL', 'LOWPRI', 'MIME', 'MIME_T', 'NOWIN',
               'PIX', 'RULER', 'SEED_EMOJI', 'SLIDE_DIR', 'TH', 'THUMBS', 'TRACKS', 'TRANS', 'WAVES', 'WIPES']
# MainWindow attributes that exist right after construction in the original (UI wiring other code relies on)
BASE_MW_ATTRS = ['H', 'W', '_loading', '_saved', '_sel', '_sels', 'auto_timer', 'b_play', 'b_zoomv', 'cancel_flag',
                 'canvas', 'cap_bcol', 'cap_bold', 'cap_box', 'cap_edit', 'cap_font', 'cap_size', 'cap_tcol',
                 'cap_widgets', 'cb_canvas', 'cb_fps', 'ck_nv', 'ck_ripple', 'ck_snap', 'clips', 'conv_bar',
                 'conv_box', 'conv_cur', 'conv_eta', 'conv_label', 'conv_q', 'conv_t0', 'editbar', 'empty_txt',
                 'header', 'hs', 'last_push', 'lbl_time', 'lib', 'libview', 'new_cap', 'p_dur', 'p_fin', 'p_fout',
                 'p_name', 'p_scale', 'p_tdur', 'p_trans', 'p_vol', 'p_x', 'p_y', 'playing', 'prog', 'proj_path',
                 'props', 'ptabs', 'redo', 'sb_hint', 'scene', 'sel', 'slots', 'sub_anim', 'sub_anim_ms', 'sub_bold',
                 'sub_cmain', 'sub_cout', 'sub_ctag', 'sub_ctag2', 'sub_cunsp', 'sub_edit', 'sub_font', 'sub_gpu',
                 'sub_lang', 'sub_maxwords', 'sub_model', 'sub_outline', 'sub_quick', 'sub_selonly', 'sub_size',
                 'sub_tag_anim', 'sub_upper', 'sub_vad', 'sub_xpos', 'sub_ypos', 'subitem', 't', 't_at_play',
                 'timer', 'tl', 'translist', 'transport', 'undo', 'view', 'wall0', 'work', 'xbox']
BASE_OBJECT_NAMES = {'play', 'primary', 'round', 'tool', 'time', 'title', 'header'}

BASE_CLIP_FIELDS = ["id", "name", "path", "kind", "track", "start", "inp", "dur", "srcdur", "w", "h", "hasaudio",
                    "fin", "fout", "vol", "scale", "x", "y", "ttype", "tdur", "nat", "text", "csize", "cfont", "ccol",
                    "cbox", "cuse", "words", "wdur", "cbold"]
BASE_PROJECT_KEYS = {"substyle", "W", "H", "lib", "clips"}
BASE_SUBSTYLE_KEYS = {"font", "bold", "size", "color", "color2", "outline_color", "outline", "tag_color",
                      "tag_color2", "ypct", "xpct", "anim", "tag_anim", "anim_ms", "upper"}
BASE_LIB_KEYS = {"kind", "dur", "w", "h", "hasaudio", "hdr", "trc", "path", "name", "pending"}
BASE_TRACKS = [("Overlay 2", "v"), ("Overlay 1", "v"), ("Video", "v"), ("Audio", "a"), ("Music", "a"),
               ("Subtitles", "s")]
BASE_TRANS = ["None", "Crossfade", "Dip to black", "Dip to white", "Slide left", "Slide right", "Slide up",
              "Slide down", "Push left", "Push right", "Push up", "Push down", "Wipe left", "Wipe right", "Wipe up",
              "Wipe down", "Circle open"]
BASE_ANIMS = ["Zoom In", "Pop", "Fade", "Slide Up", "Word reveal", "Word pop", "Karaoke highlight", "None"]
BASE_CANVAS = {"Vertical 1080x1920": (1080, 1920), "Landscape 1920x1080": (1920, 1080),
               "Square 1080x1080": (1080, 1080)}

# Timeline right-click menu of a video clip (submenu entries prefixed with "> ")
BASE_CLIP_MENU = ["Select all clips after this", "Add a gap before this clip...", "Split at playhead (S)",
                  "Duplicate", "Delete (Del)", "Fade", "> Fade in 1 s", "> Fade out 1 s", "> Fade in and out 1 s",
                  "> Remove fades", "Transition from previous clip"] + ["> " + t for t in BASE_TRANS]
BASE_LIB_MENU = ["Convert HDR to SDR...", "Remove from library"]

# A button is found by its text OR tooltip containing one of these phrases (case-insensitive). Phrases are listed
# most specific first; keep at least one of them (button text or tooltip) when restyling a button.
BUTTONS = {
    "undo": ["Undo"], "redo": ["Redo"],
    "split": ["Cut the selected clip in two", "Split"],
    "delete": ["Delete the selected clip"],
    "gap": ["Insert empty space", "Add gap"],
    "ripple": ["pull later clips", "close gap"],
    "detach": ["Split the sound of the selected video", "Detach audio"],
    "zoom_out": ["Zoom out the timeline"], "zoom_in": ["Zoom in the timeline"],
    "fit": ["Zoom to fit the whole project", "Zoom to fit"],
    "open": ["Open a saved project", "Open project"], "save": ["Save the project", "Save project"],
    "export": ["Render the finished video", "Export video"], "import": ["Import media"],
    "add_tl": ["Add to timeline"], "add_cap": ["Add caption to timeline"], "cancel_conv": ["Cancel conversion"],
    "gen_subs": ["Generate subtitles"],
    "sub_save": ["Every new session starts with the subtitle look", "Save as my default"],
    "sub_reset": ["Go back to the built-in subtitle look", "Reset to original"],
    "load_font": ["Add a font from a .ttf", "Load font file"],
    "parrot": ["Replace this subtitle's text with (Parrot Replay)", "(Parrot Replay)"],
    "wrap_round": ["Put round brackets around", "Wrap in ( )"],
    "wrap_square": ["Put square brackets around", "Wrap in [ ]"],
    "box_col": ["Box colour"], "text_col": ["Text colour"],
    "play": ["Play / pause"], "start": ["Go to the start"],
    "zoom_video": ["Zoom the preview in on the clip", "Zoom video"],
    "fit_clip": ["Fit the whole clip inside"], "fill_clip": ["Zoom the clip to cover"],
    "reset_clip": ["Back to centred"],
}


# =============================================================================== helpers
def find_button(w, key):
    phrases = [p.lower() for p in BUTTONS[key]]
    hits = []
    for b in w.findChildren(QAbstractButton):
        text = b.text().replace("&&", "&").lower()
        blob = text + " \x00 " + b.toolTip().lower()
        idx = [i for i, p in enumerate(phrases) if p in blob]
        if idx:
            hits.append((min(idx), text not in phrases, len(text), b))
    assert hits, f"no button found for {key!r} (text/tooltip containing {BUTTONS[key]})"
    hits.sort(key=lambda h: h[:3])
    return hits[0][3]


def all_shortcuts(w):
    """{portable key string: callable that fires it} for QShortcut children and QAction shortcuts."""
    out = {}
    for s in w.findChildren(QShortcut):
        k = s.key().toString(QKeySequence.PortableText)
        if k:
            out.setdefault(k, s.activated.emit)
    for a in w.findChildren(QAction):
        for ks in a.shortcuts():
            k = ks.toString(QKeySequence.PortableText)
            if k:
                out.setdefault(k, a.trigger)
    return out


def norm_key(k):
    return QKeySequence(k).toString(QKeySequence.PortableText)


def fire(w, key):
    sc = all_shortcuts(w)
    k = norm_key(key)
    assert k in sc, f"shortcut {key} missing"
    QApplication.instance().focusWidget() and QApplication.instance().focusWidget().clearFocus()
    w.tl.setFocus()
    sc[k]()


def lib_of(w, path):
    w.add_to_library(path)
    return next(l for l in w.lib if l["path"] == path)


def add(w, path, track=None, start=None):
    w.add_clip(lib_of(w, path), track, start)
    return w.sel


def mouse(tl, kind, pos, button=Qt.LeftButton, mods=Qt.NoModifier):
    t = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove, "release": QEvent.MouseButtonRelease}[kind]
    buttons = Qt.NoButton if kind == "release" else button
    btn = Qt.NoButton if kind == "move" else button
    pos = QPointF(pos)
    g = QPointF(tl.mapToGlobal(pos.toPoint()))
    ev = QMouseEvent(t, pos, g, btn, buttons, mods)
    getattr(tl, {"press": "mousePressEvent", "move": "mouseMoveEvent", "release": "mouseReleaseEvent"}[kind])(ev)


def clip_point(tl, c, frac=0.5):
    r = tl.clip_rect(c)
    return QPointF(r.left() + r.width() * frac, r.center().y())


def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                        "-of", "json", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def video_dims(info):
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    return v["width"], v["height"]


def wait_for(cond, flush, timeout=60.0):
    t0 = time.time()
    while not cond():
        if time.time() - t0 > timeout:
            raise AssertionError("timed out waiting")
        flush(20)


def _load_original_module():
    if not os.path.exists(ORIGINAL_FILE):
        return None
    spec = importlib.util.spec_from_file_location("clip_studio_original_ref", ORIGINAL_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# =============================================================================== 1. structure / API surface
def test_module_imports(cs):
    assert os.path.abspath(cs.__file__) == MODULE_FILE
    assert cs.FFMPEG, "ffmpeg not found by find_ffmpeg()"
    for f in BASE_FUNCS:
        assert callable(getattr(cs, f, None)), f"module function {f} removed"
    for c in BASE_CONSTS:
        assert hasattr(cs, c), f"module constant {c} removed"


def test_classes_and_methods_not_removed(cs):
    missing = []
    for cls, meths in BASE_CLASSES.items():
        k = getattr(cs, cls, None)
        assert k is not None, f"class {cls} removed"
        for m in meths:
            if not hasattr(k, m):
                missing.append(f"{cls}.{m}")
    assert not missing, f"methods removed: {missing}"


def test_data_model_unchanged(cs):
    assert cs.Clip.FIELDS == BASE_CLIP_FIELDS
    assert list(cs.TRACKS) == BASE_TRACKS
    assert list(cs.TRANS) == BASE_TRANS
    assert list(cs.ANIMS) == BASE_ANIMS
    assert dict(cs.CANVAS) == BASE_CANVAS
    assert cs.KIND_TRACK == {"audio": "a", "sub": "s"}
    assert cs.MIME == "application/x-clipstudio-lib" and cs.MIME_T == "application/x-clipstudio-trans"
    c = cs.Clip()
    d = c.to_dict()
    assert (d["kind"], d["track"], d["dur"], d["vol"], d["scale"], d["ttype"], d["tdur"], d["nat"], d["cbold"]) == \
        ("video", 2, 5.0, 100, 100, "None", 1.0, 0, -1)
    assert c.end == c.start + c.dur


def test_source_shortcuts_vs_original():
    """Source-level: every sc("...") / QKeySequence("...") key of the original still appears in the current file."""
    cur = open(MODULE_FILE, encoding="utf-8").read()
    keys = set(re.findall(r'\bsc\(\s*"([^"]+)"', cur))
    keys |= set(re.findall(r'QKeySequence\(\s*"([^"]+)"\s*\)', cur))
    keys |= set(re.findall(r'setShortcuts?\(\s*"([^"]+)"', cur))
    base = BASE_SHORTCUTS
    if os.path.exists(ORIGINAL_FILE):
        base = re.findall(r'\bsc\(\s*"([^"]+)"', open(ORIGINAL_FILE, encoding="utf-8").read())
    cur_norm = {norm_key(k) for k in keys}
    missing = [k for k in base if norm_key(k) not in cur_norm]
    assert not missing, f"shortcut definitions missing from source: {missing}"


def test_source_methods_vs_original():
    """If the untouched original is available, compare every class's method set against it (none removed)."""
    if not os.path.exists(ORIGINAL_FILE):
        pytest.skip("original file not available")

    def methods(path):
        t = ast.parse(open(path, encoding="utf-8").read())
        out = {}
        for n in t.body:
            if isinstance(n, ast.ClassDef):
                out[n.name] = {x.name for x in n.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))}
            elif isinstance(n, ast.FunctionDef):
                out.setdefault("<module>", set()).add(n.name)
        return out
    o, c = methods(ORIGINAL_FILE), methods(MODULE_FILE)
    missing = [f"{k}.{m}" for k, ms in o.items() for m in ms if m not in c.get(k, set())
               and not (k == "<module>" and m in c.get("<module>", set()))]
    assert not missing, f"removed vs original: {missing}"


def test_main_window_constructs_and_wiring(win, cs):
    assert win.isVisible()
    missing = [a for a in BASE_MW_ATTRS if not hasattr(win, a)]
    assert not missing, f"MainWindow attributes removed: {missing}"
    names = {o.objectName() for o in win.findChildren(QWidget)} | {win.header.objectName()}
    assert BASE_OBJECT_NAMES <= names, f"objectNames missing: {BASE_OBJECT_NAMES - names}"
    for key in BUTTONS:
        if key in ("save", "open") and hasattr(win, "b_proj"):
            # round 2: Save / Open moved into the header's project-name menu (plan); the shortcuts must remain
            try:
                find_button(win, key)
            except AssertionError:
                assert norm_key("Ctrl+S" if key == "save" else "Ctrl+O") in all_shortcuts(win)
            continue
        find_button(win, key)
    for a in ("pps", "t0", "mode", "snap_on", "ripple", "drop_hint", "hov", "hj", "plan"):
        assert hasattr(win.tl, a)
    assert win.tl.snap_on and win.tl.ripple
    assert win.W, win.H == (1080, 1920)
    assert win.cb_canvas.currentText() == "Vertical 1080x1920"
    assert [win.cb_canvas.itemText(i) for i in range(win.cb_canvas.count())] == list(BASE_CANVAS)
    assert [win.cb_fps.itemText(i) for i in range(win.cb_fps.count())] == ["30", "60"]
    assert [win.p_trans.itemText(i) for i in range(win.p_trans.count())] == BASE_TRANS
    assert [win.sub_anim.itemText(i) for i in range(win.sub_anim.count())] == BASE_ANIMS
    assert [win.sub_tag_anim.itemText(i) for i in range(win.sub_tag_anim.count())] == ["Same as subtitle"] + BASE_ANIMS
    assert [win.translist.item(i).text() for i in range(win.translist.count())] == BASE_TRANS[1:]
    assert [win.sub_model.itemText(i) for i in range(win.sub_model.count())] == \
        ["tiny", "base", "small", "medium", "large-v3"]
    tabs = {win.ptabs.tabText(i) for i in range(win.ptabs.count())}
    assert {"Caption", "Subtitle", "Fade", "Adjust", "Audio", "Transition"} <= tabs


def test_dark_theme_applies(cs, qapp):
    cs.dark_theme(qapp)
    assert qapp.palette().window().color().lightness() < 80, "theme should stay dark"
    assert qapp.styleSheet().strip()


# =============================================================================== 2. keyboard shortcuts
def test_every_original_shortcut_registered(win):
    have = set(all_shortcuts(win))
    missing = [k for k in BASE_SHORTCUTS if norm_key(k) not in have]
    assert not missing, f"shortcuts missing at runtime: {missing}"
    for s in win.findChildren(QShortcut):
        if s.key().toString(QKeySequence.PortableText) in {norm_key(k) for k in BASE_SHORTCUTS}:
            assert s.context() == Qt.ApplicationShortcut


def test_shortcuts_do_their_job(win, media, modals, tmp_path, flush):
    a = add(win, media["video"])
    b = add(win, media["video"])
    assert len(win.clips) == 2
    flush()
    # Space: play / pause
    fire(win, "Space")
    assert win.playing
    fire(win, "Space")
    assert not win.playing
    # Home / End / Left / Right
    fire(win, "End")
    assert abs(win.t - win.total()) < 1e-6
    fire(win, "Home")
    assert win.t == 0
    fire(win, "Right")
    assert abs(win.t - 1 / 30) < 1e-6
    fire(win, "Left")
    assert abs(win.t) < 1e-6
    # S: split
    win.set_time(1.0)
    win.sel = a
    fire(win, "S")
    assert len(win.clips) == 3
    # Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z
    fire(win, "Ctrl+Z")
    assert len(win.clips) == 2
    fire(win, "Ctrl+Y")
    assert len(win.clips) == 3
    fire(win, "Ctrl+Z")
    fire(win, "Ctrl+Shift+Z")
    assert len(win.clips) == 3
    # Ctrl+A: select all
    fire(win, "Ctrl+A")
    assert len(win.selected()) == 3
    # Ctrl+D: duplicate
    win.sel = win.clips[0]
    fire(win, "Ctrl+D")
    assert len(win.clips) == 4
    # Delete / Backspace
    win.sel = win.clips[-1]
    fire(win, "Delete")
    assert len(win.clips) == 3
    win.sel = win.clips[-1]
    fire(win, "Backspace")
    assert len(win.clips) == 2
    # Shift+Delete: ripple delete
    first = min(win.clips, key=lambda c: c.start)
    other = max(win.clips, key=lambda c: c.start)
    exp = max(0.0, other.start - first.dur) if other.start >= first.end - 1e-3 else other.start
    win.sel = first
    fire(win, "Shift+Delete")
    assert len(win.clips) == 1 and abs(win.clips[0].start - exp) < 1e-6
    # zoom shortcuts
    p0 = win.tl.pps
    fire(win, "Ctrl+=")
    assert win.tl.pps > p0
    p1 = win.tl.pps
    fire(win, "Ctrl++")
    assert win.tl.pps > p1
    fire(win, "Ctrl+-")
    assert win.tl.pps < win.tl.pps * 1.3 + 1
    fire(win, "Ctrl+0")
    assert win.tl.t0 == 0.0
    # Z: zoom the preview on the clip under the playhead, again to reset
    win.sel = win.clips[0]
    win.set_time(win.clips[0].start + 0.5)
    fire(win, "Z")
    assert win.view.zoomed
    fire(win, "Z")
    assert not win.view.zoomed
    # Ctrl+S: save (dialog answered with a path)
    modals.save_path = str(tmp_path / "proj")
    fire(win, "Ctrl+S")
    assert os.path.exists(str(tmp_path / "proj.clipproj"))
    # Ctrl+O: open dialog shown (cancelled)
    modals.open_path = ""
    fire(win, "Ctrl+O")
    assert "open" in modals.kinds()
    # Ctrl+E: export save dialog shown (cancelled)
    modals.save_path = ""
    n = modals.kinds().count("save")
    fire(win, "Ctrl+E")
    assert modals.kinds().count("save") == n + 1


def test_typing_guard_blocks_single_key_shortcuts(win, media, qapp, flush):
    c = add(win, media["video"])
    win.set_time(1.0)
    win.sel = c
    win.activateWindow()
    win.sub_lang.setFocus()
    flush()
    if QApplication.focusWidget() is not win.sub_lang:
        pytest.skip("focus not available on this platform")
    assert win.typing()
    sc = all_shortcuts(win)
    sc[norm_key("S")]()
    assert len(win.clips) == 1, "S must not split while typing in a text field"
    sc[norm_key("Ctrl+Z")]()          # Ctrl+Z is unguarded on purpose
    assert len(win.clips) == 0


def test_escape_cancels_timeline_drag(win, media, flush):
    a = add(win, media["video"])
    flush()
    win.tl.snap_on = False
    s0 = a.start
    p = clip_point(win.tl, a)
    mouse(win.tl, "press", p)
    assert win.tl.mode == "move"
    mouse(win.tl, "move", p + QPointF(win.tl.pps * 1.0, win.tl.clip_rect(a).height() * -0 - 0), Qt.LeftButton)
    win.tl.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert win.tl.mode is None
    assert abs(win.clips[0].start - s0) < 1e-6


# =============================================================================== 3. media / library
def test_probe(cs, media):
    v = cs.probe(media["video"])
    assert v["kind"] == "video" and abs(v["dur"] - 3.0) < 0.15 and (v["w"], v["h"]) == (320, 240) and v["hasaudio"]
    assert not v["hdr"]
    n = cs.probe(media["video_noaudio"])
    assert n["kind"] == "video" and not n["hasaudio"] and abs(n["dur"] - 2.0) < 0.15
    a = cs.probe(media["wav"])
    assert a["kind"] == "audio" and abs(a["dur"] - 2.0) < 0.1 and a["hasaudio"]
    i = cs.probe(media["png"])
    assert i["kind"] == "image" and (i["w"], i["h"]) == (200, 100)


def test_waveform_and_thumbnails(cs, media):
    cs.compute_wave(media["wav"])
    assert media["wav"] in cs.WAVES and len(cs.WAVES[media["wav"]]) > 150
    cs.compute_thumbs(media["video"], 3.0)
    assert media["video"] in cs.THUMBS and cs.THUMBS[media["video"]][1]


def test_library_import_paths(win, media, modals, flush):
    for k in ("video", "video_noaudio", "wav", "png"):
        win.add_to_library(media[k])
    assert len(win.lib) == 4 and win.libview.count() == 4
    assert set(win.lib[0]) >= BASE_LIB_KEYS
    win.add_to_library(media["video"])          # duplicates are ignored
    assert len(win.lib) == 4
    # Import media button -> file dialog
    win.lib.clear()
    win.libview.clear()
    modals.open_paths = [media["video"], media["png"]]
    find_button(win, "import").click()
    wait_imports(win)
    assert [l["name"] for l in win.lib] == ["clip_a.mp4", "pic.png"]
    # drop files onto the window
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(media["wav"])])
    ev = QDropEvent(QPointF(10, 10), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
    win.dropEvent(ev)
    wait_imports(win)
    assert any(l["path"] == media["wav"] for l in win.lib)
    # double-click / "Add to timeline"
    win.libview.itemDoubleClicked.emit(win.libview.item(0))
    assert len(win.clips) == 1 and win.clips[0].kind == "video" and win.clips[0].track == 2
    win.libview.setCurrentRow(1)
    find_button(win, "add_tl").click()
    assert len(win.clips) == 2 and win.clips[1].kind == "image" and win.clips[1].track == 1
    assert win.clips[1].scale == 50 and abs(win.clips[1].dur - 5.0) < 1e-6
    win.libview.setCurrentRow(2)
    win.add_selected_lib()
    assert win.clips[2].kind == "audio" and win.clips[2].track == 3


def test_library_context_menu(win, media, modals):
    win.add_to_library(media["video"])
    win.add_to_library(media["png"])
    pos = win.libview.visualItemRect(win.libview.item(1)).center()
    win.lib_menu(pos)
    texts = [t.strip() for t in modals.menus[-1][1]]
    for t in BASE_LIB_MENU:
        assert any(x.startswith(t) for x in texts), f"library menu entry {t!r} missing: {texts}"
    modals.menu_pick = "Remove from library"
    win.lib_menu(pos)
    assert len(win.lib) == 1 and win.libview.count() == 1
    # in use on the timeline -> remove disabled
    win.add_lib_row(0)
    pos = win.libview.visualItemRect(win.libview.item(0)).center()
    win.lib_menu(pos)
    m = modals.menus[-1][0]
    rem = [a for a in m.actions() if a.text().startswith("Remove from library")][0]
    assert not rem.isEnabled() and "in use" in rem.text()


def test_library_drag_to_timeline(win, media, flush):
    """The in-app drag: press on a library item, move, release over the timeline."""
    win.add_to_library(media["video"])
    lv = win.libview
    flush()
    p = lv.visualItemRect(lv.item(0)).center()

    def lev(t, pos, buttons):
        return QMouseEvent(t, QPointF(pos), QPointF(lv.viewport().mapToGlobal(pos)),
                           Qt.LeftButton if t != QEvent.MouseMove else Qt.NoButton, buttons, Qt.NoModifier)
    lv.mousePressEvent(lev(QEvent.MouseButtonPress, p, Qt.LeftButton))
    lv.mouseMoveEvent(lev(QEvent.MouseMove, p + QPoint(20, 0), Qt.LeftButton))
    target = win.tl.mapToGlobal(QPoint(int(win.tl.tx(0.5)), int(cs_row_y(win, 2))))
    tgt_local = lv.viewport().mapFromGlobal(target)
    lv.mouseMoveEvent(lev(QEvent.MouseMove, tgt_local, Qt.LeftButton))
    assert win.tl.drop_hint is not None
    lv.mouseReleaseEvent(lev(QEvent.MouseButtonRelease, tgt_local, Qt.NoButton))
    win.tl._auto.stop()
    assert len(win.clips) == 1 and win.clips[0].track == 2


def cs_row_y(win, track):
    """Vertical centre of a timeline row, from a clip rect so it follows any layout constant changes."""
    import clip_studio as cs
    probe = cs.Clip(track=track, start=0, dur=1)
    return win.tl.clip_rect(probe).center().y()


def test_timeline_drop_events(win, media, cs, flush):
    win.add_to_library(media["video"])
    win.add_to_library(media["wav"])
    md = QMimeData()
    md.setData(cs.MIME, b"0")
    pos = QPointF(win.tl.tx(1.0), cs_row_y(win, 2))
    win.tl.dragMoveEvent(QDragMoveEvent(pos.toPoint(), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier))
    assert win.tl.drop_hint and win.tl.drop_hint[0] == 2
    win.tl.dropEvent(QDropEvent(pos, Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier))
    assert len(win.clips) == 1 and win.clips[0].track == 2 and win.tl.drop_hint is None
    # an audio item dropped on a video row goes to the Audio track
    md2 = QMimeData()
    md2.setData(cs.MIME, b"1")
    win.tl.dropEvent(QDropEvent(QPointF(win.tl.tx(0.2), cs_row_y(win, 0)), Qt.CopyAction, md2, Qt.LeftButton,
                                Qt.NoModifier))
    assert win.clips[-1].kind == "audio" and win.clips[-1].track == 3
    # transitions drop
    b = add(win, media["video"])
    mt = QMimeData()
    mt.setData(cs.MIME_T, b"Wipe left")
    win.tl.dropEvent(QDropEvent(clip_point(win.tl, b), Qt.CopyAction, mt, Qt.LeftButton, Qt.NoModifier))
    assert b.ttype == "Wipe left"
    win.tl.apply_trans_drop("Crossfade", clip_point(win.tl, b))
    assert b.ttype == "Crossfade"


def test_translist_drag_onto_clip(win, media, flush):
    a = add(win, media["video"])
    b = add(win, media["video"])
    flush()
    tl_ = win.translist
    it = tl_.item(BASE_TRANS[1:].index("Dip to white"))
    p = tl_.visualItemRect(it).center()

    def ev(t, pos, buttons):
        return QMouseEvent(t, QPointF(pos), QPointF(tl_.viewport().mapToGlobal(pos)),
                           Qt.LeftButton if t != QEvent.MouseMove else Qt.NoButton, buttons, Qt.NoModifier)
    tl_.mousePressEvent(ev(QEvent.MouseButtonPress, p, Qt.LeftButton))
    tl_.mouseMoveEvent(ev(QEvent.MouseMove, p + QPoint(15, 0), Qt.LeftButton))
    g = win.tl.mapToGlobal(clip_point(win.tl, b).toPoint())
    tl_.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, tl_.viewport().mapFromGlobal(g), Qt.NoButton))
    assert b.ttype == "Dip to white"


# =============================================================================== 4. editing operations
def test_add_clips_to_tracks(win, media):
    v = add(win, media["video"])
    assert (v.kind, v.track, v.start) == ("video", 2, 0.0) and abs(v.dur - 3.0) < 0.15 and v.hasaudio
    v2 = add(win, media["video"])
    assert abs(v2.start - v.end) < 1e-6, "second video is appended after the first"
    i = add(win, media["png"], track=0, start=1.0)
    assert (i.kind, i.track, i.start, i.dur) == ("image", 0, 1.0, 5.0)
    a = add(win, media["wav"], track=4, start=0.5)
    assert (a.kind, a.track, a.start) == ("audio", 4, 0.5)
    bad = add(win, media["wav"], track=2, start=0.0)       # wrong track kind -> default Audio track
    assert bad.track == 3
    # overlapping on a non-magnetic track is pushed after
    i2 = add(win, media["png"], track=0, start=2.0)
    assert i2.start >= i.end - 1e-6
    # magnetic insert on the main Video track
    win.tl.ripple = True
    mid = add(win, media["video_noaudio"], track=2, start=0.5)
    vs = sorted((c for c in win.clips if c.track == 2), key=lambda c: c.start)
    for x, y in zip(vs, vs[1:]):
        assert y.start >= x.end - 1e-6, "no overlaps on the Video track after a magnetic insert"
    assert mid in vs


def test_split_delete_duplicate(win, media):
    v = add(win, media["video"])
    win.set_time(1.2)
    find_button(win, "split").click()
    assert len(win.clips) == 2
    a, b = sorted(win.clips, key=lambda c: c.start)
    assert abs(a.dur - 1.2) < 1e-6 and abs(b.start - 1.2) < 1e-6 and abs(b.inp - 1.2) < 1e-6
    assert abs(a.dur + b.dur - 3.0) < 0.15 and win.sel is b
    # split outside the clip does nothing
    win.sel = a
    win.set_time(2.5)
    win.split()
    assert len(win.clips) == 2
    win.split()
    win.sel = None
    win.split()
    assert len(win.clips) == 2
    # duplicate
    win.sel = a
    win.duplicate()
    assert len(win.clips) == 3 and win.sel.start >= a.end - 1e-6
    # delete button
    find_button(win, "delete").click()
    assert len(win.clips) == 2


def test_undo_redo(win, media):
    assert not win.undo
    add(win, media["video"])
    add(win, media["png"])
    s_two = win.snapshot()
    win.sel = win.clips[0]
    win.delete_sel()
    s_one = win.snapshot()
    find_button(win, "undo").click()
    assert win.snapshot() == s_two
    find_button(win, "redo").click()
    assert win.snapshot() == s_one
    win.do_undo()
    win.do_undo()
    win.do_undo()
    assert win.clips == []
    win.do_undo()                   # nothing left: harmless
    for _ in range(100):
        win.push_undo()
    assert len(win.undo) == 80, "undo history is capped at 80"


def test_detach_audio(win, media):
    v = add(win, media["video"])
    find_button(win, "detach").click()
    assert len(win.clips) == 2
    a = win.sel
    assert a.kind == "audio" and a.track == 3 and a.name.startswith("Audio - ") and not v.hasaudio
    assert abs(a.start - v.start) < 1e-6 and abs(a.dur - v.dur) < 1e-6
    n = len(win.clips)
    win.sel = v
    win.detach_audio()              # no audio left -> no-op
    assert len(win.clips) == n
    win.do_undo()
    assert len(win.clips) == 1 and win.clips[0].hasaudio


def test_add_gap_and_ripple_delete(win, media, modals):
    a = add(win, media["video"])
    b = add(win, media["video"])
    s_b = b.start
    modals.gap = (2.0, True)
    win.insert_gap(s_b)
    b = max(win.clips, key=lambda c: c.start)
    assert abs(b.start - (s_b + 2.0)) < 1e-6
    assert abs(min(c.start for c in win.clips)) < 1e-6
    modals.gap = (1.0, False)       # cancelled
    win.insert_gap(0.0)
    assert abs(b.start - (s_b + 2.0)) < 1e-6
    modals.gap = (0.5, True)
    end_before = max(c.end for c in win.clips)
    find_button(win, "gap").click()             # toolbar button (adds at the playhead / start)
    assert abs(max(c.end for c in win.clips) - (end_before + 0.5)) < 1e-6
    # Delete & close gap
    win.sel = min(win.clips, key=lambda c: c.start)
    first_dur = win.sel.dur
    later = max(win.clips, key=lambda c: c.start)
    st_later = later.start
    find_button(win, "ripple").click()
    assert len(win.clips) == 1 and abs(win.clips[0].start - max(0.0, st_later - first_dur)) < 1e-6


def test_selection_helpers(win, media):
    a = add(win, media["video"])
    b = add(win, media["video"])
    i = add(win, media["png"], track=0, start=0)
    win.select_all()
    assert set(win.selected()) == {a, b, i}
    win.select_after(b)
    assert win.selected() == [b]
    win.set_selection([a, i], a)
    assert win.sel is a and set(win.selected()) == {a, i}
    win.delete_sel()
    assert win.clips == [b]


def test_fades_transitions_and_props(win, media, cs):
    a = add(win, media["video"])
    b = add(win, media["video"])
    win.sel = b
    win.quick_fade(1.0, None)
    assert b.fin == 1.0 and b.fout == 0
    win.quick_fade(None, 1.0)
    assert b.fout == 1.0
    win.quick_fade(0.0, 0.0)
    assert b.fin == 0 and b.fout == 0
    win.quick_fade(99.0, 99.0)
    assert b.fin == b.dur == b.fout, "fades are clamped to the clip length"
    win.quick_fade(0.0, 0.0)
    for t in BASE_TRANS:
        win.set_trans(t)
        assert b.ttype == t
        info = cs.trans_info(win.clips, b)
        assert (info is None) == (t == "None")
    win.set_trans("Crossfade")
    fi, fo, ext = cs.eff_fades(win.clips, b)
    assert fi > 0
    assert cs.eff_fades(win.clips, a)[2] > 0, "previous clip is extended under a crossfade"
    # properties panel
    win.sel = a
    win.load_props()
    win.last_push = 0
    win.p_fin.setValue(0.5)
    assert abs(a.fin - 0.5) < 1e-6
    win.p_fout.setValue(0.7)
    assert abs(a.fout - 0.7) < 1e-6
    win.p_vol.setValue(150)
    assert a.vol == 150
    win.p_scale.setValue(80)
    win.p_x.setValue(10)
    win.p_y.setValue(-10)
    assert (a.scale, a.x, a.y) == (80, 10, -10)
    win.p_tdur.setValue(0.5)
    assert abs(a.tdur - 0.5) < 1e-6
    win.p_trans.setCurrentText("Wipe up")
    assert a.ttype == "Wipe up"
    assert not win.p_dur.isEnabled()
    img = add(win, media["png"], track=0, start=10)
    win.load_props()
    assert win.p_dur.isEnabled()
    win.p_dur.setValue(2.5)
    assert abs(img.dur - 2.5) < 1e-6
    # Fit / Fill / Reset
    win.sel = a
    find_button(win, "fill_clip").click()
    assert a.scale > 100 and a.x == 0 and a.y == 0
    find_button(win, "reset_clip").click()
    assert (a.scale, a.x, a.y) == (100, 0, 0)
    a.scale = 40
    find_button(win, "fit_clip").click()
    assert a.scale == 100
    # audio clip disables visual props
    au = add(win, media["wav"])
    win.load_props()
    en = lambda wdg: getattr(wdg, "sp", wdg).isEnabled()      # SliderSpin enables its inner spin box
    assert not en(win.p_scale) and not win.p_trans.isEnabled() and en(win.p_vol)


def test_timeline_mouse_workflows(win, media, modals, flush):
    tl = win.tl
    a = add(win, media["video"])
    b = add(win, media["video"])
    flush()
    tl.snap_on = False
    # ruler click seeks
    mouse(tl, "press", QPointF(tl.tx(1.5), 3))
    mouse(tl, "release", QPointF(tl.tx(1.5), 3))
    assert abs(win.t - 1.5) < 2 / tl.pps
    # click selects
    mouse(tl, "press", clip_point(tl, a))
    mouse(tl, "release", clip_point(tl, a))
    assert win.sel is a
    # ctrl+click adds to the selection
    mouse(tl, "press", clip_point(tl, b), mods=Qt.ControlModifier)
    assert set(win.selected()) == {a, b}
    mouse(tl, "release", clip_point(tl, b))
    mouse(tl, "press", clip_point(tl, b), mods=Qt.ControlModifier)
    assert win.selected() == [a]
    mouse(tl, "release", clip_point(tl, b))
    # shift+click selects a range on the same track
    win.sel = a
    mouse(tl, "press", clip_point(tl, b), mods=Qt.ShiftModifier)
    assert set(win.selected()) == {a, b}
    mouse(tl, "release", clip_point(tl, b))
    # ctrl+drag on empty space: marquee
    win.sel = None
    p0 = QPointF(tl.tx(0.1), cs_row_y(win, 0))
    mouse(tl, "press", p0, mods=Qt.ControlModifier)
    assert tl.mode == "marquee"
    mouse(tl, "move", QPointF(tl.tx(b.end - 0.2), cs_row_y(win, 2)), Qt.LeftButton)
    assert set(win.selected()) == {a, b}
    mouse(tl, "release", QPointF(tl.tx(b.end - 0.2), cs_row_y(win, 2)))
    assert tl.mode is None
    # trim right edge
    # (grab edges near the top of the clip: the middle of a join is the transition "+" button)
    win.sel = a
    r = tl.clip_rect(a)
    p = QPointF(r.right() - 2, r.top() + 5)
    n_undo = len(win.undo)
    mouse(tl, "press", p)
    assert tl.mode == "trimR"
    mouse(tl, "move", p - QPointF(tl.pps * 1.0, 0), Qt.LeftButton)
    mouse(tl, "release", p - QPointF(tl.pps * 1.0, 0))
    assert abs(a.dur - 2.0) < 0.1 and len(win.undo) == n_undo + 1
    # trim left edge
    r = tl.clip_rect(b)
    p = QPointF(r.left() + 2, r.top() + 5)
    s_b, d_b = b.start, b.dur
    mouse(tl, "press", p)
    assert tl.mode == "trimL"
    mouse(tl, "move", p + QPointF(tl.pps * 0.5, 0), Qt.LeftButton)
    mouse(tl, "release", p + QPointF(tl.pps * 0.5, 0))
    assert abs(b.start - (s_b + 0.5)) < 0.05 and abs(b.inp - 0.5) < 0.05 and abs(b.dur - (d_b - 0.5)) < 0.05
    # magnetic move: drag b to the very start -> it goes before a, a shuffles along
    p = clip_point(tl, b)
    mouse(tl, "press", p)
    assert tl.mode == "move"
    mouse(tl, "move", QPointF(p.x() - tl.pps * (b.start + 1.0), p.y()), Qt.LeftButton)
    assert tl.plan is not None
    mouse(tl, "release", QPointF(p.x() - tl.pps * (b.start + 1.0), p.y()))
    # it lands before or after a (by which half it covers), never overlapping
    assert (abs(b.start) < 1e-6 and abs(a.start - b.end) < 1e-6) or abs(b.start - a.end) < 1e-6
    # moving the first clip far to the right closes the gap it leaves on the magnetic track
    first, second = sorted((a, b), key=lambda c: c.start)
    s2, d1 = second.start, first.dur
    p = clip_point(tl, first)
    far = QPointF(tl.tx(second.end + 3.0) + (p.x() - tl.tx(first.start)), p.y())
    mouse(tl, "press", p)
    mouse(tl, "move", far, Qt.LeftButton)
    mouse(tl, "release", far)
    assert abs(second.start - max(0.0, s2 - d1)) < 1e-6 and first.start >= second.end - 1e-6
    # an image moved to another video row follows the row; it cannot go onto an audio row
    img = add(win, media["png"], track=0, start=8.0)
    p = clip_point(tl, img)
    mouse(tl, "press", p)
    mouse(tl, "move", QPointF(p.x(), cs_row_y(win, 1)), Qt.LeftButton)
    mouse(tl, "release", QPointF(p.x(), cs_row_y(win, 1)))
    assert img.track == 1
    p = clip_point(tl, img)
    mouse(tl, "press", p)
    mouse(tl, "move", QPointF(p.x(), cs_row_y(win, 3)), Qt.LeftButton)
    mouse(tl, "release", QPointF(p.x(), cs_row_y(win, 3)))
    assert img.track == 1
    # multi-select move
    win.set_selection([a, img], a)
    sa, si = a.start, img.start
    p = clip_point(tl, img)
    mouse(tl, "press", p)
    mouse(tl, "move", p + QPointF(tl.pps * 2.0, 0), Qt.LeftButton)
    mouse(tl, "release", p + QPointF(tl.pps * 2.0, 0))
    moved = (abs(img.start - (si + 2.0)) < 0.05 and abs(a.start - (sa + 2.0)) < 0.05)
    reverted = (abs(img.start - si) < 1e-6 and abs(a.start - sa) < 1e-6)
    assert moved or reverted


def test_timeline_context_menu(win, media, modals, flush):
    a = add(win, media["video"])
    b = add(win, media["video"])
    flush()
    pt = clip_point(win.tl, b).toPoint()
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, pt, win.tl.mapToGlobal(pt))
    win.tl.contextMenuEvent(ev)
    texts = [t.strip() for t in modals.menus[-1][1]]
    clean = [re.sub(r"\s+", " ", t.replace("&", "")) for t in texts]
    missing = [t for t in BASE_CLIP_MENU if re.sub(r"\s+", " ", t) not in clean]
    assert not missing, f"timeline context menu entries missing: {missing}\nhave: {clean}"
    modals.menu_pick = "Duplicate"
    win.tl.contextMenuEvent(ev)
    assert len(win.clips) == 3
    modals.menu_pick = "Fade in and out 1 s"
    win.sel = b
    win.tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, clip_point(win.tl, b).toPoint()))
    assert b.fin == 1.0 and b.fout == 1.0
    modals.menu_pick = "Push up"
    win.tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, clip_point(win.tl, b).toPoint()))
    assert b.ttype == "Push up"
    modals.gap = (1.5, True)
    s = b.start
    modals.menu_pick = "Add a gap before this clip..."
    win.tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, clip_point(win.tl, b).toPoint()))
    assert abs(b.start - (s + 1.5)) < 1e-6
    # empty area: no menu
    n = len(modals.menus)
    win.tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(int(win.tl.tx(50)),
                                                                            int(cs_row_y(win, 0)))))
    assert len(modals.menus) == n


def test_junction_transition_button(win, media, modals, flush):
    a = add(win, media["video"])
    b = add(win, media["video"])
    flush()
    btn = win.tl.junction_btn(b).center()
    assert win.tl.junction_at(btn) is b
    mouse(win.tl, "move", btn, Qt.NoButton)        # hover shows the "+" button
    assert win.tl.hj is b
    modals.menu_pick = "Crossfade"
    mouse(win.tl, "press", btn)
    mouse(win.tl, "release", btn)
    assert b.ttype == "Crossfade"
    texts = [t.strip() for t in modals.menus[-1][1]]
    assert any("Dip to black" in t for t in texts) and any("No transition" in t for t in texts)


def test_wheel_zoom_and_scroll(win, media, flush):
    add(win, media["video"])
    add(win, media["video"])
    flush()
    tl = win.tl
    p0 = tl.pps
    pos = QPointF(tl.tx(1.0), cs_row_y(win, 2))
    ev = QWheelEvent(pos, QPointF(tl.mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                     Qt.ControlModifier, Qt.NoScrollPhase, False)
    tl.wheelEvent(ev)
    assert abs(tl.pps - p0 * 1.2) < 1e-6
    tl.t0 = 2.0
    ev = QWheelEvent(pos, QPointF(tl.mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                     Qt.NoModifier, Qt.NoScrollPhase, False)
    tl.wheelEvent(ev)
    assert tl.t0 < 2.0
    win.zoom(1.3)
    win.on_hscroll(1500)
    assert abs(tl.t0 - 1.5) < 1e-6
    find_button(win, "fit").click()
    assert tl.t0 == 0.0
    p = tl.pps
    find_button(win, "zoom_in").click()
    assert tl.pps > p
    find_button(win, "zoom_out").click()
    assert abs(tl.pps - p) < 1e-6
    win.ck_snap.setChecked(False)
    assert not tl.snap_on
    win.ck_ripple.setChecked(False)
    assert not tl.ripple


def test_playback_and_transport(win, media, flush):
    add(win, media["video"])
    find_button(win, "play").click()
    assert win.playing
    win.wall0 -= 1.0
    win.tick()
    assert 0.9 < win.t < 2.0
    find_button(win, "play").click()
    assert not win.playing
    find_button(win, "start").click()
    assert win.t == 0
    win.set_time(2.9)
    win.toggle_play()
    win.wall0 -= 5
    win.tick()
    assert not win.playing and abs(win.t - win.total()) < 1e-6
    win.toggle_play()               # at the end: restarts from 0
    assert win.t == 0.0 and win.playing
    win.toggle_play()
    assert "/" in win.lbl_time.text()


def test_canvas_and_preview(win, media, flush):
    v = add(win, media["video"])
    win.cb_canvas.setCurrentText("Landscape 1920x1080")
    assert (win.W, win.H) == (1920, 1080)
    win.cb_canvas.setCurrentText("Square 1080x1080")
    assert (win.W, win.H) == (1080, 1080)
    win.set_time(1.0)
    win.update_preview(force=True)
    flush(50)
    assert win.clip_at_preview(QPointF(540, 540)) is v
    assert win.clip_at_preview(QPointF(5, 5)) is None
    win.sel = None
    win.update_preview(force=True)
    vp = win.view.mapFromScene(QPointF(540, 540))
    win.view.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(vp), QPointF(win.view.mapToGlobal(vp)),
                                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    win.view.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(vp),
                                           QPointF(win.view.mapToGlobal(vp)), Qt.LeftButton, Qt.NoButton,
                                           Qt.NoModifier))
    assert win.sel is v
    win.update_preview(force=True)
    assert win.xbox.isVisible()
    find_button(win, "zoom_video").click()
    assert win.view.zoomed
    find_button(win, "zoom_video").click()
    assert not win.view.zoomed
    # preview transform box: drag moves, corner drag scales
    xb = win.xbox
    ev = QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMousePress)
    ev.setPos(QPointF(540, 540))
    ev.setButton(Qt.LeftButton)
    xb.mousePressEvent(ev)
    mv = QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMouseMove)
    mv.setPos(QPointF(640, 540))
    xb.mouseMoveEvent(mv)
    xb.mouseReleaseEvent(QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMouseRelease))
    assert v.x > 5
    br = xb.corners()["br"]
    ev.setPos(br)
    xb.mousePressEvent(ev)
    assert xb.mode == "scale"
    mv.setPos(br + QPointF(100, 100))
    xb.mouseMoveEvent(mv)
    xb.mouseReleaseEvent(QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMouseRelease))
    assert v.scale > 100


# =============================================================================== 5. captions & subtitles
def test_render_caption(cs):
    c = cs.Clip(kind="image", nat=1, text="Hello world \U0001F480", cfont=cs.default_font())
    fp, W, H = cs.render_caption(c)
    assert os.path.exists(fp) and fp.startswith(cs.CAP_DIR)
    from PySide6.QtGui import QImage
    img = QImage(fp)
    assert (img.width(), img.height()) == (W, H) and W > 50 and H > 20
    c2 = cs.Clip(kind="image", nat=1, text="Hello world\nsecond line here", cfont=cs.default_font())
    _, W2, H2 = cs.render_caption(c2)
    assert H2 > H * 1.5, "two lines are taller"
    c3 = cs.Clip(kind="image", nat=1, text="Outline only", cuse=False, cbold=60, cfont=cs.default_font())
    assert os.path.exists(cs.render_caption(c3)[0])
    assert cs.get_emoji_img("\U0001F480") is not None, "seeded emoji pictures must work offline"


def test_caption_workflow(win, media, modals):
    win.new_cap.setPlainText("My caption \U0001F480")
    find_button(win, "add_cap").click()
    c = win.sel
    assert c.nat == 1 and c.kind == "image" and c.track == 1 and c.text == "My caption \U0001F480"
    assert os.path.exists(c.path) and c.name.startswith("Caption: ")
    assert win.cap_edit.isEnabled() and win.cap_edit.toPlainText() == c.text
    assert win.ptabs.tabText(win.ptabs.currentIndex()) == "Caption"
    old = c.path
    win.last_push = 0
    win.cap_edit.setPlainText("Changed text")
    assert c.text == "Changed text" and c.path != old
    win.cap_size.setValue(100)
    assert c.csize == 100
    win.cap_bold.setValue(40)
    assert c.cbold == 40
    win.cap_box.setChecked(not c.cuse)
    assert c.cuse == win.cap_box.isChecked()
    find_button(win, "box_col").click()
    assert c.cbox.lower() == "#123456"
    modals.color = __import__("PySide6.QtGui", fromlist=["QColor"]).QColor("#abcdef")
    find_button(win, "text_col").click()
    assert c.ccol.lower() == "#abcdef"
    # emoji buttons insert into the caption box
    emo = [b for b in win.findChildren(QAbstractButton) if b.text() == "\U0001F480"]
    assert len(emo) >= 2
    win.add_caption("second")                   # Overlay 1 busy at the playhead: round 2 uses Overlay 2 (P2-6)
    a, b = win.clips[0], win.clips[1]
    assert len(win.clips) == 2
    assert (b.track == 0 and abs(b.start - a.start) < 1e-6) or b.start >= a.end - 1e-6
    win.add_caption("")
    assert win.sel.text == "Your caption here"
    find_button(win, "load_font").click()       # dialog cancelled -> nothing happens
    assert "opens" in modals.kinds()


WORDS = [["hello", 0.1, 0.4], ["world", 0.5, 0.9], ["this", 1.0, 1.3], ["is", 1.4, 1.5], ["(a", 1.6, 1.7],
         ["test).", 1.8, 2.2], ["[next]", 2.3, 2.6], ["bit", 2.7, 2.9]]


def test_chunk_words_and_ass_helpers(cs):
    caps = cs.chunk_words(WORDS, 4)
    assert [len(c["words"]) for c in caps] == [4, 2, 2]
    assert cs.ass_time(3723.456) == "1:02:03.46"
    assert cs.ass_col("#112233") == "332211"
    assert cs.paren_flags(["a", "(b", "c)", "[d]", "e"]) == [0, 1, 1, 2, 0]
    c = {"start": 1.0, "end": 2.0, "words": [["a", 1.0, 1.5], ["b", 1.5, 2.0]]}
    cs.set_times(c, 2.0, 4.0)
    assert c["words"][1][1] == 3.0 and c["end"] == 4.0
    assert cs.fmt_t(75.5) == "01:15.50"


def test_subtitle_workflow(win, media, modals):
    v = add(win, media["video"])
    win.apply_subs([(0.0, 3.0)], [WORDS], 4)
    subs = sorted((c for c in win.clips if c.kind == "sub"), key=lambda c: c.start)
    assert len(subs) == 3 and all(c.track == len(BASE_TRACKS) - 1 for c in subs)
    assert len(win.sub_caps()) == 3
    assert win.sub_active(0.2) is not None and win.sub_active(5.0) is None
    s = subs[0]
    win.sel = s
    win.load_props()
    assert win.sub_edit.isEnabled() and win.sub_edit.toPlainText() == "hello world this is"
    assert win.ptabs.tabText(win.ptabs.currentIndex()) == "Subtitle"
    win.last_push = 0
    win.sub_edit.setPlainText("HELLO WORLD THIS IS")
    assert [w[0] for w in s.words] == ["HELLO", "WORLD", "THIS", "IS"]
    find_button(win, "wrap_round").click()
    assert s.words[0][0] == "(HELLO" and s.words[-1][0] == "IS)"
    find_button(win, "wrap_square").click()
    assert s.words[0][0] == "[HELLO"
    find_button(win, "parrot").click()
    assert " ".join(w[0] for w in s.words) == "(Parrot Replay)"
    win.sub_edit.setPlainText("one two three")
    assert len(s.words) == 3
    # split a subtitle between words
    win.sel = s
    win.set_time((s.words[0][2] + s.words[1][1]) / 2 * (s.dur / s.wdur) + s.start)
    n = len(win.clips)
    win.split()
    assert len(win.clips) == n + 1
    # re-applying replaces the overlapping subtitles
    win.apply_subs([(0.0, 3.0)], [WORDS[:2]], 4)
    assert len([c for c in win.clips if c.kind == "sub"]) == 1
    win.apply_subs([(0.0, 3.0)], [[]], 4)       # no speech -> nothing changes
    assert len([c for c in win.clips if c.kind == "sub"]) == 1
    assert win.speech_sources() == [v]


def test_generate_subtitles_guards(win, media, modals):
    try:
        import faster_whisper  # noqa: F401
        pytest.skip("faster_whisper installed: would really transcribe")
    except ImportError:
        pass
    find_button(win, "gen_subs").click()
    assert modals.kinds()[-1] == "information"      # nothing with sound on the timeline
    add(win, media["video"])
    win.sub_selonly.setChecked(True)
    win.sel = None
    win.generate_subtitles()
    assert modals.kinds()[-1] == "information"
    win.sub_selonly.setChecked(False)
    n = len(modals.calls)
    win.generate_subtitles()
    assert len(modals.calls) == n + 1 and "pip install" in str(modals.calls[-1][1])


def test_build_sub_ass_all_animations(win, cs, tmp_path):
    win.apply_subs([(0.0, 3.0)], [WORDS], 4)
    caps = win.sub_caps()
    for anim in BASE_ANIMS:
        for tag in ("Same as subtitle", "Pop", "Karaoke highlight", "Word reveal"):
            st = dict(win.sub_style(), anim=anim, tag_anim=tag)
            ass = cs.build_sub_ass(caps, st, 1080, 1920)
            assert "[Script Info]" in ass and "PlayResX: 1080" in ass
            assert ass.count("\nDialogue:") == len(caps)
    st = dict(win.sub_style(), anim="Karaoke highlight", upper=True)
    p = tmp_path / "subs.ass"
    p.write_text(cs.build_sub_ass(caps, st, 360, 640), encoding="utf-8")
    out = str(tmp_path / "f.png")
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=black:s=360x640:d=1", "-vf",
                        "ass=subs.ass", "-frames:v", "1", out], cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert os.path.exists(out)


def test_subtitle_preview_paint_and_drag(win, media, flush):
    win.apply_subs([(0.0, 3.0)], [WORDS], 4)
    win.set_time(0.6)
    for anim in BASE_ANIMS:
        win.sub_anim.setCurrentText(anim)
        win.sub_tag_anim.setCurrentText("Pop" if anim == "Fade" else "Same as subtitle")
        win.update_preview(force=True)
        assert not win.view.grab().isNull()
    si = win.subitem
    assert si.box is not None
    x0 = win.sub_xpos.value()
    ev = QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMousePress)
    ev.setPos(si.box.center())
    si.mousePressEvent(ev)
    mv = QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMouseMove)
    mv.setPos(si.box.center() + QPointF(100, 0))
    si.mouseMoveEvent(mv)
    si.mouseReleaseEvent(QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMouseRelease))
    assert win.sub_xpos.value() > x0


# =============================================================================== 6. persistence
def test_persisted_paths_unchanged(cs, fake_home):
    """Round 2 contract: settings live in the Parrot Studio config dir (project_store.config_dir(), env override
    PARROT_STUDIO_CONFIG_DIR); the old ~/.clip_studio locations are still read (legacy autosave, caches)."""
    base = os.path.join(fake_home, ".clip_studio")
    assert cs.MainWindow.AUTOSAVE == os.path.join(base, "autosave.clipproj")     # legacy autosave still offered
    store = getattr(cs, "store", None)
    if store is None:                                                          # pre-round-2 app
        assert cs.EMO_DIR == os.path.join(base, "emoji")
        assert cs.CAP_DIR == os.path.join(base, "captions")
        assert cs.MainWindow.SUB_DEF_FILE == os.path.join(base, "sub_defaults.json")
        return
    if hasattr(store, "cache_dir"):                                            # caches: local cache dir
        cache = os.path.abspath(store.cache_dir())
        assert os.path.abspath(cs.EMO_DIR) == os.path.join(cache, "emoji")
        assert os.path.abspath(cs.CAP_DIR) == os.path.join(cache, "captions")
        if hasattr(cs, "UI_DIR"):
            assert os.path.abspath(cs.UI_DIR) == os.path.join(cache, "ui")
        assert not cache.startswith(base), "caches must not stay in the legacy folder"
        if hasattr(cs, "LEGACY_EMO_DIR"):                                      # old emoji downloads still used
            assert cs.LEGACY_EMO_DIR == os.path.join(base, "emoji")
    cfg = os.environ["PARROT_STUDIO_CONFIG_DIR"]
    assert os.path.abspath(store.config_dir()) == os.path.abspath(cfg)
    assert os.path.abspath(cs.MainWindow.SUB_DEF_FILE) == os.path.join(os.path.abspath(cfg), "sub_defaults.json")
    assert store.legacy_dir() == base


def test_legacy_sub_defaults_are_read_once(cs, qapp, modals, fake_home):
    """A Clip Studio user's saved subtitle look (~/.clip_studio/sub_defaults.json) is brought over on first run."""
    store = getattr(cs, "store", None)
    if store is None:
        pytest.skip("pre-round-2 app")
    from conftest import dispose_window
    legacy = os.path.join(fake_home, ".clip_studio", "sub_defaults.json")
    new = cs.MainWindow.SUB_DEF_FILE
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w", encoding="utf-8") as f:
        json.dump({"size": 123, "upper": True}, f)
    for f_ in (new,):
        if os.path.exists(f_):
            os.remove(f_)
    store.update_settings(lambda d: d.pop("legacy_config_migrated", None))
    try:
        w = cs.MainWindow()
        if hasattr(w, "show_editor"):
            w.show_editor()
        w.load_sub_defaults()
        assert w.sub_style()["size"] == 123 and w.sub_style()["upper"] is True
        assert os.path.isfile(new) and os.path.isfile(legacy), "copied, the old file is left alone"
        dispose_window(w)
        os.remove(new)
        w = cs.MainWindow()                                        # migrated once: not copied again
        assert not os.path.exists(new)
        dispose_window(w)
    finally:
        for f_ in (legacy, new):
            if os.path.exists(f_):
                os.remove(f_)


def test_sub_defaults_save_load_reset(win, cs, qapp, flush):
    f = cs.MainWindow.SUB_DEF_FILE
    try:
        assert set(win.sub_style()) == BASE_SUBSTYLE_KEYS
        assert set(win.sub_factory()) == BASE_SUBSTYLE_KEYS
        win.sub_size.setValue(123)
        win.sub_anim.setCurrentText("Pop")
        find_button(win, "sub_save").click()
        assert os.path.exists(f)
        d = json.load(open(f, encoding="utf-8"))
        assert set(d) == BASE_SUBSTYLE_KEYS and d["size"] == 123 and d["anim"] == "Pop"
        w2 = cs.MainWindow()
        try:
            assert w2.sub_size.value() == 123 and w2.sub_anim.currentText() == "Pop"
        finally:
            dispose_window(w2)
        find_button(win, "sub_reset").click()
        assert not os.path.exists(f)
        assert win.sub_size.value() == win.sub_factory()["size"]
    finally:
        if os.path.exists(f):
            os.remove(f)


def _build_project(win, media):
    add(win, media["video"])
    add(win, media["video_noaudio"])
    add(win, media["png"], track=0, start=0.5)
    add(win, media["wav"], track=4, start=0.0)
    win.add_caption("Round trip \U0001F480")
    win.apply_subs([(0.0, 3.0)], [WORDS], 4)
    v2 = [c for c in win.clips if c.path == media["video_noaudio"]][0]
    win.sel = v2
    win.set_trans("Slide left")
    win.quick_fade(0.3, 0.4)
    win.sub_size.setValue(90)
    win.cb_canvas.setCurrentText("Landscape 1920x1080")


def test_project_save_load_roundtrip(win, media, modals, tmp_path, cs):
    _build_project(win, media)
    before = json.loads(win.snapshot())
    style = win.sub_style()
    modals.save_path = str(tmp_path / "myproj")
    assert win.save_project() is True
    path = str(tmp_path / "myproj.clipproj")
    assert win.proj_path == path and not win.is_dirty()
    assert "myproj" in win.windowTitle() and "*" not in win.windowTitle()
    d = json.load(open(path, encoding="utf-8"))
    # round 2 (schema v2) adds exactly "schema" and "meta"; nothing from v1 may be dropped
    assert set(d) - {"schema", "meta"} == BASE_PROJECT_KEYS
    assert d.get("schema", 2) == 2 and isinstance(d.get("meta", {}), dict)
    assert (d["W"], d["H"]) == (1920, 1080)
    assert set(d["substyle"]) == BASE_SUBSTYLE_KEYS
    assert all(list(c) == BASE_CLIP_FIELDS for c in d["clips"])
    assert all(set(l) >= BASE_LIB_KEYS for l in d["lib"]) and len(d["lib"]) == 4
    # reopen into a fresh window
    w2 = cs.MainWindow()
    try:
        w2.open_project(path)
        assert json.loads(w2.snapshot()) == before
        assert (w2.W, w2.H) == (1920, 1080) and w2.cb_canvas.currentText() == "Landscape 1920x1080"
        assert w2.sub_style() == style
        assert [l["path"] for l in w2.lib] == [l["path"] for l in d["lib"]]
        assert w2.proj_path == path and not w2.is_dirty() and not w2.undo
        # dirty marker
        w2.sel = w2.clips[0]
        w2.delete_sel()
        assert w2.is_dirty()
        if not hasattr(w2, "lbl_save"):                 # round 1: '*' in the title; round 2: header save indicator
            assert w2.windowTitle().endswith("*")
    finally:
        dispose_window(w2)
    # header buttons (round 1) / project-name menu (round 2)
    def trigger(key, menu_text):
        if hasattr(win, "b_proj"):
            modals.menu_pick = menu_text
            n_menus = len(modals.menus)
            win.b_proj.click()
            assert len(modals.menus) == n_menus + 1 and modals.menu_pick is None, f"{menu_text!r} not in the menu"
        else:
            find_button(win, key).click()
    win.sel = win.clips[0]
    win.delete_sel()
    trigger("save", "Save")                     # already has a path: saves silently
    assert len(json.load(open(path))["clips"]) == len(before) - 1
    modals.open_path = path
    modals.question_answer = __import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.Yes
    trigger("open", "Open project\u2026")
    assert len(win.clips) == len(before) - 1 and win.proj_path == path


def test_open_project_errors(win, media, modals, tmp_path):
    bad = tmp_path / "bad.clipproj"
    bad.write_text("not json")
    win.open_project(str(bad))
    assert modals.kinds()[-1] == "warning"
    missing = tmp_path / "missing.clipproj"
    clip = dict(win.sub_factory())
    proj = dict(substyle=clip, W=1080, H=1920, lib=[], clips=[
        dict(__import__("clip_studio").Clip(path=str(tmp_path / "gone.mp4"), name="gone", kind="video").to_dict())])
    missing.write_text(json.dumps(proj))
    win.open_project(str(missing))
    # round 1: a warning; round 2: a message box with a "Locate files…" button (recorded as msgbox)
    assert len(win.clips) == 1 and modals.kinds()[-1] in ("warning", "msgbox") and "gone.mp4" in str(modals.calls[-1])
    win.export()
    assert modals.kinds()[-1] == "warning", "export refuses while media is missing"


def test_autosave_and_restore(win, media, modals, cs):
    if getattr(cs, "store", None) is not None:
        return _autosave_and_restore_round2(win, media, modals, cs)
    try:
        os.remove(win.AUTOSAVE)
    except OSError:
        pass
    win.offer_restore()                         # nothing to restore -> no dialog
    assert "question" not in modals.kinds()
    win.autosave()
    assert not os.path.exists(win.AUTOSAVE), "an empty project is never autosaved"
    _build_project(win, media)
    snap = json.loads(win.snapshot())
    win.autosave()
    assert os.path.exists(win.AUTOSAVE)
    assert set(json.load(open(win.AUTOSAVE))) == BASE_PROJECT_KEYS
    assert win.auto_timer.interval() == 45000
    w2 = cs.MainWindow()
    try:
        w2.auto_timer.stop()
        modals.question_answer = __import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.Yes
        w2.offer_restore()
        assert json.loads(w2.snapshot()) == snap
        assert w2.proj_path is None and w2.is_dirty(), "a restored project stays unsaved"
    finally:
        dispose_window(w2)
    assert not os.path.exists(win.AUTOSAVE), "a clean close removes the autosave"
    win.autosave()
    modals.question_answer = __import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.No
    w3 = cs.MainWindow()
    try:
        w3.offer_restore()
        assert not os.path.exists(win.AUTOSAVE) and w3.clips == []
    finally:
        dispose_window(w3)



def _autosave_and_restore_round2(win, media, modals, cs):
    """Round 2 contract: never-saved work goes to a recovery snapshot (project_store), offered at start-up as a new
    project; the old ~/.clip_studio/autosave.clipproj is only read (see test_recovery.py)."""
    from PySide6.QtWidgets import QMessageBox
    store = cs.store
    for f in (win.AUTOSAVE,):
        if os.path.exists(f):
            os.remove(f)
    store.discard_recovery(None)
    win.offer_restore()                         # nothing to restore -> no dialog
    assert "question" not in modals.kinds()
    win.autosave(wait=True)
    assert store.find_recovery(None) is None, "an empty project is never autosaved"
    _build_project(win, media)
    snap = json.loads(win.snapshot())
    win.autosave(wait=True)
    info = store.find_recovery(None)
    assert info is not None and store.load_recovery(info)["clips"] == snap
    assert not os.path.exists(win.AUTOSAVE), "nothing is written to the old Clip Studio location any more"
    w2 = cs.MainWindow()
    try:
        modals.question_answer = QMessageBox.Yes
        w2.offer_restore()
        assert json.loads(w2.snapshot()) == snap
        assert w2.proj_path and os.path.isfile(w2.proj_path), "restored as a new project"
        assert store.find_recovery(None) is None
    finally:
        dispose_window(w2)
    win._saved = "[]"
    win.autosave(wait=True)
    assert store.find_recovery(None) is not None
    modals.question_answer = QMessageBox.No
    w3 = cs.MainWindow()
    try:
        w3.offer_restore()
        assert store.find_recovery(None) is None and w3.clips == []
    finally:
        dispose_window(w3)


def test_close_event_dirty_prompts(win, media, modals):
    add(win, media["video"])
    assert win.is_dirty()
    work = win.work
    win.close()                                  # message box (stubbed: no button) -> closes
    assert any(k == "msgbox" for k in modals.kinds())
    assert not os.path.isdir(work)


# =============================================================================== 7. export
def _run(cmd, cwd):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


def test_build_export_golden(cs, monkeypatch):
    """The ffmpeg command for a fixed project must not change (export logic is not part of a UI redesign)."""
    monkeypatch.setattr(cs, "_amix_norm", True)

    def scenario(mod):
        C = mod.Clip
        clips = [C(id="a", name="a", path="/m/a.mp4", kind="video", track=2, start=0, dur=3, srcdur=3, w=320, h=240,
                   hasaudio=True, fin=0.5),
                 C(id="b", name="b", path="/m/b.mp4", kind="video", track=2, start=3, inp=1, dur=2, srcdur=3, w=320,
                   h=240, ttype="Crossfade", tdur=0.8),
                 C(id="c", name="c", path="/m/c.mp4", kind="video", track=2, start=5, dur=2, srcdur=3, w=320, h=240,
                   ttype="Wipe left", tdur=0.5, fout=0.5),
                 C(id="d", name="d", path="/m/d.mp4", kind="video", track=2, start=7, dur=2, srcdur=3, w=320, h=240,
                   ttype="Push up", tdur=0.5),
                 C(id="e", name="e", path="/m/e.mp4", kind="video", track=2, start=9, dur=2, srcdur=3, w=320, h=240,
                   ttype="Dip to white", tdur=0.5),
                 C(id="f", name="f", path="/m/p.png", kind="image", track=1, start=1, dur=2, w=200, h=100, scale=50,
                   x=10, y=-5),
                 C(id="g", name="cap", path="/m/cap.png", kind="image", track=0, start=0, dur=3, w=400, h=90, nat=1,
                   y=-30),
                 C(id="h", name="h", path="/m/t.wav", kind="audio", track=4, start=0.5, dur=2, srcdur=2, vol=80,
                   hasaudio=True, fout=1.0)]
        out = []
        for subs in (None, "fonts", "plain"):
            for nv in (False, True):
                cmd, T = mod.build_export(clips, 1080, 1920, 30, "/o/out.mp4", nv, subs)
                out.append(["ffmpeg"] + cmd[1:] + [f"T={T:.3f}"])
        return out
    cur = scenario(cs)
    ref = _load_original_module()
    if ref is not None:
        ref._amix_norm = True
        assert cur == scenario(ref)
    h = hashlib.sha256(json.dumps(cur).encode()).hexdigest()
    assert h == GOLDEN_EXPORT_SHA, f"build_export output changed (sha {h})"


def test_build_sub_ass_golden(cs):
    st = dict(font="Arial", bold=True, size=78, color="#6CB3FF", color2="#FFFFFF", outline_color="#000000",
              outline=8, tag_color="#00FF00", tag_color2="#FF3B3B", ypct=55.0, xpct=50.0, anim="Zoom In",
              tag_anim="Same as subtitle", anim_ms=200, upper=False)
    caps = cs.chunk_words(WORDS, 4)
    outs = []
    for anim in BASE_ANIMS:
        for tag in ["Same as subtitle"] + BASE_ANIMS:
            outs.append(cs.build_sub_ass(caps, dict(st, anim=anim, tag_anim=tag, upper=anim == "Pop"), 1080, 1920))
    h = hashlib.sha256("\n".join(outs).encode()).hexdigest()
    assert h == GOLDEN_ASS_SHA, f"build_sub_ass output changed (sha {h})"


@pytest.mark.parametrize("trans", BASE_TRANS)
def test_export_runs_every_transition(cs, media, tmp_path, trans):
    a = cs.Clip(name="a", path=media["video"], kind="video", track=2, start=0, dur=3.0, srcdur=3.0, w=320, h=240,
                hasaudio=True)
    b = cs.Clip(name="b", path=media["video_noaudio"], kind="video", track=2, start=3.0, dur=2.0, srcdur=2.0, w=320,
                h=240, ttype=trans, tdur=0.6, fout=0.3)
    out = str(tmp_path / "o.mp4")
    cmd, T = cs.build_export([a, b], 180, 320, 30, out, False)
    assert abs(T - 5.0) < 1e-6
    _run(cmd, str(tmp_path))
    info = ffprobe(out)
    assert video_dims(info) == (180, 320)
    assert abs(float(info["format"]["duration"]) - 5.0) < 0.2
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}


def test_export_mixed_project_small(cs, media, tmp_path, win):
    win.add_caption("Export me \U0001F480")
    cap = win.sel
    clips = [cs.Clip(name="v", path=media["video"], kind="video", track=2, start=0, dur=3, srcdur=3, w=320, h=240,
                     hasaudio=True, fin=0.5),
             cs.Clip(name="i", path=media["png"], kind="image", track=1, start=0.5, dur=2, w=200, h=100, scale=50),
             cap,
             cs.Clip(name="m", path=media["wav"], kind="audio", track=4, start=1.0, dur=2, srcdur=2, hasaudio=True,
                     vol=70, fout=0.5)]
    win.apply_subs([(0.0, 3.0)], [WORDS], 4)
    st = win.sub_style()
    (tmp_path / "subs.ass").write_text(cs.build_sub_ass(win.sub_caps(), st, 270, 480), encoding="utf-8")
    subs = "fonts" if cs.prep_fonts(str(tmp_path), st["font"]) else "plain"
    out = str(tmp_path / "mix.mp4")
    cmd, T = cs.build_export(clips, 270, 480, 30, out, False, subs)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in fc and "ass=subs.ass" in fc
    _run(cmd, str(tmp_path))
    info = ffprobe(out)
    assert video_dims(info) == (270, 480) and abs(float(info["format"]["duration"]) - 3.0) < 0.2


def test_export_through_ui(win, media, modals, tmp_path, flush, cs, monkeypatch):
    """Full UI path: Export button -> save dialog -> background ffmpeg -> progress -> done message."""
    add(win, media["video"])
    add(win, media["wav"], track=4, start=0.0)
    win.add_caption("UI export")
    win.apply_subs([(0.0, 3.0)], [WORDS[:4]], 4)
    win.cb_canvas.setCurrentText("Square 1080x1080")
    seen = {}
    real = cs.build_export

    def spy(clips, W, H, fps, out, nvenc, subs=None):
        seen.update(W=W, H=H, fps=fps, out=out, nvenc=nvenc, subs=subs)
        return real(clips, W, H, fps, out, nvenc, subs)
    monkeypatch.setattr(cs, "build_export", spy)
    modals.save_path = str(tmp_path / "final")         # no extension: .mp4 is added
    find_button(win, "export").click()
    assert win.prog is not None
    wait_for(lambda: win.prog is None, flush, timeout=120)
    out = str(tmp_path / "final.mp4")
    assert seen["out"] == out and (seen["W"], seen["H"], seen["fps"]) == (1080, 1080, 30) and not seen["nvenc"]
    assert seen["subs"] in ("fonts", "plain")
    assert not [c for c in modals.calls if c[0] == "warning"], modals.calls
    assert ("msgbox" in modals.kinds()), "the 'Export finished' box is shown"
    info = ffprobe(out)
    assert video_dims(info) == (1080, 1080) and abs(float(info["format"]["duration"]) - win.total()) < 0.2
    assert win.last_export_dir == str(tmp_path)


def test_export_options_and_guards(win, media, modals, tmp_path, cs, monkeypatch, flush):
    win.export()
    assert modals.kinds()[-1] == "information"          # empty timeline
    add(win, media["video"])
    win.cb_fps.setCurrentText("60")
    win.ck_nv.setChecked(True)
    seen = {}
    real = cs.build_export

    def spy(clips, W, H, fps, out, nvenc, subs=None):
        seen.update(fps=fps, nvenc=nvenc, subs=subs)
        return real(clips, W, H, fps, out, nvenc, subs)
    monkeypatch.setattr(cs, "build_export", spy)
    monkeypatch.setattr(win, "run_export", lambda cmd, T: (seen.update(cmd=cmd), cs.BUS.done.emit("cancelled")))
    modals.save_path = str(tmp_path / "x.mp4")
    win.export()
    wait_for(lambda: win.prog is None, flush, timeout=20)
    assert seen["fps"] == 60 and seen["nvenc"] and seen["subs"] is None
    assert "h264_nvenc" in seen["cmd"] and seen["cmd"][-1] == str(tmp_path / "x.mp4")


def test_hdr_conversion_queue(win, media, modals, tmp_path, flush, cs):
    if not cs.has_zscale():
        pytest.skip("ffmpeg without zscale")
    src = str(tmp_path / "hdrish.mp4")
    shutil.copy(media["hdr"], src)
    p = cs.probe(src)
    assert p["hdr"] and p["trc"] == "smpte2084"
    win.add_to_library(src)                    # HDR prompt (stub answers "Use as is")
    assert any(k == "msgbox" and "HDR" in str(a) for k, a in modals.calls)
    info = win.lib[0]
    assert not info["pending"]
    win.convert_hdr(info)
    assert info["pending"] or win.conv_cur is None
    assert win.lib_entry(_mime_row(cs, 0)) is None or not info.get("pending")
    wait_for(lambda: win.conv_cur is None and not win.conv_q, flush, timeout=90)
    assert not [c for c in modals.calls if c[0] == "warning"], modals.calls
    assert win.lib[0]["path"].endswith("hdrish SDR.mp4") and os.path.exists(win.lib[0]["path"])
    assert not win.lib[0]["pending"] and win.conv_box.isHidden()
    assert not cs.probe(win.lib[0]["path"])["hdr"]
    # second conversion gets a numbered name; cancel path
    win.add_to_library(src, ask=False)
    info2 = [l for l in win.lib if l["path"] == src][0]
    win.convert_hdr(info2)
    find_button(win, "cancel_conv").click()
    wait_for(lambda: win.conv_cur is None, flush, timeout=90)
    assert not info2["pending"]


def _mime_row(cs, row):
    m = QMimeData()
    m.setData(cs.MIME, str(row).encode())
    return m


# =============================================================================== 8. painting & window sizes
def _populate(win, media, cs):
    cs.compute_wave(media["wav"])
    cs.compute_thumbs(media["video"], 3.0)
    a = add(win, media["video"])
    b = add(win, media["video"])
    add(win, media["png"], track=0, start=0.5)
    add(win, media["wav"], track=4, start=0.0)
    win.add_caption("Paint me")
    win.apply_subs([(0.0, 3.0)], [WORDS], 4)
    win.sel = b
    win.set_trans("Crossfade")
    win.quick_fade(0.5, 0.5)
    win.set_time(1.0)
    return a, b


@pytest.mark.parametrize("size", [(1280, 720), (1920, 1080), (1024, 640), (1366, 768), (2560, 1440)])
def test_paint_at_window_sizes(win, media, cs, flush, size):
    a, b = _populate(win, media, cs)
    win.resize(*size)
    flush(30)
    assert not win.grab().isNull()
    assert not win.tl.grab().isNull()
    tl = win.tl
    # paint the transient states too
    tl.hov, tl.hj = a, b
    tl.drop_hint = (2, 1.0, 2.0)
    tl.mode, tl.mq = "marquee", (QPointF(100, 40), QPointF(300, 200))
    assert not tl.grab().isNull()
    tl.mode, tl.plan = "move", ("before", a, 0.0)
    win.sel = b
    assert not tl.grab().isNull()
    tl.plan = ("free", None, 7.0)
    assert not tl.grab().isNull()
    tl.mode = tl.plan = tl.mq = tl.drop_hint = tl.hov = tl.hj = None
    for pps, t0 in ((8.0, 0.0), (600.0, 1.0), (80.0, 100.0)):
        tl.pps, tl.t0 = pps, t0
        assert not tl.grab().isNull()
    win.zoom_fit()
    win.playing = True
    win.update_preview(force=True)
    assert not win.view.grab().isNull()
    win.playing = False


def test_empty_paint_and_resizes(win, flush):
    for size in [(1024, 640), (800, 500), (1500, 920), (1920, 1080), (3000, 1800)]:
        win.resize(*size)
        flush(10)
        assert not win.grab().isNull()
    win.showMaximized()
    flush(10)
    win.showNormal()
    flush(10)
    assert not win.libview.grab().isNull()      # empty-library hint paints


def test_status_and_title(win, media):
    win.status("hello there")
    assert win.statusBar().currentMessage() == "hello there"
    assert "Parrot Studio" in win.windowTitle() or win.windowTitle().startswith("Clip Studio")   # round 2 rename
    add(win, media["video"])
    win.update_title()
    if hasattr(win, "lbl_save"):     # round 2: "<name> – Parrot Studio", unsaved state shown in the header
        t = win.windowTitle()
        assert t.endswith("Parrot Studio") and "*" not in t
        assert "not saved" in t.lower() or win.lbl_save.text()
    else:
        assert win.windowTitle().endswith("*")


def test_project_menu_actions_and_shortcuts(win, modals, tmp_path, media):
    """Round 2: Open / Save moved from header buttons into the project-name menu; the shortcuts stay."""
    if not hasattr(win, "b_proj"):
        pytest.skip("round-1 header")
    sc = all_shortcuts(win)
    for k in ("Ctrl+S", "Ctrl+Shift+S", "Ctrl+O", "Ctrl+N"):
        assert norm_key(k) in sc, f"{k} shortcut missing"
    win.b_proj.click()
    texts = [t.replace("&", "") for t in modals.menus[-1][1]]
    for want in ("Save", "Save as", "Open project", "New project", "Back to projects", "Rename"):
        assert any(t.startswith(want) for t in texts), f"{want!r} not in project menu {texts}"
    add(win, media["video"])
    modals.save_path = str(tmp_path / "via_menu")
    modals.menu_pick = "Save"
    win.b_proj.click()
    assert any(c[0] == "save" for c in modals.calls), "Save in the project menu did nothing"
    assert win.proj_path and os.path.isfile(win.proj_path)


def test_icons_render(cs, qapp):
    for n in ("play", "pause", "start", "undo", "redo", "split", "trash", "ripple", "audio", "plus", "minus", "fit"):
        assert not cs.mk_icon(n).isNull()


GOLDEN_EXPORT_SHA = "b4d4387bbd9716bd2aa45bc0dfa7323dbb23afdb9788692cf6224664e89e29a7"
GOLDEN_ASS_SHA = "ff00885428b83bb5d82f40564a000d4e54c34c8d1516ae7c0dbf9dc4a149adb3"
