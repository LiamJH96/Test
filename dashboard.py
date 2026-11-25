"""Parrot Studio project dashboard: the home screen listing projects as cards.

The dashboard manages projects on disk itself (rename, delete, recents, projects folder) through project_store and
asks the main window to open things through signals; it never touches the editor.
Every dialog goes through a small method (ask_text, confirm, pick_file, pick_dir, warn) so tests can replace them.
"""
import os
import subprocess
import sys
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

import project_store as store

BRAND = "#7FE5FF"
CARD_MIN, CARD_MAX, GAP, TEXT_H = 220, 300, 20, 58

COL = dict(card="#1F2024", card_hover="#25262B", edge="#2A2B30", edge_hover="#3A3B42", sel="#3A78E0",
           thumb="#141518", text="#E8E9EC", sub="#8A8C94", dim="#55575F", bad="#F07A7E",
           badge_bg="#3A2E12", badge_fg="#F5C060")


def reveal_in_file_manager(path):
    """Show a file (selected) or folder in Explorer / Finder / the desktop's file manager."""
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


def nice_path(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home + os.sep) else p


def _font(px, weight=QFont.Normal):
    f = QFont()
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def paint_placeholder(p, r, dim=False):
    """Tasteful stand-in for a project without a thumbnail: a quiet gradient and a play mark in the brand cyan."""
    from PySide6.QtGui import QLinearGradient
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    g.setColorAt(0, QColor("#1D252B"))
    g.setColorAt(1, QColor("#16171A"))
    p.fillRect(r, g)
    s = min(r.width(), r.height()) * 0.30
    c = r.center()
    col = QColor(BRAND)
    col.setAlpha(70 if dim else 150)
    p.setPen(QPen(col, 1.6))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(c.x() - s * 0.8, c.y() - s * 0.55, s * 1.6, s * 1.1), 6, 6)
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    t = s * 0.26
    p.drawPolygon(QPolygonF([QPointF(c.x() - t * 0.8, c.y() - t), QPointF(c.x() - t * 0.8, c.y() + t),
                             QPointF(c.x() + t, c.y())]))


