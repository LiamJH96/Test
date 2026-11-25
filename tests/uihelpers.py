"""Helpers for the workflow tests: real input events (QTest) on real widgets, media generation, ffprobe."""
import json
import os
import subprocess
import time

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QAbstractButton, QApplication, QTabWidget
from PySide6.QtTest import QTest


def flush(ms=0):
    app = QApplication.instance()
    t_end = time.time() + ms / 1000.0
    while True:
        app.processEvents()
        if time.time() >= t_end:
            break
        time.sleep(0.003)


def ff(*args):
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,width,height,channels", "-of", "json", path],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def frame_at(video, t, out_png):
    ff("-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", out_png)
    from PySide6.QtGui import QImage
    img = QImage(out_png)
    assert not img.isNull(), out_png
    return img


def count_pixels(img, pred, rect=None, step=4):
    x0, y0, x1, y1 = rect or (0, 0, img.width(), img.height())
    n = 0
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            if pred(img.pixelColor(x, y)):
                n += 1
    return n


def activate(w):
    w.activateWindow()
    w.raise_()
    flush(10)


def button(w, text):
    """A visible button whose text is exactly `text` (ampersands ignored)."""
    for b in w.findChildren(QAbstractButton):
        if b.text().replace("&&", "&").replace("&", "") == text.replace("&", "") and b.isVisible():
            return b
    raise LookupError(f"no visible button {text!r}")


def click(widget, pos=None, mods=Qt.NoModifier, btn=Qt.LeftButton):
    QTest.mouseClick(widget, btn, mods, pos if pos is not None else widget.rect().center())
    flush()


def left_tabs(w):
    for t in w.findChildren(QTabWidget):
        if t.count() and t.tabText(0).replace("&", "") == "Library":
            return t
    raise LookupError("left tab widget")


def goto_left_tab(w, name):
    t = left_tabs(w)
    for i in range(t.count()):
        if t.tabText(i).replace("&", "") == name:
            t.setCurrentIndex(i)
            flush()
            return
    raise LookupError(name)


def row_y(w, track):
    import clip_studio as cs
    return int(w.tl.clip_rect(cs.Clip(track=track, start=0, dur=1)).center().y())


def tl_point(w, t, track):
    return QPoint(int(round(w.tl.tx(t))), row_y(w, track))


def clip_center(w, c):
    return w.tl.clip_rect(c).center().toPoint()


def lib_double_click(w, row):
    lv = w.libview
    p = lv.visualItemRect(lv.item(row)).center()
    QTest.mouseClick(lv.viewport(), Qt.LeftButton, Qt.NoModifier, p)
    QTest.mouseDClick(lv.viewport(), Qt.LeftButton, Qt.NoModifier, p)
    flush()


def lib_drag_to(w, row, t, track):
    """Press on a library item, move it out over the timeline and let go (the app's own in-app drag)."""
    lv = w.libview
    p = lv.visualItemRect(lv.item(row)).center()
    QTest.mousePress(lv.viewport(), Qt.LeftButton, Qt.NoModifier, p)
    QTest.mouseMove(lv.viewport(), p + QPoint(12, 0))
    flush()
    lp = lv.viewport().mapFromGlobal(w.tl.mapToGlobal(tl_point(w, t, track)))
    QTest.mouseMove(lv.viewport(), lp)
    flush()
    hint = w.tl.drop_hint
    QTest.mouseRelease(lv.viewport(), Qt.LeftButton, Qt.NoModifier, lp)
    flush()
    w.tl._auto.stop()
    return hint


def trans_drag_to(w, name, clip):
    tl_ = w.translist
    it = tl_.findItems(name, Qt.MatchExactly)[0]
    p = tl_.visualItemRect(it).center()
    QTest.mousePress(tl_.viewport(), Qt.LeftButton, Qt.NoModifier, p)
    QTest.mouseMove(tl_.viewport(), p + QPoint(12, 0))
    flush()
    lp = tl_.viewport().mapFromGlobal(w.tl.mapToGlobal(clip_center(w, clip)))
    QTest.mouseMove(tl_.viewport(), lp)
    flush()
    QTest.mouseRelease(tl_.viewport(), Qt.LeftButton, Qt.NoModifier, lp)
    flush()


def tl_drag(w, p0, p1, steps=8, mods=Qt.NoModifier):
    tl = w.tl
    QTest.mousePress(tl, Qt.LeftButton, mods, p0)
    for i in range(1, steps + 1):
        p = p0 + (p1 - p0) * (i / steps)
        QTest.mouseMove(tl, p)
        flush()
    QTest.mouseRelease(tl, Qt.LeftButton, mods, p1)
    flush()
    tl._auto.stop()


def key(w, k, mods=Qt.NoModifier, target=None):
    """A real key press on the focused widget (default: the timeline), so QShortcut / focus rules apply."""
    tgt = target or w.tl
    if target is None:
        w.tl.setFocus()
    QTest.keyClick(tgt, k, mods)
    flush()


def inner_spin(widget):
    from PySide6.QtWidgets import QAbstractSpinBox
    if isinstance(widget, QAbstractSpinBox):
        return widget
    return widget.findChild(QAbstractSpinBox)


def type_into_spin(widget, text):
    sp = inner_spin(widget)
    sp.setFocus()
    sp.selectAll()
    QTest.keyClicks(sp, text)
    QTest.keyClick(sp, Qt.Key_Return)
    flush()


def clips_state(w):
    return json.loads(w.snapshot())


def wait_until(cond, timeout=30.0, step_ms=20):
    t0 = time.time()
    while not cond():
        if time.time() - t0 > timeout:
            return False
        flush(step_ms)
    return True
