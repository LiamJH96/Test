"""Parrot Studio project dashboard: the home screen listing projects as cards.

The dashboard manages projects on disk itself (rename, delete, recents, projects folder) through project_store and
asks the main window to open or create projects through signals; it never touches the editor.
Dialogs are made from the classes in `self.dlg` (by default the PySide6 widgets module; the main window passes its
own module, so tests that replace QMessageBox / QFileDialog / QMenu there cover the dashboard too).
"""
import json
import os
import subprocess
import sys

from PySide6 import QtWidgets
from PySide6.QtCore import QFile, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

import project_store as store

CARD_MIN, CARD_MAX, CARD_LO = 220, 300, 200
GAP_X, GAP_Y, META_H, RING = 16, 20, 64, 3      # RING: room around each card for the keyboard-focus ring
CONTENT_MAX = 1320

COL = dict(page="#141518", card="#1B1C1F", card_hover="#202125", card_down="#232428", edge="#26272C",
           edge_hover="#45464E", thumb="#0E0F11", empty_thumb="#17181B", text="#E8E9EC", sub="#767880",
           muted="#A6A8B0", dim="#4F5158", cyan="#7FE5FF", blue="#3A78E0", blue_hover="#4886EC", warn="#E2A33B")


def reveal_in_file_manager(path):
    """Show a file (selected) or folder in Explorer / Finder / the desktop's file manager."""
    if not path:
        return False
    try:
        if os.path.isfile(path) and sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True
        if os.path.isfile(path) and sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
            return True
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        return QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
    except Exception as e:
        store.log_error(f"Could not show {path} in the file manager", e)
        return False


BIN = "Recycle Bin" if sys.platform.startswith("win") else "Trash"


def move_to_trash(path):
    """Move a file or folder to the Recycle Bin / Trash. True if it worked."""
    if not sys.platform.startswith("win") and sys.platform != "darwin":
        # freedesktop.org home trash: Qt only uses it if it exists, and a fresh account may not have one yet
        base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Trash")
        try:
            for sub in ("files", "info"):
                os.makedirs(os.path.join(base, sub), mode=0o700, exist_ok=True)
        except OSError:
            pass
    ok = QFile.moveToTrash(path)
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bool(ok) and not os.path.exists(path)


def nice_path(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home + os.sep) else p


def _font(px, weight=QFont.Normal):
    f = QFont()
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def aspect_label(w, h):
    if not w or not h:
        return ""
    r = w / h
    for lab, v in (("9:16", 9 / 16), ("16:9", 16 / 9), ("1:1", 1.0), ("4:5", 0.8), ("4:3", 4 / 3)):
        if abs(r - v) < 0.02:
            return lab
    return f"{w}x{h}"


def fmt_dur(t):
    t = int(round(t))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


_summary_cache = {}


def project_summary(path):
    """(W, H, length in seconds) of a project, read cheaply and cached by modification time. (0, 0, 0) if unknown."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return 0, 0, 0.0
    if key not in _summary_cache:
        try:
            with open(path, encoding="utf-8-sig") as f:
                d = json.load(f)
            ln = max([float(c.get("start", 0)) + float(c.get("dur", 0)) for c in d.get("clips", [])] + [0.0])
            _summary_cache[key] = (int(d.get("W", 0)), int(d.get("H", 0)), ln)
        except Exception:
            _summary_cache[key] = (0, 0, 0.0)
    return _summary_cache[key]


def blurred(pm, size):
    """A soft, darkened 'cover' copy of pm for the bands beside a portrait thumbnail."""
    cover = pm.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    small = cover.scaled(max(1, cover.width() // 8), max(1, cover.height() // 8), Qt.IgnoreAspectRatio,
                         Qt.SmoothTransformation)
    return small.scaled(cover.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def paint_thumbnail(p, r, pm, w, h):
    """Draw a project picture into the 16:9 area r: 'contain' scaling; side bands get a blurred copy."""
    p.save()
    p.setClipRect(r, Qt.IntersectClip)
    p.fillRect(r, QColor(COL["thumb"]))
    sz = pm.size() / max(1.0, pm.devicePixelRatio())
    fit = sz.scaled(r.size().toSize(), Qt.KeepAspectRatio)
    dst = QRectF(r.x() + (r.width() - fit.width()) / 2, r.y() + (r.height() - fit.height()) / 2,
                 fit.width(), fit.height())
    if fit.width() < r.width() - 2 or fit.height() < r.height() - 2:
        b = blurred(pm, r.size().toSize())
        p.drawPixmap(QRectF(r.x() + (r.width() - b.width()) / 2, r.y() + (r.height() - b.height()) / 2,
                            b.width(), b.height()), b, QRectF(b.rect()))
        p.fillRect(r, QColor(14, 15, 17, 166))
    p.drawPixmap(dst, pm, QRectF(pm.rect()))
    if dst.width() < r.width() - 2:
        p.setPen(QPen(QColor(255, 255, 255, 15), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(dst.adjusted(0.5, 0.5, -0.5, -0.5))
    p.restore()


def paint_empty_thumb(p, r, w, h, text="Empty project"):
    """No picture yet: the outline of the project's canvas shape."""
    p.fillRect(r, QColor(COL["empty_thumb"]))
    w, h = (w or 1080), (h or 1920)
    fh = r.height() * 0.52
    fw = fh * w / h
    if fw > r.width() * 0.5:
        fw = r.width() * 0.5
        fh = fw * h / w
    c = r.center()
    box = QRectF(c.x() - fw / 2, c.y() - fh / 2 - 8, fw, fh)
    p.setPen(QPen(QColor("#3A3B42"), 1.5))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(box, 3, 3)
    p.setFont(_font(11))
    p.setPen(QColor(COL["dim"]))
    p.drawText(QRectF(r.x(), box.bottom() + 2, r.width(), 16), Qt.AlignHCenter | Qt.AlignTop, text)