class ProjectCard(QWidget):
    """One project: thumbnail on top, name and 'Edited ...' below. Painted by hand for full control of the look."""
    activated = Signal(object)            # double-click / Enter
    menuRequested = Signal(object, object)  # (card, global pos)
    navigate = Signal(object, int)         # (card, Qt key) arrow keys
    renameRequested = Signal(object)
    deleteRequested = Signal(object)
    focused = Signal(object)

    _pix_cache = {}

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.info = info
        self.hover = False
        self.selected = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        tip = info.path if not info.missing else f"{info.path}\n(this file can no longer be found)"
        self.setToolTip(tip)
        self.setAccessibleName(f"Project {info.name}")

    def thumb_rect(self):
        w = self.width()
        return QRectF(0, 0, w, round(w * 9 / 16))

    def more_rect(self):
        tr = self.thumb_rect()
        return QRectF(self.width() - 34, tr.bottom() + 12, 26, 26)

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

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        outer = QPainterPath()
        outer.addRoundedRect(r, 8, 8)
        p.fillPath(outer, QColor(COL["card_hover"] if self.hover else COL["card"]))
        tr = self.thumb_rect()
        p.save()
        p.setClipPath(outer)
        p.fillRect(tr, QColor(COL["thumb"]))
        pm = self.pixmap()
        if pm is not None:
            sz = pm.size().scaled(tr.size().toSize(), Qt.KeepAspectRatio)
            dst = QRectF(tr.x() + (tr.width() - sz.width()) / 2, tr.y() + (tr.height() - sz.height()) / 2,
                         sz.width(), sz.height())
            if self.info.missing:
                p.setOpacity(0.35)
            p.drawPixmap(dst, pm, QRectF(pm.rect()))
            p.setOpacity(1)
        else:
            paint_placeholder(p, tr, self.info.missing)
        p.setPen(QPen(QColor(COL["edge"]), 1))
        p.drawLine(QPointF(0, tr.bottom()), QPointF(self.width(), tr.bottom()))
        p.restore()

        if self.info.has_recovery and not self.info.missing:
            f = _font(10, QFont.DemiBold)
            p.setFont(f)
            txt = "Recovered changes"
            bw = QFontMetrics(f).horizontalAdvance(txt) + 16
            b = QRectF(10, 10, bw, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(COL["badge_bg"]))
            p.drawRoundedRect(b, 10, 10)
            p.setPen(QColor(COL["badge_fg"]))
            p.drawText(b, Qt.AlignCenter, txt)

        x, y0 = 14, tr.bottom() + 11
        textw = self.width() - x - 40
        f = _font(13, QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(COL["dim"] if self.info.missing else COL["text"]))
        p.drawText(QRectF(x, y0, textw, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(f).elidedText(self.info.name, Qt.ElideRight, int(textw)))
        f2 = _font(11)
        p.setFont(f2)
        if self.info.missing:
            p.setPen(QColor(COL["bad"]))
            sub = "File not found"
        else:
            p.setPen(QColor(COL["sub"]))
            sub = store.relative_time(self.info.modified)
            if not self.info.managed:
                sub += "  ·  " + nice_path(self.info.folder)
        p.drawText(QRectF(x, y0 + 20, textw, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(f2).elidedText(sub, Qt.ElideMiddle, int(textw)))

        if self.hover or self.selected:
            mr = self.more_rect()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(COL["sub"]))
            for i in (-5, 0, 5):
                p.drawEllipse(QPointF(mr.center().x() + i, mr.center().y()), 1.6, 1.6)

        if self.selected or self.hasFocus():
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(COL["sel"]), 2))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(COL["edge_hover"] if self.hover else COL["edge"]), 1))
            p.drawPath(outer)
        p.end()

    def enterEvent(self, ev):
        self.hover = True
        self.update()

    def leaveEvent(self, ev):
        self.hover = False
        self.update()

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self.focused.emit(self)
        self.update()

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        self.update()

    def mousePressEvent(self, ev):
        self.setFocus(Qt.MouseFocusReason)
        if ev.button() == Qt.LeftButton and (self.hover or self.selected) and \
                self.more_rect().adjusted(-4, -4, 4, 4).contains(ev.position()):
            self.menuRequested.emit(self, self.mapToGlobal(self.more_rect().bottomLeft().toPoint()))

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton and not self.more_rect().contains(ev.position()):
            self.activated.emit(self)

    def contextMenuEvent(self, ev):
        self.setFocus(Qt.MouseFocusReason)
        self.menuRequested.emit(self, ev.globalPos())

    def keyPressEvent(self, ev):
        k = ev.key()
        if k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.activated.emit(self)
        elif k == Qt.Key_F2:
            self.renameRequested.emit(self)
        elif k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.deleteRequested.emit(self)
        elif k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_Home, Qt.Key_End):
            self.navigate.emit(self, k)
        elif k == Qt.Key_Menu or (k == Qt.Key_F10 and ev.modifiers() & Qt.ShiftModifier):
            self.menuRequested.emit(self, self.mapToGlobal(self.thumb_rect().center().toPoint()))
        else:
            super().keyPressEvent(ev)