def paint_badge(p, text, x, y, right=False):
    f = _font(10, QFont.DemiBold)
    p.setFont(f)
    bw = QFontMetrics(f).horizontalAdvance(text) + 12
    b = QRectF(x - bw if right else x, y - 18, bw, 18)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(14, 15, 17, 204))
    p.drawRoundedRect(b, 4, 4)
    p.setPen(QColor("#C9CBD1"))
    p.drawText(b, Qt.AlignCenter, text)


def paint_warning(p, c, s, col):
    p.setPen(QPen(QColor(col), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPolygon(QPolygonF([QPointF(c.x(), c.y() - s / 2), QPointF(c.x() + s / 2, c.y() + s / 2.4),
                             QPointF(c.x() - s / 2, c.y() + s / 2.4)]))
    p.drawLine(QPointF(c.x(), c.y() - s / 6), QPointF(c.x(), c.y() + s / 8))
    p.drawPoint(QPointF(c.x(), c.y() + s / 3.6))


def dots_icon(color="#A6A8B0"):
    pm = QPixmap(48, 48)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    for x in (12, 24, 36):
        p.drawEllipse(QPointF(x, 24), 3.4, 3.4)
    p.end()
    return QIcon(pm)


class _Tile(QWidget):
    """Base of a grid tile: hover / pressed state and a cyan ring for keyboard focus only."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover = self.down = False
        self.kbd_focus = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

    def card_rect(self):
        return QRectF(self.rect()).adjusted(RING, RING, -RING, -RING)

    def thumb_rect(self):
        r = self.card_rect()
        return QRectF(r.x(), r.y(), r.width(), round(r.width() * 9 / 16))

    def paint_ring(self, p):
        if self.hasFocus() and self.kbd_focus:
            p.setPen(QPen(QColor(COL["cyan"]), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 10, 10)

    def enterEvent(self, ev):
        self.hover = True
        self.update()

    def leaveEvent(self, ev):
        self.hover = self.down = False
        self.update()

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self.kbd_focus = ev.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason)
        self.update()

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        self.update()


class ProjectCard(_Tile):
    """One project: picture on top, name, 'Edited 2 hours ago · 0:42 · 9:16', and a ⋯ menu button."""
    activated = Signal(object)              # click / Enter
    menuRequested = Signal(object, object)  # (card, global pos)
    navigate = Signal(object, int)          # (card, Qt key)
    renameRequested = Signal(object)
    renameCommitted = Signal(object, str)
    deleteRequested = Signal(object)

    _pix_cache = {}

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info
        self.W, self.H, self.length = project_summary(info.path) if not info.missing else (0, 0, 0.0)
        self.setToolTip(info.path if not info.missing else f"{info.path}\n(this file can no longer be found)")
        self.setAccessibleName(f"Project {info.name}")
        self.more = QToolButton(self)
        self.more.setObjectName("cardMenu")
        self.more.setIcon(dots_icon())
        self.more.setIconSize(QSize(16, 16))
        self.more.setFixedSize(24, 24)
        self.more.setCursor(Qt.PointingHandCursor)
        self.more.setToolTip("More actions")
        self.more.setFocusPolicy(Qt.NoFocus)
        self.more.clicked.connect(lambda: self.menuRequested.emit(
            self, self.more.mapToGlobal(self.more.rect().bottomLeft())))
        self.more.hide()
        self.editor = None

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        r = self.card_rect()
        self.more.move(int(r.right() - 12 - 24 + 4), int(self.thumb_rect().bottom() + 8))

    def _sync_more(self):
        self.more.setVisible(self.hover or self.hasFocus())

    def enterEvent(self, ev):
        super().enterEvent(ev)
        self._sync_more()

    def leaveEvent(self, ev):
        super().leaveEvent(ev)
        self._sync_more()

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self._sync_more()

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        self._sync_more()

    def pixmap(self):
        th = self.info.thumbnail
        if not th:
            return None
        try:
            key = (th, os.path.getmtime(th))
        except OSError:
            return None
        pm = self._pix_cache.get(key)
        if pm is None:
            pm = QPixmap(th)
            self._pix_cache[key] = pm
        return None if pm.isNull() else pm

    def meta_text(self):
        if self.info.missing:
            return "File not found"
        parts = [store.relative_time(self.info.modified)]
        if self.length > 0:
            parts.append(fmt_dur(self.length))
        if not self.info.managed:
            parts.append(nice_path(self.info.folder))
        return "  ·  ".join(x for x in parts if x)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = self.card_rect()
        outer = QPainterPath()
        outer.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        bg = COL["card_down"] if self.down else (COL["card_hover"] if self.hover else COL["card"])
        p.fillPath(outer, QColor(bg))
        tr = self.thumb_rect()
        p.save()
        p.setClipPath(outer)
        if self.info.missing:
            p.fillRect(tr, QColor("#141518"))
            c = tr.center()
            paint_warning(p, QPointF(c.x(), c.y() - 8), 24, COL["warn"])
            p.setFont(_font(11))
            p.setPen(QColor(COL["muted"]))
            p.drawText(QRectF(tr.x(), c.y() + 10, tr.width(), 16), Qt.AlignHCenter | Qt.AlignTop, "File not found")
        else:
            pm = self.pixmap()
            if pm is not None:
                paint_thumbnail(p, tr, pm, self.W, self.H)
            else:
                paint_empty_thumb(p, tr, self.W, self.H)
            a = aspect_label(self.W, self.H)
            if a:
                paint_badge(p, a, tr.left() + 8, tr.bottom() - 8)
            if self.length > 0:
                paint_badge(p, fmt_dur(self.length), tr.right() - 8, tr.bottom() - 8, right=True)
            if self.info.has_recovery:
                p.setFont(_font(10, QFont.DemiBold))
                txt = "Recovered changes"
                bw = QFontMetrics(_font(10, QFont.DemiBold)).horizontalAdvance(txt) + 14
                b = QRectF(tr.left() + 8, tr.top() + 8, bw, 18)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#3A2E12"))
                p.drawRoundedRect(b, 4, 4)
                p.setPen(QColor("#F5C060"))
                p.drawText(b, Qt.AlignCenter, txt)
        p.restore()

        x, textw = r.x() + 12, r.width() - 24 - 28
        f = _font(13, QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(COL["muted"] if self.info.missing else COL["text"]))
        if self.editor is None:
            p.drawText(QRectF(x, tr.bottom() + 10, textw, 20), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(f).elidedText(self.info.name, Qt.ElideRight, int(textw)))
        f2 = _font(11)
        p.setFont(f2)
        p.setPen(QColor(COL["sub"]))
        p.drawText(QRectF(x, tr.bottom() + 34, r.width() - 24, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(f2).elidedText(self.meta_text(), Qt.ElideMiddle, int(r.width() - 24)))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(COL["edge_hover"] if self.hover else COL["edge"]), 1))
        p.drawPath(outer)
        self.paint_ring(p)
        p.end()

    # ---- inline rename
    def start_rename(self):
        if self.info.missing or self.editor is not None:
            return
        tr = self.thumb_rect()
        e = QLineEdit(self.info.name, self)
        e.setObjectName("cardNameEdit")
        e.setGeometry(int(self.card_rect().x() + 5), int(tr.bottom() + 8), int(self.card_rect().width() - 10), 24)
        e.setSelection(len(self.info.name), -len(self.info.name))   # all selected, the start of the name in view
        e.show()
        e.setFocus(Qt.OtherFocusReason)
        self.editor = e
        e.returnPressed.connect(lambda: self.end_rename(True))
        e.installEventFilter(self)
        self.update()

    def end_rename(self, commit):
        e = self.editor
        if e is None:
            return
        self.editor = None
        text = e.text().strip()
        e.hide()
        e.deleteLater()
        self.setFocus(Qt.OtherFocusReason)
        self.update()
        if commit and text and text != self.info.name:
            self.renameCommitted.emit(self, text)

    def eventFilter(self, obj, ev):
        if obj is self.editor:
            if ev.type() == ev.Type.KeyPress and ev.key() == Qt.Key_Escape:
                self.end_rename(False)
                return True
            if ev.type() == ev.Type.FocusOut:
                QTimer.singleShot(0, lambda: self.end_rename(False))
        return super().eventFilter(obj, ev)

    # ---- input
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.down = True
            self.update()
        self.setFocus(Qt.MouseFocusReason)

    def mouseReleaseEvent(self, ev):
        was = self.down
        self.down = False
        self.update()
        if ev.button() == Qt.LeftButton and was and self.rect().contains(ev.position().toPoint()):
            self.activated.emit(self)            # a single click opens (like Clipchamp / CapCut)

    def contextMenuEvent(self, ev):
        self.setFocus(Qt.MouseFocusReason)
        self.menuRequested.emit(self, ev.globalPos())

    def keyPressEvent(self, ev):
        k = ev.key()
        if k in (Qt.Key_Return, Qt.Key_Enter):
            self.activated.emit(self)
        elif k == Qt.Key_F2:
            self.renameRequested.emit(self)
        elif k == Qt.Key_Delete:
            self.deleteRequested.emit(self)
        elif k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_Home, Qt.Key_End):
            self.navigate.emit(self, k)
        elif k == Qt.Key_Menu or (k == Qt.Key_F10 and ev.modifiers() & Qt.ShiftModifier):
            self.menuRequested.emit(self, self.mapToGlobal(self.thumb_rect().center().toPoint()))
        else:
            super().keyPressEvent(ev)


class NewCard(_Tile):
    """The 'New project' tile that starts the grid."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setToolTip("Start a new video project  (Ctrl+N)")
        self.setAccessibleName("New project")
        self.canvas_text = "Vertical 9:16 · change it any time"

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.card_rect().adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QColor("#17181B"))
        if self.hover:
            pen = QPen(QColor(COL["blue"]), 1)
        else:
            pen = QPen(QColor("#3A3B42"), 1, Qt.CustomDashLine)
            pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.drawRoundedRect(r, 8, 8)
        tr = self.thumb_rect()
        c = QPointF(tr.center().x(), tr.center().y() + 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(COL["blue_hover"] if self.hover else COL["blue"]))
        p.drawEllipse(c, 20, 20)
        p.setPen(QPen(QColor("#FFFFFF"), 2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(c.x() - 7, c.y()), QPointF(c.x() + 7, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - 7), QPointF(c.x(), c.y() + 7))
        p.setFont(_font(13, QFont.DemiBold))
        p.setPen(QColor(COL["text"]))
        p.drawText(QRectF(r.x(), tr.bottom() + 10, r.width(), 20), Qt.AlignHCenter | Qt.AlignVCenter, "New project")
        p.setFont(_font(11))
        p.setPen(QColor(COL["sub"]))
        p.drawText(QRectF(r.x() + 8, tr.bottom() + 34, r.width() - 16, 16), Qt.AlignHCenter | Qt.AlignVCenter,
                   QFontMetrics(_font(11)).elidedText(self.canvas_text, Qt.ElideRight, int(r.width() - 16)))
        self.paint_ring(p)
        p.end()

    def mousePressEvent(self, ev):
        self.setFocus(Qt.MouseFocusReason)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.clicked.emit()
        elif ev.key() in (Qt.Key_Right, Qt.Key_Down) and isinstance(self.parent(), ProjectGrid):
            self.parent().focus_index(0)
        else:
            super().keyPressEvent(ev)


class ProjectGrid(QWidget):
    """Lays the tiles out in as many columns as fit (responsive); its height follows the content."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.new_card = NewCard(self)
        self.cards = []
        self.cols = 1
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_cards(self, cards):
        for c in self.cards:
            c.hide()
            c.deleteLater()
        self.cards = cards
        for c in cards:
            c.setParent(self)
            c.show()
        self.relayout()

    def tiles(self):
        return [self.new_card] + self.cards

    def relayout(self):
        avail = max(CARD_LO, self.width() - 2 * RING)
        cols = max(2, (avail + GAP_X) // (CARD_MIN + GAP_X))
        cw = (avail - GAP_X * (cols - 1)) / cols
        if cw < CARD_LO:
            cols = max(1, (avail + GAP_X) // (CARD_LO + GAP_X))
            cw = (avail - GAP_X * (cols - 1)) / cols
        cw = int(min(CARD_MAX, cw))
        self.cols = cols
        ch = round(cw * 9 / 16) + META_H
        for i, c in enumerate(self.tiles()):
            r, k = divmod(i, cols)
            c.setGeometry(int(k * (cw + GAP_X)), int(r * (ch + GAP_Y)), cw + 2 * RING, ch + 2 * RING)
        rows = (len(self.tiles()) + cols - 1) // cols
        self.setFixedHeight(rows * ch + (rows - 1) * GAP_Y + 2 * RING)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.relayout()

    def focus_index(self, i):
        if self.cards:
            self.cards[max(0, min(len(self.cards) - 1, i))].setFocus(Qt.TabFocusReason)

    def move_from(self, card, key):
        pos = self.cards.index(card) + 1                   # index among tiles (the New tile is 0)
        step = {Qt.Key_Left: -1, Qt.Key_Right: 1, Qt.Key_Up: -self.cols, Qt.Key_Down: self.cols}.get(key)
        if key == Qt.Key_Home:
            self.focus_index(0)
        elif key == Qt.Key_End:
            self.focus_index(len(self.cards) - 1)
        elif step:
            j = pos + step
            if j <= 0 and pos + step > -self.cols:
                self.new_card.setFocus(Qt.TabFocusReason)
            elif 1 <= j <= len(self.cards):
                self.focus_index(j - 1)


class Dashboard(QWidget):
    openRequested = Signal(str)            # open this .clipproj
    newRequested = Signal(str)             # create a project with this name, open it and start renaming it
    restoreRequested = Signal(str)         # open this project and restore its recovered changes
    projectsDirChanged = Signal(str)

    def __init__(self, parent=None, brand=None, icon=None, dialogs=None):
        """brand: the logo widget for the header; icon(name, colour) -> QIcon; dialogs: module with QMessageBox,
        QFileDialog, QInputDialog and QMenu (the app's own, so they can be swapped in one place)."""
        super().__init__(parent)
        self.setObjectName("dashboard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.dlg = dialogs or QtWidgets
        self.icon = icon or (lambda name, color="#C9CBD1": QIcon())
        self.selected_path = None
        self.infos = []
        self.recover_path = None

        # header: same height, margins and logo as the editor's, so switching pages doesn't shift anything
        bar = QWidget()
        bar.setObjectName("header")
        bar.setFixedHeight(44)
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(12, 6, 12, 6)
        hb.setSpacing(8)
        if brand is None:
            brand = QLabel(store.APP_NAME)
            brand.setObjectName("title")
        hb.addWidget(brand)
        hb.addStretch(1)
        self.b_open = self._button("Open project…", "ghost", "Find a .clipproj file anywhere on your computer  (Ctrl+O)",
                                   self.open_existing, "folder", "#C9CBD1")
        hb.addWidget(self.b_open)
        self.b_new = self._button("New project", "primary", "Start a new video project  (Ctrl+N)", self.new_project,
                                  "plus", "#FFFFFF")
        hb.addWidget(self.b_new)
        self.top_bar = bar

        # page content, at most CONTENT_MAX wide and centred
        content = QWidget()
        content.setObjectName("dashContent")
        content.setMaximumWidth(CONTENT_MAX)
        cl = QVBoxLayout(content)
        cl.setContentsMargins(32 - RING, 32, 32 - RING, 32)   # the cards keep RING px for their focus ring ...
        cl.setSpacing(0)
        self.content_layout = cl
        head = QWidget()                                       # ... so everything else is inset by RING to line up
        hv = QVBoxLayout(head)
        hv.setContentsMargins(RING, 0, RING, 0)
        hv.setSpacing(0)
        cl.addWidget(head)
        tr = QHBoxLayout()
        title = QLabel("Your projects")
        title.setObjectName("pageTitle")
        tr.addWidget(title, 0, Qt.AlignBottom)
        tr.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("hint")
        tr.addWidget(self.lbl_count, 0, Qt.AlignBottom)
        hv.addLayout(tr)
        hv.addSpacing(8)
        loc = QHBoxLayout()
        loc.setSpacing(0)
        fi = QLabel()
        fi.setPixmap(self.icon("folder", "#767880").pixmap(16, 16))
        loc.addWidget(fi)
        loc.addSpacing(6)
        self.lbl_dir = QLabel()
        self.lbl_dir.setObjectName("projPath")
        self.lbl_dir.setMinimumWidth(60)
        loc.addWidget(self.lbl_dir, 0)
        loc.addSpacing(12)
        self.b_dir = self._button("Change…", "ghost", "Choose where new projects are kept", self.change_projects_dir)
        loc.addWidget(self.b_dir)
        self.b_show_dir = self._button("Open folder", "ghost", "Show the projects folder in your file manager",
                                       lambda: reveal_in_file_manager(self.projects_dir()))
        loc.addWidget(self.b_show_dir)
        loc.addStretch(1)
        hv.addLayout(loc)
        hv.addSpacing(24)

        # recovery banner
        self.banner = QFrame()
        self.banner.setObjectName("recoveryBanner")
        self.banner.setFixedHeight(44)
        rb = QHBoxLayout(self.banner)
        rb.setContentsMargins(12, 0, 8, 0)
        rb.setSpacing(8)
        wi = QLabel()
        wi.setPixmap(self.icon("warn", COL["warn"]).pixmap(16, 16))
        rb.addWidget(wi)
        self.banner_text = QLabel()
        self.banner_text.setObjectName("bannerText")
        self.banner_text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # elided, never widens the window
        self.banner_full = ""
        self.banner_text.installEventFilter(self)
        rb.addWidget(self.banner_text, 1)
        self.banner_discard = self._button("Discard", "ghost", "Throw the recovered changes away", self.discard_banner)
        rb.addWidget(self.banner_discard)
        self.banner_restore = self._button("Restore", "primary", "Open the project with the recovered changes",
                                           self.restore_banner)
        rb.addWidget(self.banner_restore)
        self.banner_wrap = QWidget()
        bw = QVBoxLayout(self.banner_wrap)
        bw.setContentsMargins(0, 0, 0, 20)
        bw.addWidget(self.banner)
        self.banner_wrap.hide()
        hv.addWidget(self.banner_wrap)

        self.grid = ProjectGrid()
        self.grid.new_card.clicked.connect(self.new_project)
        cl.addWidget(self.grid)

        # empty state
        self.empty = QWidget()
        self.empty.setObjectName("emptyState")
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(RING, 24, RING, 0)
        el.setSpacing(8)
        el.addStretch(1)
        art = QLabel()
        art.setPixmap(self._empty_art())
        art.setAlignment(Qt.AlignHCenter)
        el.addWidget(art)
        el.addSpacing(16)
        e1 = QLabel("Start your first video")
        e1.setObjectName("emptyTitle")
        e1.setAlignment(Qt.AlignHCenter)
        el.addWidget(e1)
        e2 = QLabel("Projects are saved automatically in your projects folder.")
        e2.setObjectName("emptyBody")
        e2.setAlignment(Qt.AlignHCenter)
        e2.setWordWrap(False)
        el.addWidget(e2, 0, Qt.AlignHCenter)
        el.addSpacing(16)
        self.empty_new = self._button("New project", "primary", "Start a new video project  (Ctrl+N)",
                                      self.new_project, "plus", "#FFFFFF")
        self.empty_new.setObjectName("primaryLarge")
        self.empty_new.setMinimumWidth(160)
        el.addWidget(self.empty_new, 0, Qt.AlignHCenter)
        self.empty_open = self._button("Open project…", "ghost", "Find a .clipproj file anywhere on your computer",
                                       self.open_existing)
        el.addWidget(self.empty_open, 0, Qt.AlignHCenter)
        el.addStretch(2)
        cl.addWidget(self.empty, 1)
        cl.addStretch(0)

        holder = QWidget()
        holder.setObjectName("dashBody")
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(content, 1)
        self.content = content
        sa = QScrollArea()
        sa.setObjectName("dashScroll")
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(holder)
        self.scroll = sa

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(bar)
        v.addWidget(sa, 1)
        self.refresh()

    def _button(self, text, name, tip, fn, icon=None, color="#C9CBD1"):
        b = QPushButton(text)
        b.setObjectName(name)
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda *_: fn())
        if icon:
            b.setIcon(self.icon(icon, color))
            b.setIconSize(QSize(16, 16))
        return b

    # ------------------------------------------------------------------ dialogs
    def ask_text(self, title, label, text=""):
        t, ok = self.dlg.QInputDialog.getText(self, title, label, QLineEdit.Normal, text)
        return t.strip() if ok and t and t.strip() else None

    def confirm(self, title, text, info="", ok_text="OK", danger=False):
        MB = self.dlg.QMessageBox
        m = MB(self)
        m.setIcon(MB.Warning if danger else MB.Question)
        m.setWindowTitle(title)
        m.setText(text)
        if info:
            m.setInformativeText(info)
        ok = m.addButton(ok_text, MB.AcceptRole)
        if danger:
            ok.setObjectName("danger")
            ok.style().unpolish(ok)
            ok.style().polish(ok)
        cancel = m.addButton("Cancel", MB.RejectRole)
        m.setDefaultButton(cancel if danger else ok)
        m.exec()
        return m.clickedButton() is ok

    def warn(self, title, text):
        self.dlg.QMessageBox.warning(self, title, text)

    def pick_file(self):
        p, _ = self.dlg.QFileDialog.getOpenFileName(self, "Open project", self.projects_dir(),
                                                    "Parrot Studio project (*.clipproj)")
        return p or None

    def pick_dir(self):
        p = self.dlg.QFileDialog.getExistingDirectory(self, "Choose the projects folder", self.projects_dir())
        return p or None

    # ------------------------------------------------------------------ data
    def projects_dir(self):
        return store.get_projects_dir()

    def refresh(self):
        """Re-read the projects from disk and rebuild the cards (keeps the selection)."""
        self._elide_dir()
        try:
            self.infos = store.list_projects()
        except Exception as e:                       # listing must never take the home screen down
            store.report_problem("Could not list projects", e)
            self.infos = []
        cards = []
        for info in self.infos:
            c = ProjectCard(info)
            c.activated.connect(lambda card: self.open_card(card.info))
            c.menuRequested.connect(self.card_menu)
            c.navigate.connect(self.grid.move_from)
            c.renameRequested.connect(lambda card: card.start_rename())
            c.renameCommitted.connect(lambda card, name: self.rename(card.info, name))
            c.deleteRequested.connect(lambda card: self.delete(card.info))
            cards.append(c)
        self.grid.set_cards(cards)
        has = bool(self.infos)
        self.grid.setVisible(has)
        self.empty.setVisible(not has)
        n = len([i for i in self.infos if not i.missing])
        self.lbl_count.setText(f"{n} project{'s' if n != 1 else ''}" if has else "")
        rec = next((i for i in self.infos if i.has_recovery and not i.missing), None)
        self.recover_path = rec.path if rec else None
        if rec:
            info = None
            try:
                info = store.find_recovery(rec.path)
            except Exception as e:
                store.report_problem(f"Could not read the recovered changes of {rec.path}", e)
            when = ""
            if info is not None:
                import time
                when = " from " + time.strftime("%H:%M" if time.time() - info.saved_at < 86400 else "%d %b %H:%M",
                                                time.localtime(info.saved_at)).lstrip("0")
            self.banner_full = f"“{rec.name}” wasn't closed properly. Recovered changes{when} are available."
            self.banner_text.setText(self.banner_full)
            self.banner_text.setToolTip(f"{self.banner_full}\n{rec.path}")
            self._elide_banner()
        self.banner_wrap.setVisible(rec is not None)

    def cards(self):
        return list(self.grid.cards)

    def card_for(self, path):
        return next((c for c in self.grid.cards if os.path.normcase(c.info.path) == os.path.normcase(path)), None)

    def focus_first(self):
        """Give the keyboard a sensible start without arming a button (Space must never create a project)."""
        c = self.card_for(self.selected_path) if self.selected_path else None
        if c is None and self.grid.cards:
            c = self.grid.cards[0]
        if c is not None:
            c.setFocus(Qt.OtherFocusReason)
        else:
            self.scroll.setFocus(Qt.OtherFocusReason)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        m = 24 if self.width() < 1100 else 32
        self.content_layout.setContentsMargins(m - RING, 32, m - RING, 32)
        self.content.setMaximumWidth(1640 if self.width() >= 2200 else CONTENT_MAX)
        self._elide_dir()
        self._elide_banner()

    def eventFilter(self, obj, ev):
        if obj is self.banner_text and ev.type() == ev.Type.Resize:
            self._elide_banner()
        return super().eventFilter(obj, ev)

    def _elide_banner(self):
        w = self.banner_text.width()
        if w > 20:
            self.banner_text.setText(self.banner_text.fontMetrics().elidedText(self.banner_full, Qt.ElideRight, w))

    def _elide_dir(self):
        d = self.projects_dir()
        avail = max(80, min(self.content.width(), CONTENT_MAX) - 64 - 16 - 6 - 12 - 200)
        self.lbl_dir.setText(self.lbl_dir.fontMetrics().elidedText(d, Qt.ElideMiddle, avail))
        self.lbl_dir.setToolTip(d)

    def _empty_art(self):
        dpr = 2
        pm = QPixmap(QSize(56 * dpr, 56 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#3A3B42"), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(1, 1, 54, 54), 14, 14)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(127, 229, 255, 102))
        p.drawPolygon(QPolygonF([QPointF(21, 16), QPointF(39, 26.5), QPointF(34, 33.5), QPointF(31.5, 30.5),
                                 QPointF(21, 40)]))
        p.end()
        return pm

    # ------------------------------------------------------------------ actions
    def suggest_name(self, base="Untitled video"):
        try:
            return store.unique_name(self.projects_dir(), base)
        except Exception:
            return base

    def new_project(self):
        """Create straight away with a default name; the editor then lets the user type a better one."""
        self.newRequested.emit(self.suggest_name())

    def open_existing(self):
        p = self.pick_file()
        if p:
            self.openRequested.emit(p)

    def open_card(self, info):
        if info.missing or not os.path.isfile(info.path):
            self.locate(info)
            return
        self.selected_path = info.path
        self.openRequested.emit(info.path)

    def locate(self, info):
        p = self.pick_file()
        if not p:
            return
        store.remove_recent(info.path)
        store.add_recent(p)
        self.refresh()

    def restore_banner(self):
        if self.recover_path:
            self.restoreRequested.emit(self.recover_path)

    def discard_banner(self):
        p = self.recover_path
        if not p:
            return
        if not self.confirm("Discard recovered changes?", f"Throw away the recovered changes of "
                            f"“{store.project_name(p)}”?", "The last saved version of the project is kept.",
                            "Discard", danger=True):
            return
        try:
            while True:
                info = store.find_recovery(p)
                if info is None:
                    break
                store.discard_recovery(p, info)
        except Exception as e:
            store.report_problem(f"Could not discard the recovered changes of {p}", e)
        self.refresh()

    def card_menu(self, card, gpos):
        info = card.info
        m = self.dlg.QMenu(self)
        if info.missing:
            m.addAction("Locate…", lambda: self.locate(info))
            m.addAction("Remove from list", lambda: self.forget(info))
        else:
            m.addAction("Open", lambda: self.open_card(info))
            a = m.addAction("Rename…", card.start_rename)
            a.setShortcut("F2")
            a.setShortcutContext(Qt.WidgetShortcut)
            a.setShortcutVisibleInContextMenu(True)
            m.addAction("Show in folder", lambda: reveal_in_file_manager(info.path))
            m.addSeparator()
            if not info.managed:
                m.addAction("Remove from list", lambda: self.forget(info))
            d = m.addAction(self.icon("trash", "#F07A7E"), "Delete…", lambda: self.delete(info))
            d.setShortcut("Del")
            d.setShortcutContext(Qt.WidgetShortcut)
            d.setShortcutVisibleInContextMenu(True)
        m.exec(gpos)
        m.deleteLater()

    def forget(self, info):
        store.remove_recent(info.path)
        self.refresh()

    def rename(self, info, name=None):
        if info.missing:
            return None
        if name is None:
            name = self.ask_text("Rename project", "New name:", info.name)
        if not name or name == info.name:
            return None
        try:
            new = store.rename_project(info.path, name)
        except store.ProjectError as e:
            self.warn("Could not rename", str(e))
            return None
        self.selected_path = new
        self.refresh()
        c = self.card_for(new)
        if c:
            c.setFocus(Qt.OtherFocusReason)
        return new

    def delete(self, info):
        if info.missing:
            self.forget(info)
            return False
        what = "Its project folder" if info.managed else "The project file"
        if not self.confirm("Delete project?", f"“{info.name}” will be moved to the {BIN}.",
                            f"{what} can be restored from there. Your imported media files are not touched.",
                            "Delete", danger=True):
            return False
        idx = next((i for i, c in enumerate(self.grid.cards) if c.info.path == info.path), 0)
        try:
            left = store.delete_project(info.path, trash=move_to_trash)
        except store.ProjectError:
            if not self.confirm("Delete permanently?", f"“{info.name}” couldn't be moved to the {BIN}.",
                                "Delete it permanently instead? This can't be undone. Your imported media files "
                                "are not touched.", "Delete permanently", danger=True):
                self.refresh()
                return False
            try:
                left = store.delete_project(info.path)
            except store.ProjectError as e2:
                self.warn("Could not delete", str(e2))
                self.refresh()
                return False
        if left:
            self.warn("Project deleted", "The project was deleted. Files you added to its folder yourself were "
                      "kept:\n\n" + "\n".join(os.path.basename(x) for x in left[:10]))
        self.selected_path = None
        self.refresh()
        if self.grid.cards:
            self.grid.focus_index(min(idx, len(self.grid.cards) - 1))
        return True

    def change_projects_dir(self):
        p = self.pick_dir()
        if not p:
            return
        try:
            p = store.set_projects_dir(p)
        except store.ProjectError as e:
            self.warn("Can't use that folder", str(e))
            return
        self.refresh()
        self.projectsDirChanged.emit(p)