class NewCard(QWidget):
    """The 'New project' tile that starts the grid."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Start a new video project  (Ctrl+N)")
        self.setAccessibleName("New project")

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setBrush(QColor("#1C1D21" if not self.hover else "#212227"))
        if self.hasFocus():
            pen = QPen(QColor(COL["sel"]), 2)
        else:
            pen = QPen(QColor("#4A4B53" if self.hover else "#34353B"), 1.2)
            pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.drawRoundedRect(r, 8, 8)
        tr = QRectF(0, 0, self.width(), round(self.width() * 9 / 16))
        c = tr.center()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#3A78E0" if self.hover or self.hasFocus() else "#2A2B30"))
        p.drawEllipse(c, 22, 22)
        p.setPen(QPen(QColor("#FFFFFF" if self.hover or self.hasFocus() else "#C9CBD1"), 2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(c.x() - 8, c.y()), QPointF(c.x() + 8, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - 8), QPointF(c.x(), c.y() + 8))
        p.setFont(_font(13, QFont.DemiBold))
        p.setPen(QColor(COL["text"]))
        p.drawText(QRectF(14, tr.bottom() + 11, self.width() - 28, 18), Qt.AlignLeft | Qt.AlignVCenter, "New project")
        p.setFont(_font(11))
        p.setPen(QColor(COL["sub"]))
        p.drawText(QRectF(14, tr.bottom() + 31, self.width() - 28, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   "Start from a blank timeline")
        p.end()

    def enterEvent(self, ev):
        self.hover = True
        self.update()

    def leaveEvent(self, ev):
        self.hover = False
        self.update()

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self.update()

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
        elif ev.key() in (Qt.Key_Right, Qt.Key_Down) and self.parent() is not None:
            self.parent().focus_index(0)
        else:
            super().keyPressEvent(ev)


class ProjectGrid(QWidget):
    """Lays the cards out in as many columns as fit (responsive); height follows the content."""

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
        avail = max(CARD_MIN, self.width())
        cols = max(1, (avail + GAP) // (CARD_MIN + GAP))
        w = min(CARD_MAX, (avail - GAP * (cols - 1)) / cols)
        if w >= CARD_MAX:          # wide window: more columns of the largest size instead of huge cards
            cols = max(1, (avail + GAP) // (CARD_MAX + GAP))
            w = min(CARD_MAX, (avail - GAP * (cols - 1)) / cols)
        self.cols = cols
        w = int(w)
        h = round(w * 9 / 16) + TEXT_H
        for i, c in enumerate(self.tiles()):
            r, k = divmod(i, cols)
            c.setGeometry(int(k * (w + GAP)), int(r * (h + GAP)), w, h)
        rows = (len(self.tiles()) + cols - 1) // cols
        self.setFixedHeight(rows * h + (rows - 1) * GAP + 4)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.relayout()

    def focus_index(self, i):
        if self.cards:
            i = max(0, min(len(self.cards) - 1, i))
            self.cards[i].setFocus(Qt.TabFocusReason)

    def move_from(self, card, key):
        i = self.cards.index(card)
        pos = i + 1                                   # index among tiles (the New tile is 0)
        step = {Qt.Key_Left: -1, Qt.Key_Right: 1, Qt.Key_Up: -self.cols, Qt.Key_Down: self.cols}.get(key)
        if key == Qt.Key_Home:
            self.focus_index(0)
        elif key == Qt.Key_End:
            self.focus_index(len(self.cards) - 1)
        elif step:
            j = pos + step
            if j <= 0:
                self.new_card.setFocus(Qt.TabFocusReason)
            elif j <= len(self.cards):
                self.focus_index(j - 1)


class Dashboard(QWidget):
    openRequested = Signal(str)          # open this .clipproj
    newRequested = Signal(str)           # create a project with this name and open it
    projectsDirChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dashboard")
        self.selected_path = None

        # top bar: same height, colours and wordmark as the editor header
        bar = QWidget()
        bar.setObjectName("header")
        bar.setFixedHeight(44)
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(12, 6, 12, 6)
        hb.setSpacing(8)
        mark = QLabel("Parrot Studio")
        mark.setObjectName("title")
        hb.addWidget(mark)
        hb.addStretch(1)
        self.b_open = QPushButton("Open other…")
        self.b_open.setObjectName("ghost")
        self.b_open.setToolTip("Browse for a .clipproj file anywhere on your computer  (Ctrl+O)")
        self.b_open.setCursor(Qt.PointingHandCursor)
        self.b_open.clicked.connect(self.open_existing)
        hb.addWidget(self.b_open)
        self.b_new = QPushButton("New project")
        self.b_new.setObjectName("primary")
        self.b_new.setToolTip("Start a new video project  (Ctrl+N)")
        self.b_new.setCursor(Qt.PointingHandCursor)
        self.b_new.clicked.connect(self.new_project)
        hb.addWidget(self.b_new)
        self.top_bar = bar

        # body
        body = QWidget()
        body.setObjectName("dashBody")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(40, 28, 40, 32)
        bl.setSpacing(0)
        title = QLabel("Your projects")
        title.setObjectName("dashTitle")
        bl.addWidget(title)
        bl.addSpacing(6)
        pr = QHBoxLayout()
        pr.setSpacing(6)
        lab = QLabel("Projects folder")
        lab.setObjectName("dashMeta")
        pr.addWidget(lab)
        self.lbl_dir = QLabel()
        self.lbl_dir.setObjectName("dashPath")
        self.lbl_dir.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pr.addWidget(self.lbl_dir)
        self.b_dir = QPushButton("Change…")
        self.b_dir.setObjectName("link")
        self.b_dir.setToolTip("Choose where new projects are created")
        self.b_dir.setCursor(Qt.PointingHandCursor)
        self.b_dir.clicked.connect(self.change_projects_dir)
        pr.addWidget(self.b_dir)
        self.b_show_dir = QPushButton("Show")
        self.b_show_dir.setObjectName("link")
        self.b_show_dir.setToolTip("Open the projects folder in your file manager")
        self.b_show_dir.setCursor(Qt.PointingHandCursor)
        self.b_show_dir.clicked.connect(lambda: reveal_in_file_manager(self.projects_dir()))
        pr.addWidget(self.b_show_dir)
        pr.addStretch(1)
        bl.addLayout(pr)
        bl.addSpacing(18)

        self.banner = QFrame()
        self.banner.setObjectName("recoveryBanner")
        rb = QHBoxLayout(self.banner)
        rb.setContentsMargins(14, 8, 8, 8)
        self.banner_text = QLabel()
        self.banner_text.setObjectName("bannerText")
        self.banner_text.setWordWrap(True)
        rb.addWidget(self.banner_text, 1)
        self.banner_btn = QPushButton("Open and review")
        self.banner_btn.setCursor(Qt.PointingHandCursor)
        self.banner_btn.clicked.connect(self._open_banner_project)
        rb.addWidget(self.banner_btn)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 18)
        wl.addWidget(self.banner)
        self.banner_wrap = wrap
        wrap.hide()
        bl.addWidget(wrap)

        self.grid = ProjectGrid()
        self.grid.new_card.clicked.connect(self.new_project)
        bl.addWidget(self.grid)

        self.empty = QWidget()
        self.empty.setObjectName("emptyState")
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(0, 48, 0, 0)
        el.setSpacing(10)
        el.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        art = QLabel()
        art.setPixmap(self._empty_art())
        art.setAlignment(Qt.AlignHCenter)
        el.addWidget(art)
        e1 = QLabel("No projects yet")
        e1.setObjectName("emptyTitle")
        e1.setAlignment(Qt.AlignHCenter)
        el.addWidget(e1)
        e2 = QLabel("Create a project to start editing. Your work is saved automatically as you go.")
        e2.setObjectName("hint")
        e2.setAlignment(Qt.AlignHCenter)
        el.addWidget(e2)
        el.addSpacing(8)
        er = QHBoxLayout()
        er.setSpacing(8)
        er.addStretch(1)
        b1 = QPushButton("New project")
        b1.setObjectName("primary")
        b1.setCursor(Qt.PointingHandCursor)
        b1.clicked.connect(self.new_project)
        b1.setMinimumWidth(120)
        er.addWidget(b1)
        b2 = QPushButton("Open other…")
        b2.setCursor(Qt.PointingHandCursor)
        b2.clicked.connect(self.open_existing)
        er.addWidget(b2)
        er.addStretch(1)
        el.addLayout(er)
        self.empty_new = b1
        bl.addWidget(self.empty)
        bl.addStretch(1)

        sa = QScrollArea()
        sa.setObjectName("dashScroll")
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(body)
        self.scroll = sa

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(bar)
        v.addWidget(sa, 1)
        self.infos = []
        self.refresh()

    # ------------------------------------------------------------------ dialogs (replaceable in tests)
    def ask_text(self, title, label, text=""):
        t, ok = QInputDialog.getText(self, title, label, QLineEdit.Normal, text)
        return t.strip() if ok and t.strip() else None

    def confirm(self, title, text, info="", ok_text="OK", danger=False):
        m = QMessageBox(self)
        m.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
        m.setWindowTitle(title)
        m.setText(text)
        if info:
            m.setInformativeText(info)
        ok = m.addButton(ok_text, QMessageBox.AcceptRole)
        if danger:
            ok.setObjectName("danger")
        cancel = m.addButton("Cancel", QMessageBox.RejectRole)
        m.setDefaultButton(cancel if danger else ok)
        m.exec()
        return m.clickedButton() is ok

    def warn(self, title, text):
        QMessageBox.warning(self, title, text)

    def pick_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open a project", self.projects_dir(),
                                           "Parrot Studio project (*.clipproj)")
        return p or None

    def pick_dir(self):
        p = QFileDialog.getExistingDirectory(self, "Choose the projects folder", self.projects_dir())
        return p or None

    # ------------------------------------------------------------------ data
    def projects_dir(self):
        return store.get_projects_dir()

    def refresh(self):
        """Re-read the projects from disk and rebuild the cards (keeps the selection)."""
        self.lbl_dir.setText(nice_path(self.projects_dir()))
        self.lbl_dir.setToolTip(self.projects_dir())
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
            c.renameRequested.connect(lambda card: self.rename(card.info))
            c.deleteRequested.connect(lambda card: self.delete(card.info))
            c.focused.connect(self._select_card)
            c.selected = info.path == self.selected_path
            cards.append(c)
        self.grid.set_cards(cards)
        has = bool(self.infos)
        self.grid.setVisible(has)
        self.empty.setVisible(not has)
        rec = [i for i in self.infos if i.has_recovery and not i.missing]
        if rec:
            names = ", ".join(f"“{i.name}”" for i in rec[:3]) + (" and others" if len(rec) > 3 else "")
            self.banner_text.setText(f"Unsaved changes were recovered for {names}. Open the project to restore or "
                                     "discard them.")
            self.banner_path = rec[0].path
            self.banner_wrap.show()
        else:
            self.banner_wrap.hide()

    def cards(self):
        return list(self.grid.cards)

    def card_for(self, path):
        return next((c for c in self.grid.cards if os.path.normcase(c.info.path) == os.path.normcase(path)), None)

    def _select_card(self, card):
        self.selected_path = card.info.path
        for c in self.grid.cards:
            if c.selected != (c is card):
                c.selected = c is card
                c.update()

    def focus_first(self):
        if self.grid.cards:
            c = self.card_for(self.selected_path) if self.selected_path else None
            (c or self.grid.cards[0]).setFocus(Qt.OtherFocusReason)
        elif self.empty.isVisible():
            self.empty_new.setFocus(Qt.OtherFocusReason)

    def _empty_art(self):
        dpr = 2
        pm = QPixmap(QSize(150 * dpr, 96 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        for i, (x, y, a) in enumerate([(18, 18, 90), (34, 8, 255)]):
            r = QRectF(x, y, 98, 66)
            p.setPen(QPen(QColor("#34353B"), 1))
            p.setBrush(QColor("#1F2024"))
            p.drawRoundedRect(r, 8, 8)
            if i == 1:
                p.save()
                path = QPainterPath()
                path.addRoundedRect(r.adjusted(1, 1, -1, -1), 7, 7)
                p.setClipPath(path)
                paint_placeholder(p, r)
                p.restore()
        p.end()
        return pm

    # ------------------------------------------------------------------ actions
    def new_project(self):
        name = self.ask_text("New project", "Name your project:", self.suggest_name())
        if name:
            self.newRequested.emit(name)

    def suggest_name(self):
        base = "My video"
        try:
            return store.unique_name(self.projects_dir(), base)
        except Exception:
            return base

    def open_existing(self):
        p = self.pick_file()
        if p:
            self.openRequested.emit(p)

    def open_card(self, info):
        if info.missing or not os.path.isfile(info.path):
            if self.confirm("Project not found", f"“{info.name}” can no longer be found at\n{info.path}",
                            "It may have been moved, renamed or deleted. Remove it from this list?",
                            "Remove from list"):
                store.remove_recent(info.path)
                self.refresh()
            return
        self.openRequested.emit(info.path)

    def _open_banner_project(self):
        p = getattr(self, "banner_path", None)
        if p:
            self.openRequested.emit(p)

    def card_menu(self, card, gpos):
        info = card.info
        m = QMenu(self)
        if info.missing:
            m.addAction("Remove from list", lambda: self.forget(info))
        else:
            m.addAction("Open", lambda: self.open_card(info))
            m.addAction("Rename…\tF2", lambda: self.rename(info))
            m.addAction("Show in folder", lambda: reveal_in_file_manager(info.path))
            m.addSeparator()
            if not info.managed:
                m.addAction("Remove from list", lambda: self.forget(info))
            m.addAction("Delete…\tDel", lambda: self.delete(info))
        m.exec(gpos)

    def forget(self, info):
        store.remove_recent(info.path)
        self.refresh()

    def rename(self, info):
        if info.missing:
            return
        name = self.ask_text("Rename project", "New name:", info.name)
        if not name or name == info.name:
            return
        try:
            new = store.rename_project(info.path, name)
        except store.ProjectError as e:
            self.warn("Could not rename", str(e))
            return
        self.selected_path = new
        self.refresh()
        c = self.card_for(new)
        if c:
            c.setFocus(Qt.OtherFocusReason)

    def delete(self, info):
        if info.missing:
            self.forget(info)
            return
        what = ("the project folder" if info.managed else "the project file")
        if not self.confirm("Delete project", f"Delete “{info.name}”?",
                            f"This permanently deletes {what}. The video, audio and picture files you imported "
                            "are not touched.", "Delete", danger=True):
            return
        idx = next((i for i, c in enumerate(self.grid.cards) if c.info.path == info.path), 0)
        try:
            left = store.delete_project(info.path)
        except store.ProjectError as e:
            self.warn("Could not delete", str(e))
            self.refresh()
            return
        if left:
            self.warn("Project deleted", "The project was deleted. Some files you added to its folder were kept:\n\n"
                      + "\n".join(os.path.basename(x) for x in left[:10]))
        self.selected_path = None
        self.refresh()
        if self.grid.cards:
            self.grid.focus_index(min(idx, len(self.grid.cards) - 1))

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
