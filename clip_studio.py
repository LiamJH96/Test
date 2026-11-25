"""Parrot Studio - a free Clipchamp-style multi-track editor (PySide6 + ffmpeg).

Run:  python clip_studio.py
Needs: pip install PySide6 numpy imageio-ffmpeg   (ffmpeg on PATH or imageio-ffmpeg)
Projects, autosave and crash recovery live in project_store.py; the project home screen in dashboard.py.
"""
import sys, os, re, json, base64, hashlib, copy, math, shutil, subprocess, threading, time, tempfile, uuid

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, QRectF, QPointF, QPoint, QRect, QSizeF, QMimeData, Signal, QObject
from PySide6.QtGui import (QDesktopServices, QFontDatabase, QImage, QFontMetrics, QFontMetricsF, QAction, QColor, QPainter, QPen, QBrush, QPalette, QPixmap, QIcon, QFont,
                           QKeySequence, QShortcut, QDrag, QPolygonF, QPainterPath, QTransform)
from PySide6.QtWidgets import (QColorDialog, QPlainTextEdit, QFontComboBox, QFrame, QToolTip, QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QListWidget,
                               QListWidgetItem, QPushButton, QLabel, QDoubleSpinBox, QSpinBox, QComboBox,
                               QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsItem, QGraphicsPixmapItem, QGraphicsPathItem, QGraphicsSimpleTextItem, QSlider, QTabWidget, QProgressBar,
                               QSplitter, QScrollArea, QFileDialog, QMessageBox, QScrollBar, QMenu, QGroupBox,
                               QFormLayout, QCheckBox, QProgressDialog, QAbstractSpinBox, QLineEdit, QInputDialog,
                               QGridLayout, QStyledItemDelegate, QProxyStyle, QStyle, QStackedWidget, QSizePolicy)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem

try:
    import numpy as np
except Exception:  # waveform is optional
    np = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # sibling modules, wherever we are started from
import project_store as store  # noqa: E402
from dashboard import Dashboard, reveal_in_file_manager  # noqa: E402

# ----------------------------------------------------------------------------- ffmpeg helpers


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOWPRI = NOWIN | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)   # background jobs must not slow down Windows
IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}
AUD_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", creationflags=NOWIN)


def probe(path):
    ext = os.path.splitext(path)[1].lower()
    txt = run([FFMPEG, "-hide_banner", "-i", path]).stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", txt)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    m = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})[ ,\[]", txt)
    w, h = (int(m[1]), int(m[2])) if m else (1080, 1920)
    r = re.search(r"rotation of (-?\d+)", txt)
    if r and abs(int(r[1])) in (90, 270):
        w, h = h, w
    hasaudio = bool(re.search(r"Audio:", txt))
    if ext in IMG_EXT:
        kind = "image"
    elif ext in AUD_EXT or "Video:" not in txt:
        kind = "audio"
    else:
        kind = "video"
    m = re.search(r"Video:[^\n]*(smpte2084|arib-std-b67)", txt)
    return dict(kind=kind, dur=dur, w=w, h=h, hasaudio=hasaudio, hdr=bool(m and kind == "video"),
                trc=m[1] if m else "")


_amix_norm = None


def amix_has_normalize():
    global _amix_norm
    if _amix_norm is None:
        _amix_norm = "normalize" in run([FFMPEG, "-hide_banner", "-h", "filter=amix"]).stdout
    return _amix_norm


WAVES = {}  # path -> numpy peaks, 100 bins/sec


class Bus(QObject):
    wave = Signal()
    prog = Signal(float)
    done = Signal(str)
    conv = Signal(float, float)
    call = Signal(object)
    convdone = Signal(str)


BUS = Bus()


_has_zscale = None


def has_zscale():
    global _has_zscale
    if _has_zscale is None:
        _has_zscale = " zscale " in run([FFMPEG, "-hide_banner", "-filters"]).stdout
    return _has_zscale


def hdr_filter(trc):
    tin = "arib-std-b67" if trc == "arib-std-b67" else "smpte2084"
    return (f"zscale=tin={tin}:min=bt2020nc:pin=bt2020:rin=tv:t=linear:npl=100,format=gbrpf32le,"
            "zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def compute_wave(path):
    if np is None or path in WAVES:
        return
    try:
        r = subprocess.run([FFMPEG, "-v", "error", "-threads", "2", "-i", path, "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                           capture_output=True, creationflags=LOWPRI)
        a = np.frombuffer(r.stdout, dtype=np.int16)
        n = len(a) // 80
        if n:
            WAVES[path] = np.abs(a[:n * 80].reshape(n, 80)).max(axis=1) / 32768.0
            BUS.wave.emit()
    except Exception:
        pass


THUMBS = {}  # path -> (interval seconds, [jpg files])
PIX = {}


def compute_thumbs(path, dur):
    if path in THUMBS or dur <= 0:
        return
    try:
        d = tempfile.mkdtemp(prefix="cs_th_")
        iv = max(1.0, dur / 120.0)
        for skip in (["-skip_frame", "nokey"], []):   # key frames only is fast; short / long-GOP clips need all
            subprocess.run([FFMPEG, "-v", "error", "-threads", "2", *skip, "-i", path, "-an", "-vf",
                            f"fps=1/{iv:.3f},scale=-2:80", "-q:v", "6", os.path.join(d, "t%05d.jpg")],
                           creationflags=LOWPRI)
            files = sorted(os.listdir(d))
            if files:
                break
        if files:
            THUMBS[path] = (iv, [os.path.join(d, f) for f in files])
            BUS.wave.emit()
    except Exception:
        pass


def pix_of(f):
    if f not in PIX:
        PIX[f] = QPixmap(f)
    return PIX[f]


def mk_icon(name, color="#e6e6eb", size=22):
    """Crisp vector icons drawn in code (no emoji / font dependence)."""
    pm = QPixmap(size * 3, size * 3)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size * 3 / 22.0, size * 3 / 22.0)
    col = QColor(color)
    p.setPen(QPen(col, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    fill = lambda: (p.setPen(Qt.NoPen), p.setBrush(col))
    P = QPointF
    if name == "play":
        fill()
        p.drawPolygon(QPolygonF([P(7, 4), P(7, 18), P(18, 11)]))
    elif name == "pause":
        fill()
        p.drawRoundedRect(QRectF(5.5, 4, 4, 14), 1, 1)
        p.drawRoundedRect(QRectF(12.5, 4, 4, 14), 1, 1)
    elif name == "start":
        fill()
        p.drawRect(QRectF(4.5, 4, 2.6, 14))
        p.drawPolygon(QPolygonF([P(18, 4), P(18, 18), P(9, 11)]))
    elif name in ("undo", "redo"):
        if name == "redo":
            p.translate(22, 0)
            p.scale(-1, 1)
        path = QPainterPath()
        path.moveTo(8, 8)
        path.cubicTo(12, 4.5, 18.5, 6.5, 18.5, 13)
        p.drawPath(path)
        fill()
        p.drawPolygon(QPolygonF([P(3.5, 8.5), P(9.5, 3.5), P(9.8, 11.5)]))
    elif name == "split":
        p.drawRoundedRect(QRectF(3, 7, 6, 8), 1.5, 1.5)
        p.drawRoundedRect(QRectF(13, 7, 6, 8), 1.5, 1.5)
        p.drawLine(P(11, 2.5), P(11, 19.5))
    elif name == "trash":
        p.drawLine(P(4, 6), P(18, 6))
        p.drawRoundedRect(QRectF(6, 6.5, 10, 11.5), 1.5, 1.5)
        p.drawLine(P(9, 3.5), P(13, 3.5))
        p.drawLine(P(9, 9.5), P(9, 15))
        p.drawLine(P(13, 9.5), P(13, 15))
    elif name == "ripple":
        fill()
        p.drawRect(QRectF(3.5, 4, 2.4, 14))
        p.drawPolygon(QPolygonF([P(8.5, 11), P(13, 6.5), P(13, 15.5)]))
        p.setPen(QPen(col, 1.8, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(P(12, 11), P(19, 11))
    elif name == "audio":
        fill()
        p.drawPolygon(QPolygonF([P(3.5, 8), P(7.5, 8), P(12, 4.5), P(12, 17.5), P(7.5, 14), P(3.5, 14)]))
        p.setPen(QPen(col, 1.6, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(10, 7, 6, 8), -60 * 16, 120 * 16)
        p.drawArc(QRectF(11.5, 4.5, 8, 13), -60 * 16, 120 * 16)
    elif name == "plus":
        p.drawLine(P(11, 4.5), P(11, 17.5))
        p.drawLine(P(4.5, 11), P(17.5, 11))
    elif name == "minus":
        p.drawLine(P(4.5, 11), P(17.5, 11))
    elif name == "fit":
        for (x, y, dx, dy) in [(4, 4, 1, 1), (18, 4, -1, 1), (4, 18, 1, -1), (18, 18, -1, -1)]:
            p.drawPolyline(QPolygonF([P(x + 5 * dx, y), P(x, y), P(x, y + 5 * dy)]))
    elif name == "grid":
        for x, y in ((4, 4), (12.5, 4), (4, 12.5), (12.5, 12.5)):
            p.drawRoundedRect(QRectF(x, y, 5.5, 5.5), 1.2, 1.2)
    elif name == "export":
        p.drawPolyline(QPolygonF([P(4, 13), P(4, 18), P(18, 18), P(18, 13)]))
        p.drawLine(P(11, 3.5), P(11, 14))
        p.drawPolyline(QPolygonF([P(7, 7.5), P(11, 3.5), P(15, 7.5)]))
    elif name == "check":
        p.drawPolyline(QPolygonF([P(4.5, 11.5), P(9, 16), P(17.5, 6.5)]))
    elif name == "warn":
        p.drawPolygon(QPolygonF([P(11, 3.5), P(19.5, 18.5), P(2.5, 18.5)]))
        p.drawLine(P(11, 9), P(11, 13))
        p.drawPoint(P(11, 15.8))
    elif name == "folder":
        p.drawPolyline(QPolygonF([P(3, 17.5), P(3, 5), P(8.5, 5), P(10.5, 7.5), P(19, 7.5), P(19, 17.5), P(3, 17.5)]))
    elif name == "chev_down":
        p.drawPolyline(QPolygonF([P(6, 8.5), P(11, 13.5), P(16, 8.5)]))
    p.end()
    return QIcon(pm)


# ----------------------------------------------------------------------------- captions (CapCut-style label boxes)

LOGO_B64 = "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"

SEED_EMOJI = {'1f480': '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', '1f62d': 'iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAABRNUlEQVR42u29d5hkV3nn/3nPvZU7TtBETVDWjLIEiCgJEWSSDeuRvexibBYLr5dkswTb6+0ZGxtYB2xjey2Znw27jhovYEwySYgMkhACzShrlCanjpXuPef9/XHOvXWrpno0EhLJ7nnq6e7qmqp7z3nj9/2+74F///o396Ug4fvqf1+Nf//6969/y1/yY2/uFNm6FdkKwBTbN++ULYu8djuwZccmhW1sBbZuRUXQfxeAHyXfNjUlbN4p7DggbL5R2YJ7vJsogLueiB2XCZtPUnZsV7ahwo+PUMiPw6ZvnfIabrbhjt2ZmHe/+53j55g7al20sTKaqVdkoVJ3tuyMjWLARZFtqUk63XpnJhpvmjRt7m6uaP4CfzTLNnHHfOYUBoAfAwshP4omHcAIOrjym7bcXv6jS35n/fLSvo2j8cLGhjTXlUrJxljStUbciERSV+MaRlxZoKRohAigTpDEWtMVZQFnmtaapsXssam5f86OPLSQ1ndNt+sPfPjQa3a9971Xt46xFOG6ftQE4kdFAOT6LVvMlk3bVbbhskvfssVFb73kqtOqpblLJuL5S+pm/sJaOTklKqUTEruRck2EskIp3KnRwl1Lz4Yg9ImTGnAKqULHkLQUTWXBJdFMJ4l3zXUbt83a+i2zbuzmhxeefefV236rm3kFVWT79i1my9Xb3Y+Cq/ihF4Drr98SbdnR2/i3vOX3Gq8+6cNn16O559YrneeMRs2nNeqdZVJxUBO/2bGAMRCJw4gSeZOBAWeMGPGq2m9DFOfAqCrOqzROwaqQqsE5sHihaAEdQ7MdH13o1L/V7NS+eLA78rlvzZ53++ve85czoN5Sbd9i+CEXBPmhNvXidRMMn/6t5559Wm3f8xvlhRfW4vazRie6Y1Ss3+ySgVIEMYoxUBaIBI1EJDIQiRcAKXwX7Wl/+EBU/Kar4jdcM0ugWAcJ5N9TK6QOEgfdiLnpUqub1L620C199p7uqR9/3m989juQ+txjCrMtt1z/LgDHj+K14Een1HyhfNllp1Ue/tmx6sLzR2vpRsYTv4nl2LmSEYlFpGK8EERAbKAk3twbBZNC5MA4v+niwABi+t2AOnBBJtSAM2AFXAQ2BideIFLn9zWxaFfRRNUkTklSg1WYi5lZKO1utepf2J0s/4cP7r30M+973/s6xwr1vwtAv6nfQvQz27EKTE1NlV9W+fhVq8oHXzNZmb28Om7HqSvEYl0lhliMKRuhYnoWIFKIU4gSiJ3X+rgB0bh/xMvATIKMgtTBVEFKeGlIwSWgTdB5sNNgD4E9AukcpDOQpsEFRGBLYI23BIkXBrpOXeKc6ThIbERH6ExHC7OtxtcfSlb/9d8ffs5H/uAP/mAhu9ert+fi9m9bALyvxMjVWK65ufTtda957vLq9H9bUp65qjqRlKgaKEXOVQ1SNkYqAuXIb3qcQNSF2EK5DOVlUDoZyqdD6XSI14E5GWQpmBpQAyr4QEGH3L4DOkAbaIGbBd0HycOQ7oLuXdDdBd290J2DRCEtgS1DItC1aMehXedM1yldjWg7OjORHmpPfHGmPf5n7z/88x9773vf0spSSfkBu4YfqADoFCZbgBunLr1oY33vm5ZUZ65uLOlUKRkoG6UqUAnaXomgpBB3vKZXGlBZD7VzoXoxlM6B6GSQybDR4rUb6018rnQuaH5/EOhfL/5vYsJrDBB7VdcFcAfA3gudW6F1KzTvgM5BHwskFbAV6FpoO+hapa3QcUJiaR4u60x79KMPddb88aVTN98ALrMG9t+UAGgvEdPfn7pm2U+Wb3j9ivqhXxyd6KymolAtOcpqqBioGq/xZQtxy2t+fT00ngr1Z0PpfIhWAVVQC5r4Dcf2NpOiz5fj3HYIDFULwqIFQYm825BSeP9ZSO6B9jdg/iuw8B1oT3tBSMrQVeg46CiurWo6qdBV5o5Wj+6fn/jgV0vPfu+r3/F/H8os4Q8CQ5AfnNYLX526+Hkbao/81srx2afLiINKlFKJImoqlCOv8eXUb3ylCqMXwMgLofFMiNZ7U64paNdvvkhBa83AZj/a5g8IQf5z5hqyhy08XwIph5RyFjq3wdxnYPZGWHgYkpIXhq5Ax0LbKW21dNKYpuHg0cbOXen6rU/7H9/aDvoDsQbfVwG4YYr4im2kb/hPbxj7pbM+9asn1/a9fXRpp0o5cq4aYWoYqgYqMZSCxtfqMPZUGP9JqF4KZknQ8k7YkGzTo4FNNotsvJyYjTpG+4vug56V0WDPtASm4rOM5F6Y/RQc+SQsPABJDEktCIGFtjovDGm0cKRidzeX/cUXW5f+9i++6x/262XEcmOflP14CECm+f8yddk558b3/P768SMvZNRBPXZaESM1gWoMFYG46U39xNNgyU9D/ZlgGmBbIeym4KeLGz64+YPfOY4gDNP64nNu4PnwnBb/HtyOqYNEkNwP0/8Chz8OC49AUvdWoZ1Ay0EbRzM1NOHA/PjXb2+e9pYrt33tqxpsyo+FAPRuRvj6tqe8+OzK3e8bW7qwkbJxWouN1NT7+WoM5QRKLRg9G5b/LIw+F8wY2KbXeokHNnVA40UGnisKhD6KGyhu/GKWwIX4oPD7Ma/RYBmAqOYFoXs3HLweDn0a2guQNKCj0EpDwuEs3TSaOdw4cFdz3duftm3HB8B9XwAk+f74e8Nt205784bynneOLWs1qJRS6iamCtSC1pcWoDYCy14GS6+G0mpwTaAbovBoiD9fxAIMFQSG/H8deF6PIwD4OKPPLdhFBCATltS/d1T3L5/7Kuz7IEx/x8cGnTJ0UmgprqnWtLpRe6bcfXBhxe+ftWfXVq6T5Prria6++smLC+TJzO9F0Kve8InKHy75r7+9sbH/rdWJFOolS5WIuoFaBGXnTf7EZlj1X2DkqX6hXStcXjQkgpfjW4Li7zIsEFy0+n/sxusim7vocwU3kaWe6rwliBqQHIED/wAHPgyttncLbedjgwUszSRy84b751Z+8P9VfvlN73jHO2aK6fIT/RU9mZs/NTU19tbqO993xsS+15fGLDRKjgYRdQmb34GKgxUvhTVvhtrpYGdBOwUNLSyuPJrp1eNrZPFnKfyubiDSf7SHHf6cDvtbdt2pRxpNFUaeArUNHlSye8GUsvqEcRKpMY4l8dwFa+e+u/mcp7/k8xe/8/b5qSnMjTc+8XFB9MSb/SkjV9yob3/3u8d/3v7F/z5jcu/PyShO6yWRugg1A9UISk2oT8Daa7y/NyVwMwObzAloW1EwOM7muyG/24IZz55LT0wA9ESEYvB59dmLa0PlVBi5ANxhaN8HJgITIaggIqBuIp47a0ly4MJzf+Kln33L/7h99votW6LtO3fqD60ATE1hrth2o3vNW98/+gZ595+fPr7/PzEiqdbiSOoSgj0D8TyMrYd1b4LRZ3r8nZYv4Q7dKHd8bc41zQ34ZTcgSIPvd7wNs0OetyEOKAqPXfy1+bUUfw+xg53zNYqRS/x1t+7w7sbEWXorGJNMxHOnLVvYf/5pz9nymV/487+bu34L0fadT5wleCJjADGgv3m9ln/2u+v+/KyJvf+FBtaNRJGpAfUobP4CTJwLa18H1bWgc6FWHw/4bi3I56Pl97JILEABHBq87WF1APpdjz6K5ck3dDGrw5D3Cp/lHFgLGmoThz4Oe/7Op4jdMrQsuqBIy6U0bfzAkZP+5S9qr37Ve97xnpknMk2UJzTiR9np1r37jPF9b41GVKlHUEeoRVDFm/2ll8Dqa6AyCW4eoijU6M2QaL0I41JA+FhEEMyQ1C88l4PPmVDo8Cxw0WygaDVkABEcIiQ6DEwqZA856cT6krNpwJEvwO4PwHzTA0fNBG2BLDilaeWuo6v+9tqJ9/7iH7/l6pZ9gqBjeYJyfSOI+/a2M954ZnnXH1bHrVCPhTpC3fTAnWVPhTWvgXgEaPrNz0gaZnDji8JgBrS/IAwii0C+5ji/68AS6JBMYBgQpItkCcf7P67wvoW/ZQKQkU8cwAjMfB0e/mtoNqFTgVaKNkEWUpfOi7lnfsX/2vRb+96uap8QbkH8PW/+9URyNfaGd1z4knXmjt+tNpKIckmpEip4xm/+koth1c+FYG+65+uUIbi9OU7On/3d9TQ6Sxdzky9D3u9EkUCGQMDF1G4RjT+mvO8WgZLDpmd0tOxndeCOwMhFsCqBPf8HXAdcCXEObCyxS9za7sH//o1fP/0ekTvfr5cRc2OgHf0ggkCdwsjrcdv/xws3X1L+9v9dNtk8iWrJUhfj83wDcRsmNsGqV0Fc9SVViQIzpxDBy0DgJsOCPzvgW90iG1H4WQafWyxgGxYEFqP9dMj39DjBoz1+1lDECDQgjJr6DKG8HkojsLDT/18TgaqgRiuSmKptPeOFz7nkG6d88JFd32tQ+L24AFFg+9RU4xL98w9tXHLo+VQiqw0TSd1AI/Kw7sgaWPsaqC33Zt8En5+ZfQlm3HAcU65eaPrM/6CGD2r5YuVfPcHbdgM/uyE1A7d4+qlFtHAIquiCNcndQEZCdZ6KZmpw8NOw5+O+mNQCmhYWJKXdjR8+NPmdf7KXX/Wrv/vhvd9LUBh/D2APIqI32b9569rRI88nNk6rJpIq3uxHHahOwIpXQHkS0lmITJD8KCxShuHrkA2T/g1VO7zIIxwH9j2eK1isCjgMEVwkIFQWeZ0tvNdi+IT13EMdgjqKgzSBiWdD5yjs/yKUan5dHTG25E4emz3v+dPffCeqr/Vu7/sYA0xNYURwn5t6ynNP4TtvLVWcUimJp2sZT9GKI1j2Qp/quRmv8dkGqOv5bsELghY30xQWb7GNlEIMMBAfyLCUTxaJBYbFAG54BK/DIvph2ETxPWSIdXDhnoOgZNpf/BwNTKalV0D7AEzfBeWaTx2rYkjVra8cePWXp879vMDfPl64OHo8fv/ybWjt3W8ff3b7Mx9YNT6/wVVLSMMIFQlBXxeWPROWPA1Y6C2KyXo63LG+X1wvohZdBLixiwBE9jjAjiu8pz2xh9qCf7cnFis8Goikth8uVu2PDVwhVsjjhcSzj8oroXk/pAued+AcOKEiqYnbrfPPverlH73oN3cc1SnMtscIFz9mC7B95xa5mg+5b83945tW1mefRjlypiJCKZA1ozaMngITTwVt+4UUE1C+jKNXtAaZJkvBkw3iAMPyfk4gcyg8VBcBgga1lCGmezH/P+w5KZSNGW4xtJD+5deW/T5ghewclJfA0mdD5+M+RiibwEGM3MrGwqmXzHzp158zpb8M4h5rPPCYnIduIZLt2I/9+mUXXBzd/OmVkwvLaZQcdTE0QmWvVoU1PwW11SBt7woET9POUjQTOnf6LHfWtMGQ3H5wg6NFED3pB32GIoCcYBww4BKOa/4Zkp0M/F1DzKDh/V1wg/l3Cu4g4AK28LMrwYHPwcFvQVKFZgotHM2EI0cr7dvtGa+47Le/86+P1RWYx0Tk3I67+FotbXB3v2XFaHM5cWwpYYhDU4ZxMH4BVJaDmwumLQnpTRr8WgqS8fjCcxKed6knfmT+L2P05j9nhM9uIQ0r/hyI+tn7aje8X1L4ezrwSIb8HP6PdnvXmTcBPMr/O+YzbOG6wnu5bv/96+B1F9aKcB90YeJCqK8ASbxixRiiWJc02vWT7J63vuetrxllK5o10D6xLiB07Hz63vOes7J+dIsaVCrGEIvvxpEE6qth9ExfyzdpT9o1dNtkymWN13Z1vZTIRIWAUL0wmYImqw7R4kFwSBcngSqLkELcgAsQFieHDjP3wzR+4P3cAGJYNP8U0kAtfIZoIVh2XkjiOoyfD83P+x7FkkHLLtKuuDWVmSsvk6/9lAj/V3kSBEAEveaam0uro5f8ytJ6u+IqJUeMFwBjIY5hfJNH+Fy713yZpXo52CGFAk1hc5zzL1VQnMfITdhYEyNRhO/qZAjFyyyyuUVTPkxABiFgGUgF5ThmfRF3EATVqfpuIpeG/25y12cyYrwOxAT5I/s4W3hr5xlS9bXQWA/JLojLSOxw5VhGqwmTnf2/8v63vuYj8nt/NfeECkDGTbuq9trnrSofeYGNBJNtfhz57pz6eqis9BeZa7KPVomCFXAykKb5BVGnONtF0w6u1aS70KS10MKmlqgcU58YpTyxhKg2golLhaBQF6F8ZTWC4kabIUCQPgo/UPuBj2NSOzeAGwQUoNvBNWdpH52mNTMPqSMqxVRHapRqdUy1hpTrGJOV/gvC4+h9d64QO4RMQUowdjo093qsIDZEsRUbG1aWZy48Q77+s8BfnmgsICeK+Imq3P5rK//pjLEDLzeNSKO6CPXYM3pqZVjxLI/2mdQDPoNon6FX9QvAnQNckqDtBVqz0+x/6DAP3j3NQw/Oc3B/l047JYoNazeM8MwXncq68zZSGhlDIhMsC4tg/MejgR3PCugidYFhLkGPBYfE4JKUZG6a+755P1/59AMc2NPEOaVWjVi5usq6U0ZYd8oSlq1dSnViHCnXkCj24mkLgaIrPDLE0Dpv+p2BQzfD0fugE0HTYZtOaVl2TY9980MTv/78t73t7fMnMrAiPoGmTWO2Yz+y9fKLl5jpy0xJe+3WBo/n11ZDadybflNY7Kzd2gSgRwXEoU6xiUXbTZqHD7PrrgN8+6Yj3LFzlv37u3Q6WlBuwze/Os/+w/D6d62jHDvUucKWDpp28yjp3vE2nuPQ8RdjHfXeQ53DVGDmgRZ/c91d7LxthlotQkOsE317jlr1MKtW7WXTuWNccNFyTj5jubdu5SqRiXvpYG4NCm7CuYAnlLwbmNsDSQdiQSLBGWQyal544aG/eYkIf3/D1GUx3Jh+zy5AEU5u77iqUU0nU4m0FIUhDJFCXAkpXwAu1BR84UD5VgVNlbTbxS7M8PDd+/jKDfu45ZZpjhyxlEtCrSaMNgzGgFNhYS6hG8PkupMoTY75qNjakAkuFgQOo4Mfz9zLEAGQIdXAYRZBCuVdoFSitmIJYysmiGWGkZqjVo8R8QMoul3lwYfa7HqwzU1fP8pTn3qQZ16xmpPWL8fVx4hLZQTXSwuzlLFY3HIdX1KvroDOA55KFotoHNklDVte2939s1NTN3z48q1XdHTb8XGB6NFQv3P+HHfd1FVrz7L3/+7ykc4KrcQal8VIOYJYoboERk8OKF8IbjKkT/o1x9qUtDlP68ABvvH5B/inf3yY226bwylMTkSMjwr1mqFSFlySkrYdS1ZP8NLXPY2Xve5p1BoOui1EhtXcdRE00B6HAmaPww8sInzpECpZgfqtNrQ5e5ZPZWKEsy89jZIIR3ZP011oUytDvW6oVoVGXSiXDLOzljvvXODBe2cZqXRZOmkQYzBi/D0WUUHnvAtQBZv2hKN1CJxFrOCsoDaVJDUrInfrDWdeuefhrVuIth2nWhifCFZ0fueOlyytdM+xsdgoEiNRNoDBQGVpQKy6PiBU531UFvmHVM51ElyryfSeg3zq44/whS8eAQfLl0ZUq1AygokF27V0WpZlqyc457mbuOQnNrP61ElgFppz3qUMDncQHVIJLBaY3BDAiCGav1i3cLHII4VCVobnZzQv//90YZqlK8b4j792GU9/6Sa++bHbuPMrd9GaaVGtx5jYUC07atWIVlu5b1eTD3zgYV64p8Vlz1/DyNIlxNUaYkL6a0NASCEtTFNvBcoTvjs5Npg4NV2J7PJae2Lt/J4XQ/RVtls3xM89ugVQELkRvebaa+vPnv70b65pzJ6elmMXlyUyFQNlheqo1/7IhNo+/Rh/sAQ2tbj2PPvv28/2f3yQL351hnpNWDoZUa8K1YqhFEG3mVCqlLj4Refx0jddwSVXnc7oEgvtg+jCHKKFrpsitq9uwNqcKK17GLFTj8MXKDZ9uH5GkEvz58UmaKeFMS0m14yx+dlnsH7zGtJ2l8MPHcHZlGot8lhO7K3eQtOx8455Ogtd1q2KKFfLSGYJXEiLNVDI1HkroMb/re2VSa3irGjJpma2Wx658Nkv+tBFX965oIps2/ZYLUBQgP/44B+f0WDhGYmHayOJA6wLXvpMFFCtqAD8hAsTwSVAt82hBw9x/T8+yK3fmWPpRMxoQyiXoFwWbGJpNy1rN6/iOa+8lLOfeTLQRFu7oZN4WZJCMCTqU8pshJ9EBU0U+ieB6XGw/8V4AHL8VDBLcTXyv1vbK2tnOI61MN9F5+eReo1TLhxj7eYr+c6n1/Plv/8mRx+ZoT4WE0eG2CjxUsP0nHLDDYexqeXlrxBGV04ipXLh3jO4OAikWig1PEDUXUBMBHEadUR03Cyce6r99lOATzyuINCnD0LVdZ67rNIaTyPjSgajJgR2cQXi0UJFq2Atwzg2Z8F1E+YPHOWfP/QIt313nuVLS4zWyTe/0+yixFy65QKe9bMXMra0As19aLuV768vJJme1pEWiKQKYgvEUXoYQJEIWszb5XjkUO0XIi2MkSsida7Y+VOketFX8hYFZmfR+TnKIw0uecmprNu8jM9/4Jvc+aX7KFcN9VpM1FWMEWIjfPFLR6lXDC97hUHGRokqpVAgLXQcZcAaEZRHfM+hAWOENDY6XmnHI037PJBPiOjjCwKnptScl7z715ZV5053pUiishFTMphIfR9fbZm/Q1NADMJDUbTbpTM9z0c/vJsvf+0oSycixhpCtQKlktCa6zKydIQX/PIzeNbPnEe13MQdOYB0Ohn4UKCGuSFNI8US60C9Pkce3bGdQIuWbQcaQB0D/D1XMMMUyrqup5F54FYAcgCxDlottNNkZE2DM56xARPH7LnzIGknpVKLEPUuQRHuvq9Fo+o4ZX0NoggjEjTKFR7hOqyFzhzOap4kSeKklUTls6/8+b/7xI03dR+rBRBAz0+fubHB/FNSQcSI5tU6Y3yfm2jo45N8XKbvDhBc1+HaLb7+5f3c8KXDjI4KIyNCpexjxeZsl1WnL+eFr7+U9ecshdl96EIrh/81BFW5QorkYAtiEKMhQdX+xk0p9gRmZlkLdyVDgsYiOXXgb4Nw7aJ4UQHqzsvcA5QxBWm30d17KE+Mc8Wrz2HJuhE+9xc3M39knvqIRzknxwSbOj756UOsWV1l8wUGVyp7iMUVkcFAHIkqEJVBWqgICGKNUNf5zadwywXAlxebQBIvTveC8fTA02qms8IaIY5UsrK9mggxlRD4hNKuau77nRpM2uH+e6b5xKcPUS7B+IihEgvGKM3ZhA2XrObFb3wqy9bU4NBeaCUoDlWf96pN0RD4qHOoemsgxmBKMS4qIXEMxvg6QUSAVYt+XPqBqWzjdRiCWMj/tVBVd8EKuUI8Ib3agwaLoCHY1sxoBWsjYgrz77xFEqfooSPQXOD8K1YxOvEMPvHH3+TII9NUR0oosGQyYt+BlE984gCrV9ZYsnIENXFID7OHDdcnYCqotHIUPDVCPe5WR9vTTwO+vHXr8MBnuAUIL55w7adUKxY1RhERzQRAYh9wqC0UdCRslm+a6Mx3uPFLh5meTlixPKJSgjhSFuYSzn72Ol705ksYG1H04X24boKmCa6T0J1vsTCTMjfTZWa2Q3PB0Wk5ktSBEUplodEwjE6UmZisMLKkQn28iqlXoFLBlGKMKQyCdIWNlYFqW5/Uy4AADBsZoAHxs2jqH6QJLklxiQvPBdchiokixERI5PN7KcUQCcYYRB06N492upxy/lJ+6u2X8tHf/wYHdh2lNlrGOWXJeMS9D7b4xjcPc9VPVPxuGUGKVcJwcRqVvQAa9aCrES2VnFTbM08BYes21W1Dot94aPq3DXfNtTeXyve84FwTO5yYnhkWCeYmSGA2V1cij/RZMGK5+65Zvnv7PGMjQrXsjURzLmHzc9bz4jdfSKPcJb3vCLRbtGebHNrb4qFdLXbtarF7X8qRo5aFpqPbVd9BVSjqlSKhVhXGxw2rVpQ4+ZQKp55ZZ/X6ERrL60T1GlRKRFnjSbHsegziK8Ore66Q/yuoKi61kCTYZpfOTJvZwy2OHuxw5HDC3ExKc8GSdD3iF0VQrRpGGhHj4zETS0tMLqnQmKxSqpeRuIyJY8R20Qf3s3bjEn7qrU/hw7//TQ7cO011tEy9Ds228I1bZjn/3DHWrh8LyU6xhOzjAI3i0GXlvCUMAW5Dmuf9f7/2+uXyrvcdDEW94wvA1imEbeizdm1bX6Z9OqI55pG5gMBTL/g9DwCp8z40bSd869ZZmu2UFUsjTCQ057qc8bRVvOi/nUuDNuk9B+gcXeCh+2b59rfn2XlHm72HHO2un/ZaioRSDJWyBLcvIQD2C9xqK7PzlgceTPn6zS0mxmc59dQKlzy1zqaLJhhZNYqrVymVSwXcxwzEA/ro42IU0sSinS7pTIsDD81z350L3HtXi0d2Jxw5YlloKmmqPcSWXhhSiqBU9vD28uURG9eXOe3UKus2NBg7qUHUqGLSCHYfYvW6pfzUmy/i/73nJg4+PE9lpMRIQzhwoMvO2+dYvcYXjnrBbYYReNekEqGa5NmPGqGk3ZVJ87bTgIObdx6LhR8jAJt3bhHYzrLS0fVRYie9ke8PrPwHWURNr0sn+EsTKfsOtLnvwSbVihCXDc3ZLhs2L+Ml//UcRmiS3nOQQ4/M8rWvHOVrN7U4cMRhIqVeFcZHDaUgzLHBm84MYFJQjMdBnCN1gk2VTheaTeWmm1rcdluL889f4IUvXcop506QjNWJq3EeQ+Ypog7D/Qv5thjUQtpJcPML7L13llu+Nsuttyywe09KJwCflTLUq4KJPDFK+ikOOKckCRydsew/ZLl9Z5fRkQVO2zDHJRc1OOf8UUZXjBJVLNFDB1izcZKXveECtr/nFmYOt6mW/YzCO+9b4BmzHUYmC1TyIASqFkU9IJd5M6NYIDJu5IzykVOAry3fdJnAjccXgOWbtgtArbP/FBEaDkNWfdWgRT4ITREX9WdlIhAru/e2mZlOqNcMnfmUJSsavPh1ZzPZsKT37uPBe2b42CeOsOOuDlEkLF8ilMqGchSYTjFEIp4kJCG4Kwx19im48WN7rX+MjUC3K8wuKN+4qcVDD+3n5T/d5eIrliETDaJK7GO3LCYwRcuvAziRoGlK2kpoHZ7nlhsP85lPzvLwIwnGQKMhLJ0USiWIjHjaowjGHBtMqyrO+kpvkirdIKzf3tnhjvs6XHB7kxdc2WbdmWNorUb0sGPDWZP8xH85m+v/6LskiaNWM+zb3+XQ4YSRyQrq1KOizhbcgEPV+BMsfL+lWIyWYy017OwpAHzhxkdPAy8PhYOaa6+rlKyx4IxgvACof3N1vQDQmUDpzsy0cORI6msXXUepEnHVz53FmpPL2F37efi+WbZ/6DD3PdhlYtRQqwZzXxbiWIgiIYq8MPv70JxBo0gIMrXHnVTBppCkvqu6WoaResShIyl/84HDpE551lXgTIOoFPUieXvsiQ9ZW7qzimsmtA7N8akPHeJTn5jFJsqSCUO95qfSlmKTX2ccSM/e95Knpk7FX58T3/CTKqlVRurQTR1zC8pN326z/0DCy1+Wsun8SVCH2aNsftpSLnv5Bj75f+7BiKHZdhyZTthgss3vgULeAtg8RgvFaRBcObJRnLRPRWIuvzG1g6zheBjxE1WJ37JifUkcKoVwOWiiqoaULVyMFlgs5Rgpl5ifs8iI8KJXn8K5T59E9xxl/kCLT35mml0Pdlk2YahUDOWy+EeslCNHyaSUwtR3KUxxz0o7qYHUVPMoXp0f5l2yHl1sZ+4jNhw45Ph//3iE5aurnHNpjDNljDHHMHh67iDEGZ2UZLbJZ//5EB//6AzVirBiqaFS8jFJqeR9exQHcnPIDCt0iPqT0Bw0TB0kVujamFZq6CQxpdhRrym7Dzj+8UPT/Kc4YtP54vEvM8NlL13LoX0dPvtPD2JKzs9PjMXPE6IAdKkHhTRLTbPvArFYjOtseM+7f2dU3v72uWxi+XALEIiff/pnfzZiNDnZGCWVId004npWoBAHqBokEk67aBnn70o4+5IlXPayVXBwGmm3uP2OBXbsbDE6GlGqGGply2g5pRLaBRNnmNVJ5nSMI3oSs7qUBUZoyihdKowxzSb9GkvbD5BYkEiIjBCV/OeWSngLEqzH8qWGPfssN3xmmtPOHaUyUfJkEtFFSCGKWohcyne/Ncu/fmqWakVYNmmoVr11qZQEH3CLZ7qH5Yk0YbfdyHf0UuZ0kpK2qTNHQ+YZ4zBL5ACj0Sx1M00jTrAxNCOYiWJEIvYfsXz0U9OsOKnMSesi3MwCcWR42WtOg0qJAw/Ns+6MibDOmeJ5RSzOQc7+ZXbAKhjV8aXcNQrMDeIBfQKQ/bEy868NcemoU6WIt3nNz5gpLv9wcRKCbAfNhA3rGlzztjNpNAxMz6ILLSSG+3d1sIllYtwyElRkn57MXnsKe3Uj+9jAflnHnCxnQcZIo6qfDShRKEAZLjBf4L+f/SfUzRwz00pntkN7rk13PvEso1A7MMZbmNGG48F72xw9krJqueA6GgK14sDoAOLg+dTNpuWrX5wnaSsrVkRUa0KtDJWSf63tQmIdNnVYJ5RU6UQj/OPoG/hu5QVeMWzmm1NKrk1DZxnVg6zkIVbyAKtkFyvj+xjXR2g4KI/DAw84bv1uk6s21L3ZOzJPbdyx5efW0ZxJGElaMN/xOEDPB6LqAljmelZaHDYnhLnRSufQqA/y+zOBfgEAtgHarNZR6jnq2AeYhahTQWzm/wu9fN0UM7dAo9aB2RChGaBRZWJljUbpKJ14KXdXL+GO0tO5v3Q+0/FKXNyAUkxcFuplWFpWqmWlWhJqZYMYeOQwzHc2cdaWp3L+GQfptmsknS4L0x2m97U5uGuevfccYd89MxzZv8DCQkp7DlZvKFNdXgO1HpOPzHBal/XHzsw3Hft3d6iUlNgomlg6XUdXoFSOqIxUGD2pSmNJlepkneWTKXdPb+DovZtZsaTM6gmHKrS6jnaitLujNLsnsbdzGg8nl0KaYpIFJtJ9nBp/m03mq6wzt1CbO8LuvV1/fVbBWvTwPGauxYgYaCV+PUOmourQjJAS6hp9/SfBAqgyMt4+0HjUWsD2IB2TyaG6iNadd+vS19gyWJocpDODn5vfTXxQmDlFHE974cn8nwdfzufnnsf8ko1QG2G8Aac3YPmY4aTRmCUNw1gtYqQSUYkNkRGqJcNoNWL7rfPc/q3dtB45TLxqN5FMUK8L4xMlVp8+Bs9ehrpTOLyvy0O3T/PIHdN0U8sFly9hcrVBZ5oBH1mMx+/Tg/qKUVasG+Xw7kN0246R5RVWnTbOmrOXseLUcZaeXGNsaYlqPfLQdDSDuWUV8nCJC09rsOWiUebalnbisE7ppI75jmW2ZTmy4Dgwl3Jgts7RheXctnAut7V+iqXzuzjFfoSXnvVFGCnB0W5gGRm02cHHeINVSe2Z/RwPCAScAp5lcLWana0DbNkSTsg8HhTckHZZPd+3sMcSlqxHUFScp0GpLRRTMsxF8qRYEGi3QBvcveTFLEyex7POspy6vMzKsRLLR2JGKxHlyBMgrIrP8/1BHDiFpY2Yk5dV+a6JcAsJHG1iNSYKVDQx/tgYqVdYtrTOshdMcNELlvkbSFtwaA6xMuSwqB5SKAh0YGSiypa3beKeW2ep1kusOXOcFafUKJfwnU5pB7otmEmwHYAZkkcqODGcvLzG0kZMy8JYOaIc0sTIGCJRVIWudcx1LAfnU/bNJtx3oMRX7lrO6GSJs5/5IOjBntl1vopDJD6SzEGGAeVzDsWSZ2seKBTnX1vSpNMA2L79BGoBRpslwcVOe5F43tyiLpgeg6ii4UWiISWMtFAV8/VqBSS1NA+0SNptLjrb8NpnjFOKIlqpwyl0U2U+dSRWSR1Y9ebLhmuYsOox9SjkuWIQ/DXkm9q10GnCdBONjAcVMJDanuPTYchf4XfnYK7JmpMbrDlzdXBrHZg/jDbb0LWIs4WqXwSSoqlDjCCRYd4qRzo+fopEicR/jwPCWTEwVouZqMect7pOcnpKszvH/P0OFrpQsQFdzbqnpNCvYkLwF3y/epa1ZhOZg8JogO5VBRWNI2srJ1wOVmcjVaJslEORQ6E6pLAyWFTJS7IBtQocPk0VZyJWjlcYqRgenE6ZSzKB1lyYbRZrBFMWBUZtZtZ8tU3ow6ilv8gjFs+bk+IpYWbI9I8hSGCSovtnUZkNi2qRUE89lkbhUw4p8EScg9R5S2YyXCB4QxEN+IaP00dLjvUTEasmquxIIJlrwYQNh1KEtXPaX2oeXPcC+mjDOlLoLFPFqKcvPXZSaL+1Uf9wDjRGVULsHNKSQcKl0t/vR6AUGGGhq8x0lLb1C+Lg2KNAgykbytzXAcjNycDHB8vQxwparAR8bE+AqOsxkgZnDSrHFJZ0yJplWVR2jWnh7Uxmpp1fCySkr1oQ7MEFyYShr7ClfTWNwXpXTp+Ux0AIsbFx2sXlWENxsJWSBx4SpE10oPtGe5CqtwheCDRsknOKVSWxStdyDIRKga1lB1uYlUI51BSYuoNlXVMgqQxU+mRY718mKK5/0LQWIuDFmkxUeoDYwBjJaMjBpBSIvhEatFYHBpL3v3+vUVQL5KdeQKjO4pztF4KMryo4F4s9YQHomHri1CQZ56BHd5PQoqYY5/oqgqrOF4dcaP0qnrHtsi6hfqXLCFmii+ikDm/VUGd6jRMS9adzMmATh07S00WmhQZSyzEzBGVA1aUQA2QcAWEgW+6zDMUi5OARE8fyEU2voVQLI3XCOmrGvVCXZwDqFHHisYAcIgi4jUoKlc6JzwfQ0a5T6WZa6LS/+kvmCga7Z/J9MANaEyDWPJMoMJt0+LytYT4ut8iOXqtZltJp1GtB79OOxW57yEQRHSYQA++Xm2gtDLwyvRhgka7LYbPFVPvXwmuvFCxAQXjFDDSlaq+rOmxMHkflNS9/VdZJqrbcelQLsGWT/7j9Otpc42iSCZzz0X4x3fd5Z4qakKoYLxQ50daYAVdAXr40A1oxjKg9jMfa066w+Jlvdlqg8w20iueTt4otakM66HWx0bEywA0MBFMX9d2XhvEuQ5kFepyWUx3QRDdoNrQvVdXCYEnF9lxHhtgOUBdFQcU0k1p94VEFYGv4vj+Nmla1KcFf+3pcKAKp9NJBl4JGPsrNbHlu+wo8eRu4eX3DNrOiUq+AIScwxU/7rIzLa/e4gZ77wU5hlf6x8qrDYwAWcRtFAXFhsLXLWMkS7uexDZ/J8nU0ILYaOoB73Lse9bvICA7m30eaqXcBBUKyWh9vOesw/mULaX3F/KO6gK1b/XWcdvKmpomiubzQ5DQ8pMdIzny6AyGrC2QSKr3m0Nxnml7gkt++BIHXofH5YLNWFtRrPmd3oDSdFabE9GUevU03vZF0Gofv2SMq/H1gYrgWTycLHbya9oiwgaQiBb7JYj3Ehfwm3Lv0CaUymAWY3Cr0ZNvfs48BpAdsOs+YUvx+qarn2KqZm25MzAPs2NQv2mbQxirIf37j1jkr8Z7Uml61sTDdpJ+Gn+JsNgnDBTzaFe6+cIOuUHcbHJWbF56GK1/R/Uhf67TpdSXlF5vNDo5C944ZeFP3KGNfCrMLs/fIuI/ZieKZsKmvIUh2/drfSjD0+KkC5lGE2aUvYC50WeUDpl1ueVV7pFd14kGzAk/C16L8hjqR2SjeOPeoFkBA2YIRMWpNZVfXGtSp5Gij7e2C3+OeyddQJOoPFGSgq6aw3LLIwizSmJXjOeAj7txEFgO0AH7n1qkwmUSLi1sMFk3vuSy7KE7uKkZsxfhCpS/QVSd9vNlB0ZKBe9Rha6GD/QimF0CHOYKaW55QLHJZLaCfJqgBWLHWkFJ68FW/8juzCrJ12/EsAPCFTf4TOzL6cDsVVVXjepXfvCPKK5z0ghJ6xIQeV2Ag4g4bMZhaa1CsbOUedURDnlOZIdH74EkhpoDeZS4iswqmJ6QihfOCo0KFszgqbnAieMEKFDaW4wydQSSr0PXmZxUTloziVRQyZ3OT35dbF9dStTd53mUDRdR0rCE1lftwKV+4LB/Ws7gAHNy5RQGS2rL7rDPzEny/df1v3ht1n1WiAi8tBCseLUx7UHDe4FHoIdAeqJEHnAX07JisIBAhchzAaUGrC1NIcs11fcMY1Tls12HbntrdeyS4TopLetd0zCRvV0gBi1ZGe2Y4J2noIgNm1PtmKeIEBYvpY+tskFaGBdh8TcnX1iLO4dSFrvEQ9OWxmoZanUo3NamLR+/3fL/LHh0I2rFpu7/92ooH7HQ0i2M0U2rnPGMmCwZ7bteBCUwV8T5KEFRSJDv2TaIQS2kWMvbNTdQCcqsDNK2swtVbUO35eAYQF4o22P/NdR02SXHthM5Ml9npNgvzKUnHN2RWq4bGWInGWJnKaBmplnoNJkVASIdh0VFeG+iL7nMYoXcPQ+Of/vaOAChFuQD3TL7Lx8yqWpx12VwKbNj0TAg0O7LQ1yPmmiMr74M7ObjzpEfvDNq6Dd0GfPqS1z509cduuFdda41zDucMzoXqUrAK6hQ1BXTQ2P6eOFV/Ay7ME1LTx5PrI5wUCLs9VluBtBvYwXIMNDvY+NGjdVun2I7FLbQ5vLfJXTvnuPPuOXbvTZlvGVLnS7Tl2DFWV9auKXPmWaOcsWmM8eU1TL3q6WYyiNTJsSlj1hcr0td0nBM0B2Y+9dcMillCD/pVl+ZzgTLUTwoWww30wDrXi89suJ5U4gMLS0+9F76QK/dxBUBAdQojL3pR5+VvXHdrms5cJgXX7q2QYpxinTeDxmjPV2W+VLP8OAiAs6iLcs6a0+FgXdZdLkIff1UK2pX7yWwGXzaJrBBu2cRhmx1mD8zzrZun+cpXjvLgwRhXX8HIirXUNyynXqmiztFZmOeRw/u5d+cevvytg5y+fporL1/CpvPHKU/UiasZmVSOxQU0N49D072sVJ15kWNimlwoQgxhs0Uu7G5GvcsAp6zk22eVAxPNBlazVSIntLXx3Ve++a/2h46vE+4NVLZBtzxxa3d+N+XUibWGyCnWKcYGPMBqwGAM4hQJffqSzdWRrDbgCr5MQh3AQ5deA6TX0q+9LEhygy892FmyARRxcC1pyN56g6VtCmmzw/77Z/nYJ/bz9VvbmKWnsPayp7PhnAtZvW4jS5Yup1Zv4JzSbM5x6MA+9jxwDw/vuJWdt36N+z+wjxdc2eG5Vy5nbEUEtajQZ6oFVlEcsgpTKEFIj6uj/SQe7dt4f/+2kCZrdpBUZknzwNqvnzoXzLwWOsULvt8GLMk66aRCUpu4CQ5kHV/uxAQg3EjTTNzU0crhim0udU7VpSJqsw9SNCqY/9CQICYzWSCa+pFmSq+JAfXNHCn9VmAAmOs/A0Hz0nBf4CBRQUPUd8MkStpM2H3fDH9//V6+e5+y/Lznsuk5V3HOBRexccOpTE4uoVarY6I44z/Q7nTYv383959/MXduupAdn/kIH/nkt5hdcLz8ZYbRkxpILepxAvLsIeqLEVQZimbosLJC0P40DVS/LIezNqSkRfZ1gN6zjdZe6pcbDUvQfj9zsGNL3aQyelMG8g0bExMvsv8K8KlTz7r3ld+96xbR5vOt9cFG5KQPlRTnJ8VqVKgRSKgMqg0pVZYheD226ucdFlltUixjyqBQCFIgoaj2umH8k/4AKrWKa3c5vGeO7R86wO33KSc/88Wcf+XLOP+8zaxYsYpqpYGIkFpLJCa3MqVSxPLly6lVS0xMTlCbWMKtHy3z+S99nWrlIC97qVAzI8SVqH/4VGjR1kKHUdZKl/n+YfWp3OTj1yJjPqkbONJW0wD79uYl9DrDgyLmMHBmAVTFqnTMyF2V8clvHW9g5HGnhV/3ur9MbDx6AyriElVrFZvmFimXNm+1XBAKQdQWpmUkBejQ5VFvGuoKxYnox2jI4ISWLEXMQSDTi45FcInSXejyuc8f5badbVZffDkXPP8nOefsU6jXKqSJxQW/KoWgS4M2panFOsfEaI3N557JOT/xM4xt3MznvjDNzd+aRzvWT2jLC+3awwuc9qF8RfzrGKyKAlqnQqpamAnpAR7fEp0WxtC5Phqg04IiWvVdR6l3fzZFIwVbGv3i89/xuRkez7h4v0bK4bT8+Zm0Nm+sFZeKeonTAb+T1QmCXw+poWZ2SW0eRQrekqRWSQf8mIZFdMdwHn3qaEKE7f1kCBQykkmiiLXcdecCX/naNGMnn8bZl7+YjetXoKpY6zBG/KiVnCmUhRWSZ49GDEmqxEY4/axTOe2yl5JWx/js5w+yf28Tsa7Yh5GH8eokp3q5HBktTH2ll+e7wn2nYS2sC5FDfgB1GqymlxafiRXOmrSab35xL6xHgGTBlt2Cq3wmuE557OcFhP9y4KwtO7tR/eslEGfVuVSxQeJsOjCyplA3yIIZza1Amqdp1kE3hUQdqfouVpufpag9QEz7j17suftC4SdUG8VCay7hG988ykJSYsPTr2TdKetxaeIXV6Tnv2VIaaZQHYyMwVmlJCmnnnsua86/lEceSfj2t+chsagtQt2+FlEMRaSIIhfSPBc03QZGUKpKoo5u6v14X1ahuVNHM+uq2rO62eaH37NyjHNqy9axQP2O2ZG1X+fxHhghoFNgXv/6355P4tonu66kLnGS2iAAmSUIGpFteg8tLKZrmt+UhOg3yaqLWswGeoiZFBYwT+1lGCfPZBEEe3a3uPvuWcZXr2LdpnOJjdcc6SP/SL/mFwO6YlXSCKl1jDSqrD/vEky9wXd3TDM7k/ghaNoPA+fDoAbGCvWEV/vy/vy+nfi1UA0Yh8u13wd9tt/0Z4LgCpsfBCFNwSUOo0acaXzqle+5cX/xNh9zDLB5CwKOuaUbPzlny3vKOOMsNk3DBwYhtbYomb0cNVXCyBTvy9TZPAvoWPXar+TfC91Oxx4JmdULCO+ZqZHrHei96+Eu03OwbPUKxsbHcWnaIy8PnjifEUuGEEJ7E+T8jSxZcRLjk6Ps3ddm736f3urgmVCB8Wm1f85z0aXlGH3hnlNVvxYhC9BCSO9JOB58c9rvTV2+9pCm3iI7q85YFx1NavPz1ZUfw1mu33LM5KsTF4Crt+N0CvP3Z914d2IanxUX4VInLiX7wNwauGPy0pAiquIKtYLs2OVuiAGs8yNgsv9vXcEVZAWWnBKedQSrJ5kELpxxiu0q+/Z3MEClbIjLpT7oOOtoznZfB+rRGcAiBCq3GiIRoiimVK5QrQidjnLoQMe/3krPvtvQHxE+yxUP+8g0HQr1FH/P1vk16FqlN4jD76wL+X5vXQsK5npgj9+H4A5SpyUMSVT50t2r3/4NBdmy/djc/7GcGaTbd26R7VeLnRvb8Hcz3XjBWGds6lFKm/YEwPakEGd7mHQG9OThPpBapZ0oifXNLl4QvDbkQY7L/ovmAtEb1xfaXtTk8Fo3dczOJVSqsLDvfvbdfwc2qiClCnGlRlyrU26MURudoF4fpVSuYIzBRBEmjqlU61RHJqiMjFKqjWAqdaTaoJMqe3begmsfJopgZrYbxg76mcgSDj7IKGFa8PWe/Vyo42SanwV/DhIL7cQHgjnwk7lV7dVdQnqXr3UaHjY0qaYJinVmoRuls6WV17/lLVe3vjB1WfRoJ4g9al/Alu3bnYJc99N/fsPY+1/4lUbafYGNHS41uFixiW/csHmfvCLG+Tm3RjHqhQHRnMRgVekGKDk7TkACkyjH/ArooF8aCdwSDVml6/lv44Wx004pVw3aOsRd//xnPHzyBdQnV1Cpj1IbGWdkfJL6yDjlcpVSqUwUx/kU826nQ6e1QKs5x8LsNM35GdoLMywceJD27m9TMV2aJaHTcQWqu+txAnXgWgdqHDnqVzD/NgB+3UCTz4U7q7IGmpnXeE/xsrYX/OVpeeqfK1llntqOvete+FFlp7D1Rss2vrcjY7LawOsuuST52NsuubY+PXNlKbFRagRJ1AdLxhPgjfjDJCT4MwndL0h/Od056KQ+EFQktJeHBTB+8aJsPL70qHI2J1+4/oGMooEW7YgjqFVj0u5h5nZ8lsOpzzic629c0sAl8K0DARvQXmkhMlCKoVqBWt0QRSVKrRRnB5keBqzkmYEtTHXPgjerPfArM/tpsHiiSidcn8nIK64Q8OWb7rwVSf1JMX7jFZcozr8xqY1kobHmr37xLe898tpwuitPxJlB2dkzZs2rPj439+CXVrqDl3dSnI3USOT8KTCRPwyMJELE9967kLdLKOe64Cetc3RToWslT8mFXpdR3lQaCkJWFBvy4Lx3w2V1eQ/GOAxqoBz7QQ5SK9MY84vk8+weS8YNUrYkyieUZrN+jPFjW6PY1/lt6vysJHr8Bl/lDG1vtndAmHW+K9hmrBwNJl97cU6Gg6gqXTIcIFT0nPY0PwN68njJ5WY/c7tpipask6M6fse+pWf+LdzBiZ4deMKnhimIvOlNnY//6uY/mF+YfU5ZOtgoVolExCgSeSxesYjxhRPrfMTu26AkJ3VYJz7wsf1tfXj2ACIe9EkBo4ozQuoKaaLNBjD0yBkihsj4k0bKJSEuae+sKhUflC5yTlT+VLGbzPhmTvDpVYJBjO0BEtb0eHsuDI8sjINxffFL8N/BQiS2B4SRC4BQyiqlgYCTpprXWnqbfezPklpnXRwtlFf+yat//SOHH8vpoSd8cGSGDt655jWfmzfjH66oGE3V+egzBIBpD+lKA0bg/ZcLBBKv8U6VrssCwNAJ7DQPBjON8R3Cmp+V5PEBzXsSyOrlDmLxswSyntRySahVhHpVaNSEkboyWldGGspIXRmp9R6jtf6/NerKaM3//2pZKMWC5KfeS6HwEdjBNgnnGHmNTnOX5a8/LWx+nvmo5vffdT0ALEM+bdEKBGVxaeYG/HprqrhUXcW5aFrHv7Jv8qx/KBRReUIFQEC3X415y1ve0pqfPP33Dyf1mZK1xqU4D0BAmjpf2Up8Vc5mcKUTrA3FoHBTaaq5v0zCQiQZRBwGi2TCkPURpnkhpIAuhhNHBZfPBpLs0LLY+/FS7KeTl0pCuSSU4/C98CiF57LXxXEYVxeFgl+RBqI2nIia5Fi9hsg9Va/heXt7YZRdan3ck4Z7zu7fD5nU/CiAYk3fhvXIsP6060gT//EuRUvO6kJaa89V1/7BL2z76DRTw4dCPyEW4OrtuOu3EP3k73z5G63KxHXWGqFr0VTVps5Tr4Jv8sLgfactQJcZLy61jtS53F8mITf2AZIL37WHl7siny4rmYaNcF1fgCqAPJLNiZbez6Z4bPGwR+E1RXZvATskirR3FK4Lx8C6NAzLCMI95Nqze7LWC7r36X4N0mApCWtjg8anNvj5wrq6VL0l8ObDaSrRjIx86P4Nr/nE1JQatj2208MfkwAAumUTKiK6+qLxPzycjN0eW2eCKfLgRjfbdB+hen9lcWkvj3fBD9og7WmaSbojceoPxg4TQjK30LXZgIxwilZ2FnEIjaMoJY7k2G7mJ+CrWJWslkNbqyuch5wVRUww6bZn7lPr7yXJLZzru2dr/VrkWYrL5MvRh7WkBEHwAmBTVZO6aDqp7153Vu133vSmN3W2bn5sJ4c/HgFAtnn+xzm/sHNfZeXS3zjaqSYmTTWLBVzqzb83X+GCs8JFhodnMYLTPEe2WWpUMJOpy8CiAJdmEzLpUcIkfC9Fykjd9Mbk8BhX4nh07nCNiD/fB7G9Mm3GxVJFXP+1JrYA+OQV0EDizECvbC0KjSU2YCRe651fu8R/19ThUsWkVuc7Je02lr7zjDc9tPP664nYcuKnhj9uASj0Z8qz3nnfR93I5J/aJDLadaqpoom/2Exa09SRJsEkZvRn6ygWlWxhodLMKoTnEudINYOOM1jX9bQum9VfgiXLfM+etdonCI9783Nehr/G2Bgml5RDc2ihYmddDi17C+DjlSRzZX33RO4KbO7jQw1ACymitX5dEiXNXEDiN19SdSTGpLXR7Ve+d8P7FWTLjscn7/HjXp0wU/CCZ06+89ufaz1taTrzDBs760QirECS+dAIxJHi0ayM02mt8zNoPfqezbbKR536uoFgjJAkjiQ1Pm0SKWCr7ZBvxWBiVq+pUTJCkvTK0oVZVY9L/TOySrfrGB0ts3JlGdIUsd1wKpoJcYjve0udkqRKkjifDhbq/bZY0nXkeL/n+UV51O8DaL/h+cYnDvXRvyulzhx1Y/ecdNqad4jcmKoev+DzZFgA2Irq9UQTV+88Ul89+eZZV98jHRu5VF2GTtlUSRMbgkHrGSsqOGdDbJANW3S94KcQAKbBVyZOaafeavQoNj0/LK4D3TYb1lZZurRCq+NCsPm9+YHsuF510Oko69ZXWbbMQKedB3++yJXmsHBq/bUmIdPJ8v00BH1Z4cw617v/VHHOhrnC4MJapZkLCK5VU3Wmm8qRbn0umTzpV85643d23TCVdas+vi/zPdnILbjbt1B+6tSumzpjq94206kl2rHiUlQT52HK4iPnrWueFmY/p8XqViFazhavnfrgMOfN2YxkGna5nTA2aTh78xjdjtLpZhr3PWh/eDTb/vTvCy+YQCLnJ1NToMIHFyD49K6d9q47KVQ7bV5GP/b+816+DFPpOrTr0CS4VQskqTa7ZXRk/Lef+557Pn7zNZQu57H7/SdOAIDN15PqFOa5v3/v38ZjjXe2k7JIN/VssNSFoMViE4/VG00Dj8Dlea7LF8bleXGmLZkl8BbAFYv5PdjNpmjiQfJLLhlj2Uk1pmeDD7W9OsBj8f0ZLS9Jldk5x9mbRjnt9Bra7KIZjTd780LLe2pdbq3SolULuEca7jtjVrkAmKVWMeoQZ/P02YWYyqaKdlOXdkpRuzL5l8/5w9Pfq1OY+5+HY2tf26F+XwRg4IOUzciWnyZ6+h+e9O5WdfIvk6RktGudTXp5a5IqxnYpuY7f+MSRJFmU6/PaHOcOubHNUqZQPk5tsYQv4YCqwPqxKTrbZHJCueKKZTgi5uZ6QtB37MzxzL4rpnDK0aMpS5dVufy5S4ldG5qtgDk4P0XLFXr88J+VlXetzaxbwDvCPblwzzYNaxAi/JK2MS4h6fpon8w9JNZpx5jpePRj685d+zaRG1M2I1u2fG/a/4RYgMwVXA+I7Oxe8YqzfnXGTP6T7URGEptmkKVNIbIJZW2H7MAfuuSFwwWswJEmLse4k/C6xCqd1BMnijMbbKF/06mfU6zTTc49t85lz13OQkuZmbU57z7Tajdk+lnuVUKCkSRw5EhKqVzmqpes4qSlETrdAtvD6jXNrkHy+UfdcK2JDddewO3TJAh6akkCmKOp9WzkVCm7FpFNQpXPr4Pr2lQ6mHkz8qUNz1r92lNfd8uMThEzEPU/Hu3/3rKAwa9N6M3XUpIrbpyf/tSlr7vln9PqeHv2JTE2TZE4LvlVjrSLs/6GTZgpYDTCGo+0iKg/pCobjBAG3XRT57MI6WmcH1tjCtO+QVoJJl7gOc+eIDYRX/nifg4eShgfj6lW/SjayPROnc1Z5oUSbqvlmJmx1CeqvPClaznjtDK67yia2ML4nv6BV3lzj3V0054F8BmPZ0VpiBVsVuSxtkCvt0SagHU5jK5dl2qXeN40vrn87PFXn/rK2/frFGUgLZr+7+XriRMA4OI9WL2Wklz19SP3/d05r931Rfmryc70i4xLnVM1+ci5EBc4FUSFWEGN59mZWHsT0pw/DwDn6KpgreRAULZwGUSXHWyGCG6ui2GBZz1nkpPWNvjqF/ax+8E5KnFKvWEolw1RJH0+P00d3Y7SalmciThl81Ke/dwVrF4K7J/BddIgZL1ev6zELSE2kUA/73ZDKud6D3WZq+u11KfWN7NgLZpGvbgyVVzHOukSz0n95qUbxv7zWW98aNftU5TZjGXHE7dnj1sApDfXSPJ+wq0Ie7A3X0vp1Ffevv/Oa8949Z7b5NpGZ/YVMV0coqoqLnWkXYdEDhMJack3mGpkwpFrion92Wsum4sngrUm7ztwzlfGMu3PRrnmI61n2phEOWPjOGvWbWTHd+bYcdthjuxfYHYuDZ28GT/Qv3+tUeLk0yfZdMFSzjyrTiVponvmcN0Ulx9apT0LlFECXA/V8zBvLwj03To2D/o0b6IpoHshLtDAErYdVLvGzJr6l5ZtrL9q81t3P6jXUmIP6bDNf7zm/wmxACJ5BpQLwcV7sLdPUT7rdXcfOvjlZ/z8d/7hniPSmX6tqBVRXJo6Y1OLhDp9pKHNvOR5AE6Fkho/fDJMyhYEl0p+QFlPswINLWcfhZkAgJlvY1opjck6T71khPMumGDfvjZ79raZme6QtH19v1wtsXx5hdVr6iydjCnThSNH/NzeUNNXp4VRbYXYQTwEnI/sCRF8mm+4j4GSUNp0Gjbe+depU9Ra0tSrh7GWpB3LdDzxkXVXrn7daf/hOwf0esqAZU+hefcJ2Pwn3AX0pYebsXo9ZXnWV+dUt/zSN970qX2tWfc2Qcuapuq6Toi9BfBUML/hLgKDIVEoBX5W4gwixrOLCwGgDRU0sYKIyzt8chaagLiEdP8s0dEFqiMVNiyts2HNCJix/iZEm0KrDQdbJM0OLvEzEH2jh+QmH+0f9OTwqRw53c1rf5I4xPn8PrGhsyd1PZ8ffs/oXKTOoZh2WnJSja5b++JL3nbaiz41e/cnqDBPmgd9T+DmP2ECcIwVyIYObvYxAbI9fbrwm/96zYr9riPvUq2MuKTl/HE2gjqDxsa3mVshdorGvvEkjgSJlDQwc7JR6Rk7Np+xabQ3pMmEWEF6LUW2m5IcTpEjC54NHHuhIvTkO2e9Oc5PRvM0L4FCoUb7ikOZBcD5Fm/VYP7F+jjHZoUej4ekabAIWSroMmw/dc6NGMF1bLn27mf86eF3inwq1espP5mb/6RagOAOYDPulmuJ3Z7LNN5245/+7Kt2zd3t9r57htUrG8mMwyIa+/nPJgodOU4wzhNvvDvwtCuXBhPseqZW8ZpvNFgBPAU9r+dLxjjUnqS6AA5kI+xkgPriegMse2Nxs4GOmjeTevq3YFKPBzgUEodV6w+MLHRPuSAQmjOkHJpatQ5tMmmW68OHzqzc/z9ecN3h67ZeflmkN99Y4n7ssM1/Ir+eMAHoswLFrx3oxWBZfWP02an11edt+5MP/uJrm3d9PH3pO/dw8pXlyFEmsU6ccTYSiYUojKHRkj+QSq0hLhk/+dJ66+D9rCPChZkR2Zl5GjKCojvQ3umjx9ACdYD5pIX5zK5vIruG7mayQcxBAKLg68lQPfF4hguNKx4G96CW75Syqupcx5ajxBrZKHd86RWNf/mN9/7FB7509ehpla2vvNHxL1g2D1jVH2YBGCoEPXcgTOIu3/Og/v3UpvLPbHv/193b3/8fzjn4N2+5Pzn1TQuuMVaVpouirqIlkzrBRA6jkR/UFHkTa1PJT6xzoQ+OQCBVo/nYceMkRPhS2Hw5dhx9cV3dwIRY1b6pHrhsw31wkXUu2UJerw5sYr0FCBU9l2UBWQesTVxiI9o6GlXs7Nx55Xv+7IY1P/97k9s48kd/fFrlJ6r3OiZx7PHKcwJI7A+XCxhqCQoSvIWdlqlNZbNt50zEf/6fL3/zu77w+aNP/bUZJp8nxNTcnMUZg41EI+unkKg/mcw5E9rOJecaxOICO9tzufypKllWUBxe3Xde4jEl4mJ3cHEWoWph0EPmGsJROZqRWHMBCEifsx7zT7MBDoo6q2qda+lohLUs0b1feMGSb7zr+j/6zU+Pg3z3+k3lzexMF1u3J2Pzn7QY4BiMIDOkO3IhSLfcTOl1113DdX/0a5/f9dfj33rp1/73q/Z2V73uKCdtLmmbsnQwKqrW4FwsGllSNURhUIILFCnEuwBnxAd/hKMGQ8dQb0i49DUIy+AoV8mGPWhBIAp+39EDgFwICENPnwd3xGcAicWIxVpBU6uqDpeqdLQiiatGy9lz59rKnms/f8EbPzjxyzNHr7nmmhLPu042s7Pf3295cjf+yQ8CF3MJW8Pv9+Ou/Y/XyebNV1U2/sKnZkq88n2v//Vf+dDXjp77czsWNv5iyyzZGEkqJSyRJlYcxjoTxmz67pjUOFQsEhmM0dzviyuafnIXIX1jvIadDWB7J924nt/PuXp9AhBYwChRt0cgcF2LM6k6Ky61EqVawVpDwx56+NKxe95/xbJbP/Cubdc9NAHc8NeXVS/vXudHjh+H0fNkbf6TLgDHFQJ/w/rGzZ/SZ157cfyV259h3vy7790dwbuu++2f/Js/feQVv3BXc+OrbFQ+rSMjUezaiLpU1BqHSNpFulhKouTdSdnhySZkBoS+QfHC0T8nQBflAeQzCorDHcKUUwmcPcmrhlDyB1+pqFWbOteSOE6pRnE6T9nO3H/uyL1/90urP/pXr/nNf9n1ReCP3/CGypXP/pxu5saEB9BhAM+TvfGLnGvx5H0NzRC29p77ApjlbDLt1TV9yutuSQC2/8aF6//X/v969e5k1ZamNM6bjVdWRhYe4b2VN/PM0Z1uvlSW2KiICaeNR9IbKBmGDYYDzX2aaIrafxwBGAB7JBvAnFsAX4vwAiA6knT1K3Ob5E3tP5LmyFrG071JQ2e/uy7e80//c8MH/+EnfvObuxS46dqLS9XJlmxevtNxOY6tvXMatv4ANv/7KgA5ZX8K2b4T2bIJ7csScggR4Shm+55NcvW2nd0IuGWKk35z7289a2+66kXabl16jfmLMy+p3hF3EOKSw8QGY0QzATDG9/dhfI9ElgUY6Q0YHXayR/Hot4yi61NC0cHRbM46UaskiVBVx82dTbzfXnNXVC1/Y3Vp/8e3rfy9L16wrbkvBa6f2lTesnkn+RFBg7n91sKabOs7benHSwAyIdg6FcaW63BrkIVtOzZvMtfuvVLe96Y/7UQo6fVE//C5809uVOfOXhodvXR+gctsq3N+WZKJamwpxYqJQsDnkwInQTDC5kvhFDk5pgtaC2eHad6XJ+owGg40dA6SVOimho6W5+Nq+fZq1X1pmsmvzTJ5+ytf/u1d8RWeu/KGP35D5U9WfU5ZvtNxEO3z81v7fpatYUzv93PzfyACcFzXMCgEAQjZwaZo547NHN07qb903XVJhtLt/uhF9ekde8+cm0mfMjujF6ed9gXGpRsM6WgkWqtEvsHTGA1t5i6fDTT0YKp8kJ/JU8DUQTcVUicdSzznovgRU6ncNtKIblkyoTctP2dyx/KX3z2XpY03TE3FrSXfiNatekg3s7Nf479Pqd2PlAAMjQ+2DrmuzQjLkVvuvljgFi4GuIa0uHj6mVPGd90xt+GRA5WN3dn01KSbbFDrNkSuuwLVhqo0BFcFrYRDbqMCDJT6Y6+jthFdECNNR+mARDwYV0oPUhm5b/Vae/9Zm+oPyPPvPFzEE9xfUGISueXoxXrxGbcoB8M1/RBv/A+VADxmyxAE4pajmIsnkXv2IqdXcbyOtM+ECqg7rcIn58d27anW798/0XDdVj3udhqR2kpkXWwNgpjEqOm2y5UFU6o116xsNc9cP7PACzbOifl6axAtvvka4os3Y+4BTq/ikbthG74V4QcU3P14CcDxBCGzDjsQNmPYizwwhiSzaPcIunknVrZjH881XL+FaBNE5ZVI6SJkwwqUeZTluKFa/iO08T/0AlAUhCFYgvSBSsMEwm+OsBrZMYlsBu7Ze+zrT1+FDj5/+ip0B7D5KMoelM2FzRsG2PyIbfqPlAAcVxBOxDoMHoq4/QTueUsoZT96uVt+WH37j50APG7BOFEBGbbBJ1jv+FHa8GPuQ2G1wJ7HMlfmR0UwnowC14/b1/8P23mS0Kqte6QAAAAASUVORK5CYII='}
EMO_DIR = os.path.join(store.cache_dir(), "emoji")          # caches: safe to delete, rebuilt when needed
CAP_DIR = os.path.join(store.cache_dir(), "captions")
LEGACY_EMO_DIR = os.path.join(store.legacy_dir(), "emoji")    # Clip Studio's downloads are still used
EMO_MEM, EMO_STATE = {}, {"offline": False}
EMOJI_RE = re.compile("(?:[\U0001F1E6-\U0001F1FF]{2}|[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55]"
                      "[\U0001F3FB-\U0001F3FF]?\uFE0F?(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF\u2640\u2642]"
                      "[\U0001F3FB-\U0001F3FF]?\uFE0F?)*)")
EMOJI_PICK = ["\U0001F480", "\U0001F62D", "\U0001F602", "\U0001F525", "\U0001F4AF", "\U0001F633", "\U0001F923",
              "\U0001F440", "\U0001F64F", "\U0001F60E", "\U0001F972", "\U0001F92F"]


def default_font():
    fams = QFontDatabase.families()
    for key in ("proxima nova", "metropolis", "montserrat", "figtree", "segoe ui"):
        for f in sorted(fams, key=len):
            if key in f.lower():
                return f
    return "Arial"


def get_emoji_img(e):
    """Apple-style emoji picture for e (cached on disk; downloaded once if needed). None if unavailable."""
    if e in EMO_MEM:
        return EMO_MEM[e]
    os.makedirs(EMO_DIR, exist_ok=True)
    for k, v in SEED_EMOJI.items():
        fp = os.path.join(EMO_DIR, k + ".png")
        if not os.path.exists(fp):
            with open(fp, "wb") as f:
                f.write(base64.b64decode(v))
    cps = [f"{ord(ch):x}" for ch in e]
    names = ["-".join(cps)]
    nf = [x for x in cps if x != "fe0f"]
    if nf != cps:
        names.append("-".join(nf))
    img = None
    for n in names:
        fp = os.path.join(EMO_DIR, n + ".png")
        old = os.path.join(LEGACY_EMO_DIR, n + ".png")
        if not os.path.exists(fp) and os.path.exists(old):
            fp = old
        if os.path.exists(fp):
            img = QImage(fp)
            break
    if img is None and not EMO_STATE["offline"]:
        import urllib.request
        for n in names:
            try:
                url = f"https://cdn.jsdelivr.net/npm/emoji-datasource-apple@15.0.1/img/apple/64/{n}.png"
                data = urllib.request.urlopen(url, timeout=4).read()
                with open(os.path.join(EMO_DIR, n + ".png"), "wb") as f:
                    f.write(data)
                img = QImage.fromData(data)
                break
            except Exception as ex:
                if not isinstance(ex, urllib.error.HTTPError):
                    EMO_STATE["offline"] = True
                    break
    if img is not None and img.isNull():
        img = None
    EMO_MEM[e] = img
    return img


def regular_only(fam):
    st = " ".join(QFontDatabase.styles(fam)).lower()
    return "bold" not in st


def eff_bold(c):
    """Caption text weight 0-100. Automatic: a heavy look when only a Regular weight is installed."""
    return int(c.cbold) if c.cbold >= 0 else (20 if regular_only(c.cfont) else 0)


def caption_layout(text, size, fam, bold=0):
    font = QFont(fam)
    font.setPixelSize(size)
    if bold > 0:   # thickened text needs a little more room between letters
        font.setLetterSpacing(QFont.AbsoluteSpacing, size * 0.09 * bold / 100 * 0.5)
    st = " ".join(QFontDatabase.styles(fam)).lower()
    if "semibold" in st or "bold" in st:
        font.setWeight(QFont.DemiBold)
    fm = QFontMetricsF(font)
    emw, sp = size * 1.12, fm.horizontalAdvance(" ")
    pad = size * 0.38
    maxw = 1080 * 0.84 - 2 * pad
    lines = []
    for para in text.split("\n"):
        toks = []   # (pieces, width); an emoji never gets a line of its own, it stays with the word before it
        for chunk in para.split():
            pieces, pos = [], 0
            for m in EMOJI_RE.finditer(chunk):
                if m.start() > pos:
                    pieces.append(("t", chunk[pos:m.start()]))
                pieces.append(("e", m.group()))
                pos = m.end()
            if pos < len(chunk):
                pieces.append(("t", chunk[pos:]))
            pw = sum(fm.horizontalAdvance(v) if k == "t" else emw for k, v in pieces)
            if toks and all(k == "e" for k, _ in pieces):
                toks[-1] = (toks[-1][0] + [("t", " ")] + pieces, toks[-1][1] + sp + pw)
            else:
                toks.append((pieces, pw))
        if not toks:
            continue
        n = len(toks)

        def span(i, j):
            return sum(t[1] for t in toks[i:j]) + sp * (j - i - 1)

        cnt, curw = 1, 0.0
        for i, t in enumerate(toks):   # fewest lines possible
            if i and curw + sp + t[1] > maxw:
                cnt, curw = cnt + 1, t[1]
            else:
                curw += (sp if i else 0) + t[1]
        INF = float("inf")
        best = [[INF] * (n + 1) for _ in range(cnt + 1)]
        prev = [[0] * (n + 1) for _ in range(cnt + 1)]
        best[0][0] = 0.0
        for k in range(1, cnt + 1):    # spread the words so the lines come out even
            for j in range(1, n + 1):
                for i in range(j - 1, -1, -1):
                    w = span(i, j)
                    if w > maxw and j - i > 1:
                        break
                    if best[k - 1][i] == INF:
                        continue
                    cost = best[k - 1][i] + (maxw - w) ** 2
                    if cost < best[k][j]:
                        best[k][j], prev[k][j] = cost, i
        cuts, j = [], n
        for k in range(cnt, 0, -1):
            cuts.append((prev[k][j], j))
            j = prev[k][j]
        for i, j in reversed(cuts):
            cur = []
            for t in toks[i:j]:
                if cur:
                    cur.append(("t", " "))
                cur += t[0]
            lines.append((cur, span(i, j)))
    return font, fm, emw, pad, lines


def render_caption(c):
    """Draw the caption as a transparent PNG: one white rounded box per line, each hugging its own text."""
    size = max(12, int(c.csize))
    text = c.text.strip() or " "
    bold = eff_bold(c)
    key = hashlib.md5(f"{text}|{size}|{c.cfont}|{c.ccol}|{c.cbox}|{int(c.cuse)}|{bold}|v9".encode()).hexdigest()[:16]
    os.makedirs(CAP_DIR, exist_ok=True)
    fp = os.path.join(CAP_DIR, key + ".png")
    font, fm, emw, pad, lines = caption_layout(text, size, c.cfont, bold)
    if not lines:
        lines = [([("t", " ")], 0.0)]
    lh = size * 1.24
    boxw = [w + 2 * pad for _, w in lines]
    W, H = int(math.ceil(max(boxw))) + 8, int(math.ceil(lh * len(lines))) + 8
    if not os.path.exists(fp):
        img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        p.setFont(font)
        if c.cuse:
            # one merged shape: outer corners rounded, where a narrower line meets a wider one the sides run
            # straight into it (no curve), exactly like CapCut's label captions
            rad = size * 0.3
            path = QPainterPath()
            fillets = []
            for i, bw in enumerate(boxw):
                top, bot = 4 + i * lh, 4 + (i + 1) * lh
                if i > 0 and boxw[i - 1] >= bw - 0.5:
                    top -= rad + 1
                if i < len(boxw) - 1 and boxw[i + 1] > bw + 0.5:
                    bot += rad + 1
                q = QPainterPath()
                q.addRoundedRect(QRectF((W - bw) / 2, top, bw, bot - top), rad, rad)
                path = q if i == 0 else path.united(q)
            for i in range(len(boxw) - 1):   # soft inner corners where the line width changes
                Y = 4 + (i + 1) * lh
                wide, nar = max(boxw[i], boxw[i + 1]), min(boxw[i], boxw[i + 1])
                step = (wide - nar) / 2
                if step < 1:
                    continue
                f = min(rad, step)
                up = boxw[i] < boxw[i + 1]     # narrower line is the upper one
                for sx in (-1, 1):
                    cx = W / 2 + sx * nar / 2
                    cy2 = Y - f if up else Y + f
                    sq = QPainterPath()
                    sq.addRect(QRectF(cx if sx > 0 else cx - f, min(Y, cy2), f, f))
                    ci = QPainterPath()
                    ci.addEllipse(QPointF(cx + sx * f, cy2), f, f)
                    fillets.append(sq.subtracted(ci))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(c.cbox))
            p.drawPath(path)
            for fp_ in fillets:
                p.drawPath(fp_)
        faux = bold > 0
        emo_font = QFont("Segoe UI Emoji")
        emo_font.setPixelSize(int(size * 0.9))
        for i, (pieces, w) in enumerate(lines):
            x = (W - w) / 2
            cy = 4 + i * lh + lh / 2
            base = cy + (fm.ascent() - fm.descent()) / 2
            for kind, v in pieces:
                if kind == "t":
                    if c.cuse and not faux:
                        p.setPen(QColor(c.ccol))
                        p.drawText(QPointF(x, base), v)
                    elif c.cuse:  # only a regular weight is installed: thicken it to a semibold look
                        tp = QPainterPath()
                        tp.addText(QPointF(x, base), font, v)
                        p.setPen(QPen(QColor(c.ccol), size * 0.09 * bold / 100, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                        p.setBrush(QColor(c.ccol))
                        p.drawPath(tp)
                    else:
                        tp = QPainterPath()
                        tp.addText(QPointF(x, base), font, v)
                        p.setBrush(Qt.NoBrush)
                        p.setPen(QPen(QColor(0, 0, 0), size * 0.17, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                        p.drawPath(tp)
                        p.setPen(Qt.NoPen)
                        p.setBrush(QColor(c.ccol))
                        p.drawPath(tp)
                    x += fm.horizontalAdvance(v)
                else:
                    em = get_emoji_img(v)
                    es = size * 1.04
                    if em is not None:
                        p.drawImage(QRectF(x + (emw - es) / 2, cy - es / 2 + size * 0.03, es, es), em)
                    else:
                        p.setFont(emo_font)
                        p.setPen(QColor(c.ccol))
                        p.drawText(QRectF(x, cy - lh / 2, emw, lh), Qt.AlignCenter, v)
                        p.setFont(font)
                    x += emw
        p.end()
        img.save(fp)
    return fp, W, H


# ----------------------------------------------------------------------------- subtitles (animated, burned in with ASS)

ANIMS = ["Zoom In", "Pop", "Fade", "Slide Up", "Word reveal", "Word pop", "Karaoke highlight", "None"]


def prep_fonts(workdir, family):
    """Copy matching font files next to the subtitles so ffmpeg/libass can always find them."""
    fd = os.path.join(workdir, "fonts")
    os.makedirs(fd, exist_ok=True)
    key = re.sub(r"\s+", "", family).lower()
    dirs = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"),
        "/usr/share/fonts", os.path.expanduser("~/.fonts"),
    ]
    n = 0
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                low = f.lower().replace(" ", "")
                if low.startswith(key) and low.endswith((".ttf", ".otf", ".ttc")):
                    try:
                        dst = os.path.join(fd, f)
                        if not os.path.exists(dst):
                            shutil.copy(os.path.join(root, f), dst)
                        n += 1
                    except Exception:
                        pass
    return n > 0


def chunk_words(words, max_words=4, max_gap=0.8, max_chars=28):
    caps, cur = [], []
    for w in words:
        if cur:
            gap = w[1] - cur[-1][2]
            chars = sum(len(x[0]) + 1 for x in cur) + len(w[0])
            if (len(cur) >= max_words or gap > max_gap or chars > max_chars
                    or cur[-1][0][-1:] in ".?!"):
                caps.append(cur)
                cur = []
        cur.append(list(w))
    if cur:
        caps.append(cur)
    out = []
    for c in caps:
        start, end = c[0][1], c[-1][2]
        out.append({"start": start, "end": max(end, start + 0.4), "words": c})
    return out


def set_times(c, s, e):
    """Move/resize a caption, rescaling its word timings with it."""
    o0, o1 = c["start"], c["end"]
    span = max(o1 - o0, 1e-3)
    for w in c["words"]:
        w[1] = s + (w[1] - o0) / span * (e - s)
        w[2] = s + (w[2] - o0) / span * (e - s)
    c["start"], c["end"] = s, e


def ass_col(hexc):
    h = hexc.lstrip("#")
    return h[4:6] + h[2:4] + h[0:2]


def ass_time(t):
    cs = int(round(max(0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def paren_flags(toks):
    """0 = normal word, 1 = inside ( ), 2 = inside [ ]  (each kind can have its own colour)."""
    flags, inp = [], 0
    for t in toks:
        if t.startswith("("):
            inp = 1
        elif t.startswith("["):
            inp = 2
        flags.append(inp)
        if t.endswith((")", "]")):
            inp = 0
    return flags


def build_sub_ass(caps, st, W, H):
    k = W / 1080.0
    size = max(8, round(st["size"] * k))
    outline = round(st["outline"] * k, 1)
    x, y = int(W * st.get("xpct", 50) / 100), int(H * st["ypct"] / 100)
    D = max(50, int(st["anim_ms"]))
    mainc, unsp, tagc = ass_col(st["color"]), ass_col(st["color2"]), ass_col(st["tag_color"])
    tcol = {1: tagc, 2: ass_col(st.get("tag_color2", "#FF3B3B"))}
    anim = st["anim"]
    ta = st.get("tag_anim", "Same as subtitle")
    mixed = ta != "Same as subtitle"
    bold = -1 if st["bold"] else 0

    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{st['font']},{size},&H00{mainc},&H00{unsp},&H00{ass_col(st['outline_color'])},"
        f"&H00000000,{bold},0,0,0,100,100,0,0,1,{outline},0,5,{int(60 * k)},{int(60 * k)},0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for c in caps:
        ws, start, end = c["words"], c["start"], c["end"]
        toks = []
        for w in ws:
            t = w[0].upper() if st["upper"] else w[0]
            toks.append(t.replace("{", "(").replace("}", ")"))
        flags = paren_flags(toks)

        pos = f"\\pos({x},{y})"
        g = ""
        eff = anim if (not mixed or anim == "Slide Up") else "None"
        if eff == "Zoom In":
            g = f"\\fscx60\\fscy60\\alpha&HFF&\\t(0,{D},\\fscx100\\fscy100\\alpha&H00&)"
        elif eff == "Pop":
            a = int(D * 0.6)
            g = (f"\\fscx60\\fscy60\\alpha&HFF&\\t(0,{a},\\fscx115\\fscy115\\alpha&H00&)"
                 f"\\t({a},{D},\\fscx100\\fscy100)")
        elif eff == "Fade":
            g = f"\\fad({D},120)"
        elif eff == "Slide Up":
            pos = f"\\move({x},{y + int(H * 0.035)},{x},{y},0,{D})"
            g = f"\\fad({D},0)"

        if mixed:  # tag words ( ) / [ ] get their own animation
            def extra(wa, rel):
                if wa == "Zoom In":
                    return f"\\fscx60\\fscy60\\alpha&HFF&\\t(0,{D},\\fscx100\\fscy100\\alpha&H00&)"
                if wa == "Pop":
                    a2 = int(D * 0.6)
                    return (f"\\fscx60\\fscy60\\alpha&HFF&\\t(0,{a2},\\fscx115\\fscy115\\alpha&H00&)"
                            f"\\t({a2},{D},\\fscx100\\fscy100)")
                if wa == "Fade" or (wa == "Slide Up" and anim != "Slide Up"):
                    return f"\\alpha&HFF&\\t(0,{D},\\alpha&H00&)"
                if wa == "Word reveal":
                    return f"\\alpha&HFF&\\t({rel},{rel + 1},\\alpha&H00&)"
                if wa == "Word pop":
                    return (f"\\alpha&HFF&\\fscx60\\fscy60\\t({rel},{rel + 1},\\alpha&H00&)"
                            f"\\t({rel},{rel + D},\\fscx100\\fscy100)")
                return ""
            anyk = "Karaoke highlight" in (anim, ta)
            parts, prev_end = [], start
            for i, (t, f) in enumerate(zip(toks, flags)):
                w = ws[i]
                wa = ta if f else anim
                rel = max(0, int((w[1] - start) * 1000))
                c1 = tcol[f] if f else mainc
                sp = " " if i < len(ws) - 1 else ""
                pre = ""
                if anyk:
                    gap = max(0, int(round((w[1] - prev_end) * 100)))
                    dur = max(1, int(round((w[2] - w[1]) * 100)))
                    prev_end = w[2]
                    pre = f"{{\\k{gap}}}" if gap else ""
                    if wa == "Karaoke highlight":
                        blk = pre + f"{{\\1c&H{c1}&\\2c&H{tcol[f] if f else unsp}&\\kf{dur}}}"
                    else:
                        blk = pre + f"{{\\1c&H{c1}&\\2c&H{c1}&\\k{dur}{extra(wa, rel)}}}"
                else:
                    blk = f"{{\\1c&H{c1}&{extra(wa, rel)}}}"
                parts.append(blk + t + sp)
            body = "".join(parts)
        elif anim in ("Word reveal", "Word pop", "Karaoke highlight"):
            parts, prev_end = [], start
            for i, (t, f) in enumerate(zip(toks, flags)):
                w = ws[i]
                rel = max(0, int((w[1] - start) * 1000))
                c1 = tcol[f] if f else mainc
                sp = " " if i < len(ws) - 1 else ""
                if anim == "Karaoke highlight":
                    gap = max(0, int(round((w[1] - prev_end) * 100)))
                    dur = max(1, int(round((w[2] - w[1]) * 100)))
                    c2 = tcol[f] if f else unsp
                    blk = (f"{{\\k{gap}}}" if gap else "") + f"{{\\1c&H{c1}&\\2c&H{c2}&\\kf{dur}}}"
                    prev_end = w[2]
                elif anim == "Word reveal":
                    blk = f"{{\\1c&H{c1}&\\alpha&HFF&\\t({rel},{rel + 1},\\alpha&H00&)}}"
                else:
                    blk = (f"{{\\1c&H{c1}&\\alpha&HFF&\\fscx60\\fscy60"
                           f"\\t({rel},{rel + 1},\\alpha&H00&)\\t({rel},{rel + D},\\fscx100\\fscy100)}}")
                parts.append(blk + t + sp)
            body = "".join(parts)
        else:
            body = " ".join(f"{{\\1c&H{tcol[f] if f else mainc}&}}{t}" for t, f in zip(toks, flags))

        out.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,"
                   f"{{\\an5{pos}{g}}}{body}")
    return "\n".join(out) + "\n"




def swatch_icon(color):
    """A small rounded colour square, for colour buttons."""
    dpr = 2
    pm = QPixmap(16 * dpr, 16 * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(255, 255, 255, 60), 1))
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(0.5, 0.5, 15, 15), 3, 3)
    p.end()
    return QIcon(pm)


class ColorButton(QPushButton):
    changed = Signal()

    def __init__(self, color):
        super().__init__()
        self.color = color
        self.setObjectName("swatch")
        self.setFixedWidth(110)
        self.setIconSize(QSize(16, 16))
        self.clicked.connect(self.pick)
        self.paint()

    def paint(self):
        self.setIcon(swatch_icon(self.color))
        self.setText(self.color.upper())

    def set_color(self, color):
        self.color = color
        self.paint()

    def pick(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Pick colour")
        if c.isValid():
            self.set_color(c.name().upper())
            self.changed.emit()




class SubItem(QGraphicsItem):
    """Paints the current caption over the video, in video-pixel coordinates (mirrors the export look)."""

    def __init__(self, win):
        super().__init__()
        self.win, self.W, self.H, self.t = win, 1080, 1920, 0.0
        self.box, self.drag, self.dragging = None, None, False
        self.setZValue(10)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setAcceptHoverEvents(True)

    def boundingRect(self):
        return QRectF(0, 0, self.W, self.H)

    def set_time(self, t):
        self.t = t
        on = self.win.sub_active(t) is not None
        if on or getattr(self, "_had", True):
            self.update()
        self._had = on

    def handle(self):
        return 9.0 / (self.win.view.transform().m11() or 1.0)

    def shape(self):
        p = QPainterPath()
        if self.box is not None:
            m = self.handle()
            p.addRect(self.box.adjusted(-m, -m, m, m))
        return p

    def corner(self, pos):
        if self.box is None:
            return None
        m = self.handle()
        b = self.box
        for name, pt in (("tl", b.topLeft()), ("tr", b.topRight()), ("bl", b.bottomLeft()), ("br", b.bottomRight())):
            if abs(pos.x() - pt.x()) <= m and abs(pos.y() - pt.y()) <= m:
                return name
        return None

    def hoverMoveEvent(self, e):
        self.setCursor(Qt.SizeFDiagCursor if self.corner(e.pos()) else Qt.SizeAllCursor)

    def mousePressEvent(self, e):
        st = self.win.sub_style()
        cx, cy = self.W * st["xpct"] / 100, self.H * st["ypct"] / 100
        pos = e.pos()
        if self.corner(pos):
            self.drag = ("size", cx, cy, max(1.0, math.hypot(pos.x() - cx, pos.y() - cy)), st["size"])
        else:
            self.drag = ("move", pos.x() - cx, pos.y() - cy)
        self.dragging = True

    def mouseMoveEvent(self, e):
        if not self.drag:
            return
        pos, w = e.pos(), self.win
        if self.drag[0] == "move":
            nx = (pos.x() - self.drag[1]) / self.W * 1000
            ny = (pos.y() - self.drag[2]) / self.H * 1000
            w.sub_xpos.setValue(int(max(0, min(1000, round(nx)))))
            w.sub_ypos.setValue(int(max(50, min(950, round(ny)))))
        else:
            _, cx, cy, d0, size0 = self.drag
            d = math.hypot(pos.x() - cx, pos.y() - cy)
            w.sub_size.setValue(int(max(10, min(300, round(size0 * d / d0)))))

    def mouseReleaseEvent(self, e):
        self.drag, self.dragging = None, False
        self.win.sub_changed()

    def paint(self, p, opt, widget=None):
        win, t = self.win, self.t
        c = win.sub_active(t)
        if not c:
            self.box = None
            return
        st = win.sub_style()
        W, H = self.W, self.H
        k = W / 1080.0
        px = max(8.0, st["size"] * k)
        font = QFont(st["font"])
        font.setBold(st["bold"])
        font.setPixelSize(int(px))
        h0 = QFontMetricsF(font).height()
        font.setPixelSize(max(6, int(round(px * px / h0))))
        fm = QFontMetricsF(font)

        toks = [(w[0].upper() if st["upper"] else w[0]) for w in c["words"]]
        flags = paren_flags(toks)
        space = fm.horizontalAdvance(" ")
        maxw = W - 120 * k
        lines, cur = [[]], 0.0
        for i, tk in enumerate(toks):
            wd = fm.horizontalAdvance(tk)
            if lines[-1] and cur + space + wd > maxw:
                lines.append([])
                cur = 0.0
            if lines[-1]:
                cur += space
            lines[-1].append((i, wd))
            cur += wd
        lh = fm.height()
        total = lh * len(lines)
        cx, cy = W * st.get("xpct", 50) / 100.0, H * st["ypct"] / 100.0
        pos = {}
        for li, line in enumerate(lines):
            lw = sum(wd for _, wd in line) + space * (len(line) - 1)
            x, top = cx - lw / 2, cy - total / 2 + li * lh
            for i, wd in line:
                pos[i] = (x, top, wd)
                x += wd + space
        pad = 12 * k
        self.box = QRectF(QPointF(min(v[0] for v in pos.values()) - pad, min(v[1] for v in pos.values()) - pad / 2),
                          QPointF(max(v[0] + v[2] for v in pos.values()) + pad,
                                  max(v[1] for v in pos.values()) + lh + pad / 2))

        rel = (t - c["start"]) * 1000
        D = max(50, st["anim_ms"])
        a = st["anim"]
        ta = st.get("tag_anim", "Same as subtitle")
        mixed = ta != "Same as subtitle"
        ga_a = a if (not mixed or a == "Slide Up") else "None"
        gs, ga, dy = 1.0, 1.0, 0.0
        if ga_a == "Zoom In":
            q = min(1.0, rel / D)
            gs, ga = 0.6 + 0.4 * q, q
        elif ga_a == "Pop":
            A = D * 0.6
            if rel < A:
                q = rel / A
                gs, ga = 0.6 + 0.55 * q, q
            elif rel < D:
                gs = 1.15 - 0.15 * ((rel - A) / max(D - A, 1))
        elif ga_a == "Fade":
            ga = min(1.0, rel / D)
            out = (c["end"] - t) * 1000
            if out < 120:
                ga *= max(0.0, out / 120)
        elif ga_a == "Slide Up":
            q = min(1.0, rel / D)
            dy, ga = H * 0.035 * (1 - q), q

        mainc, unspc, tagc = QColor(st["color"]), QColor(st["color2"]), QColor(st["tag_color"])
        tcol = {1: tagc, 2: QColor(st.get("tag_color2", "#FF3B3B"))}
        words = []
        for i, tk in enumerate(toks):
            x, top, wd = pos[i]
            w = c["words"][i]
            fill, s, frac, al = (tcol[flags[i]] if flags[i] else mainc), 1.0, None, 1.0
            wa = ta if (mixed and flags[i]) else a
            if wa == "Word reveal":
                if t < w[1]:
                    continue
            elif wa == "Word pop":
                if t < w[1]:
                    continue
                s = 0.6 + 0.4 * min(1.0, max(0.0, (t - w[1]) * 1000 / D))
            elif wa == "Karaoke highlight":
                frac = 0.0 if t < w[1] else min(1.0, (t - w[1]) / max(w[2] - w[1], 1e-3))
            elif mixed and wa == "Zoom In":
                q = min(1.0, rel / D)
                s, al = 0.6 + 0.4 * q, q
            elif mixed and wa == "Pop":
                A = D * 0.6
                if rel < A:
                    q = rel / A
                    s, al = 0.6 + 0.55 * q, q
                elif rel < D:
                    s = 1.15 - 0.15 * ((rel - A) / max(D - A, 1))
            elif mixed and (wa == "Fade" or (wa == "Slide Up" and a != "Slide Up")):
                al = min(1.0, rel / D)
            path = QPainterPath()
            path.addText(QPointF(x, top + fm.ascent()), font, tk)
            if s != 1.0:
                tr = QTransform()
                tr.translate(x + wd / 2, top + lh / 2)
                tr.scale(s, s)
                tr.translate(-(x + wd / 2), -(top + lh / 2))
                path = tr.map(path)
            words.append((path, fill, (tcol[flags[i]] if flags[i] else unspc), frac, x, top, wd, lh, al))

        p.setRenderHint(QPainter.Antialiasing)
        p.save()
        p.setOpacity(ga)
        p.translate(0, dy)
        p.translate(cx, cy)
        p.scale(gs, gs)
        p.translate(-cx, -cy)
        pen = QPen(QColor(st["outline_color"]), max(0.0, 2 * st["outline"] * k))
        pen.setJoinStyle(Qt.RoundJoin)
        if st["outline"] > 0:
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            for path, *_r, al in words:
                p.setOpacity(ga * al)
                p.drawPath(path)
        p.setPen(Qt.NoPen)
        for path, fill, ufill, frac, x, top, wd, lh_, al in words:
            p.setOpacity(ga * al)
            if frac is None:
                p.setBrush(fill)
                p.drawPath(path)
            else:
                p.setBrush(ufill)
                p.drawPath(path)
                if frac > 0:
                    p.save()
                    p.setClipRect(QRectF(x - 4, top - 6, wd * frac + 4, lh_ + 12))
                    p.setBrush(fill)
                    p.drawPath(path)
                    p.restore()
        p.restore()
        if not win.playing:
            m = self.handle()
            blue = QColor("#5B9BFF")
            p.setPen(QPen(blue, m / 6, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.box)
            p.setBrush(blue)
            p.setPen(Qt.NoPen)
            b = self.box
            for pt in (b.topLeft(), b.topRight(), b.bottomLeft(), b.bottomRight()):
                p.drawRect(QRectF(pt.x() - m / 2, pt.y() - m / 2, m, m))




# ----------------------------------------------------------------------------- data model

TRACKS = [("Overlay 2", "v"), ("Overlay 1", "v"), ("Video", "v"), ("Audio", "a"), ("Music", "a"), ("Subtitles", "s")]
KIND_TRACK = {"audio": "a", "sub": "s"}
SLIDE_DIR = {"Slide left": (1, 0), "Slide right": (-1, 0), "Slide up": (0, 1), "Slide down": (0, -1),
             "Push left": (1, 0), "Push right": (-1, 0), "Push up": (0, 1), "Push down": (0, -1)}
WIPES = ["Wipe left", "Wipe right", "Wipe up", "Wipe down", "Circle open"]
TRANS = ["None", "Crossfade", "Dip to black", "Dip to white", *SLIDE_DIR, *WIPES]
BIG = 1e9


class Clip:
    FIELDS = ["id", "name", "path", "kind", "track", "start", "inp", "dur", "srcdur", "w", "h", "hasaudio",
              "fin", "fout", "vol", "scale", "x", "y", "ttype", "tdur", "nat", "text", "csize", "cfont", "ccol", "cbox",
              "cuse", "words", "wdur", "cbold"]

    def __init__(self, **k):
        self.id = uuid.uuid4().hex[:8]
        self.name = ""
        self.path = ""
        self.kind = "video"
        self.track = 2
        self.start = 0.0
        self.inp = 0.0
        self.dur = 5.0
        self.srcdur = BIG
        self.w, self.h = 1080, 1920
        self.hasaudio = False
        self.fin = 0.0
        self.fout = 0.0
        self.vol = 100
        self.scale = 100
        self.x = 0.0
        self.y = 0.0
        self.ttype = "None"
        self.tdur = 1.0
        self.nat = 0
        self.text, self.csize, self.cfont = "", 64, "Segoe UI"
        self.ccol, self.cbox, self.cuse = "#000000", "#ffffff", True
        self.cbold = -1                 # caption text weight 0-100; -1 = automatic
        self.words, self.wdur = [], 1.0
        for a, b in k.items():
            setattr(self, a, b)

    @property
    def end(self):
        return self.start + self.dur

    def to_dict(self):
        return {f: getattr(self, f) for f in self.FIELDS}


def prev_of(clips, c):
    best = None
    for o in clips:
        if o is not c and o.track == c.track and abs(o.end - c.start) < 0.05:
            if best is None or o.end > best.end:
                best = o
    return best


def next_of(clips, c):
    for o in clips:
        if o is not c and o.track == c.track and abs(o.start - c.end) < 0.05:
            return o
    return None


def trans_info(clips, c):
    """(kind, duration, prev) if c has a usable transition with the clip before it, else None."""
    if c.ttype == "None" or c.kind in ("audio", "sub"):
        return None
    p = prev_of(clips, c)
    if not p or p.kind in ("audio", "sub"):
        return None
    d = min(c.tdur, c.dur * 0.9, p.dur * 0.9)
    return (c.ttype, d, p) if d > 0.05 else None


def eff_fades(clips, c):
    """(fade_in, fade_out, extension) actually applied to clip c, including transitions."""
    fi, fo, ext = c.fin, c.fout, 0.0
    ti = trans_info(clips, c)
    if ti:
        if ti[0] == "Crossfade":
            fi = max(fi, ti[1])
        elif ti[0] == "Dip to black":
            fi = max(fi, ti[1] / 2)
    nx = next_of(clips, c)
    if nx:
        t2 = trans_info(clips, nx)
        if t2:
            if t2[0] == "Dip to black":
                fo = max(fo, t2[1] / 2)
            elif t2[0] != "Dip to white":
                ext = t2[1]
    return fi, fo, ext


def fitf(c, W, H):
    """Pixels-per-source-pixel at size 100: fit-to-canvas for media, true size for captions."""
    return W / 1080.0 if c.nat else min(W / max(1, c.w), H / max(1, c.h))


def dip_white(clips, c):
    """(fade-in-from-white, fade-out-to-white) seconds for 'Dip to white' transitions."""
    win = wout = 0.0
    ti = trans_info(clips, c)
    if ti and ti[0] == "Dip to white":
        win = ti[1] / 2
    nx = next_of(clips, c)
    if nx:
        t2 = trans_info(clips, nx)
        if t2 and t2[0] == "Dip to white":
            wout = t2[1] / 2
    return win, wout


def motion_exprs(clips, c, W, H):
    """ffmpeg overlay x/y expressions for clip c, including slide/push transitions."""
    xe, ye = f"(W-w)/2+{c.x / 100 * W:.2f}", f"(H-h)/2+{c.y / 100 * H:.2f}"
    ti = trans_info(clips, c)
    if ti and ti[0] in SLIDE_DIR:
        ux, uy = SLIDE_DIR[ti[0]]
        f = f"max(0,min(1,1-(t-{c.start:.3f})/{ti[1]:.3f}))"
        xe += f"+{ux * W}*{f}"
        ye += f"+{uy * H}*{f}"
    nx = next_of(clips, c)
    if nx:
        t2 = trans_info(clips, nx)
        if t2 and t2[0].startswith("Push"):
            ux, uy = SLIDE_DIR[t2[0]]
            f = f"max(0,min(1,(t-{nx.start:.3f})/{t2[1]:.3f}))"
            xe += f"-{ux * W}*{f}"
            ye += f"-{uy * H}*{f}"
    return xe, ye


def wipe_alpha(kind, S, D):
    p = f"clip((T-{S:.3f})/{D:.3f},0,1)"
    cond = {"Wipe left": f"gt(X,W*(1-{p}))", "Wipe right": f"lt(X,W*{p})",
            "Wipe up": f"gt(Y,H*(1-{p}))", "Wipe down": f"lt(Y,H*{p})",
            "Circle open": f"lt(hypot(X-W/2,Y-H/2),{p}*hypot(W/2,H/2))"}[kind]
    return f"if({cond},alpha(X,Y),0)"


def project_len(clips):
    return max([c.end for c in clips] + [0.0])


# ----------------------------------------------------------------------------- export


def build_export(clips, W, H, fps, out, nvenc, subs=None):
    """Return (cmd list, total seconds)."""
    T = max(project_len(clips), 0.1)
    inputs, fparts, kmap = [], [], {}
    vclips = [c for c in clips if c.track <= 2 and c.kind in ("video", "image")]
    vclips.sort(key=lambda c: (-c.track, c.start))
    last = "base"
    fparts.append(f"color=c=black:s={W}x{H}:r={fps}:d={T:.3f}[base]")
    for i, c in enumerate(vclips):
        fi, fo, ext = eff_fades(clips, c)
        k = inputs.count("-i")
        if c.kind == "image":
            inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{c.dur + ext:.3f}", "-i", c.path]
        else:
            inputs += ["-ss", f"{c.inp:.3f}", "-t", f"{c.dur:.3f}", "-i", c.path]
        kmap[c.id] = k
        bw, bh = max(2, int(W * c.scale / 100)), max(2, int(H * c.scale / 100))
        if c.nat:
            sf = f"scale=trunc(iw*{fitf(c, W, H) * c.scale / 100:.5f}/2)*2:trunc(ih*{fitf(c, W, H) * c.scale / 100:.5f}/2)*2:flags=bicubic"
        else:
            sf = f"scale={bw}:{bh}:force_original_aspect_ratio=decrease:flags=bicubic"
        ch = (f"[{k}:v]fps={fps},{sf},setsar=1,format=rgba,setpts=PTS-STARTPTS")
        if c.kind == "video" and ext > 0:
            ch += f",tpad=stop_mode=clone:stop_duration={ext:.3f}"
        if fi > 0:
            ch += f",fade=t=in:st=0:d={fi:.3f}:alpha=1"
        if fo > 0:
            ch += f",fade=t=out:st={max(0, c.dur + ext - fo):.3f}:d={fo:.3f}:alpha=1"
        wi, wo = dip_white(clips, c)
        if wi > 0:
            ch += f",fade=t=in:st=0:d={wi:.3f}:color=white"
        if wo > 0:
            ch += f",fade=t=out:st={max(0, c.dur + ext - wo):.3f}:d={wo:.3f}:color=white"
        xe, ye = motion_exprs(clips, c, W, H)
        ti = trans_info(clips, c)
        ov = "overlay=x='{x}':y='{y}':eof_action=pass:enable='between(t,{a:.3f},{b:.3f})':format=auto"
        if ti and ti[0] in WIPES:
            d = ti[1]
            fparts.append(ch + f"[p{i}]")
            fparts.append(f"[p{i}]split[p{i}a][p{i}b]")
            fparts.append(f"[p{i}a]trim=duration={d:.3f},setpts=PTS-STARTPTS+{c.start:.3f}/TB,format=gbrap,"
                          f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{wipe_alpha(ti[0], c.start, d)}',"
                          f"format=rgba[v{i}w]")
            fparts.append(f"[p{i}b]trim=start={d:.3f},setpts=PTS-STARTPTS+{c.start + d:.3f}/TB[v{i}r]")
            fparts.append(f"[{last}][v{i}w]" + ov.format(x=xe, y=ye, a=c.start, b=c.start + d) + f"[b{i}w]")
            fparts.append(f"[b{i}w][v{i}r]" + ov.format(x=xe, y=ye, a=c.start + d, b=c.end + ext) + f"[b{i}]")
        else:
            ch += f",setpts=PTS+{c.start:.3f}/TB[v{i}]"
            fparts.append(ch)
            fparts.append(f"[{last}][v{i}]" + ov.format(x=xe, y=ye, a=c.start, b=c.end + ext) + f"[b{i}]")
        last = f"b{i}"
    if subs:
        fparts.append(f"[{last}]ass=subs.ass" + (":fontsdir=fonts" if subs == "fonts" else "") + "[vsub]")
        last = "vsub"
    fparts.append(f"[{last}]format=yuv420p[vout]")

    alabels = []
    aclips = [c for c in clips if c.vol > 0 and (c.kind == "audio" or (c.kind == "video" and c.hasaudio))]
    for c in aclips:
        if c.id not in kmap:
            k = inputs.count("-i")
            inputs += ["-ss", f"{c.inp:.3f}", "-t", f"{c.dur:.3f}", "-i", c.path]
        else:
            k = kmap[c.id]
        fi, fo, _ = eff_fades(clips, c)
        ms = int(c.start * 1000)
        ch = (f"[{k}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS,"
              f"volume={c.vol / 100:.3f}")
        if fi > 0:
            ch += f",afade=t=in:st=0:d={fi:.3f}"
        if fo > 0:
            ch += f",afade=t=out:st={max(0, c.dur - fo):.3f}:d={fo:.3f}"
        lab = f"a{len(alabels)}"
        ch += f",adelay={ms}|{ms}[{lab}]"
        fparts.append(ch)
        alabels.append(lab)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *inputs]
    maps = ["-map", "[vout]"]
    if alabels:
        n = len(alabels)
        if n == 1:
            fparts.append(f"[{alabels[0]}]anull[aout]")
        elif amix_has_normalize():
            fparts.append("".join(f"[{a}]" for a in alabels) + f"amix=inputs={n}:duration=longest:normalize=0[aout]")
        else:
            fparts.append("".join(f"[{a}]" for a in alabels) + f"amix=inputs={n}:duration=longest,volume={n}[aout]")
        maps += ["-map", "[aout]"]
    enc = (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19", "-b:v", "0"] if nvenc
           else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17"])
    cmd += ["-filter_complex", ";".join(fparts), *maps, "-t", f"{T:.3f}", "-r", str(fps), *enc,
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", out]
    return cmd, T


# ----------------------------------------------------------------------------- timeline widget

LBL, RULER, TH = 96, 28, 46
COL = {"sub": QColor(150, 70, 150), "video": QColor(54, 112, 190), "image": QColor(140, 84, 190), "audio": QColor(44, 150, 96)}

# timeline colours (made once: the timeline repaints a lot)
TL = dict(bg="#141518", lane="#17181B", lane_main="#1A1B1F", lane_line="#222328", head="#1B1C1F",
          head_edge="#26272C", head_text="#A6A8B0", head_text_main="#E8E9EC", ruler="#1B1C1F",
          tick="#34353B", tick_minor="#2A2B30", ruler_text="#8A8C95", playhead="#F2555A",
          accent="#7FE5FF", select="#F2F3F5", empty="#767880", empty_title="#A6A8B0")
TLC = {k: QColor(v) for k, v in TL.items()}
TRACK_STYLE = {   # body, band (name strip), edge (1px border), wave
    "video":   ("#34557A", "#2A4563", "#4E77A6", "#9FBBE0"),
    "overlay": ("#54497F", "#433A67", "#7466AB", "#B7ADE0"),
    "caption": ("#7A5A2C", "#634823", "#A57B3E", "#E3C08A"),
    "audio":   ("#2F5E48", "#254C3A", "#468A69", "#8FD1AE"),
    "music":   ("#2A5660", "#21454E", "#3F7E8C", "#86C6D2"),
    "sub":     ("#63395A", "#502E49", "#8E5582", "#D6A2C8"),
}
TRACK_CHIP = ["overlay", "overlay", "video", "audio", "music", "sub"]   # header colour chip, by TRACKS index


def _track_colours():
    out = {}
    for k, (body, band, edge, wave) in TRACK_STYLE.items():
        bt, wv = QColor(band), QColor(wave)
        bt.setAlpha(235)
        wv.setAlpha(190)
        out[k] = (QColor(body), QColor(band), QColor(edge), wv, bt)
    return out


TRACK_Q = _track_colours()


def clip_style(c):
    if c.nat:
        return "caption"
    if c.kind == "sub":
        return "sub"
    if c.kind == "audio":
        return "music" if c.track == 4 else "audio"
    if c.kind == "image" or c.track in (0, 1):
        return "overlay"
    return "video"


def fmt_ruler(t, step):
    """Ruler label: 0:05, 1:05.5, 0:00.25, 1:00:00 (only as precise as the tick spacing needs)."""
    d = 0 if step >= 1 else (2 if step == 0.25 else 1)
    t = max(0.0, round(t, d))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    sec = f"{s:0{3 + d}.{d}f}" if d else f"{int(s):02d}"
    return f"{int(h)}:{int(m):02d}:{sec}" if h else f"{int(m)}:{sec}"


def fmt_t(t):
    t = max(0, t)
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


class Timeline(QWidget):
    seek = Signal(float)
    selection = Signal()
    edited = Signal()

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.pps, self.t0 = 80.0, 0.0
        self.mode = None
        self.drop_hint = None
        self.hov = None
        self.hj = None
        self.snap_on = True
        self.ripple = True        # main Video track: close gaps / insert instead of refusing to overlap
        self.mpos = None
        self.mq = None
        self._multi = None
        self._collapse = None
        self.plan = None
        self.press_t = 0.0
        self.ext_lib = self.ext_g = None
        self.hold = 0.0
        self._snap_t = None       # time the dragged clip / edge snapped to (drawn as a thin line)
        self._cache, self._ver = None, 0
        self._tl = time.perf_counter()
        self._auto = QTimer(self)
        self._auto.setInterval(16)
        self._auto.timeout.connect(self._auto_tick)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.th = TH              # track height: grows/shrinks with the timeline (see resizeEvent)
        self.setMinimumHeight(RULER + 30 * len(TRACKS) + 4)
        self.setFocusPolicy(Qt.StrongFocus)
        BUS.wave.connect(self._invalidate)
        # fonts and pens made once, not on every repaint
        self.f_small = QFont(self.font())
        self.f_small.setPixelSize(11)
        self.fm_small = QFontMetrics(self.f_small)
        self.f_head = QFont(self.font())
        self.f_head.setPixelSize(12)
        self.f_head.setWeight(QFont.Medium)
        self.f_empty = QFont(self.font())
        self.f_empty.setPixelSize(13)
        self.f_empty.setWeight(QFont.Medium)
        self.f_empty2 = QFont(self.font())
        self.f_empty2.setPixelSize(12)
        self.pen_sel = QPen(TLC["select"], 2)
        self.pen_hov = QPen(QColor(255, 255, 255, 140), 1)
        self.pen_fade = QPen(QColor(255, 255, 255, 110), 1)
        self.c_band_text = QColor("#F2F3F5")
        self.c_fade = QColor(0, 0, 0, 110)
        self.c_dim = QColor(0, 0, 0, 50)
        self.c_lift = QColor(255, 255, 255, 18)
        self.c_drag = QColor(255, 255, 255, 30)
        self.c_accent_fill = QColor(127, 229, 255, 28)
        self.c_off_body, self.c_off_band = QColor("#3A1D20"), QColor("#4A2226")      # offline media
        self.pen_off_hatch = QPen(QColor(229, 72, 77, 60), 1)
        self.pm_off_warn = mk_icon("warn", "#F07A7E", 12).pixmap(24, 24)

    def _invalidate(self):
        self._ver += 1
        self.update()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.th = max(30, min(64, (self.height() - RULER - 4) // len(TRACKS)))

    # coordinates
    def tx(self, t):
        return LBL + (t - self.t0) * self.pps

    def xt(self, x):
        return (x - LBL) / self.pps + self.t0

    def row_at(self, y):
        r = int((y - RULER) // self.th)
        return r if 0 <= r < len(TRACKS) else None

    def clip_rect(self, c):
        return QRectF(self.tx(c.start), RULER + c.track * self.th + 3, c.dur * self.pps, self.th - 6)

    def clip_at(self, pos):
        for c in reversed(self.win.clips):
            if self.clip_rect(c).contains(pos):
                return c
        return None

    def junction_at(self, pos):
        """The clip whose left edge touches the clip before it, if pos is near that join."""
        if pos.x() < LBL or pos.y() < RULER:
            return None
        for c in self.win.clips:
            if c.kind in ("audio", "sub") or not prev_of(self.win.clips, c):
                continue
            r = self.clip_rect(c)
            if abs(pos.x() - r.left()) <= 14 and r.top() <= pos.y() <= r.bottom():
                return c
        return None

    def junction_btn(self, c):
        r = self.clip_rect(c)
        return QRectF(r.left() - 11, r.center().y() - 11, 22, 22)

    def trans_menu(self, c, gpos):
        m = QMenu(self)
        m.setTitle("Transition")
        order = ["Dip to black"] + [t for t in TRANS if t not in ("Dip to black", "None")] + ["None"]
        for t in order:
            if t == "None":
                m.addSeparator()
            lab = "No transition" if t == "None" else (t + "  (default)" if t == "Dip to black" else t)
            a = m.addAction(lab)
            a.setCheckable(True)
            a.setChecked(t == c.ttype)
            a.triggered.connect(lambda _=False, t=t: self.set_trans_on(c, t))
        m.exec(gpos)

    def set_trans_on(self, c, t):
        self.win.sel = c
        self.win.set_trans(t)
        self.selection.emit()
        self.update()

    def overlaps(self, c, track, start, dur):
        for o in self.win.clips:
            if o is not c and o.track == track and start < o.end - 1e-3 and o.start < start + dur - 1e-3:
                return True
        return False

    def snap(self, t, others_exclude, pix=7):
        self._snap_t = None
        if not self.snap_on or (QApplication.keyboardModifiers() & Qt.AltModifier):
            return t
        ex = others_exclude if isinstance(others_exclude, (list, tuple, set)) else (others_exclude,)
        cands = [0.0, self.win.t]
        for o in self.win.clips:
            if o not in ex:
                cands += [o.start, o.end]
        best, bd = t, pix / self.pps
        for cnd in cands:
            if abs(cnd - t) < bd:
                best, bd = cnd, abs(cnd - t)
        if best != t:
            self._snap_t = best
        return best

    # painting (everything except the playhead is cached; during playback only the playhead is redrawn)
    def _cache_key(self):
        w = self.win
        sig = tuple((c.id, c.start, c.dur, c.inp, c.track, c.fin, c.fout, c.ttype, c.tdur, c.name, c.text, c.kind)
                    for c in w.clips)
        return (self.size().width(), self.size().height(), self.th, self.devicePixelRatioF(), self.t0, self.pps,
                tuple(id(x) for x in w._sels), id(w.sel), id(self.hov), id(self.hj), self.mode, self.mq, self.plan and (self.plan[0], self.plan[2]),
                self.drop_hint, sig, len(THUMBS), len(WAVES), self._ver, self._snap_t, frozenset(w.offline),
                tuple(c.path for c in w.clips))

    def playhead_update(self):
        x = self.tx(self.win.t)
        for xx in (getattr(self, "_phx", x), x):
            self.update(QRect(int(xx) - 9, 0, 19, self.height()))
        self._phx = x

    def paintEvent(self, ev):
        key = self._cache_key()
        if key != getattr(self, "_ck", None) or self._cache is None:
            dpr = self.devicePixelRatioF()
            pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
            pm.setDevicePixelRatio(dpr)
            q = QPainter(pm)
            self.paint_static(q)
            q.end()
            self._cache, self._ck = pm, key
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        x = self.tx(self.win.t)
        if x >= LBL:
            col = TLC["playhead"]
            xi = int(x)
            p.fillRect(QRectF(xi, RULER - 2, 1, self.height()), col)      # 1px crisp line from the ruler down
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawPolygon(QPolygonF([QPointF(xi - 5.5, 2), QPointF(xi + 6.5, 2), QPointF(xi + 6.5, RULER - 12),
                                     QPointF(xi + 0.5, RULER - 4), QPointF(xi - 5.5, RULER - 12)]))   # head in the ruler
        self._phx = x

    def paint_static(self, p):
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(self.rect(), TLC["bg"])
        w = self.width()
        nt = len(TRACKS)
        # tracks: plain lanes (no zebra stripes) and a header column with a colour chip per track type
        p.setFont(self.f_head)
        for i, (nm, kind) in enumerate(TRACKS):
            y = RULER + i * self.th
            p.fillRect(LBL, y, w - LBL, self.th, TLC["lane_main"] if nm == "Video" else TLC["lane"])
            p.setPen(TLC["lane_line"])
            p.drawLine(LBL, y + self.th - 1, w, y + self.th - 1)
            p.fillRect(0, y, LBL, self.th, TLC["head"])
            p.setPen(TLC["head_edge"])
            p.drawLine(0, y + self.th - 1, LBL, y + self.th - 1)
            p.fillRect(QRectF(0, y + 6, 3, self.th - 12), TRACK_Q[TRACK_CHIP[i]][2])
            p.setPen(TLC["head_text_main"] if nm == "Video" else TLC["head_text"])
            p.drawText(14, y, LBL - 18, self.th, Qt.AlignVCenter | Qt.AlignLeft, nm)
        p.setPen(TLC["head_edge"])
        p.drawLine(LBL - 1, RULER, LBL - 1, RULER + self.th * nt)
        # ruler: major ticks with short labels, minor ticks in between
        p.fillRect(0, 0, w, RULER, TLC["ruler"])
        p.setPen(TLC["head_edge"])
        p.drawLine(0, RULER - 1, w, RULER - 1)
        step = next(s for s in [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300] if s * self.pps >= 70)
        k = math.floor(self.t0 / step)           # integer index: no float drift in the labels
        minors = 4 if step in (0.25, 2, 120) else 5
        p.setFont(self.f_small)
        while True:
            t = k * step
            x = self.tx(t)
            if x >= w:
                break
            p.setPen(TLC["tick_minor"])
            for j in range(1, minors):
                xm = int(self.tx(t + j * step / minors))
                if LBL <= xm < w:
                    p.drawLine(xm, RULER - 5, xm, RULER - 2)
            if x >= LBL:
                p.setPen(TLC["tick"])
                p.drawLine(int(x), 4, int(x), RULER - 2)
                p.setPen(TLC["ruler_text"])
                p.drawText(QRectF(x + 4, 2, 90, RULER - 10), Qt.AlignLeft | Qt.AlignVCenter, fmt_ruler(t, step))
            k += 1
        # clips
        p.setClipRect(LBL, RULER, w - LBL, self.th * nt)
        dragging = self.win.sel if self.mode == "move" else None
        order = [c for c in self.win.clips if c is not dragging] + ([dragging] if dragging else [])
        fm = self.fm_small
        for c in order:
            r = self.clip_rect(c)
            if r.right() < LBL or r.left() > w:
                continue
            sel = c in self.win._sels
            body, band, edge, wave, band_t = TRACK_Q[clip_style(c)]
            offline = c.path in self.win.offline and not c.nat and c.kind != "sub"
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(Qt.NoPen)
            p.setBrush(self.c_off_body if offline else body)
            p.drawRoundedRect(r, 4, 4)
            p.setRenderHint(QPainter.Antialiasing, False)
            p.save()
            path = QPainterPath()
            path.addRoundedRect(r, 4, 4)
            p.setClipPath(path, Qt.IntersectClip)
            tiles = None
            if offline:                   # media can't be found: hatched, no stale thumbnails or waveform
                p.setPen(self.pen_off_hatch)
                x0 = int(max(r.left(), LBL - r.height())) - int(r.height())
                for x in range(x0 - x0 % 8, int(min(r.right(), w)) + 8, 8):
                    p.drawLine(QPointF(x, r.bottom()), QPointF(x + r.height(), r.top()))
            elif c.nat:
                pass                  # captions: plain colour, no thumbnails
            elif c.kind == "image":
                tiles = (None, [c.path])
            elif c.kind == "video" and c.path in THUMBS:
                tiles = THUMBS[c.path]
            if tiles:
                iv, files = tiles
                first = pix_of(files[0])
                if not first.isNull():
                    tw = max(8, int(first.width() * r.height() / max(1, first.height())))
                    x = r.left() + max(0, int((LBL - r.left()) // tw)) * tw
                    while x < min(r.right(), w):
                        idx = 0 if iv is None else min(len(files) - 1, int((c.inp + (x - r.left()) / self.pps) / iv))
                        p.drawPixmap(QRectF(x, r.top(), tw, r.height()), pix_of(files[idx]), QRectF(pix_of(files[idx]).rect()))
                        x += tw
                    p.fillRect(r, self.c_dim)
                else:
                    tiles = None
            peaks = WAVES.get(c.path) if c.kind == "audio" and np is not None and not offline else None
            if peaks is not None:
                p.setPen(wave)
                top = r.top() + 16
                mid = (top + r.bottom()) / 2
                scale = (r.bottom() - top) / 2 - 2
                for x in range(int(max(r.left(), LBL)), int(min(r.right(), w)), 2):
                    j = int((c.inp + (x - r.left()) / self.pps) * 100)
                    if 0 <= j < len(peaks):
                        amp = float(peaks[j:j + max(1, int(200 / self.pps))].max()) * scale
                        p.drawLine(x, int(mid - amp), x, int(mid + amp))
            # fade shading, with a thin diagonal so the fade length reads clearly
            if c.fin > 0 or c.fout > 0:
                p.setPen(Qt.NoPen)
                p.setBrush(self.c_fade)
                if c.fin > 0:
                    p.drawPolygon(QPolygonF([r.topLeft(), QPointF(r.left() + c.fin * self.pps, r.top()), r.bottomLeft()]))
                if c.fout > 0:
                    p.drawPolygon(QPolygonF([r.topRight(), QPointF(r.right() - c.fout * self.pps, r.top()), r.bottomRight()]))
                p.setRenderHint(QPainter.Antialiasing, True)
                p.setPen(self.pen_fade)
                if c.fin > 0:
                    p.drawLine(r.bottomLeft(), QPointF(r.left() + c.fin * self.pps, r.top()))
                if c.fout > 0:
                    p.drawLine(QPointF(r.right() - c.fout * self.pps, r.top()), r.bottomRight())
                p.setRenderHint(QPainter.Antialiasing, False)
            # name band
            p.fillRect(QRectF(r.left(), r.top(), r.width(), 16), band_t if tiles else (self.c_off_band if offline else band))
            tr = r.adjusted(6, 0, -4, 0).intersected(QRectF(LBL, r.top(), w, 16))
            name = c.name
            if offline and tr.width() > 20:
                p.drawPixmap(QRectF(tr.left(), r.top() + 2, 12, 12), self.pm_off_warn, QRectF(self.pm_off_warn.rect()))
                tr = tr.adjusted(16, 0, 0, 0)
                name += " \u00b7 offline"
            if tr.width() > 4:
                p.setFont(self.f_small)
                p.setPen(self.c_band_text)
                p.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(name, Qt.ElideRight, int(tr.width())))
            p.restore()
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setBrush(Qt.NoBrush)
            if sel:
                p.fillPath(path, self.c_lift)
                primary = c is self.win.sel or len(self.win._sels) < 2      # the primary clip stays solid
                p.setPen(self.pen_sel if primary else QPen(QColor(242, 243, 245, 150), 1.5))
                p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 4, 4)
            else:
                p.setPen(self.pen_hov if c is self.hov else QPen(edge, 1))
                p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            if c is dragging:
                p.fillPath(path, self.c_drag)
                p.setPen(QPen(TLC["select"], 1))
                p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            ti = trans_info(self.win.clips, c)
            if ti:                       # transition chip: same place as before, so clicking it still works
                b = QRectF(r.left() - 8, r.center().y() - 8, 16, 16)
                p.setPen(QPen(TLC["bg"], 1))
                p.setBrush(TLC["head_text_main"])
                p.drawRoundedRect(b, 3, 3)
                cx, cy = b.center().x(), b.center().y()
                p.setPen(Qt.NoPen)
                p.setBrush(TLC["head"])
                p.drawPolygon(QPolygonF([QPointF(cx - 5, cy - 4), QPointF(cx - 5, cy + 4), QPointF(cx, cy)]))   # bow-tie
                p.drawPolygon(QPolygonF([QPointF(cx + 5, cy - 4), QPointF(cx + 5, cy + 4), QPointF(cx, cy)]))
            elif c is self.hj and prev_of(self.win.clips, c):
                b = self.junction_btn(c).adjusted(1, 1, -1, -1)
                p.setBrush(QColor("#3A78E0"))
                p.setPen(QPen(TLC["bg"], 2))
                p.drawEllipse(b)
                p.setPen(QPen(QColor("#FFFFFF"), 1.8, Qt.SolidLine, Qt.RoundCap))
                cx, cy = b.center().x(), b.center().y()
                p.drawLine(QPointF(cx - 4.5, cy), QPointF(cx + 4.5, cy))
                p.drawLine(QPointF(cx, cy - 4.5), QPointF(cx, cy + 4.5))
            p.setRenderHint(QPainter.Antialiasing, False)
        acc = TLC["accent"]
        if self.mode == "move" and dragging is not None and getattr(self, "orig", None):
            s0, _, d0, tr0 = self.orig              # faint ghost where the clip came from
            if abs(s0 - dragging.start) > 1e-3 or tr0 != dragging.track:
                g = QRectF(self.tx(s0), RULER + tr0 * self.th + 3, d0 * self.pps, self.th - 6)
                p.setOpacity(0.35)
                p.setRenderHint(QPainter.Antialiasing, True)
                p.setPen(QPen(TLC["select"], 1, Qt.DashLine))
                p.setBrush(TRACK_Q[clip_style(dragging)][0])
                p.drawRoundedRect(g, 4, 4)
                p.setRenderHint(QPainter.Antialiasing, False)
                p.setOpacity(1.0)
        if self.mode == "marquee" and self.mq:
            q = QRectF(self.mq[0], self.mq[1]).normalized()
            p.setPen(QPen(acc, 1))
            p.setBrush(self.c_accent_fill)
            p.drawRect(q)
        if self.mode == "move" and self.plan and self.win.sel:
            kind, ref, st = self.plan
            c = self.win.sel
            y = RULER + c.track * self.th
            p.setRenderHint(QPainter.Antialiasing, True)
            if kind in ("before", "after"):          # insertion marker between clips, drawn over transition chips
                x = self.tx(ref.start if kind == "before" else ref.end)
                p.setPen(QPen(acc, 2))
                p.drawLine(QPointF(x, y + 1), QPointF(x, y + self.th - 1))
                p.setBrush(acc)
                p.setPen(Qt.NoPen)
                p.drawPolygon(QPolygonF([QPointF(x - 4, y + 1), QPointF(x + 4, y + 1), QPointF(x, y + 6)]))
                p.drawPolygon(QPolygonF([QPointF(x - 4, y + self.th - 1), QPointF(x + 4, y + self.th - 1),
                                         QPointF(x, y + self.th - 6)]))
            elif abs(st - c.start) > 1e-3:           # it will slide to the nearest free spot
                p.setPen(QPen(acc, 1, Qt.DashLine))
                p.setBrush(self.c_accent_fill)
                p.drawRoundedRect(QRectF(self.tx(st), y + 3, c.dur * self.pps, self.th - 6), 4, 4)
            p.setRenderHint(QPainter.Antialiasing, False)
        p.setClipping(False)
        if self.mode in ("move", "trimL", "trimR") and self._snap_t is not None:
            xs = int(self.tx(self._snap_t))          # snap line through the ruler, with a marker on it
            if xs >= LBL:
                p.setPen(QPen(acc, 1))
                p.drawLine(xs, 0, xs, RULER + self.th * nt)
                p.setRenderHint(QPainter.Antialiasing, True)
                p.setPen(Qt.NoPen)
                p.setBrush(acc)
                p.drawPolygon(QPolygonF([QPointF(xs, RULER - 9), QPointF(xs + 3, RULER - 6), QPointF(xs, RULER - 3),
                                         QPointF(xs - 3, RULER - 6)]))
                p.setRenderHint(QPainter.Antialiasing, False)
        if not self.win.clips:
            area = QRectF(LBL, RULER, w - LBL, self.th * nt)
            cy = area.center().y()
            p.setFont(self.f_empty)
            p.setPen(TLC["empty_title"])
            p.drawText(QRectF(area.left(), cy - 22, area.width(), 20), Qt.AlignCenter,
                       "Drag media from the Library onto a track")
            p.setFont(self.f_empty2)
            p.setPen(TLC["empty"])
            p.drawText(QRectF(area.left(), cy + 2, area.width(), 18), Qt.AlignCenter,
                       "or double-click an item to add it")
        # drop hint
        if self.drop_hint:
            tr, t0, d = self.drop_hint
            p.setPen(QPen(acc, 1.5, Qt.DashLine))
            p.setBrush(self.c_accent_fill)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.drawRoundedRect(QRectF(self.tx(t0), RULER + tr * self.th + 3, d * self.pps, self.th - 6), 4, 4)
            p.setRenderHint(QPainter.Antialiasing, False)

    # mouse
    def mousePressEvent(self, ev):
        pos = ev.position()
        self.setFocus()
        if pos.y() < RULER or pos.x() < LBL:
            self.mode = "scrub"
            self.seek.emit(max(0, self.xt(pos.x())))
            return
        j = self.junction_at(pos)
        if j is not None and ev.button() == Qt.LeftButton and self.junction_btn(j).contains(pos):
            self.mode = None
            self.trans_menu(j, ev.globalPosition().toPoint())
            return
        c = self.clip_at(pos)
        w = self.win
        mods = ev.modifiers()
        self._collapse = None
        if c and ev.button() == Qt.LeftButton and (mods & Qt.ControlModifier):      # add / remove from selection
            cur = list(w.selected())
            if c in cur:
                cur.remove(c)
            else:
                cur.append(c)
            w.set_selection(cur, c if c in cur else None)
            self.mode = None
            return
        if c and ev.button() == Qt.LeftButton and (mods & Qt.ShiftModifier) and w.sel and w.sel.track == c.track:
            a, b = sorted((w.sel.start, c.start))
            w.set_selection([o for o in w.clips if o.track == c.track and a - 1e-6 <= o.start <= b + 1e-6], c)
            self.mode = None
            return
        if c is None and ev.button() == Qt.LeftButton and (mods & Qt.ControlModifier):   # drag a box
            self.mode, self.mq = "marquee", (pos, pos)
            return
        if c is not None and c in w._sels and len(w._sels) > 1:
            if ev.button() == Qt.LeftButton:
                self._collapse = c           # plain click on a multi-selection: keep it for dragging
        else:
            w.sel = c
        self.selection.emit()
        self.update()
        if not c or ev.button() != Qt.LeftButton:
            if not c:
                self.mode = "scrub"
                self.seek.emit(max(0, self.xt(pos.x())))
            return
        r = self.clip_rect(c)
        self._pre = self.win.snapshot()        # becomes one undo step only if the drag really changes something
        self.orig = (c.start, c.inp, c.dur, c.track)
        self.press = pos
        self.mpos = pos
        self.press_t = self.xt(pos.x())
        self.plan = None
        self.hold = 0.0
        if pos.x() < r.left() + 7:
            self.mode = "trimL"
        elif pos.x() > r.right() - 7:
            self.mode = "trimR"
        else:
            self.mode = "move"
        if self.mode != "move" and self._collapse is not None:
            w.sel = c
            self._collapse = None
        self._multi = ({o: o.start for o in w._sels if o in w.clips} if (self.mode == "move" and len(w._sels) > 1)
                       else None)
        self._tl = time.perf_counter()
        self._auto.start()

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self.mode == "scrub":
            self.seek.emit(max(0, self.xt(pos.x())))
            return
        if not self.mode:
            hc = self.clip_at(pos)
            hj = self.junction_at(pos)
            if hc is not self.hov or hj is not self.hj:
                self.hov, self.hj = hc, hj
                self.update()
            if hj is not None and self.junction_btn(hj).contains(pos):
                self.setCursor(Qt.PointingHandCursor)
                QToolTip.showText(ev.globalPosition().toPoint(), "Add / change transition", self)
            elif hc:
                r = self.clip_rect(hc)
                self.setCursor(Qt.SizeHorCursor if (pos.x() < r.left() + 7 or pos.x() > r.right() - 7)
                               else Qt.OpenHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return
        self.mpos = pos
        if self.mode == "marquee":
            self.mq = (self.mq[0], pos)
            q = QRectF(self.mq[0], self.mq[1]).normalized()
            self.win.set_selection([o for o in self.win.clips if self.clip_rect(o).intersects(q)])
            self.update()
            return
        self.drag_update()

    # ---- dragging: the clip is anchored to the mouse in TIME, so it keeps following while the view scrolls
    def free_mode(self):
        return bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)

    def drag_update(self):
        c = self.win.sel
        if not c or self.mode not in ("move", "trimL", "trimR") or self.mpos is None:
            return
        pos = self.mpos
        s0, i0, d0, tr0 = self.orig
        dt = self.xt(pos.x()) - self.press_t
        if self.mode == "move" and self._multi:
            ex = list(self._multi)
            raw = s0 + dt
            ns = self.snap(raw, ex)
            if abs(ns - raw) < 1e-9:
                ns = self.snap(raw + d0, ex) - d0
            delta = max(ns - s0, -min(self._multi.values()))
            for o, st0 in self._multi.items():
                o.start = st0 + delta
            self.plan = None
            self.update()
            return
        if self.mode == "move":
            raw = s0 + dt
            ns = self.snap(raw, c)
            if abs(ns - raw) < 1e-9:  # start did not snap: try snapping the end instead
                ns = self.snap(raw + d0, c) - d0
            ns = max(0.0, ns)
            row = max(0, min(len(TRACKS) - 1, int((pos.y() - RULER) // self.th)))
            if TRACKS[row][1] == KIND_TRACK.get(c.kind, "v"):
                c.track = row
            c.start = ns                     # free movement; overlaps are sorted out when you let go
            self.plan = self.resolve(c)
        elif self.mode == "trimL":
            ns = self.snap(s0 + dt, c)
            ns = max(ns, s0 - i0) if c.kind not in ("image", "sub") else ns
            ns = max(0.0, min(ns, s0 + d0 - 0.1))
            nd, ni = d0 - (ns - s0), i0 + (ns - s0)
            if not self.overlaps(c, c.track, ns, nd):
                c.start, c.dur, c.inp = ns, nd, max(0.0, ni)
            self.win.after_edit(light=True)
        elif self.mode == "trimR":
            ne = self.snap(s0 + d0 + dt, c)
            nd = max(0.1, ne - s0)
            if c.kind not in ("image", "sub"):
                nd = min(nd, c.srcdur - c.inp)
            if not self.overlaps(c, c.track, s0, nd):
                c.dur = nd
            self.win.after_edit(light=True)
        self.update()

    def resolve(self, c):
        """Where the dragged clip will really end up if released now: (kind, ref clip, start)."""
        tr, st, d = c.track, c.start, c.dur
        others = sorted((o for o in self.win.clips if o is not c and o.track == tr), key=lambda o: o.start)
        hit = [o for o in others if st < o.end - 1e-3 and o.start < st + d - 1e-3]
        magnetic = self.ripple and tr == 2 and not self.free_mode()
        if not hit:
            return ("free", None, st)
        if magnetic:
            mid = st + d / 2
            idx = sum(1 for o in others if (o.start + o.end) / 2 < mid)
            return ("before", others[idx], others[idx].start) if idx < len(others) else ("after", others[-1], others[-1].end)
        cands = [0.0] + [o.end for o in others] + [max(0.0, o.start - d) for o in others]
        good = [x for x in cands if not self.overlaps(c, tr, x, d)]
        if not good:
            good = [max(o.end for o in others)]
        return ("free", None, min(good, key=lambda x: abs(x - st)))

    def commit_move(self):
        c = self.win.sel
        if not c or self.plan is None:
            return
        s0, i0, d0, tr0 = self.orig
        kind, ref, st = self.plan
        clips = self.win.clips
        magnetic = self.ripple and not self.free_mode()
        moved = c.track != tr0 or abs(st - s0) > 1e-3
        if magnetic and tr0 == 2 and c.track == 2 and moved:   # re-ordering on the main track: close the gap left behind
            for o in clips:
                if o is not c and o.track == tr0 and o.start >= s0 + d0 - 1e-3:
                    o.start = max(0.0, o.start - d0)
        if kind == "before":
            c.start = ref.start
        elif kind == "after":
            c.start = ref.end
        else:
            c.start = st
        if c.track == 2 and magnetic:
            self.push_chain(c)

    def commit_multi(self):
        """Several clips moved together: keep the move only if there is room, otherwise put them back."""
        sel = set(self._multi)
        for o in sel:
            for x in self.win.clips:
                if x not in sel and x.track == o.track and o.start < x.end - 1e-3 and x.start < o.end - 1e-3:
                    for oo, st0 in self._multi.items():
                        oo.start = st0
                    self.win.status("No room there: the clips went back. Move them to an empty spot (or Alt to ignore snapping).")
                    return

    def push_chain(self, c):
        """Push the clips after c along the main track so nothing overlaps."""
        seq = sorted((o for o in self.win.clips if o.track == c.track), key=lambda o: (o.start, o is not c))
        for i in range(1, len(seq)):
            if seq[i].start < seq[i - 1].end - 1e-6:
                seq[i].start = seq[i - 1].end

    # ---- edge auto-scroll with acceleration (clip moves, trims and library drags all use it)
    def _auto_tick(self):
        now = time.perf_counter()
        dt = min(0.05, now - self._tl)
        self._tl = now
        if self.mode in ("move", "trimL", "trimR") and not (QApplication.mouseButtons() & Qt.LeftButton):
            class _E:  # the mouse button was released somewhere we did not see it
                pass
            self.mouseReleaseEvent(_E())
            return
        if self.mode in ("move", "trimL", "trimR") and self.mpos is not None:
            x = self.mpos.x()
        elif self.ext_lib is not None:
            x = self.mapFromGlobal(self.ext_g).x()
        else:
            self._auto.stop()
            return
        zone = 70.0
        left, right = LBL + zone, self.width() - zone
        depth = 0.0
        if x < left:
            depth = -min(2.5, (left - x) / zone)
        elif x > right:
            depth = min(2.5, (x - right) / zone)
        if depth == 0.0:
            self.hold = 0.0
            return
        self.hold += dt
        vis = max(1.0, (self.width() - LBL) / self.pps)
        total = self.win.total()
        accel = 1.0 + min(self.hold, 2.0) * 1.5          # speeds up the longer you stay at the edge
        v = (vis * 0.5 + total * 0.12) * abs(depth) * accel
        lim = max(self.t0, total + 5.0)
        nt0 = max(0.0, min(lim, self.t0 + (1 if depth > 0 else -1) * v * dt))
        if nt0 == self.t0:
            return
        self.t0 = nt0
        self.win.sync_scroll()
        if self.ext_lib is not None and self.mode is None:
            self.ext_hint(self.ext_lib, self.ext_g)
        else:
            self.drag_update()
        self.update()

    def mouseReleaseEvent(self, ev):
        self._auto.stop()
        if self.mode == "move":
            if self._multi:
                self.commit_multi()
            else:
                self.commit_move()
            self.plan = None
            self.win.after_edit()
        elif self.mode in ("trimL", "trimR"):
            self.win.after_edit()
        if self.mode in ("move", "trimL", "trimR") and self.win.snapshot() != self._pre:
            w = self.win
            w.undo.append(self._pre)
            w.undo = w.undo[-80:]
            w.redo.clear()
            w.last_push = time.time()
        elif self.mode == "move" and self._collapse is not None:       # plain click on one of several: select just it
            self.win.sel = self._collapse
            self.selection.emit()
        self._multi = self._collapse = self.mq = None
        self.plan = None
        self.mode = None
        self._snap_t = None
        self.update()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape and self.mode in ("move", "trimL", "trimR"):   # cancel a drag
            self._auto.stop()
            self.mode, self.plan = None, None
            self._snap_t = None
            self.win.restore(self._pre)
            self.win.status("Move cancelled.")
            return
        super().keyPressEvent(ev)

    def contextMenuEvent(self, ev):
        c = self.clip_at(QPointF(ev.pos()))
        if not c:
            return
        if c not in self.win._sels:
            self.win.sel = c
        self.selection.emit()
        self.update()
        m = QMenu(self)
        m.addAction("Select all clips after this", lambda: self.win.select_after(c))
        m.addAction("Add a gap before this clip...", lambda: self.win.insert_gap(c.start))
        m.addSeparator()
        m.addAction("Split at playhead (S)", self.win.split)
        m.addAction("Duplicate", self.win.duplicate)
        m.addAction("Delete (Del)", self.win.delete_sel)
        fm = m.addMenu("Fade")
        fm.addAction("Fade in 1 s", lambda: self.win.quick_fade(1.0, None))
        fm.addAction("Fade out 1 s", lambda: self.win.quick_fade(None, 1.0))
        fm.addAction("Fade in and out 1 s", lambda: self.win.quick_fade(1.0, 1.0))
        fm.addAction("Remove fades", lambda: self.win.quick_fade(0.0, 0.0))
        if c.kind != "audio":
            sub = m.addMenu("Transition from previous clip")
            for t in TRANS:
                a = sub.addAction(t, lambda t=t: self.win.set_trans(t))
                a.setCheckable(True)
                a.setChecked(t == c.ttype)
        m.exec(ev.globalPos())

    def leaveEvent(self, ev):
        if self.hj is not None or self.hov is not None:
            self.hj = self.hov = None
            self.update()

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            f = 1.2 if ev.angleDelta().y() > 0 else 1 / 1.2
            tt = self.xt(ev.position().x())
            self.pps = max(8.0, min(600.0, self.pps * f))
            self.t0 = max(0.0, tt - (ev.position().x() - LBL) / self.pps)
        else:
            self.t0 = max(0.0, self.t0 - ev.angleDelta().y() / 120 * 60 / self.pps * 3)
        self.win.sync_scroll()
        self.update()

    # drag & drop from the library
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasFormat(MIME) or ev.mimeData().hasFormat(MIME_T):
            ev.setDropAction(Qt.CopyAction)
            ev.accept()

    def dragMoveEvent(self, ev):
        lib = self.win.lib_entry(ev.mimeData())
        if lib:
            row, t = self.drop_target(lib, ev.position())
            self.drop_hint = (row, t, lib["dur"] if lib["kind"] != "image" else 5.0)
            self.update()
        ev.setDropAction(Qt.CopyAction)
        ev.accept()

    def dragLeaveEvent(self, ev):
        self.drop_hint = None
        self.update()

    def apply_trans_drop(self, name, pos):
        c = self.clip_at(pos)
        if not c:
            t = self.xt(pos.x())
            c = min((o for o in self.win.clips if o.kind not in ("audio", "sub")), key=lambda o: abs(o.start - t), default=None)
            if c and abs(c.start - t) > 0.4:
                c = None
        if c and c.kind not in ("audio", "sub"):
            self.win.sel = c
            self.win.set_trans(name)
            if not prev_of(self.win.clips, c):
                QToolTip.showText(self.mapToGlobal(pos.toPoint()),
                                  "Move this clip right next to the previous one for the transition to play.")
        else:
            self.win.status("Drop the transition on a video or image clip.")

    def dropEvent(self, ev):
        if ev.mimeData().hasFormat(MIME_T):
            self.apply_trans_drop(bytes(ev.mimeData().data(MIME_T)).decode(), ev.position())
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
            return
        lib = self.win.lib_entry(ev.mimeData())
        self.drop_hint = None
        if lib:
            row, t = self.drop_target(lib, ev.position())
            self.win.add_clip(lib, row, t)
        ev.setDropAction(Qt.CopyAction)
        ev.accept()
        self.update()

    def ext_hint(self, lib, gpos):
        self.ext_lib, self.ext_g = lib, gpos
        if not self._auto.isActive():
            self._tl = time.perf_counter()
            self.hold = 0.0
            self._auto.start()
        lp = self.mapFromGlobal(gpos)
        if self.rect().contains(lp):
            row, t = self.drop_target(lib, QPointF(lp))
            self.drop_hint = (row, t, lib["dur"] if lib["kind"] != "image" else 5.0)
        else:
            self.drop_hint = None
        self.update()

    def ext_drop(self, lib, gpos):
        self.ext_lib = self.ext_g = None
        self._auto.stop()
        lp = self.mapFromGlobal(gpos)
        self.drop_hint = None
        if self.rect().contains(lp):
            row, t = self.drop_target(lib, QPointF(lp))
            self.win.add_clip(lib, row, t)
        self.update()

    def drop_target(self, lib, pos):
        want = "a" if lib["kind"] == "audio" else "v"
        row = self.row_at(pos.y())
        if row is None or TRACKS[row][1] != want:
            row = 3 if want == "a" else (1 if lib["kind"] == "image" else 2)
        return row, max(0.0, self.snap(self.xt(pos.x()), None))


MIME = "application/x-clipstudio-lib"


class LibraryList(QListWidget):
    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(QPixmap(120, 68).size())
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setWordWrap(True)
        self.setGridSize(QPixmap(136, 100).size())
        self.setDragEnabled(False)          # we start drags ourselves (more reliable than Qt's own)
        self.setDragDropMode(QListWidget.NoDragDrop)
        self.setObjectName("library")
        self.setTextElideMode(Qt.ElideMiddle)
        self._press = None
        self._drag = None

    def resizeEvent(self, ev):
        """As many columns as fit (at least 124 px each), thumbnails 16:9, room for a two-line name."""
        super().resizeEvent(ev)
        vw = self.viewport().width() - 2 * 4
        cols = max(1, vw // 124)
        cell = max(100, vw // cols)
        iw = cell - 12
        ih = int(iw * 9 / 16)
        if self.iconSize() != QSize(iw, ih):
            self.setIconSize(QSize(iw, ih))
            self.setGridSize(QSize(cell, ih + 12 + 34))

    def mousePressEvent(self, ev):
        super().mousePressEvent(ev)
        it = self.itemAt(ev.position().toPoint())
        self._press = (ev.position().toPoint(), it) if (it and ev.button() == Qt.LeftButton) else None

    # Drag is done inside the app (a floating thumbnail + our own drop), not through Windows' drag & drop,
    # which was refusing to start on some PCs.
    def _lib_for(self, it):
        w = self.window()
        try:
            e = w.lib[self.row(it)]
            return None if e.get("pending") or e.get("missing") else e
        except Exception:
            return None

    def _end_drag(self):
        lib, ghost = self._drag
        self._drag = None
        ghost.hide()
        ghost.deleteLater()
        self.viewport().releaseMouse()
        self.releaseKeyboard()
        self.unsetCursor()
        return lib

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape and getattr(self, "_drag", None):     # cancel the drag: nothing is added
            self._end_drag()
            tl = self.window().tl
            tl.drop_hint = None
            tl._auto.stop()
            tl.update()
            return
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace) and ev.modifiers() == Qt.NoModifier:
            self.window().delete_lib_selected()
            return
        super().keyPressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if getattr(self, "_drag", None):
            lib = self._end_drag()
            self.window().tl.ext_drop(lib, ev.globalPosition().toPoint())
            return
        self._press = None
        super().mouseReleaseEvent(ev)

    def mouseMoveEvent(self, ev):
        g = ev.globalPosition().toPoint()
        if getattr(self, "_drag", None):
            lib, ghost = self._drag
            ghost.move(g - QPoint(ghost.width() // 2, ghost.height() // 2))
            self.window().tl.ext_hint(lib, g)
            return
        if self._press and (ev.buttons() & Qt.LeftButton):
            p0, it = self._press
            if (ev.position().toPoint() - p0).manhattanLength() >= 6:
                self._press = None
                lib = self._lib_for(it)
                if lib is None:
                    return
                self.setCurrentItem(it)
                ghost = QLabel()
                ghost.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowTransparentForInput)
                ghost.setAttribute(Qt.WA_ShowWithoutActivating)
                ghost.setPixmap(it.icon().pixmap(100, 56))
                ghost.setWindowOpacity(0.85)
                ghost.adjustSize()
                ghost.move(g - QPoint(ghost.width() // 2, ghost.height() // 2))
                ghost.show()
                self._drag = (lib, ghost)
                self.viewport().grabMouse()
                self.grabKeyboard()                     # so Esc reaches us and can cancel
                self.setCursor(Qt.ClosedHandCursor)
                return
        super().mouseMoveEvent(ev)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self.count() == 0:
            p = QPainter(self.viewport())
            r = self.viewport().rect().adjusted(16, 0, -16, 0)
            f = QFont(self.font())
            f.setPixelSize(13)
            f.setWeight(QFont.DemiBold)
            p.setFont(f)
            p.setPen(QColor("#A6A8B0"))
            top = r.center().y() - 28
            p.drawText(QRect(r.left(), top, r.width(), 20), Qt.AlignCenter, "Your media shows up here.")
            f.setPixelSize(12)
            f.setWeight(QFont.Normal)
            p.setFont(f)
            p.setPen(QColor("#767880"))
            p.drawText(QRect(r.left(), top + 28, r.width(), 60), Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                       "Click Import media, or drop video, image and audio files onto this window.")

    def startDrag(self, actions):
        it = self.currentItem()
        if not it or not (it.flags() & Qt.ItemIsEnabled):
            return
        d = QDrag(self)
        m = QMimeData()
        m.setData(MIME, str(self.row(it)).encode())
        d.setMimeData(m)
        pm = it.icon().pixmap(100, 56)
        d.setPixmap(pm)
        d.setHotSpot(pm.rect().center())
        d.exec(Qt.CopyAction)


MIME_T = "application/x-clipstudio-trans"


class TransList(QListWidget):
    """Drag a transition onto a clip (done inside the app, like the Library drag)."""

    def __init__(self):
        super().__init__()
        self.addItems(TRANS[1:])
        self.setDragEnabled(False)
        self.setObjectName("transitions")
        self.viewport().setCursor(Qt.OpenHandCursor)
        self._press = self._drag = None

    def mousePressEvent(self, ev):
        super().mousePressEvent(ev)
        it = self.itemAt(ev.position().toPoint())
        self._press = (ev.position().toPoint(), it) if (it and ev.button() == Qt.LeftButton) else None

    def mouseMoveEvent(self, ev):
        g = ev.globalPosition().toPoint()
        if self._drag is not None:
            self._drag[1].move(g + QPoint(10, 10))
            return
        if self._press and (ev.buttons() & Qt.LeftButton):
            p0, it = self._press
            if (ev.position().toPoint() - p0).manhattanLength() >= 6:
                self._press = None
                self.setCurrentItem(it)
                ghost = QLabel(it.text())
                ghost.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowTransparentForInput)
                ghost.setAttribute(Qt.WA_ShowWithoutActivating)
                ghost.setStyleSheet("background:#3A78E0;color:#FFFFFF;padding:4px 10px;border-radius:4px;font-weight:600;")
                ghost.adjustSize()
                ghost.move(g + QPoint(10, 10))
                ghost.show()
                self._drag = (it.text(), ghost)
                self.viewport().grabMouse()
                self.viewport().setCursor(Qt.ClosedHandCursor)
                return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            name, ghost = self._drag
            self._drag = None
            ghost.hide()
            ghost.deleteLater()
            self.viewport().releaseMouse()
            self.viewport().setCursor(Qt.OpenHandCursor)
            tl = self.window().tl
            lp = tl.mapFromGlobal(ev.globalPosition().toPoint())
            if tl.rect().contains(lp):
                tl.apply_trans_drop(name, QPointF(lp))
            return
        self._press = None
        super().mouseReleaseEvent(ev)


class SliderSpin(QWidget):
    """A slider and a number box that stay in sync (like Clipchamp's property sliders)."""
    valueChanged = Signal(float)

    def __init__(self, lo, hi, step, suffix="", dec=2):
        super().__init__()
        self.step = step
        self.sl = QSlider(Qt.Horizontal)
        self.sl.setRange(int(round(lo / step)), int(round(hi / step)))
        self.sp = QDoubleSpinBox()
        self.sp.setRange(lo, hi)
        self.sp.setSingleStep(step)
        self.sp.setDecimals(dec)
        self.sp.setSuffix(suffix)
        self.sp.setFixedWidth(84)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.sl, 1)
        lay.addWidget(self.sp)
        self.sl.valueChanged.connect(lambda v: self.sp.setValue(v * step))
        self.sp.valueChanged.connect(self._sp)

    def _sp(self, v):
        self.sl.blockSignals(True)
        self.sl.setValue(int(round(v / self.step)))
        self.sl.blockSignals(False)
        self.valueChanged.emit(v)

    def value(self):
        return self.sp.value()

    def setValue(self, v):
        self.sp.setValue(v)

    def setEnabled(self, e):
        self.sl.setEnabled(e)
        self.sp.setEnabled(e)


# ----------------------------------------------------------------------------- preview slots


class Slot:
    """One media player + video item + image item on a preview track."""

    def __init__(self, parent_item, vol_default=1.0):
        self.player = QMediaPlayer()
        self.ao = QAudioOutput()
        self.player.setAudioOutput(self.ao)
        self.box = QGraphicsPathItem(parent_item)
        self.box.setPen(QPen(Qt.NoPen))
        self.box.setFlag(QGraphicsPathItem.ItemClipsChildrenToShape, True)
        self.vitem = QGraphicsVideoItem(self.box)
        self.player.setVideoOutput(self.vitem)
        self.pitem = QGraphicsPixmapItem(self.box)
        self.vitem.hide()
        self.pitem.hide()
        self.cid = None
        self.path = None
        self.pix = None

    def release(self):
        if self.cid is not None:
            self.cid = None
            try:
                self.player.pause()
            except Exception:
                pass
        self._sig = None        # forces the picture to be shown again next time this slot is used
        self.vitem.hide()
        self.pitem.hide()

    def set_clip_path(self, W, H, px, py, w, h, wipe):
        p = QPainterPath()
        if not wipe:
            p.addRect(0, 0, W, H)
        else:
            kind, q = wipe
            q = max(0.0, min(1.0, q))
            if kind == "Wipe left":
                p.addRect(px + w * (1 - q), py, w * q, h)
            elif kind == "Wipe right":
                p.addRect(px, py, w * q, h)
            elif kind == "Wipe up":
                p.addRect(px, py + h * (1 - q), w, h * q)
            elif kind == "Wipe down":
                p.addRect(px, py, w, h * q)
            else:
                r = q * math.hypot(w / 2, h / 2)
                p.addEllipse(QPointF(px + w / 2, py + h / 2), r, r)
        self.box.setPath(p)

    def show_clip(self, c, W, H, t, playing, alpha, vol, off, wipe=None):
        if self.cid != c.id:
            self.cid = c.id
            self.force_seek = True
        if c.path != self.path:
            self.path = c.path
            self._pixver = getattr(self, "_pixver", 0) + 1
            if c.kind == "image":
                self.pix = QPixmap(c.path)
            else:
                self.player.setSource(QUrl.fromLocalFile(c.path))
            self.force_seek = True
        f = fitf(c, W, H) * c.scale / 100
        w, h = c.w * f, c.h * f
        px = (W - w) / 2 + c.x / 100 * W + off[0] * W
        py = (H - h) / 2 + c.y / 100 * H + off[1] * H
        sig = (c.id, c.kind, W, H, round(w, 2), round(h, 2), round(px, 2), round(py, 2), round(alpha, 3), wipe,
               self.path, getattr(self, "_pixver", 0))
        changed = sig != getattr(self, "_sig", None)
        self._sig = sig
        if changed:
            self.set_clip_path(W, H, px, py, w, h, wipe)
        if c.kind == "image":
            if changed:
                self.vitem.hide()
                if self.pix is not None and not self.pix.isNull():
                    self.pitem.setPixmap(self.pix)
                    self.pitem.setTransform(QTransform.fromScale(w / self.pix.width(), h / self.pix.height()))
                    self.pitem.setPos(px, py)
                    self.pitem.setOpacity(alpha)
                    self.pitem.show()
            return
        if changed:
            self.pitem.hide()
            if c.kind == "video":
                self.vitem.setSize(QSizeF(w, h))
                self.vitem.setPos(px, py)
                self.vitem.setOpacity(alpha)
                self.vitem.show()
        self.ao.setVolume(max(0.0, min(1.0, vol)) if c.hasaudio else 0.0)
        tgt = t - c.start + c.inp
        run_ok = playing
        if tgt > c.inp + c.dur:
            tgt, run_ok = c.inp + c.dur - 0.04, False
        pos = self.player.position() / 1000.0
        if run_ok:
            if self.force_seek or abs(pos - tgt) > 0.3:
                self.player.setPosition(int(tgt * 1000))
                self.force_seek = False
            if self.player.playbackState() != QMediaPlayer.PlayingState:
                self.player.play()
        else:
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            if self.force_seek or abs(pos - tgt) > 0.04:
                self.player.setPosition(int(tgt * 1000))
                self.force_seek = False
                if self.player.playbackState() == QMediaPlayer.StoppedState:
                    self.player.pause()


# ----------------------------------------------------------------------------- preview editing


class TransformBox(QGraphicsItem):
    """Selection frame in the preview: drag to move, drag a corner to resize the selected clip."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.c = None
        self.mode = None
        self.snapx = self.snapy = False
        self.setZValue(100)
        self.setAcceptHoverEvents(True)
        self.setVisible(False)

    def geom(self):
        c, W, H = self.c, self.win.W, self.win.H
        fb = fitf(c, W, H)
        return W / 2 + c.x / 100 * W, H / 2 + c.y / 100 * H, c.w * fb * c.scale / 100, c.h * fb * c.scale / 100

    def rect(self):
        cx, cy, w, h = self.geom()
        return QRectF(cx - w / 2, cy - h / 2, w, h)

    def hs(self):
        try:
            return 14 / max(self.win.view.transform().m11(), 0.01)
        except RuntimeError:                 # the window is being torn down
            return 14.0

    def boundingRect(self):
        if not self.c:
            return QRectF()
        hs = self.hs()
        return self.rect().adjusted(-hs, -hs, hs, hs).united(QRectF(0, 0, self.win.W, self.win.H))

    def set_clip(self, c):
        if c is None and self.c is None:
            return
        self.prepareGeometryChange()
        self.c = c
        self.setVisible(c is not None)
        self.update()

    def corners(self):
        r = self.rect()
        return {"tl": r.topLeft(), "tr": r.topRight(), "bl": r.bottomLeft(), "br": r.bottomRight()}

    def corner_at(self, pos):
        hs = self.hs() * 1.4
        for n, pt in self.corners().items():
            if abs(pos.x() - pt.x()) < hs and abs(pos.y() - pt.y()) < hs:
                return n
        return None

    def hit(self, pos):
        return bool(self.c) and (self.corner_at(pos) or self.rect().contains(pos))

    def paint(self, p, opt, widget=None):
        if not self.c:
            return
        pen = QPen(QColor("#5B9BFF"), 2)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(self.rect())
        if self.snapx or self.snapy:
            gp = QPen(QColor("#FF5AC8"), 1, Qt.DashLine)
            gp.setCosmetic(True)
            p.setPen(gp)
            if self.snapx:
                p.drawLine(QPointF(self.win.W / 2, 0), QPointF(self.win.W / 2, self.win.H))
            if self.snapy:
                p.drawLine(QPointF(0, self.win.H / 2), QPointF(self.win.W, self.win.H / 2))
        hs = self.hs()
        hp = QPen(QColor("#111214"), 1)       # dark rim so the handles show on white video too
        hp.setCosmetic(True)
        p.setPen(hp)
        p.setBrush(QColor(255, 255, 255))
        for pt in self.corners().values():
            p.drawRect(QRectF(pt.x() - hs / 2, pt.y() - hs / 2, hs, hs))

    def hoverMoveEvent(self, ev):
        n = self.corner_at(ev.pos())
        if n:
            self.setCursor(Qt.SizeFDiagCursor if n in ("tl", "br") else Qt.SizeBDiagCursor)
        elif self.rect().contains(ev.pos()):
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, ev):
        si = getattr(self.win, "subitem", None)
        if si is not None and si.box is not None and si.shape().contains(ev.pos()):
            ev.ignore()  # a subtitle is under the cursor: let it be dragged instead of the video frame
            return
        if not self.c or not self.hit(ev.pos()):
            ev.ignore()
            return
        self.win.push_undo()
        cx, cy, w, h = self.geom()
        self.p0, self.x0, self.y0, self.s0 = ev.pos(), self.c.x, self.c.y, self.c.scale
        self.d0 = max(1.0, math.hypot(ev.pos().x() - cx, ev.pos().y() - cy))
        self.center0 = (cx, cy)
        self.mode = "scale" if self.corner_at(ev.pos()) else "move"
        ev.accept()

    def mouseMoveEvent(self, ev):
        c, W, H = self.c, self.win.W, self.win.H
        if not c or not self.mode:
            return
        if self.mode == "move":
            x = self.x0 + (ev.pos().x() - self.p0.x()) / W * 100
            y = self.y0 + (ev.pos().y() - self.p0.y()) / H * 100
            self.snapx, self.snapy = abs(x) < 1.5, abs(y) < 1.5
            c.x = 0.0 if self.snapx else x
            c.y = 0.0 if self.snapy else y
        else:
            d = math.hypot(ev.pos().x() - self.center0[0], ev.pos().y() - self.center0[1])
            c.scale = max(5.0, min(500.0, self.s0 * d / self.d0))
        self.win.after_drag()

    def mouseReleaseEvent(self, ev):
        self.mode = None
        self.snapx = self.snapy = False
        self.update()


class PreviewView(QGraphicsView):
    """The video preview. Wheel = zoom in on the picture, right/middle-drag = move around, Z = zoom to the clip."""

    def __init__(self, scene, win):
        super().__init__(scene)
        self.win = win
        self.zoomed = False
        self._pan = None
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def fit(self):
        if not self.zoomed:
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)

    def drawForeground(self, p, rect):
        """Over the picture, in screen pixels: say so when the media under the playhead is offline."""
        if not self.win.offline_at_playhead():
            return
        p.save()
        p.resetTransform()
        vr = QRectF(self.viewport().rect())
        f = QFont(self.font())
        f.setPixelSize(13)
        p.setFont(f)
        lines = ["Media offline", "Right-click the item in the Library and choose Locate file\u2026"]
        fm = QFontMetrics(f)
        bw = min(vr.width() - 24, max(fm.horizontalAdvance(t) for t in lines) + 32)
        box = QRectF(vr.center().x() - bw / 2, vr.center().y() - 44, bw, 88)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 21, 24, 230))
        p.drawRoundedRect(box, 8, 8)
        p.drawPixmap(QRectF(box.center().x() - 10, box.top() + 12, 20, 20),
                     mk_icon("warn", "#F07A7E", 20).pixmap(40, 40), QRectF(0, 0, 40, 40))
        p.setPen(QColor("#E8E9EC"))
        p.drawText(QRectF(box.left(), box.top() + 38, bw, 18), Qt.AlignCenter, lines[0])
        p.setPen(QColor("#A6A8B0"))
        p.drawText(QRectF(box.left() + 8, box.top() + 58, bw - 16, 18), Qt.AlignCenter,
                   fm.elidedText(lines[1], Qt.ElideRight, int(bw - 16)))
        p.restore()

    def _label(self):
        b = getattr(self.win, "b_zoomv", None)
        if b is not None:
            b.setText("Reset zoom" if self.zoomed else "Zoom video")

    def reset(self):
        self.zoomed = False
        self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)
        self._label()

    def zoom_to(self, rect):
        m = max(rect.width(), rect.height()) * 0.03
        self.zoomed = True
        self.fitInView(rect.adjusted(-m, -m, m, m), Qt.KeepAspectRatio)
        self._label()

    def wheelEvent(self, ev):
        f = 1.25 if ev.angleDelta().y() > 0 else 0.8
        fit = self.viewport().width() / max(1.0, self.scene().sceneRect().width())
        fit = min(fit, self.viewport().height() / max(1.0, self.scene().sceneRect().height()))
        cur = self.transform().m11()
        new = max(fit, min(fit * 14, cur * f))
        if new <= fit * 1.02:
            self.reset()
        else:
            self.zoomed = True
            self.scale(new / cur, new / cur)
            self._label()
        ev.accept()

    def mouseMoveEvent(self, ev):
        if self._pan is not None:
            d = ev.position().toPoint() - self._pan
            self._pan = ev.position().toPoint()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._pan is not None and ev.button() in (Qt.RightButton, Qt.MiddleButton):
            self._pan = None
            self.unsetCursor()
            return
        super().mouseReleaseEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() in (Qt.RightButton, Qt.MiddleButton):
            self._pan = ev.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            return
        pos = self.mapToScene(ev.position().toPoint())
        xb = self.win.xbox
        if ev.button() == Qt.LeftButton and not (xb and xb.hit(pos)):
            c = self.win.clip_at_preview(pos)
            if c is not None and c is not self.win.sel:
                self.win.sel = c
                self.win.load_props()
                self.win.tl.update()
                self.win.update_preview(force=True)
        super().mousePressEvent(ev)


# ----------------------------------------------------------------------------- main window

HINTS = [("Space", "play"), ("S", "split"), ("Del", "delete"), ("Ctrl+Z", "undo"), ("Ctrl+wheel", "zoom"),
         ("Ctrl+click", "select several"), ("Alt", "no snapping"), ("Shift", "place freely"), ("Esc", "cancel a drag")]


def hint_html(items):
    """Status bar shortcut hints: the key a little brighter than what it does."""
    return "&nbsp;&nbsp;&nbsp;".join(f"<span style='color:#C9CBD1'>{k}</span> <span style='color:#767880'>{d}</span>"
                                     for k, d in items)


def scrolled(w):
    """Put a panel page in a scroll area, so tall pages never force a tall window."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sa.setWidget(w)
    return sa


APP_TITLE = "Parrot Studio"


def brand_label():
    """The Parrot Studio logo (wordmark) for the editor and dashboard headers."""
    t = QLabel(APP_TITLE)
    t.setObjectName("title")
    lg = QPixmap()
    if lg.loadFromData(base64.b64decode(LOGO_B64)):
        lg.setDevicePixelRatio(2.0)    # stored at 320x56, shown at 160x28
        t.setPixmap(lg)
        t.setToolTip("Parrot Studio")
    return t

PROJECT_FILTER = "Parrot Studio project (*.clipproj)"
PROJECT_KEYS = ("substyle", "W", "H", "lib", "clips")      # what project_dict() writes; other keys are kept as-is


class ElideButton(QPushButton):
    """A push button whose text is elided to fit, so a long project name never widens the window.
    Its minimum is a fixed 80 px; it asks for the full text width up to maximumWidth()."""

    def __init__(self, text=""):
        super().__init__(text)
        self.full = text

    def set_full_text(self, text):
        self.full = text
        self.updateGeometry()
        self._elide()

    def _chrome(self):
        """Width the button needs besides its text (padding, chevron, spacing, border), as the style measures it."""
        return QPushButton.sizeHint(self).width() - math.ceil(self.fontMetrics().horizontalAdvance(self.text()))

    def sizeHint(self):
        sh = super().sizeHint()
        need = math.ceil(self.fontMetrics().horizontalAdvance(self.full)) + self._chrome() + 4
        return QSize(min(self.maximumWidth(), need), sh.height())

    def minimumSizeHint(self):
        return QSize(80, super().minimumSizeHint().height())

    def _elide(self):
        fm = self.fontMetrics()
        avail = self.width() - self._chrome()
        t = self.full if fm.horizontalAdvance(self.full) <= avail + 1 else \
            fm.elidedText(self.full, Qt.ElideRight, max(20, avail))
        if t != self.text():
            self.setText(t)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() in (ev.Type.FontChange, ev.Type.StyleChange):    # the stylesheet font arrives late
            self.updateGeometry()
            self._elide()


class Relay(QObject):
    """Runs a function on the GUI thread (worker threads emit; the queued connection delivers)."""
    call = Signal(object)


CANVAS = {"Vertical 1080x1920": (1080, 1920), "Landscape 1920x1080": (1920, 1080), "Square 1080x1080": (1080, 1080)}


class MainWindow(QMainWindow):
    _sel = None
    _sels = []

    @property
    def sel(self):
        return self._sel

    @sel.setter
    def sel(self, v):
        self._sel = v
        self._sels = [v] if v is not None else []
        self.sync_tools()

    def sync_tools(self):
        """Toolbar buttons that need a selected clip look disabled without one (kept in step with the selection)."""
        if getattr(self, "b_split", None) is None:
            return
        c = self._sel
        has = c is not None
        for b in (self.b_split, self.b_delete, self.b_ripple):
            b.setEnabled(has)
        self.b_detach.setEnabled(has and c.kind == "video" and bool(c.hasaudio))

    def selected(self):
        """Every selected clip (Ctrl+click, Shift+click or a drag box can select several)."""
        out = [c for c in self._sels if c in self.clips]
        return out or ([self._sel] if self._sel in self.clips else [])

    def set_selection(self, cl, primary=None):
        cl = [c for c in cl if c in self.clips]
        self._sels = cl
        self._sel = primary if primary in cl else (cl[-1] if cl else None)
        self.sync_tools()
        if len(cl) > 1:
            self.status(f"{len(cl)} clips selected: drag to move them together, Del to delete.")
        self.load_props()
        self.tl.update()

    def select_all(self):
        self.set_selection([c for c in self.clips if c.kind != "sub"] or list(self.clips))

    def select_after(self, c):
        self.set_selection([o for o in self.clips if o.track == c.track and o.start >= c.start - 1e-6], c)

    def insert_gap(self, t=None):
        t = self.t if t is None else t
        v, ok = QInputDialog.getDouble(self, "Add a gap", "Seconds of empty space to add here:", 1.0, 0.1, 600.0, 2)
        if not ok:
            return
        self.push_undo()
        for o in self.clips:
            if o.start >= t - 1e-6:
                o.start += v
        self.after_edit()
        self.status(f"Added {v:g} s of empty space.")

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 920)
        self.clips, self.lib = [], []
        self.sel = None
        self.t = 0.0
        self.playing = False
        self.undo, self.redo, self.last_push = [], [], 0.0
        self.W, self.H = 1080, 1920
        self.proj_path = None
        self._saved = "[]"
        self._loading = False
        self.work = tempfile.mkdtemp(prefix="clipstudio_")
        self.offline = set()
        self.init_project_state()

        # library
        self.libview = LibraryList()
        self.libview.itemDoubleClicked.connect(lambda it: self.add_lib_row(self.libview.row(it)))
        self.libview.setContextMenuPolicy(Qt.CustomContextMenu)
        self.libview.customContextMenuRequested.connect(self.lib_menu)
        b_imp = QPushButton("Import media")
        b_imp.setToolTip("Import video, pictures and audio files")
        b_imp.setStyleSheet("padding: 4px 6px;")      # fits the narrowest panel
        b_imp.clicked.connect(self.import_media)
        b_add = QPushButton("Add to timeline")
        b_add.clicked.connect(self.add_selected_lib)
        lib_w = QWidget()
        ll = QVBoxLayout(lib_w)
        ll.setContentsMargins(12, 12, 12, 12)
        ll.setSpacing(8)
        lib_hint = QLabel("Drag items onto the timeline")
        lib_hint.setObjectName("hint")
        ll.addWidget(lib_hint)
        ll.addWidget(self.libview, 1)
        self.conv_q, self.conv_cur, self.conv_t0 = [], None, 0.0
        self.conv_box = QGroupBox("Converting HDR to SDR")
        cb = QVBoxLayout(self.conv_box)
        self.conv_label = QLabel("")
        self.conv_label.setWordWrap(True)
        self.conv_bar = QProgressBar()
        self.conv_bar.setRange(0, 1000)
        self.conv_bar.setTextVisible(False)
        self.conv_bar.setObjectName("slim")
        self.conv_eta = QLabel("")
        self.conv_eta.setObjectName("hint")
        b_cc = QPushButton("Cancel conversion")
        b_cc.clicked.connect(self.cancel_conv)
        for wdg in (self.conv_label, self.conv_bar, self.conv_eta, b_cc):
            cb.addWidget(wdg)
        self.conv_box.hide()
        ll.addWidget(self.conv_box)
        b_imp.setIcon(mk_icon("plus", "#C9CBD1"))
        b_imp.setIconSize(QSize(14, 14))
        lr = QHBoxLayout()
        lr.setSpacing(8)
        lr.addWidget(b_imp, 1)
        lr.addWidget(b_add, 1)
        ll.addLayout(lr)
        tr_w = QWidget()
        tl_ = QVBoxLayout(tr_w)
        tl_.setContentsMargins(12, 12, 12, 12)
        tl_.setSpacing(8)
        tr_hint = QLabel("Drag a transition onto a clip\n(the clip must touch the one before it)")
        tr_hint.setObjectName("hint")
        tr_hint.setWordWrap(True)
        tl_.addWidget(tr_hint)
        self.translist = TransList()
        tl_.addWidget(self.translist, 1)
        cap_w = QWidget()
        cl = QVBoxLayout(cap_w)
        cl.setContentsMargins(12, 12, 12, 12)
        cl.setSpacing(8)
        cap_intro = QLabel("Type a caption, then add it to the timeline at the red playhead.")
        cap_intro.setWordWrap(True)
        cap_intro.setObjectName("hint")
        cl.addWidget(cap_intro)
        self.new_cap = QPlainTextEdit()
        self.new_cap.setPlaceholderText("Using people's voices on a soundboard \U0001F480")
        self.new_cap.setFixedHeight(110)
        cl.addWidget(self.new_cap)
        cl.addWidget(self.emoji_row(self.new_cap))
        b_cap = QPushButton("Add caption to timeline")
        b_cap.setObjectName("primary")
        b_cap.clicked.connect(lambda: self.add_caption())
        cl.addWidget(b_cap)
        hint = QLabel("Long text wraps onto new lines automatically. Each line gets a white box that snaps to "
                      "the width of its text, centred, like CapCut. Select a caption on the timeline to change its "
                      "font, size and colours.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        cl.addWidget(hint)
        cl.addStretch(1)
        left = QTabWidget()
        left.tabBar().setExpanding(False)
        left.addTab(lib_w, "Library")
        left.addTab(scrolled(cap_w), "Captions")
        left.addTab(self.build_sub_left(), "Subtitles")
        left.addTab(tr_w, "Transitions")

        # preview
        self.scene = QGraphicsScene()
        self.xbox = None
        self.view = PreviewView(self.scene, self)
        self.view.setBackgroundBrush(QColor("#111214"))
        self.view.setFrameShape(QGraphicsView.NoFrame)
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.canvas = None
        self.slots = {}
        self.build_canvas()

        # properties
        self.props = self.build_props()

        self.make_controls()
        pv = QWidget()
        pl = QVBoxLayout(pv)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        pl.addWidget(self.view, 1)
        pl.addWidget(self.transport_bar)
        top = QSplitter(Qt.Horizontal)
        top.addWidget(left)
        top.addWidget(pv)
        top.addWidget(self.props)
        self.props.setMinimumWidth(300)
        left.setMinimumWidth(260)
        top.setSizes([300, 840, 360])
        top.setStretchFactor(0, 0)     # the preview takes up any change in window width
        top.setStretchFactor(1, 1)
        top.setStretchFactor(2, 0)
        top.setCollapsible(1, False)

        # timeline + scrollbar
        self.tl = Timeline(self)
        self.tl.seek.connect(self.set_time)
        self.tl.selection.connect(self.load_props)
        self.hs = QScrollBar(Qt.Horizontal)
        self.hs.setObjectName("timelineScroll")
        self.hs.valueChanged.connect(self.on_hscroll)
        tlbox = QWidget()
        tv = QVBoxLayout(tlbox)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(0)
        tv.addWidget(self.tl, 1)
        tv.addWidget(self.hs)

        # editing bar above the timeline
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        bl.addWidget(self.editbar_w)
        bl.addWidget(tlbox, 1)

        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(bottom)
        main.setStretchFactor(0, 1)    # extra height goes to the preview, not to empty timeline space
        main.setStretchFactor(1, 0)
        main.setCollapsible(0, False)
        main.setCollapsible(1, False)
        main.setSizes([600, 360])
        self.main_split = main
        root = QWidget()
        rv = QVBoxLayout(root)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        rv.addWidget(self.header)
        rv.addWidget(main, 1)
        self.editor_page = root
        # the project home screen and the editor share the window; show_dashboard() / show_editor() switch
        self.dashboard = Dashboard(brand=brand_label(), icon=mk_icon, dialogs=sys.modules[__name__])
        self.dashboard.openRequested.connect(self.open_project)
        self.dashboard.newRequested.connect(lambda name: self.create_and_open(name, rename=True))
        self.dashboard.restoreRequested.connect(lambda p: self.open_project(p, recover="restore"))
        self.pages = QStackedWidget()
        self.pages.addWidget(self.editor_page)
        self.pages.addWidget(self.dashboard)
        self.setCentralWidget(self.pages)
        self.sb_hint = QLabel()
        self.sb_hint.setTextFormat(Qt.RichText)
        self.sb_hint.setText(hint_html(HINTS))
        self.statusBar().addPermanentWidget(self.sb_hint)
        self.status("Welcome! Import media to get started.")

        # shortcuts (editor ones are switched off while the project dashboard is showing)
        self.editor_shortcuts = []

        def sc(keys, fn, guard=True, editor=True):
            s = QShortcut(QKeySequence(keys), self)
            s.setContext(Qt.ApplicationShortcut)
            s.activated.connect(lambda: (None if guard and self.typing() else fn()))
            if editor:
                self.editor_shortcuts.append(s)
        sc("Space", self.toggle_play)
        sc("S", self.split)
        sc("Delete", self.delete_sel)
        sc("Shift+Delete", self.ripple_delete)
        sc("Ctrl+Z", self.do_undo, False)
        sc("Ctrl+Y", self.do_redo, False)
        sc("Ctrl+S", self.save_project, False)
        sc("Left", lambda: self.set_time(self.t - 1 / 30))
        sc("Right", lambda: self.set_time(self.t + 1 / 30))
        sc("Home", lambda: self.set_time(0))
        sc("End", lambda: self.set_time(self.total()))
        sc("Ctrl+Shift+Z", self.do_redo, False)
        sc("Ctrl+D", self.duplicate)
        sc("Ctrl+=", lambda: self.zoom(1.3), False)
        sc("Ctrl++", lambda: self.zoom(1.3), False)
        sc("Ctrl+-", lambda: self.zoom(1 / 1.3), False)
        sc("Ctrl+0", self.zoom_fit, False)
        sc("Backspace", self.delete_sel)
        sc("Ctrl+A", self.select_all)
        sc("Z", self.zoom_clip)
        sc("Ctrl+O", self.open_project, False, editor=False)
        sc("Ctrl+E", self.export, False)
        sc("Ctrl+Shift+S", self.save_project_as, False)
        sc("Ctrl+N", self.new_project, False, editor=False)
        sc("F2", self.start_rename)

        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        # autosave: edits are noticed right away (after_edit) or by this poll (library, canvas, subtitle look...);
        # stopping auto_timer switches autosave off (the tests do this)
        self.auto_timer = QTimer(self)
        self.auto_timer.setInterval(1000)
        self.auto_timer.timeout.connect(self.note_change)
        self.auto_timer.start()
        self.wall0 = 0.0
        self.t_at_play = 0.0
        self.sync_scroll()
        self.setAcceptDrops(True)
        BUS.prog.connect(self.on_prog)
        BUS.call.connect(lambda f: f())
        BUS.conv.connect(self.on_conv_prog)
        BUS.convdone.connect(self.on_conv_done)
        BUS.done.connect(self.on_export_done)
        self.prog = None
        self.cancel_flag = False
        for cb in self.findChildren(QComboBox):      # roomier popup rows (font lists keep their own preview)
            if not isinstance(cb, QFontComboBox):
                cb.setItemDelegate(QStyledItemDelegate(cb))
        self.fit_header(self.width())
        self.load_props()                            # inspector starts in its "no clip selected" state
        self._saved_meta = self.meta_doc()
        self._undo_ready = True
        # keys a focused control needs for itself must reach it instead of the window's shortcuts
        from PySide6.QtWidgets import QAbstractSlider, QAbstractButton, QAbstractItemView
        self._key_widgets = (QAbstractSlider, QComboBox, QAbstractButton, QAbstractItemView)
        for wdg in self.editor_page.findChildren(QWidget):
            if isinstance(wdg, self._key_widgets):
                wdg.installEventFilter(self)

    # ------------------------------------------------------------------ misc UI
    def status(self, msg, ms=6000):
        self.statusBar().showMessage(msg, ms)

    def make_controls(self):
        def btn(text, tip, fn, name="tool", icon=None, fixed=None):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            b.setObjectName(name)
            if icon:
                ic = mk_icon(icon, "#0a0a0e" if name == "play" else "#C9CBD1")
                ic.addPixmap(mk_icon(icon, "#4F5158").pixmap(48, 48), QIcon.Disabled)   # dims with the button
                b.setIcon(ic)
                b.setIconSize(QSize(16, 16))
            if fixed:
                b.setFixedSize(*fixed)
            b.setCursor(Qt.PointingHandCursor)
            return b

        def sep():
            f = QFrame()
            f.setFrameShape(QFrame.VLine)
            f.setObjectName("vsep")
            f.setFixedHeight(16)
            return f
        self.b_play = btn("", "Play / pause  (Space)", self.toggle_play, "play", "play", (36, 36))
        b_start = btn("", "Go to the start  (Home)", lambda: self.set_time(0), "round", "start", (28, 28))
        self.lbl_time = QLabel()
        self.lbl_time.setObjectName("time")
        self.lbl_time.setTextFormat(Qt.RichText)
        self.lbl_time.setText(self.time_html())
        self.lbl_time.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.transport_bar = QWidget()
        self.transport_bar.setObjectName("transport")
        self.transport_bar.setFixedHeight(44)
        self.transport = QHBoxLayout(self.transport_bar)
        self.transport.setContentsMargins(12, 4, 12, 4)
        self.transport.setSpacing(8)
        # time on the left, play controls centred, preview zoom on the right
        self.transport.addWidget(self.lbl_time)
        self.transport.addStretch(1)
        self.transport.addWidget(b_start)
        self.transport.addWidget(self.b_play)
        self.transport.addStretch(1)
        self.b_zoomv = btn("Zoom video", "Zoom the preview in on the clip so you can see it big  (Z).  "
                           "Mouse wheel zooms, right-drag moves around. Only changes the preview, not the export.",
                           self.zoom_clip, icon="fit")
        zr = QWidget()
        zl = QHBoxLayout(zr)
        zl.setContentsMargins(0, 0, 0, 0)
        zl.addStretch(1)
        zl.addWidget(self.b_zoomv)
        zr.setMinimumWidth(self.lbl_time.sizeHint().width())   # same width both sides keeps Play centred
        self.lbl_time.setMinimumWidth(self.lbl_time.sizeHint().width())
        self.transport.addWidget(zr)

        self.editbar_w = QWidget()
        self.editbar_w.setObjectName("toolbar")
        self.editbar_w.setFixedHeight(40)
        eb = QHBoxLayout(self.editbar_w)
        eb.setContentsMargins(8, 6, 8, 6)
        eb.setSpacing(4)
        eb.addWidget(btn("Undo", "Undo  (Ctrl+Z)", self.do_undo, icon="undo"))
        eb.addWidget(btn("Redo", "Redo  (Ctrl+Y)", self.do_redo, icon="redo"))
        eb.addSpacing(4)
        eb.addWidget(sep())
        eb.addSpacing(4)
        self.b_split = btn("Split", "Cut the selected clip in two at the red playhead  (S)", self.split, icon="split")
        eb.addWidget(self.b_split)
        self.b_delete = btn("Delete", "Delete the selected clip  (Del)", self.delete_sel, icon="trash")
        eb.addWidget(self.b_delete)
        eb.addWidget(btn("Add gap", "Insert empty space at the playhead: everything after it moves later",
                         lambda: self.insert_gap(), icon="plus"))   # lambda: clicked() would pass False as the time
        self.b_ripple = btn("Delete && close gap", "Delete the clip and pull later clips on that track back  "
                            "(Shift+Del)", self.ripple_delete, icon="ripple")
        eb.addWidget(self.b_ripple)
        eb.addSpacing(4)
        eb.addWidget(sep())
        eb.addSpacing(4)
        self.b_detach = btn("Detach audio", "Split the sound of the selected video into its own audio clip",
                            self.detach_audio, icon="audio")
        eb.addWidget(self.b_detach)
        eb.addStretch(1)
        self.ck_snap = QCheckBox("Snap")
        self.ck_snap.setChecked(True)
        self.ck_snap.setToolTip("Snap clips to each other and to the playhead")
        self.ck_snap.toggled.connect(lambda v: setattr(self.tl, "snap_on", v))
        eb.addWidget(self.ck_snap)
        eb.addSpacing(8)
        self.ck_ripple = QCheckBox("Magnetic")
        self.ck_ripple.setChecked(True)
        self.ck_ripple.setToolTip("Video track: dropping a clip between others inserts it and the rest shuffle along; "
                                  "gaps close when you move a clip away.  Hold Shift while dropping to place it freely.")
        self.ck_ripple.toggled.connect(lambda v: setattr(self.tl, "ripple", v))
        eb.addWidget(self.ck_ripple)
        eb.addSpacing(8)
        eb.addWidget(sep())
        eb.addSpacing(4)
        eb.addWidget(btn("", "Zoom out the timeline  (Ctrl+wheel)", lambda: self.zoom(1 / 1.3), "round", "minus", (28, 28)))
        eb.addWidget(btn("", "Zoom in the timeline  (Ctrl+wheel)", lambda: self.zoom(1.3), "round", "plus", (28, 28)))
        eb.addWidget(btn("Fit", "Zoom to fit the whole project", self.zoom_fit, icon="fit"))
        self.editbar = eb

        def field(text):
            lab = QLabel(text)
            lab.setObjectName("field")
            return lab
        hd = QHBoxLayout()
        hd.setContentsMargins(12, 6, 12, 6)
        hd.setSpacing(8)
        t = brand_label()                                   # the logo; in the editor it leads back to the projects
        t.setToolTip("Back to projects")
        t.setCursor(Qt.PointingHandCursor)
        t.mousePressEvent = lambda ev: self.show_dashboard() if ev.button() == Qt.LeftButton else None
        hd.addWidget(t)
        hd.addSpacing(12)
        hd.addWidget(sep())
        hd.addSpacing(4)
        self.b_proj = ElideButton("Untitled")
        self.b_proj.setObjectName("projectName")
        self.b_proj.setMinimumWidth(80)
        self.b_proj.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.b_proj.setToolTip("Project options")
        self.b_proj.setCursor(Qt.PointingHandCursor)
        self.b_proj.setIcon(mk_icon("chev_down", "#A6A8B0"))
        self.b_proj.setIconSize(QSize(10, 10))
        self.b_proj.setLayoutDirection(Qt.RightToLeft)       # chevron after the name
        self.b_proj.setMaximumWidth(320)
        self.b_proj.clicked.connect(self.project_menu)
        hd.addWidget(self.b_proj)
        self.name_edit = QLineEdit()                          # inline rename (F2), shown in place of the name
        self.name_edit.setObjectName("projectNameEdit")
        self.name_edit.hide()
        self.name_edit.installEventFilter(self)
        self.name_edit.returnPressed.connect(self.finish_rename)
        hd.addWidget(self.name_edit)
        self.lbl_save = QPushButton("")
        self.lbl_save.setObjectName("saveState")
        self.lbl_save.setIconSize(QSize(12, 12))
        self.lbl_save.setMinimumWidth(130)
        self.lbl_save.clicked.connect(self.save_state_clicked)
        hd.addWidget(self.lbl_save)
        hd.addStretch(1)
        self.cb_canvas = QComboBox()
        self.cb_canvas.addItems(list(CANVAS))
        self.cb_canvas.setToolTip("Shape of the video you are making")
        self.cb_canvas.setMinimumWidth(170)
        self.cb_canvas.currentTextChanged.connect(self.set_canvas)
        self.lbl_canvas = field("Canvas")
        self.lbl_canvas.setMinimumWidth(1)          # never adds to the window's minimum width
        hd.addWidget(self.lbl_canvas)
        hd.addWidget(self.cb_canvas)
        hd.addSpacing(8)
        self.cb_fps = QComboBox()
        self.cb_fps.addItems(["30", "60"])
        self.cb_fps.setToolTip("Frame rate of the exported video")
        self.cb_fps.setFixedWidth(72)
        self.lbl_fps = field("Frame rate")
        self.lbl_fps.setMinimumWidth(1)          # never adds to the window's minimum width
        hd.addWidget(self.lbl_fps)
        hd.addWidget(self.cb_fps)
        hd.addSpacing(8)
        hd.addWidget(sep())
        hd.addSpacing(4)
        self.ck_nv = QCheckBox("GPU (NVENC)")
        self.ck_nv.setToolTip("Faster export on NVIDIA graphics cards")
        hd.addWidget(self.ck_nv)
        hd.addSpacing(8)
        b_exp = btn("Export video", "Render the finished video to an MP4 file", self.export, "primary")
        b_exp.setMinimumWidth(122)
        b_exp.setIcon(mk_icon("export", "#FFFFFF"))
        b_exp.setIconSize(QSize(16, 16))
        hd.addWidget(b_exp)
        self.header = QWidget()
        self.header.setLayout(hd)
        self.header.setObjectName("header")
        self.header.setFixedHeight(44)

    def ripple_delete(self):
        cl = self.selected()
        if not cl:
            return
        self.push_undo()
        for c in sorted(cl, key=lambda x: -x.start):
            self.clips.remove(c)
            for o in self.clips:
                if o.track == c.track and o.start >= c.end - 1e-3 and o not in cl:
                    o.start = max(0.0, o.start - c.dur)
        self.sel = None
        self.load_props()
        self.after_edit()
        self.status("Deleted and closed the gap.")

    def detach_audio(self):
        c = self.sel
        if not c or c.kind != "video" or not c.hasaudio:
            self.status("Select a video clip that has sound first.")
            return
        a = Clip(**{f: getattr(c, f) for f in Clip.FIELDS if f != "id"})
        a.kind, a.track, a.w, a.h = "audio", 3, 0, 0
        while self.tl.overlaps(a, 3, a.start, a.dur):
            a.track = 4 if a.track == 3 else 3
            if self.tl.overlaps(a, a.track, a.start, a.dur):
                a.track = 3
                a.start = max(o.end for o in self.clips if o.track == 3 and a.start < o.end and o.start < a.start + a.dur)
        self.push_undo()
        c.hasaudio = False
        a.name = "Audio - " + c.name
        a.ttype, a.fin, a.fout = "None", c.fin, c.fout
        self.clips.append(a)
        self.sel = a
        self.load_props()
        self.after_edit()
        self.status("Audio detached to its own clip.")

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        n = 0
        for u in ev.mimeData().urls():
            f = u.toLocalFile()
            if f and os.path.isfile(f):
                self.add_to_library(f)
                n += 1
        if n:
            self.status(f"Imported {n} file(s).")
            ev.acceptProposedAction()

    def typing(self):
        w = QApplication.focusWidget()
        return isinstance(w, (QAbstractSpinBox, QLineEdit, QPlainTextEdit))

    def build_canvas(self):
        for tr in self.slots.values():
            for s in tr:
                s.release()
                s.player.stop()
        self.slots = {}
        self.scene.clear()
        self.canvas = QGraphicsRectItem(0, 0, self.W, self.H)
        self.canvas.setBrush(QBrush(Qt.black))
        self.canvas.setPen(QPen(QColor("#2A2B30"), 0))   # hairline so the black frame shows on the dark surround
        self.canvas.setFlag(QGraphicsRectItem.ItemClipsChildrenToShape, True)
        self.scene.addItem(self.canvas)
        self.empty_txt = QGraphicsSimpleTextItem("Your video preview\nwill appear here", self.canvas)
        self.empty_txt.setBrush(QBrush(QColor("#55575F")))
        f = QFont()
        f.setPixelSize(max(24, self.W // 22))
        self.empty_txt.setFont(f)
        br = self.empty_txt.boundingRect()
        self.empty_txt.setPos((self.W - br.width()) / 2, (self.H - br.height()) / 2)
        self.subitem = SubItem(self)
        self.subitem.W, self.subitem.H = self.W, self.H
        self.scene.addItem(self.subitem)
        self.xbox = TransformBox(self)
        self.scene.addItem(self.xbox)
        self.scene.setSceneRect(-20, -20, self.W + 40, self.H + 40)
        for ti in range(len(TRACKS)):
            self.slots[ti] = [Slot(self.canvas), Slot(self.canvas)]
        # draw order: Video track bottom, Overlay 2 on top
        for ti, z in [(2, 1), (1, 2), (0, 3), (3, 0), (4, 0)]:
            for s in self.slots[ti]:
                s.box.setZValue(z)
        self.view.reset()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.view.fit()
        n = 9 if self.width() >= 1500 else (6 if self.width() >= 1100 else 4)   # leave room for status messages
        if n != getattr(self, "_hint_n", None):
            self._hint_n = n
            self.sb_hint.setText(hint_html(HINTS[:n]))
        self.fit_header()
        self.update_title()

    def fit_header(self, width=None):
        """Below 1180 px the header drops the Canvas / Frame rate captions and caps the project name at 200 px."""
        narrow = (width or self.width()) < 1180
        self.b_proj.setMaximumWidth(200 if narrow else 320)
        for lab in (self.lbl_canvas, self.lbl_fps):
            lab.setVisible(not narrow)

    def showEvent(self, ev):
        super().showEvent(ev)
        if not getattr(self, "_split_done", False):   # first show: share the height sensibly on small screens too
            self._split_done = True
            tot = sum(self.main_split.sizes())
            if tot > 0:
                share = 0.40 if tot < 560 else 0.42
                b = max(self.main_split.widget(1).minimumSizeHint().height(), min(360, int(tot * share)))
                self.main_split.setSizes([tot - b, b])
        self.view.fit()

    def set_canvas(self, name):
        if (self.W, self.H) == CANVAS[name]:
            return
        if not self._opening and not getattr(self, "_restoring", False) and getattr(self, "_undo_ready", False):
            self.push_undo()                          # the canvas size is part of undo
        self.W, self.H = CANVAS[name]
        self.build_canvas()
        self.update_title()

    def build_props(self):
        def ss(lo, hi, step, suffix="", dec=2):
            d = SliderSpin(lo, hi, step, suffix, dec)
            d.valueChanged.connect(self.apply_props)
            return d

        def dsp(lo, hi, step, suffix="", dec=2):
            d = QDoubleSpinBox()
            d.setRange(lo, hi)
            d.setSingleStep(step)
            d.setDecimals(dec)
            d.setSuffix(suffix)
            d.valueChanged.connect(self.apply_props)
            return d
        self.p_name = QLabel("Click a clip on the timeline to edit it")
        self.p_name.setWordWrap(True)
        self.p_name.setObjectName("panelTitle")
        self.p_fin = ss(0, 5, 0.1, " s")
        self.p_fout = ss(0, 5, 0.1, " s")
        self.p_vol = ss(0, 300, 5, " %", 0)
        self.p_scale = ss(5, 500, 5, " %", 0)
        self.p_x = ss(-150, 150, 1, " %", 0)
        self.p_y = ss(-150, 150, 1, " %", 0)
        self.p_dur = dsp(0.1, 3600, 0.5, " s")
        self.p_trans = QComboBox()
        self.p_trans.addItems(TRANS)
        self.p_trans.currentTextChanged.connect(self.apply_props)
        self.p_tdur = ss(0.1, 3, 0.1, " s")
        tabs = QTabWidget()
        tabs.setUsesScrollButtons(True)
        tabs.tabBar().setExpanding(False)

        def tab(title, rows, note=""):
            w = QWidget()
            f = QFormLayout(w)
            f.setContentsMargins(12, 12, 12, 0)
            f.setHorizontalSpacing(12)
            f.setVerticalSpacing(8)
            for lab, wdg in rows:
                f.addRow(lab, wdg)
            if note:
                n = QLabel(note)
                n.setWordWrap(True)
                n.setObjectName("hint")
                f.addRow(n)
            tabs.addTab(scrolled(w), title)
        tab("Fade", [("Fade in", self.p_fin), ("Fade out", self.p_fout)],
            "Fades the selected clip in from / out to black (and its sound). This is not a transition: it only "
            "affects this clip.")
        rb = QWidget()
        rl = QHBoxLayout(rb)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        self.fit_btns = []
        for txt, tip, mode in [("Fit", "Fit the whole clip inside the video", "fit"),
                               ("Fill", "Zoom the clip to cover the whole video", "fill"),
                               ("Reset", "Back to centred and normal size", "reset")]:
            b = QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, m=mode: self.set_fit(m))
            rl.addWidget(b)
            self.fit_btns.append(b)
        tab("Adjust", [("", rb), ("Size", self.p_scale), ("X", self.p_x), ("Y", self.p_y),
                       ("Image length", self.p_dur)],
            "Tip: click the clip in the preview, then drag it to move it or drag a corner handle to resize it. "
            "Size 100 = fit the video.")
        tab("Audio", [("Volume", self.p_vol)])
        tab("Transition", [("Style", self.p_trans), ("Length", self.p_tdur)],
            "Plays between this clip and the one before it. The clips must be touching. You can also drag a "
            "transition from the Transitions tab onto a clip.")
        tabs.insertTab(0, scrolled(self.build_sub_panel()), "Subtitle")
        tabs.insertTab(0, scrolled(self.build_caption_panel()), "Caption")
        self.ptabs = tabs
        self.goto_tab("Fade")
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 12, 0, 0)        # tab text, pane line and fields all start at x=12
        lay.setSpacing(4)
        eyebrow = QLabel("Selected clip")
        eyebrow.setObjectName("eyebrow")
        self.p_eyebrow = eyebrow
        top = QWidget()
        tl_ = QVBoxLayout(top)
        tl_.setContentsMargins(12, 0, 12, 0)
        tl_.setSpacing(4)
        tl_.addWidget(eyebrow)
        tl_.addWidget(self.p_name)
        self.p_top = top
        lay.addWidget(top)
        lay.addSpacing(8)
        lay.addWidget(tabs, 1)
        # shown instead of the tabs while nothing is selected
        emp = QWidget()
        el = QVBoxLayout(emp)
        el.setContentsMargins(24, 0, 24, 0)
        el.setSpacing(0)
        el.addStretch(1)
        ic = QLabel()
        ic.setPixmap(mk_icon("fit", "#4F5158", 32).pixmap(32, 32))
        ic.setAlignment(Qt.AlignHCenter)
        el.addWidget(ic)
        el.addSpacing(8)
        t1 = QLabel("No clip selected")
        t1.setObjectName("emptyHead")
        t1.setAlignment(Qt.AlignHCenter)
        el.addWidget(t1)
        el.addSpacing(4)
        t2 = QLabel("Click a clip on the timeline to edit it.")
        t2.setObjectName("hint")
        t2.setWordWrap(True)
        t2.setFixedWidth(220)
        t2.setAlignment(Qt.AlignHCenter)
        el.addWidget(t2, 0, Qt.AlignHCenter)
        el.addStretch(2)
        emp.hide()
        self.p_empty = emp
        lay.addWidget(emp, 1)
        return w

    def emoji_row(self, edit):
        w = QWidget()
        g = QGridLayout(w)            # 6 x 2 grid: fits narrow panels
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(4)
        for i, e in enumerate(EMOJI_PICK):
            b = QPushButton(e)
            b.setFixedSize(28, 28)
            b.setObjectName("emoji")
            b.setToolTip("Insert this emoji")
            b.clicked.connect(lambda _=False, e=e: (edit.insertPlainText(e), edit.setFocus()))
            g.addWidget(b, i // 6, i % 6)
        g.setColumnStretch(6, 1)
        return w

    def build_caption_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 12, 12, 0)
        v.setSpacing(8)
        v.addWidget(QLabel("Caption text"))
        self.cap_edit = QPlainTextEdit()
        self.cap_edit.setPlaceholderText("Select a caption on the timeline to edit its text")
        self.cap_edit.setFixedHeight(72)
        self.cap_edit.textChanged.connect(self.apply_caption)
        v.addWidget(self.cap_edit)
        self.cap_emoji = self.emoji_row(self.cap_edit)
        v.addWidget(self.cap_emoji)
        f = QFormLayout()
        self.cap_font = QFontComboBox()
        self.cap_font.setEditable(False)
        self.cap_font.setMinimumContentsLength(12)
        self.cap_font.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cap_font.setCurrentFont(QFont(default_font()))
        self.cap_font.currentFontChanged.connect(lambda *_: self.apply_caption())
        self.cap_size = SliderSpin(24, 160, 2, " px", 0)
        self.cap_size.valueChanged.connect(lambda *_: self.apply_caption())
        self.cap_bold = SliderSpin(0, 100, 5, " %", 0)
        self.cap_bold.valueChanged.connect(lambda *_: self.apply_caption())
        self.cap_box = QCheckBox("White label boxes")
        self.cap_box.toggled.connect(lambda *_: self.apply_caption())
        self.cap_bcol = ColorButton("#FFFFFF")          # same colour control as the subtitle look
        self.cap_tcol = ColorButton("#000000")
        self.cap_bcol.setToolTip("Box colour")
        self.cap_tcol.setToolTip("Text colour")
        for b in (self.cap_bcol, self.cap_tcol):
            b.setFixedWidth(96)
        self.cap_bcol.changed.connect(lambda: self.set_cap_colour("cbox", self.cap_bcol.color))
        self.cap_tcol.changed.connect(lambda: self.set_cap_colour("ccol", self.cap_tcol.color))
        b_font = QPushButton("Load font file...")
        b_font.setToolTip("Add a font from a .ttf / .otf file (e.g. Montserrat or Proxima Nova)")
        b_font.clicked.connect(self.load_font)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(8)
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        f.addRow("Font", self.cap_font)
        f.addRow("", b_font)
        f.addRow("Size", self.cap_size)
        f.addRow("Boldness", self.cap_bold)
        f.addRow(self.cap_box)
        crow = QHBoxLayout()                     # box + text colour on one row keeps the panel short
        crow.setSpacing(8)
        crow.addWidget(self.cap_bcol)
        crow.addWidget(self.cap_tcol)
        crow.addStretch(1)
        f.addRow("Colours", crow)
        v.addLayout(f)
        n = QLabel("Each line gets its own box that hugs the text. Press Enter for a new line. Drag the caption in "
                   "the preview to move it.")
        n.setWordWrap(True)
        n.setObjectName("hint")
        v.addWidget(n)
        v.addStretch(1)
        self.cap_widgets = [self.cap_edit, self.cap_font, self.cap_size, self.cap_bold, self.cap_box, self.cap_bcol,
                            self.cap_tcol, b_font, self.cap_emoji]
        return w

    def load_font(self):
        fn, _ = QFileDialog.getOpenFileNames(self, "Choose font files", "", "Fonts (*.ttf *.otf)")
        fams = []
        for f in fn:
            i = QFontDatabase.addApplicationFont(f)
            if i >= 0:
                fams += QFontDatabase.applicationFontFamilies(i)
        if fams and self.sel is not None and self.sel.nat:
            self.cap_font.setCurrentFont(QFont(fams[0]))
        elif fams:
            self.status(f"Loaded font {fams[0]}. Pick it in a Font list (subtitles), or select a caption to use it.")

    # ------------------------------------------------------------------ subtitles
    def goto_tab(self, name):
        for i in range(self.ptabs.count()):
            if self.ptabs.tabText(i) == name:
                self.ptabs.setCurrentIndex(i)
                return

    def build_sub_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 12, 12, 0)
        v.setSpacing(8)
        v.addWidget(QLabel("Subtitle text"))
        self.sub_edit = QPlainTextEdit()
        self.sub_edit.setPlaceholderText("Select a subtitle on the Subtitles track to edit its words")
        self.sub_edit.setFixedHeight(90)
        self.sub_edit.textChanged.connect(self.apply_sub_text)
        v.addWidget(self.sub_edit)
        qr = QHBoxLayout()
        qr.setSpacing(8)
        b1 = QPushButton("(Parrot Replay)")
        b1.setToolTip("Replace this subtitle's text with (Parrot Replay)")
        b1.clicked.connect(lambda: self.sub_edit.setPlainText("(Parrot Replay)"))
        b2 = QPushButton("Wrap in ( )")
        b2.setToolTip("Put round brackets around the text (round tag colour)")
        b2.clicked.connect(lambda: self.wrap_sub("(", ")"))
        b3 = QPushButton("Wrap in [ ]")
        b3.setToolTip("Put square brackets around the text (square tag colour)")
        b3.clicked.connect(lambda: self.wrap_sub("[", "]"))
        for b in (b1, b2, b3):
            b.setCursor(Qt.PointingHandCursor)
            qr.addWidget(b)
        qr.addStretch(1)
        v.addLayout(qr)
        self.sub_quick = (b1, b2, b3)
        n = QLabel("Words in ( ) or [ ] use the tag colour. Drag the subtitle in the preview to move it, drag a "
                   "corner to resize it. The look (font, colours, animation) is set in the Subtitles tab on the "
                   "left and applies to every subtitle. For emojis use a Caption instead.")
        n.setWordWrap(True)
        n.setObjectName("hint")
        v.addWidget(n)
        v.addStretch(1)
        return w

    def build_sub_left(self):
        fams = set(QFontDatabase.families())
        self.sub_model = QComboBox()
        self.sub_model.addItems(["tiny", "base", "small", "medium", "large-v3"])
        self.sub_model.setCurrentText("small")
        self.sub_lang = QLineEdit("en")
        self.sub_lang.setPlaceholderText("en, de, auto...")
        self.sub_gpu = QCheckBox("GPU")
        self.sub_vad = QCheckBox("Skip silence")
        self.sub_vad.setChecked(True)
        self.sub_maxwords = QSpinBox()
        self.sub_maxwords.setRange(1, 12)
        self.sub_maxwords.setValue(4)
        self.sub_selonly = QCheckBox("Selected clip only")
        self.sub_selonly.setToolTip("Untick = subtitle the whole timeline (every clip with sound)")
        b = QPushButton("Generate subtitles")
        b.setToolTip("Transcribe every clip with sound on the timeline (not the Music track), or only the selected "
                     "clip when 'Selected clip only' is ticked.")
        b.setObjectName("primary")
        b.clicked.connect(self.generate_subtitles)
        gen = QGroupBox("Auto subtitles")
        gf = QFormLayout(gen)
        self._form_style(gf)
        gf.setContentsMargins(0, 8, 0, 0)
        gf.addRow("Model", self.sub_model)
        gf.addRow("Language", self.sub_lang)
        gf.addRow("Words per line", self.sub_maxwords)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.sub_gpu)
        row.addWidget(self.sub_vad)
        row.addStretch(1)
        gf.addRow("", row)                      # in the field column, under the other fields
        gf.addRow(self.sub_selonly)
        gf.addRow(b)
        hint = QLabel("Subtitles every clip with sound on the timeline (not the Music track). They appear on the "
                      "Subtitles track at the bottom; drag them to retime. The first run downloads the speech model.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        gf.addRow(hint)

        self.sub_font = QFontComboBox()
        self.sub_font.setEditable(False)
        self.sub_font.setMinimumContentsLength(12)
        self.sub_font.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.sub_font.setCurrentFont(QFont("Rubik" if "Rubik" in fams else "Arial"))
        self.sub_bold = QCheckBox("Bold")
        self.sub_bold.setChecked(True)
        self.sub_upper = QCheckBox("UPPERCASE")
        self.sub_size = QSpinBox()
        self.sub_size.setRange(10, 300)
        self.sub_size.setValue(78)
        self.sub_cmain, self.sub_cunsp = ColorButton("#6CB3FF"), ColorButton("#FFFFFF")
        self.sub_cout, self.sub_ctag = ColorButton("#000000"), ColorButton("#00FF00")
        self.sub_ctag2 = ColorButton("#FF3B3B")
        self.sub_outline = QSpinBox()
        self.sub_outline.setRange(0, 40)
        self.sub_outline.setValue(8)
        self.sub_ypos = QSlider(Qt.Horizontal)
        self.sub_ypos.setRange(50, 950)
        self.sub_ypos.setValue(550)
        self.sub_xpos = QSlider(Qt.Horizontal)
        self.sub_xpos.setRange(0, 1000)
        self.sub_xpos.setValue(500)
        self.sub_anim = QComboBox()
        self.sub_anim.addItems(ANIMS)
        self.sub_tag_anim = QComboBox()
        self.sub_tag_anim.addItems(["Same as subtitle"] + ANIMS)
        self.sub_tag_anim.setToolTip("A different animation for words inside ( ) or [ ], e.g. (Parrot Replay)")
        self.sub_anim_ms = QSpinBox()
        self.sub_anim_ms.setRange(50, 1500)
        self.sub_anim_ms.setSingleStep(50)
        self.sub_anim_ms.setValue(200)
        self.sub_anim_ms.setSuffix(" ms")
        sty = QGroupBox("Subtitle look (all subtitles)")
        sf = QFormLayout(sty)
        self._form_style(sf)
        sf.setContentsMargins(0, 8, 0, 0)
        sf.addRow("Font", self.sub_font)
        b_font2 = QPushButton("Load font file...")
        b_font2.setToolTip("Add a font from a .ttf / .otf file for subtitles and captions")
        b_font2.clicked.connect(self.load_font)
        sf.addRow("", b_font2)
        r2 = QHBoxLayout()
        r2.setSpacing(12)
        r2.addWidget(self.sub_bold)
        r2.addWidget(self.sub_upper)
        r2.addStretch(1)
        sf.addRow("", r2)
        sf.addRow("Size", self.sub_size)
        sf.addRow("Text colour", self.sub_cmain)
        sf.addRow("Not yet spoken", self.sub_cunsp)
        sf.addRow("Outline colour", self.sub_cout)
        sf.addRow("( ) tag colour", self.sub_ctag)
        sf.addRow("[ ] tag colour", self.sub_ctag2)
        sf.addRow("Outline width", self.sub_outline)
        sf.addRow("Vertical", self.sub_ypos)
        sf.addRow("Horizontal", self.sub_xpos)
        sf.addRow("Animation", self.sub_anim)
        sf.addRow("Tag animation", self.sub_tag_anim)
        sf.addRow("Anim. length", self.sub_anim_ms)
        dr = QHBoxLayout()
        bsave = QPushButton("Save as my default")
        bsave.setToolTip("Every new session starts with the subtitle look you have set here")
        bsave.clicked.connect(self.save_sub_defaults)
        breset = QPushButton("Reset to original")
        breset.setToolTip("Go back to the built-in subtitle look and forget the saved default")
        breset.clicked.connect(self.reset_sub_defaults)
        dr.addWidget(bsave)
        dr.addWidget(breset)
        sf.addRow(dr)
        for wdg, sig in [(self.sub_font, "currentFontChanged"), (self.sub_bold, "toggled"), (self.sub_upper, "toggled"),
                         (self.sub_size, "valueChanged"), (self.sub_outline, "valueChanged"),
                         (self.sub_ypos, "valueChanged"), (self.sub_xpos, "valueChanged"),
                         (self.sub_anim, "currentTextChanged"), (self.sub_tag_anim, "currentTextChanged"), (self.sub_anim_ms, "valueChanged")]:
            getattr(wdg, sig).connect(lambda *_: self.sub_changed())
        for cb in (self.sub_cmain, self.sub_cunsp, self.sub_cout, self.sub_ctag, self.sub_ctag2):
            cb.changed.connect(self.sub_changed)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(12, 12, 12, 12)
        iv.setSpacing(8)
        sub_intro = QLabel("Generate subtitles from the speech in your clips, then style them below.")
        sub_intro.setObjectName("hint")
        sub_intro.setWordWrap(True)
        iv.addWidget(sub_intro)
        iv.addWidget(gen)
        iv.addWidget(sty)
        iv.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(inner)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.load_sub_defaults()
        return sa

    @staticmethod
    def _form_style(f):
        """Forms in narrow panels: fields stretch, long rows wrap instead of being cut off."""
        f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        f.setRowWrapPolicy(QFormLayout.DontWrapRows)
        f.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(8)

    SUB_DEF_FILE = os.path.join(store.config_dir(), "sub_defaults.json")

    def sub_factory(self):
        fams = set(QFontDatabase.families())
        return dict(font="Rubik" if "Rubik" in fams else "Arial", bold=True, size=78, color="#6CB3FF",
                    color2="#FFFFFF", outline_color="#000000", outline=8, tag_color="#00FF00", tag_color2="#FF3B3B", ypct=55.0, xpct=50.0,
                    anim="Zoom In", tag_anim="Same as subtitle", anim_ms=200, upper=False)

    def load_sub_defaults(self):
        store.migrate_legacy_config()          # first run after Clip Studio: bring the saved look along
        try:
            with open(self.SUB_DEF_FILE, encoding="utf-8") as f:
                d = {**self.sub_factory(), **json.load(f)}
            self.apply_sub_style(d)
        except Exception:
            pass

    def save_sub_defaults(self):
        try:
            os.makedirs(os.path.dirname(self.SUB_DEF_FILE), exist_ok=True)
            with open(self.SUB_DEF_FILE, "w", encoding="utf-8") as f:
                json.dump(self.sub_style(), f)
            self.status("Saved. New sessions will start with this subtitle look.")
        except Exception as e:
            self.status(f"Could not save defaults: {e}")

    def reset_sub_defaults(self):
        try:
            if os.path.exists(self.SUB_DEF_FILE):
                os.remove(self.SUB_DEF_FILE)
        except Exception:
            pass
        self.apply_sub_style(self.sub_factory())
        self.status("Subtitle look reset to the original defaults.")

    def sub_style(self):
        return dict(font=self.sub_font.currentFont().family(), bold=self.sub_bold.isChecked(),
                    size=self.sub_size.value(), color=self.sub_cmain.color, color2=self.sub_cunsp.color,
                    outline_color=self.sub_cout.color, outline=self.sub_outline.value(), tag_color=self.sub_ctag.color, tag_color2=self.sub_ctag2.color,
                    ypct=self.sub_ypos.value() / 10.0, xpct=self.sub_xpos.value() / 10.0,
                    anim=self.sub_anim.currentText(), tag_anim=self.sub_tag_anim.currentText(), anim_ms=self.sub_anim_ms.value(), upper=self.sub_upper.isChecked())

    def apply_sub_style(self, d):
        """Set the whole subtitle look at once (one undo step when the user does it)."""
        if hasattr(self, "sub_anim_ms") and d == self.sub_style():
            return
        quiet = getattr(self, "_restoring", False)
        if not quiet and not self._opening:
            self.push_undo_for(("substyle-all",))
        self._restoring = True
        try:
            self._apply_sub_style(d)
        finally:
            self._restoring = quiet
        self.sub_changed()

    def _apply_sub_style(self, d):
        try:
            self.sub_font.setCurrentFont(QFont(d["font"]))
            self.sub_bold.setChecked(d["bold"])
            self.sub_upper.setChecked(d["upper"])
            self.sub_size.setValue(d["size"])
            self.sub_cmain.set_color(d["color"])
            self.sub_cunsp.set_color(d["color2"])
            self.sub_cout.set_color(d["outline_color"])
            self.sub_ctag.set_color(d["tag_color"])
            self.sub_ctag2.set_color(d.get("tag_color2", "#FF3B3B"))
            self.sub_outline.setValue(d["outline"])
            self.sub_ypos.setValue(int(d["ypct"] * 10))
            self.sub_xpos.setValue(int(d["xpct"] * 10))
            self.sub_anim.setCurrentText(d["anim"])
            self.sub_tag_anim.setCurrentText(d.get("tag_anim", "Same as subtitle"))
            self.sub_anim_ms.setValue(d["anim_ms"])
        except Exception:
            pass

    def sub_changed(self):
        if not self._opening and not getattr(self, "_restoring", False):
            self.push_undo_for(("substyle", id(self.sender())))
        if getattr(self, "subitem", None) is not None:
            self.subitem.update()

    def sub_abs_words(self, c):
        k = c.dur / max(c.wdur, 1e-6)
        return [[w[0], c.start + w[1] * k, c.start + w[2] * k] for w in c.words]

    def sub_active(self, t):
        for c in self.clips:
            if c.kind == "sub" and c.words and c.start <= t < c.end:
                return {"start": c.start, "end": c.end, "words": self.sub_abs_words(c)}
        return None

    def sub_caps(self):
        return [{"start": c.start, "end": c.end, "words": self.sub_abs_words(c)}
                for c in sorted(self.clips, key=lambda x: x.start) if c.kind == "sub" and c.words]

    def make_sub(self, words, start, end):
        end = max(end, start + 0.1)
        c = Clip(kind="sub", track=len(TRACKS) - 1, start=start, dur=end - start, wdur=end - start, srcdur=BIG,
                 words=[[w[0], w[1] - start, w[2] - start] for w in words])
        c.name = "Subtitle: " + " ".join(w[0] for w in words)[:32]
        return c

    def split_sub(self, c):
        ab = self.sub_abs_words(c)
        left = [w for w in ab if (w[1] + w[2]) / 2 < self.t]
        right = [w for w in ab if (w[1] + w[2]) / 2 >= self.t]
        if not left or not right:
            self.status("Put the playhead between two words of this subtitle to split it.")
            return
        self.push_undo()
        a, b = self.make_sub(left, c.start, self.t), self.make_sub(right, self.t, c.end)
        self.clips.remove(c)
        self.clips += [a, b]
        self.sel = b
        self.load_props()
        self.after_edit()
        self.status("Subtitle split.")

    def wrap_sub(self, o, c):
        t = self.sub_edit.toPlainText().strip().strip("()[]").strip()
        if t:
            self.sub_edit.setPlainText(f"{o}{t}{c}")

    def apply_sub_text(self):
        c = self.sel
        if self._loading or not c or c.kind != "sub":
            return
        toks = self.sub_edit.toPlainText().split()
        if not toks:
            return
        self.push_undo_for(("subtext", c.id))
        if len(toks) == len(c.words):
            for w, t in zip(c.words, toks):
                w[0] = t
        else:
            step = c.wdur / len(toks)
            c.words = [[t, i * step, (i + 1) * step] for i, t in enumerate(toks)]
        c.name = "Subtitle: " + " ".join(toks)[:32]
        self.p_name.setText(c.name)
        self.after_edit()

    def speech_sources(self):
        """Clips with sound on the timeline (not the Music track), in order."""
        return sorted((x for x in self.clips if x.kind in ("video", "audio") and x.hasaudio and x.vol > 0
                       and x.track != 4), key=lambda x: x.start)

    def generate_subtitles(self):
        src = self.speech_sources()
        if self.sub_selonly.isChecked():
            src = [c for c in self.selected() if c in src] or src[:0]
            if not src:
                QMessageBox.information(self, "Subtitles", "\"Selected clip only\" is ticked: click a video or "
                                        "audio clip with sound on the timeline first, or untick it.")
                return
        if not src:
            QMessageBox.information(self, "Subtitles", "Add a video or audio clip with sound to the timeline first.")
            return
        try:
            import faster_whisper  # noqa: F401
        except Exception:
            QMessageBox.information(self, "Subtitles", "Auto subtitles need the speech package. Open PowerShell and "
                                    "run:\n\npip install faster-whisper\n\nthen restart Parrot Studio.")
            return
        if any(x.kind == "sub" and any(x.start < c.end and x.end > c.start for c in src) for x in self.clips):
            if QMessageBox.question(self, "Subtitles", "Replace the subtitles already on the timeline for "
                                    + ("this clip?" if self.sub_selonly.isChecked() else "these clips?")) != QMessageBox.Yes:
                return
        self.status("Loading the speech model (the first run downloads it)...", 600000)
        model, lang = self.sub_model.currentText(), self.sub_lang.text().strip() or None
        gpu, vad, maxw = self.sub_gpu.isChecked(), self.sub_vad.isChecked(), self.sub_maxwords.value()
        total_dur = max(sum(c.dur for c in src), 0.1)

        def work():
            try:
                from faster_whisper import WhisperModel
                m = WhisperModel(model, device="cuda" if gpu else "cpu", compute_type="float16" if gpu else "int8")
                groups, done = [], 0.0
                for n, c in enumerate(src, 1):
                    wav = os.path.join(self.work, "sub_src.wav")
                    r = run([FFMPEG, "-y", "-v", "error", "-ss", f"{c.inp:.3f}", "-t", f"{c.dur:.3f}", "-i", c.path,
                             "-vn", "-ac", "1", "-ar", "16000", wav])
                    if r.returncode != 0:
                        raise RuntimeError(r.stderr[-400:])
                    segs, _ = m.transcribe(wav, language=lang, word_timestamps=True, vad_filter=vad)
                    words = []
                    for sg in segs:
                        for w in (sg.words or []):
                            t = w.word.strip()
                            if t:
                                words.append([t, c.start + float(w.start), c.start + min(float(w.end), c.dur)])
                        pct = int(min(100, (done + min(sg.end, c.dur)) / total_dur * 100))
                        BUS.call.emit(lambda pct=pct, n=n: self.status(
                            f"Transcribing clip {n} of {len(src)}... {pct}%", 600000))
                    done += c.dur
                    groups.append(words)
                BUS.call.emit(lambda: self.apply_subs([(c.start, c.end) for c in src], groups, maxw))
            except Exception as e:
                msg = str(e)
                BUS.call.emit(lambda: (self.status("Subtitles failed."), QMessageBox.warning(self, "Subtitles", msg)))
        threading.Thread(target=work, daemon=True).start()

    def after_subs(self, c, words, maxw):
        self.apply_subs([(c.start, c.end)], [words], maxw)

    def apply_subs(self, ranges, groups, maxw):
        if not any(groups):
            self.status("No speech found.")
            return
        self.push_undo()
        self.clips = [x for x in self.clips
                      if not (x.kind == "sub" and any(x.start < e and x.end > st for st, e in ranges))]
        n = 0
        for words in groups:               # each clip is chunked on its own so a subtitle never spans two clips
            caps = chunk_words(words, maxw) if words else []
            for i, cp in enumerate(caps):
                if i + 1 < len(caps):
                    cp["end"] = max(cp["start"] + 0.1, min(cp["end"], caps[i + 1]["start"]))
                self.clips.append(self.make_sub(cp["words"], cp["start"], cp["end"]))
            n += len(caps)
        self.sel = None
        self.load_props()
        self.after_edit()
        self.status(f"Done: {n} subtitles added across the timeline. Press Space to play and check them.")

    def set_cap_colour(self, attr, col):
        c = self.sel
        if self._loading or not c or not c.nat or getattr(c, attr).lower() == col.lower():
            return
        self.push_undo()
        setattr(c, attr, col.lower())
        self.rerender_caption(c)
        self.load_props()
        self.after_edit()

    def pick_cap_colour(self, attr):
        c = self.sel
        if not c or not c.nat:
            return
        col = QColorDialog.getColor(QColor(getattr(c, attr)), self, "Pick a colour")
        if col.isValid():
            self.push_undo()
            setattr(c, attr, col.name())
            self.rerender_caption(c)
            self.load_props()
            self.after_edit()

    def rerender_caption(self, c):
        c.path, c.w, c.h = render_caption(c)
        first = (c.text.strip().split("\n") or [""])[0]
        c.name = "Caption: " + (first[:26] + ("..." if len(first) > 26 else ""))

    def apply_caption(self):
        c = self.sel
        if self._loading or not c or not c.nat:
            return
        self.push_undo_for(("caption", c.id, id(self.sender())))
        c.text = self.cap_edit.toPlainText()
        c.cfont = self.cap_font.currentFont().family()
        c.csize = int(self.cap_size.value())
        c.cbold = int(self.cap_bold.value())
        c.cuse = self.cap_box.isChecked()
        self.rerender_caption(c)
        self.p_name.setText(c.name)
        self.after_edit()

    def add_caption(self, text=None):
        self.push_undo()
        text = (text if text is not None else self.new_cap.toPlainText()).strip() or "Your caption here"
        c = Clip(kind="image", track=1, start=self.t, dur=3.0, srcdur=BIG, nat=1, text=text, y=-30.0,
                 cfont=default_font())
        self.rerender_caption(c)
        free = [tr for tr in (1, 0) if not self.tl.overlaps(c, tr, c.start, c.dur)]
        if free:                                     # at the playhead: Overlay 1, else Overlay 2
            c.track = free[0]
        else:
            while self.tl.overlaps(c, c.track, c.start, c.dur):
                c.start = max(o.end for o in self.clips if o.track == c.track and c.start < o.end
                              and o.start < c.start + c.dur)
            self.status(f"Both overlay tracks are busy at the playhead, so the caption went to {fmt_t(c.start)}.")
        self.clips.append(c)
        self.sel = c
        self.load_props()
        self.after_edit()
        if free:
            self.status("Caption added. Edit it in the Caption tab, drag it in the preview, drag its edges to change "
                        "timing.")

    def load_props(self):
        c = self.sel
        self.sync_tools()
        self._loading = True
        try:
            self.p_name.setText(c.name if c else "Click a clip on the timeline to edit it")
            iscap = bool(c and c.nat)
            for wdg in self.cap_widgets:
                wdg.setEnabled(iscap)
            if not iscap and self.cap_edit.toPlainText():
                self.cap_edit.blockSignals(True)          # never show the previous caption's text greyed out
                self.cap_edit.clear()
                self.cap_edit.blockSignals(False)
            if iscap:
                if self.cap_edit.toPlainText() != c.text:
                    self.cap_edit.setPlainText(c.text)
                self.cap_font.setCurrentFont(QFont(c.cfont))
                self.cap_size.setValue(c.csize)
                self.cap_bold.setValue(eff_bold(c))
                self.cap_box.setChecked(c.cuse)
                self.cap_bcol.set_color(c.cbox.upper())
                self.cap_tcol.set_color(c.ccol.upper())
            issub = bool(c and c.kind == "sub")
            if not issub and self.sub_edit.toPlainText():
                self.sub_edit.blockSignals(True)
                self.sub_edit.clear()
                self.sub_edit.blockSignals(False)
            self.sub_edit.setEnabled(issub)
            for b in getattr(self, "sub_quick", ()):
                b.setEnabled(issub)
            if issub:
                txt = " ".join(w[0] for w in c.words)
                if self.sub_edit.toPlainText() != txt:
                    self.sub_edit.setPlainText(txt)
            key = c.id if (iscap or issub) else None
            if key != getattr(self, "_last_cap", None):
                if iscap:
                    self.goto_tab("Caption")
                elif issub:
                    self.goto_tab("Subtitle")
                elif self.ptabs.tabText(self.ptabs.currentIndex()) in ("Caption", "Subtitle"):
                    self.goto_tab("Fade")
            self._last_cap = key
            for wdg in (self.p_fin, self.p_fout, self.p_vol, self.p_scale, self.p_x, self.p_y, self.p_dur,
                        self.p_trans, self.p_tdur):
                wdg.setEnabled(bool(c))
            if c:
                self.p_fin.setValue(c.fin)
                self.p_fout.setValue(c.fout)
                self.p_vol.setValue(c.vol)
                self.p_scale.setValue(c.scale)
                self.p_x.setValue(c.x)
                self.p_y.setValue(c.y)
                self.p_dur.setValue(c.dur)
                self.p_dur.setEnabled(c.kind == "image")
                self.p_trans.setCurrentText(c.ttype)
                self.p_trans.setEnabled(c.kind != "audio")
                self.p_tdur.setValue(c.tdur)
                self.p_scale.setEnabled(c.kind != "audio")
                self.p_x.setEnabled(c.kind != "audio")
                self.p_y.setEnabled(c.kind != "audio")
                if c.kind == "sub":
                    for wdg in (self.p_fin, self.p_fout, self.p_vol, self.p_scale, self.p_x, self.p_y, self.p_dur,
                                self.p_trans, self.p_tdur):
                        wdg.setEnabled(False)
            for b in getattr(self, "fit_btns", ()):
                b.setEnabled(bool(c) and c.kind not in ("audio", "sub"))
            self.show_inspector_tabs(c)
        finally:
            self._loading = False

    def show_inspector_tabs(self, c):
        """Only the inspector tabs that do something for the selected clip; an empty state when none is selected."""
        n = len(self.selected())
        if c is None:
            vis = set()
        elif c.kind == "sub":          # with several clips selected, the tabs follow the primary one (it is edited)
            vis = {"Subtitle"}
        elif c.kind == "audio":
            vis = {"Fade", "Audio"}
        else:
            vis = {"Fade", "Adjust", "Transition"}
            if c.nat:
                vis.add("Caption")
            if c.kind == "video" and c.hasaudio:
                vis.add("Audio")
        self.ptabs.setVisible(bool(vis))
        self.p_empty.setVisible(not vis)
        self.p_top.setVisible(bool(vis))
        self.p_eyebrow.setText(f"{n} clips selected" if n > 1 else "Selected clip")
        for i in range(self.ptabs.count()):
            self.ptabs.setTabVisible(i, self.ptabs.tabText(i) in vis)
        if vis and self.ptabs.tabText(self.ptabs.currentIndex()) not in vis:
            first = {"audio": "Audio", "image": "Adjust"}.get(c.kind, "Fade") if n == 1 else "Fade"
            for name in (first, "Caption", "Subtitle", "Fade", "Adjust", "Audio"):
                if name in vis:
                    self.goto_tab(name)
                    break

    def apply_props(self, *_):
        c = self.sel
        if self._loading or not c:
            return
        self.push_undo_for(("props", c.id, id(self.sender())))
        c.fin = min(self.p_fin.value(), c.dur)
        c.fout = min(self.p_fout.value(), c.dur)
        c.vol, c.scale = self.p_vol.value(), self.p_scale.value()
        c.x, c.y = self.p_x.value(), self.p_y.value()
        c.ttype, c.tdur = self.p_trans.currentText(), self.p_tdur.value()
        if c.kind == "image":
            nd = self.p_dur.value()
            if not self.tl.overlaps(c, c.track, c.start, nd):
                c.dur = nd
        self.after_edit()

    def quick_fade(self, fi, fo):
        c = self.sel
        if not c:
            return
        self.push_undo()
        if fi is not None:
            c.fin = min(fi, c.dur)
        if fo is not None:
            c.fout = min(fo, c.dur)
        self.load_props()
        self.after_edit()

    def set_trans(self, t):
        if self.sel:
            self.push_undo()
            self.sel.ttype = t
            self.load_props()
            self.after_edit()

    # ------------------------------------------------------------------ library & clips
    def lib_entry(self, mime):
        try:
            e = self.lib[int(bytes(mime.data(MIME)).decode())]
            return None if e.get("pending") or e.get("missing") else e
        except Exception:
            return None

    def import_media(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Import media", "",
                                                "Media (*.mp4 *.mov *.mkv *.webm *.avi *.png *.jpg *.jpeg *.bmp *.webp "
                                                "*.gif *.mp3 *.wav *.m4a *.aac *.flac *.ogg);;All files (*)")
        for f in files:
            self.add_to_library(f)

    @staticmethod
    def icon_image(info):
        """The library picture as a QImage (safe to make on a worker thread), or a null image."""
        img = QImage()
        path = info["path"]
        if info["kind"] == "image":
            img = QImage(path)
        elif info["kind"] == "video":
            tmp = os.path.join(tempfile.gettempdir(), f"cs_{uuid.uuid4().hex[:8]}.png")
            run([FFMPEG, "-y", "-v", "error", "-ss", str(min(1.0, info["dur"] / 2)), "-i", path, "-frames:v", "1",
                 "-vf", "scale=240:-2", tmp])
            img = QImage(tmp)
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not img.isNull():
            img = img.scaled(240, 136, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return img

    def placeholder_icon(self, info, offline=False):
        pm = QPixmap(120, 68)
        if info.get("kind") == "audio" and not offline:
            pm.fill(QColor(TRACK_STYLE["audio"][0]))
            p = QPainter(pm)
            p.drawPixmap(48, 22, mk_icon("audio", TRACK_STYLE["audio"][3], 24).pixmap(24, 24))
            p.end()
        else:
            pm.fill(QColor("#2A2B30"))
            if offline:
                p = QPainter(pm)
                p.drawPixmap(48, 22, mk_icon("warn", "#E2A33B", 24).pixmap(24, 24))
                p.end()
        return QIcon(pm)

    def make_icon(self, info):
        pm = QPixmap.fromImage(self.icon_image(info))
        if pm.isNull():
            pm = QPixmap(120, 68)
            if info["kind"] == "audio":
                pm.fill(QColor(TRACK_STYLE["audio"][0]))
                p = QPainter(pm)
                p.drawPixmap(48, 22, mk_icon("audio", TRACK_STYLE["audio"][3], 24).pixmap(24, 24))
                p.end()
            else:
                pm.fill(QColor("#2A2B30"))
        return QIcon(pm.scaled(120, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def start_bg(self, info):
        path = info["path"]
        if info["hasaudio"] and info["kind"] in ("audio", "video"):
            threading.Thread(target=compute_wave, args=(path,), daemon=True).start()
        if info["kind"] == "video":
            threading.Thread(target=compute_thumbs, args=(path, info["dur"]), daemon=True).start()

    def add_to_library(self, path, ask=True, known=None):
        """Import a file. known: the entry stored in a project (its details are trusted, no re-probing)."""
        if any(l["path"] == path for l in self.lib):
            return
        if known and all(k in known for k in ("kind", "dur", "w", "h", "hasaudio")):
            info = dict(known)
            info.setdefault("hdr", False)
            info.setdefault("trc", "")
        else:
            info = probe(path)
        info["path"], info["name"], info["pending"] = path, os.path.basename(path), False
        info.pop("missing", None)
        self.lib.append(info)
        it = QListWidgetItem(info["name"])
        it.setIcon(self.placeholder_icon(info))        # the real picture follows from a worker thread
        it.setToolTip(path)
        self.libview.addItem(it)
        self._load_icon(info)
        if info["hdr"] and ask:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("HDR video detected")
            box.setText(f"\"{info['name']}\" looks like an HDR video.")
            box.setInformativeText("HDR clips often look washed out or dull in a normal (SDR) video. Convert it to SDR "
                                   "now? A new file called \"<name> SDR.mp4\" is saved next to the original, and the "
                                   "original is never changed. The clip is greyed out in the library until it's ready.")
            yes = box.addButton("Convert to SDR", QMessageBox.AcceptRole)
            box.addButton("Use as is", QMessageBox.RejectRole)
            box.setDefaultButton(yes)
            box.exec()
            if box.clickedButton() is yes:
                self.convert_hdr(info)
                return
        else:
            self.start_bg(info)
        self.status(f"Imported {info['name']}")

    def _load_icon(self, info):
        if info["kind"] == "audio":
            return

        def work():
            img = self.icon_image(info)
            if not img.isNull():
                self._post(lambda: self._set_lib_icon(info, img))
        threading.Thread(target=work, daemon=True).start()

    def _set_lib_icon(self, info, img):
        if info in self.lib:
            it = self.libview.item(self.lib.index(info))
            if it is not None:
                it.setIcon(QIcon(QPixmap.fromImage(img).scaled(120, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation)))

    def add_offline_lib(self, entry):
        """A library entry whose file can't be found: kept (and saved) so nothing is lost; shown greyed."""
        info = dict(entry)
        info["missing"] = True
        info.setdefault("name", os.path.basename(info["path"]))
        info["pending"] = False
        self.lib.append(info)
        it = QListWidgetItem(info["name"] + "\n(offline)")
        it.setIcon(self.placeholder_icon(info, offline=True))
        it.setForeground(QColor("#767880"))
        it.setToolTip(f"File not found:\n{info['path']}\nRight-click and choose Locate file\u2026 to find it.")
        self.libview.addItem(it)

    def delete_lib_selected(self):
        """Delete key in the Library: remove the selected item from the library (never from the timeline)."""
        r = self.libview.currentRow()
        if not 0 <= r < len(self.lib):
            return
        info = self.lib[r]
        if info.get("pending"):
            return
        if any(c.path == info["path"] for c in self.clips):
            self.status(f"\u201c{info['name']}\u201d is used on the timeline, so it stays in the library.")
            return
        self.remove_lib(info)
        self.status(f"Removed {info['name']} from the library.")

    def refresh_offline(self):
        """Which media paths can't be found (cached here, so painting never touches the disk)."""
        paths = {c.path for c in self.clips if not c.nat and c.kind != "sub" and c.path}
        self.offline = {p for p in paths if not os.path.exists(p)}

    def offline_at_playhead(self):
        return any(c.path in self.offline and c.start <= self.t < c.end for c in self.clips
                   if not c.nat and c.kind in ("video", "image"))

    def missing_paths(self):
        paths = {l["path"] for l in self.lib if l.get("missing")}
        paths |= {c.path for c in self.clips if not c.nat and c.kind != "sub" and c.path and not os.path.exists(c.path)}
        return sorted(paths)

    def locate_file(self, old):
        """Let the user find a moved file; every clip and library entry using it is relinked. Other missing files
        found in the same folder are offered too."""
        name = os.path.basename(old)
        ext = os.path.splitext(name)[1]
        p, _ = QFileDialog.getOpenFileName(self, f"Locate {name}", os.path.dirname(old) if os.path.isdir(
            os.path.dirname(old)) else "", f"{name} (*{ext});;All files (*)")
        if not p:
            return False
        n = self.relink({old: p})
        folder = os.path.dirname(p)
        more = {m: os.path.join(folder, os.path.basename(m)) for m in self.missing_paths()
                if os.path.isfile(os.path.join(folder, os.path.basename(m)))}
        if more and QMessageBox.question(
                self, "Locate files", f"{len(more)} more missing file(s) were found in the same folder. "
                "Use them too?", QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes:
            n += self.relink(more)
        self.status(f"Relinked {n} item(s).")
        return True

    def relink(self, mapping):
        """{old path: new path}: point clips and library entries at the new files. Returns how many changed."""
        n = 0
        self.push_undo()
        for c in self.clips:
            new = mapping.get(c.path)
            if new:
                if c.name == os.path.basename(c.path):
                    c.name = os.path.basename(new)
                c.path = new
                n += 1
                if c.hasaudio and c.kind in ("audio", "video"):
                    threading.Thread(target=compute_wave, args=(new,), daemon=True).start()
        for old, new in mapping.items():
            row = next((i for i, l in enumerate(self.lib) if l["path"] == old), None)
            if row is None:
                continue
            known = {k: v for k, v in self.lib[row].items() if k != "missing"}
            self.lib.pop(row)
            self.libview.takeItem(row)
            if any(l["path"] == new for l in self.lib):
                continue
            self.add_to_library(new, ask=False, known=known if os.path.getsize(new) == known.get("size", -1) else None)
            self.lib.insert(row, self.lib.pop())                  # keep the row where it was
            self.libview.insertItem(row, self.libview.takeItem(self.libview.count() - 1))
            self.start_bg(self.lib[row])
            n += 1
        self.after_edit()
        return n

    # ---- HDR -> SDR conversion (same filter chain as the standalone HDR-to-SDR script)
    def lib_menu(self, pos):
        it = self.libview.itemAt(pos)
        if not it:
            return
        info = self.lib[self.libview.row(it)]
        m = QMenu(self)
        if info.get("missing"):
            m.addAction("Locate file\u2026", lambda: self.locate_file(info["path"]))
            m.addSeparator()
        a = m.addAction("Convert HDR to SDR...", lambda: self.convert_hdr(info))
        a.setEnabled(info["kind"] == "video" and not info.get("pending") and not info.get("missing"))
        used = any(c.path == info["path"] for c in self.clips)
        r = m.addAction("Remove from library" + ("  (in use on the timeline)" if used else ""),
                        lambda: self.remove_lib(info))
        r.setEnabled(not used and not info.get("pending"))
        m.exec(self.libview.mapToGlobal(pos))

    def remove_lib(self, info):
        if info in self.lib:
            row = self.lib.index(info)
            self.lib.pop(row)
            self.libview.takeItem(row)

    def convert_hdr(self, info):
        if not has_zscale():
            QMessageBox.warning(self, "Can't convert", "This copy of ffmpeg can't convert HDR (it has no 'zscale' "
                                "filter). Install the full ffmpeg build: open PowerShell and run\n\n"
                                "winget install Gyan.FFmpeg\n\nthen restart Parrot Studio.")
            self.start_bg(info)
            return
        if info.get("pending"):
            return
        d = os.path.dirname(info["path"])
        base = os.path.splitext(os.path.basename(info["path"]))[0]
        out = os.path.join(d, f"{base} SDR.mp4")
        n = 2
        while os.path.exists(out):
            out = os.path.join(d, f"{base} SDR {n}.mp4")
            n += 1
        info["pending"] = True
        self.set_item_pending(info, True)
        self.conv_q.append(dict(info=info, out=out, cancel=False, proc=None))
        self.status(f"Queued HDR to SDR conversion of {info['name']}")
        if self.conv_cur is None:
            self.next_conv()

    def set_item_pending(self, info, pending):
        it = self.libview.item(self.lib.index(info))
        if it:
            it.setText(("Converting...\n" if pending else "") + info["name"])
            it.setFlags(it.flags() & ~Qt.ItemIsEnabled if pending else it.flags() | Qt.ItemIsEnabled)

    def next_conv(self):
        if not self.conv_q:
            self.conv_cur = None
            self.conv_box.hide()
            return
        job = self.conv_q.pop(0)
        self.conv_cur = job
        self.conv_t0 = time.time()
        self.conv_label.setText(f"Converting {job['info']['name']} to SDR...")
        self.conv_bar.setValue(0)
        self.conv_eta.setText("Starting...")
        self.conv_box.show()
        threading.Thread(target=self.run_convert, args=(job,), daemon=True).start()

    def run_convert(self, job):
        info = job["info"]
        dur = max(0.1, info["dur"])
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
               "-i", info["path"], "-vf", hdr_filter(info.get("trc", "")), "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "18", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
               "-color_range", "tv", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", job["out"]]
        tail, err = [], ""
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                 creationflags=LOWPRI)
            job["proc"] = p
            t0 = time.time()
            for line in p.stdout:
                line = line.strip()
                m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
                if m:
                    f = min(1.0, int(m[1]) / 1e6 / dur)
                    el = time.time() - t0
                    BUS.conv.emit(f, (el / f) * (1 - f) if f > 0.01 else -1.0)
                elif "=" not in line and line:
                    tail.append(line)
            p.wait()
            if job["cancel"]:
                err = "cancelled"
            elif p.returncode != 0:
                err = "\n".join(tail[-8:]) or f"ffmpeg exit {p.returncode}"
        except Exception as e:
            err = str(e)
        if err and os.path.exists(job["out"]):
            try:
                os.remove(job["out"])
            except OSError:
                pass
        BUS.convdone.emit(err)

    def on_conv_prog(self, f, eta):
        self.conv_bar.setValue(int(f * 1000))
        if eta < 0:
            self.conv_eta.setText(f"{f * 100:.0f}%  -  working out time left...")
        else:
            self.conv_eta.setText(f"{f * 100:.0f}%  -  about {int(eta // 60)}:{int(eta % 60):02d} left")

    def on_conv_done(self, err):
        job, self.conv_cur = self.conv_cur, None
        if job is None:
            return
        info = job["info"]
        info["pending"] = False
        if err:
            self.set_item_pending(info, False)
            self.start_bg(info)
            if err == "cancelled":
                self.status("Conversion cancelled. The original HDR file is still in the library.")
            else:
                QMessageBox.warning(self, "Conversion failed", err)
        else:
            new = probe(job["out"])
            new.update(path=job["out"], name=os.path.basename(job["out"]), pending=False)
            info.clear()
            info.update(new)
            it = self.libview.item(self.lib.index(info))
            it.setIcon(self.make_icon(info))
            it.setToolTip(info["path"])
            self.set_item_pending(info, False)
            self.start_bg(info)
            self.status(f"Done! {info['name']} is ready to use.")
        self.next_conv()

    def cancel_conv(self):
        if self.conv_cur:
            self.conv_cur["cancel"] = True
            try:
                self.conv_cur["proc"].kill()
            except Exception:
                pass

    def add_lib_row(self, r):
        if 0 <= r < len(self.lib):
            if self.lib[r].get("pending"):
                self.status("That clip is still converting to SDR - it'll be available when the bar finishes.")
                return
            if self.lib[r].get("missing"):
                self.status("That file can't be found. Right-click it and choose Locate file\u2026")
                return
            self.add_clip(self.lib[r], None, None)

    def add_selected_lib(self):
        self.add_lib_row(self.libview.currentRow())

    def add_clip(self, lib, track, start):
        self.push_undo()
        want = "a" if lib["kind"] == "audio" else "v"
        if track is None or TRACKS[track][1] != want:
            track = 3 if want == "a" else (1 if lib["kind"] == "image" else 2)
        dur = 5.0 if lib["kind"] == "image" else max(0.1, lib["dur"])
        if start is None:
            start = max([c.end for c in self.clips if c.track == track] + [0.0])
        c = Clip(name=lib["name"], path=lib["path"], kind=lib["kind"], track=track, start=start, inp=0.0, dur=dur,
                 srcdur=BIG if lib["kind"] == "image" else dur, w=lib["w"], h=lib["h"], hasaudio=lib["hasaudio"])
        if lib["kind"] == "image" and track < 2:
            c.scale = 50
        if track == 2 and self.tl.ripple and self.tl.overlaps(c, track, c.start, c.dur) and not self.tl.free_mode():
            kind, ref, st = self.tl.resolve(c)       # dropped between clips: insert it, the rest move along
            c.start = ref.start if kind == "before" else (ref.end if kind == "after" else st)
            self.clips.append(c)
            self.tl.push_chain(c)
        else:
            while self.tl.overlaps(c, track, c.start, c.dur):
                c.start = max(o.end for o in self.clips if o.track == track and c.start < o.end and o.start < c.start + c.dur)
            self.clips.append(c)
        self.sel = c
        self.load_props()
        self.after_edit()
        if len(self.clips) == 1:
            QTimer.singleShot(0, self.zoom_fit)
        self.status(f"Added {c.name}. Drag it to move, drag its edges to trim.")

    def split(self):
        c = self.sel
        if not c:
            self.status("Click a clip first, then move the red playhead over it and press S.")
            return
        if not (c.start + 0.05 < self.t < c.end - 0.05):
            self.status("Move the red playhead inside the selected clip to split it.")
            return
        if c.kind == "sub":
            self.split_sub(c)
            return
        self.push_undo()
        d1 = self.t - c.start
        n = Clip(**{f: getattr(c, f) for f in Clip.FIELDS if f != "id"})
        n.start, n.inp, n.dur = self.t, c.inp + d1, c.dur - d1
        n.fin, n.ttype = 0.0, "None"
        c.dur, c.fout = d1, 0.0
        self.clips.append(n)
        self.sel = n
        self.load_props()
        self.after_edit()
        self.status("Clip split at the playhead.")

    def duplicate(self):
        c = self.sel
        if not c:
            return
        self.push_undo()
        n = Clip(**{f: getattr(c, f) for f in Clip.FIELDS if f != "id"})
        n.start = c.end
        while self.tl.overlaps(n, n.track, n.start, n.dur):
            n.start = max(o.end for o in self.clips if o.track == n.track and n.start < o.end and o.start < n.start + n.dur)
        self.clips.append(n)
        self.sel = n
        self.after_edit()

    def delete_sel(self):
        cl = self.selected()
        if cl:
            self.push_undo()
            self.clips = [c for c in self.clips if c not in cl]
            self.sel = None
            self.load_props()
            self.after_edit()

    # ------------------------------------------------------------------ undo
    def snapshot(self):
        return json.dumps([c.to_dict() for c in self.clips])

    def undo_state(self):
        """What an undo step holds: the clips plus the canvas size and the subtitle look."""
        return json.dumps({"clips": [c.to_dict() for c in self.clips], "W": self.W, "H": self.H,
                           "sub": self.sub_style()})

    def restore(self, s):
        """Go back to an undo state (or a plain clip snapshot). The selection is kept for clips that still exist."""
        d = json.loads(s)
        ids = [c.id for c in self.selected()]
        prim = self.sel.id if self.sel is not None else None
        self._restoring = True
        try:
            if isinstance(d, dict):
                if (d.get("W"), d.get("H")) != (self.W, self.H) and d.get("W") and d.get("H"):
                    self.W, self.H = int(d["W"]), int(d["H"])
                    for name, wh in CANVAS.items():
                        if wh == (self.W, self.H):
                            self.cb_canvas.blockSignals(True)
                            self.cb_canvas.setCurrentText(name)
                            self.cb_canvas.blockSignals(False)
                    self.build_canvas()
                if d.get("sub") and d["sub"] != self.sub_style():
                    self.apply_sub_style(d["sub"])
                d = d.get("clips", [])
            self.clips = [Clip(**x) for x in d]
        finally:
            self._restoring = False
        keep = [c for c in self.clips if c.id in ids]
        self._sels = keep
        self._sel = next((c for c in keep if c.id == prim), keep[-1] if keep else None)
        self.sync_tools()
        self.load_props()
        self.after_edit()

    def push_undo(self):
        self.undo.append(self.undo_state())
        self.undo = self.undo[-80:]
        self.redo.clear()
        self.last_push = time.time()
        self._coalesce = None

    def push_undo_for(self, key):
        """One undo step per burst of edits to the same control of the same clip: a property edit never merges
        into an unrelated action that happened just before it."""
        if getattr(self, "_restoring", False) or not getattr(self, "_undo_ready", False):
            return
        if key != getattr(self, "_coalesce", None) or time.time() - self.last_push > 0.8:
            self.push_undo()
            self._coalesce = key
        else:
            self.last_push = time.time()             # a slow slider drag stays one step

    def do_undo(self):
        if self.undo:
            self.redo.append(self.undo_state())
            self.restore(self.undo.pop())
            self._coalesce = None

    def do_redo(self):
        if self.redo:
            self.undo.append(self.undo_state())
            self.restore(self.redo.pop())
            self._coalesce = None

    # ------------------------------------------------------------------ time / zoom
    def total(self):
        return project_len(self.clips)

    def after_edit(self, light=False):
        self.sync_tools()
        if not light:
            self.refresh_offline()
            self.update_title()
            self.note_change()
        self.sync_scroll()
        self.tl.update()
        self.update_preview(force=True)
        self.update_label()

    def time_html(self):
        return (f"<span style='color:#E8E9EC'>{fmt_t(self.t)}</span>"
                f"<span style='color:#767880'>&nbsp;/&nbsp;{fmt_t(self.total())}</span>")

    def update_label(self):
        self.lbl_time.setText(self.time_html())

    def set_time(self, t):
        self.t = max(0.0, t)
        if self.playing:
            self.wall0, self.t_at_play = time.perf_counter(), self.t
        self.tl.playhead_update()
        self.update_label()
        # dragging the playhead fires hundreds of events: refresh the video at most ~25x a second
        now = time.perf_counter()
        if now - getattr(self, "_pv_last", 0.0) >= 0.04:
            self._pv_last = now
            self.update_preview(force=True)
        else:
            if getattr(self, "_pv_timer", None) is None:
                self._pv_timer = QTimer(self)
                self._pv_timer.setSingleShot(True)
                self._pv_timer.timeout.connect(lambda: (setattr(self, "_pv_last", time.perf_counter()),
                                                        self.update_preview(force=True)))
            if not self._pv_timer.isActive():
                self._pv_timer.start(40)

    def toggle_play(self):
        if self.playing:
            self.playing = False
            self.b_play.setIcon(mk_icon("play", "#0a0a0e"))
        else:
            if self.t >= self.total() - 0.01:
                self.t = 0.0
            self.playing = True
            self.timer.start()
            self.wall0, self.t_at_play = time.perf_counter(), self.t
            self.b_play.setIcon(mk_icon("pause", "#0a0a0e"))
        self.update_preview(force=True)

    def tick(self):
        if not self.playing:
            self.timer.stop()          # idle: no timer wake-ups at all
            return
        if self.playing:
            self.t = self.t_at_play + (time.perf_counter() - self.wall0)
            if self.t >= self.total():
                self.t = self.total()
                self.playing = False
                self.b_play.setIcon(mk_icon("play", "#0a0a0e"))
            vis_r = self.tl.xt(self.tl.width())
            if self.t > vis_r - 0.5 or self.t < self.tl.t0:
                self.tl.t0 = max(0.0, self.t - 1.0)
                self.sync_scroll()
                self.tl.update()
            else:
                self.tl.playhead_update()
            self.update_preview()
            txt = self.time_html()
            if txt != self.lbl_time.text():
                self.lbl_time.setText(txt)

    def zoom(self, f):
        self.tl.pps = max(8.0, min(600.0, self.tl.pps * f))
        self.sync_scroll()
        self.tl.update()

    def zoom_fit(self):
        tot = max(self.total(), 5.0)
        self.tl.pps = max(8.0, (self.tl.width() - LBL - 30) / tot)
        self.tl.t0 = 0.0
        self.sync_scroll()
        self.tl.update()

    def sync_scroll(self):
        vis = max(1.0, (self.tl.width() - LBL) / self.tl.pps)
        mx = max(self.total() + 10, vis)
        self.hs.blockSignals(True)
        self.hs.setRange(0, int(max(0, mx - vis) * 1000))
        self.hs.setPageStep(int(vis * 1000))
        self.hs.setValue(int(self.tl.t0 * 1000))
        self.hs.blockSignals(False)

    def on_hscroll(self, v):
        self.tl.t0 = v / 1000.0
        self.tl.update()

    # ------------------------------------------------------------------ preview
    def active(self, ti, t):
        res = []
        for c in self.clips:
            if c.track != ti or c.kind == "sub":
                continue
            fi, fo, ext = eff_fades(self.clips, c)
            if c.start - 1e-6 <= t < c.end + ext:
                lt = t - c.start
                a = 1.0
                if fi > 0 and lt < fi:
                    a = min(a, lt / fi)
                rem = c.end + ext - t
                if fo > 0 and rem < fo:
                    a = min(a, rem / fo)
                off, wipe = [0.0, 0.0], None
                ti_ = trans_info(self.clips, c)
                if ti_ and lt < ti_[1]:
                    if ti_[0] in SLIDE_DIR:
                        ux, uy = SLIDE_DIR[ti_[0]]
                        off = [ux * (1 - lt / ti_[1]), uy * (1 - lt / ti_[1])]
                    elif ti_[0] in WIPES:
                        wipe = (ti_[0], lt / ti_[1])
                    elif ti_[0] == "Dip to white":  # preview shows this as a dip to black
                        a = min(a, lt / (ti_[1] / 2)) if lt < ti_[1] / 2 else a
                nx = next_of(self.clips, c)
                t2 = trans_info(self.clips, nx) if nx else None
                if t2 and t2[0].startswith("Push") and t > nx.start:
                    ux, uy = SLIDE_DIR[t2[0]]
                    q = min(1.0, (t - nx.start) / t2[1])
                    off = [off[0] - ux * q, off[1] - uy * q]
                if t2 and t2[0] == "Dip to white" and rem < t2[1] / 2:
                    a = min(a, rem / (t2[1] / 2))
                vol = c.vol / 100.0
                if c.kind != "image":
                    vol *= a
                res.append((c, max(0.0, a), vol, tuple(off), wipe))
        res.sort(key=lambda r: r[0].start)
        return res[-2:]

    def zoom_clip(self):
        if self.view.zoomed:
            self.view.reset()
            self.b_zoomv.setText("Zoom video")
            return
        c = self.sel if (self.sel and self.sel.kind in ("video", "image") and self.sel.start <= self.t < self.sel.end) else None
        if c is None:
            for ti in (0, 1, 2):
                for cc, *_ in reversed(self.active(ti, self.t)):
                    if cc.kind in ("video", "image"):
                        c = cc
                        break
                if c:
                    break
        if c is None:
            self.status("Move the playhead over a video first.")
            return
        fb = fitf(c, self.W, self.H) * c.scale / 100
        w, h = c.w * fb, c.h * fb
        cx, cy = self.W / 2 + c.x / 100 * self.W, self.H / 2 + c.y / 100 * self.H
        self.view.zoom_to(QRectF(cx - w / 2, cy - h / 2, w, h))
        self.b_zoomv.setText("Reset zoom")

    def clip_at_preview(self, pos):
        for ti in (0, 1, 2):
            for c, *_ in reversed(self.active(ti, self.t)):
                if c.kind in ("audio", "sub"):
                    continue
                fb = fitf(c, self.W, self.H)
                w, h = c.w * fb * c.scale / 100, c.h * fb * c.scale / 100
                cx, cy = self.W / 2 + c.x / 100 * self.W, self.H / 2 + c.y / 100 * self.H
                if abs(pos.x() - cx) <= w / 2 and abs(pos.y() - cy) <= h / 2:
                    return c
        return None

    def after_drag(self):
        self.load_props()
        self.update_preview(force=True)
        self.tl.update()

    def set_fit(self, mode):
        c = self.sel
        if not c or c.kind in ("audio", "sub"):
            return
        self.push_undo()
        if mode == "reset":
            c.x = c.y = 0.0
            c.scale = 100.0
        elif mode == "fill":
            fb = fitf(c, self.W, self.H)
            c.scale = min(500.0, 100.0 * max(self.W / max(1, c.w), self.H / max(1, c.h)) / fb)
            c.x = c.y = 0.0
        else:
            c.scale = 100.0
        self.load_props()
        self.after_edit()

    def update_preview(self, force=False):
        self.empty_txt.setVisible(not self.clips)
        if self.offline or getattr(self, "_off_shown", False):      # the "Media offline" note follows the playhead
            self._off_shown = bool(self.offline)
            self.view.viewport().update()
        if getattr(self, "subitem", None) is not None:
            self.subitem.set_time(self.t)
        vis = None
        c = self.sel
        if c is not None and c.kind not in ("audio", "sub") and not self.playing and c in self.clips:
            ext = eff_fades(self.clips, c)[2]
            if c.start - 1e-6 <= self.t < c.end + ext:
                vis = c
        if self.xbox is not None:
            self.xbox.set_clip(vis)
        for ti in range(len(TRACKS)):
            act = self.active(ti, self.t)
            slots = self.slots[ti]
            ids = {r[0].id for r in act}
            for s in slots:
                if s.cid is not None and s.cid not in ids:
                    s.release()
            for c, a, vol, off, wipe in act:
                s = next((s for s in slots if s.cid == c.id), None)
                if s is None:
                    s = next((s for s in slots if s.cid is None), None)
                if s is None:
                    continue
                s.show_clip(c, self.W, self.H, self.t, self.playing, a, vol, off, wipe)

    # ------------------------------------------------------------------ project files
    # A project is a .clipproj file (see project_store.py). Changes are saved automatically: an edit is noticed at
    # once (after_edit) or by the once-a-second poll (auto_timer), the project is written SAVE_DEBOUNCE_MS after
    # the last change (at least every SAVE_MAX_MS while changes keep coming), serialised here on the GUI thread and
    # written atomically by a worker thread. A crash-recovery snapshot is written RECOVERY_MS after each change and
    # removed once the project file has caught up.
    AUTOSAVE = store.legacy_autosave_path()        # Clip Studio's autosave file: offered for restore once
    SAVE_DEBOUNCE_MS, SAVE_MAX_MS, RECOVERY_MS = 2000, 30000, 400

    def init_project_state(self):
        self.writer = store.AsyncWriter()
        self.relay = Relay()
        self.relay.call.connect(lambda f: f())
        self.track_session = False   # main() turns this on: the session lock records the open project
        self.unclean_sessions = []
        self._extra = {}             # keys of the opened file that project_dict() doesn't make (kept when saving)
        self._disk_sig = None        # (mtime, size) of the project file when we last loaded or saved it
        self._last_doc = None        # the project state (JSON) last noticed ...
        self._saved_doc = None       # ... and last written to the project file
        self._gen = self._rec_gen = self._done_gen = 0
        self._conflict = False
        self._opening = False
        self._last_thumb = 0.0
        self.save_state, self.save_error = "", ""

        def single(ms, fn):
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(ms)
            t.timeout.connect(fn)
            return t
        self.save_timer = single(self.SAVE_DEBOUNCE_MS, self.autosave)
        self.save_max_timer = single(self.SAVE_MAX_MS, self.autosave)
        self.rec_timer = single(self.RECOVERY_MS, self.write_recovery)

    def _post(self, fn):
        """Run fn on the GUI thread (safe to call from the writer thread)."""
        try:
            self.relay.call.emit(fn)
        except RuntimeError:          # window already gone
            pass

    def project_dict(self):
        return dict(substyle=self.sub_style(), W=self.W, H=self.H,
                    lib=[{k: v for k, v in l.items() if k != "missing"} for l in self.lib if not l.get("pending")],
                    clips=[c.to_dict() for c in self.clips])

    def document(self):
        return {**self._extra, **self.project_dict()}

    def document_text(self, path=None):
        path = path or self.proj_path
        return json.dumps(store.prepare_document(self.document(), store.project_name(path) if path else None),
                          ensure_ascii=False)

    def state_doc(self):
        return json.dumps(self.project_dict(), ensure_ascii=False)

    def project_name(self):
        return store.project_name(self.proj_path) if self.proj_path else "Untitled"

    def meta_doc(self):
        return json.dumps([self.W, self.H, self.sub_style(),
                           [l["path"] for l in self.lib if not l.get("pending")]])

    def is_dirty(self):
        """Unsaved changes: the clips, or the canvas size, subtitle look or library."""
        if bool(self.clips) and self.snapshot() != self._saved:
            return True
        base = getattr(self, "_saved_meta", None)
        return base is not None and self.meta_doc() != base

    def mark_clean(self, snap=None):
        self._saved = self.snapshot() if snap is None else snap
        self._saved_meta = self.meta_doc()

    def update_title(self):
        if self.on_dashboard():
            self.setWindowTitle(APP_TITLE)
            return
        # "<name> – Parrot Studio"; no dirty star (autosave), except when saving failed or there is no file yet
        nm = self.project_name()
        note = " (not saved)" if (self.save_state == "failed" or (not self.proj_path and self.is_dirty())) else ""
        self.setWindowTitle(f"{nm}{note} \u2013 {APP_TITLE}")
        b = getattr(self, "b_proj", None)
        if b is not None:
            b.set_full_text(nm)
            b.setToolTip(f"{nm}\nProject options" if self.proj_path else "Project options")
            if not self.proj_path:
                self.set_save_state("untitled" if self.is_dirty() else "")

    # ---- pages
    def on_dashboard(self):
        return getattr(self, "pages", None) is not None and self.pages.currentWidget() is self.dashboard

    def show_editor(self):
        self.pages.setCurrentWidget(self.editor_page)
        for s in self.editor_shortcuts:
            s.setEnabled(True)
        self.statusBar().show()
        self.sb_hint.show()
        self.update_title()
        self.tl.setFocus()
        QTimer.singleShot(0, self.view.fit)

    def show_dashboard(self):
        """Back to the project home screen. The open project is saved first (False if the user cancelled)."""
        if not self.on_dashboard() and (self.proj_path or self.is_dirty()):
            if not self.leave_project("dashboard"):
                return False
        if self.proj_path or self.clips or self.lib:
            self.close_project()
        self.pages.setCurrentWidget(self.dashboard)
        for s in self.editor_shortcuts:            # Delete, arrows, Space... belong to the dashboard now
            s.setEnabled(False)
        self.sb_hint.hide()
        self.statusBar().clearMessage()
        self.statusBar().hide()
        self.dashboard.refresh()
        self.update_title()
        self.dashboard.focus_first()
        return True

    def close_project(self):
        """Empty the editor. The project must already be saved (see leave_project). The playhead time is kept."""
        if self.playing:
            self.toggle_play()
        for t in (self.save_timer, self.save_max_timer, self.rec_timer):
            t.stop()
        self._opening = True
        try:
            self.proj_path = None
            self.clips, self.lib = [], []
            self.libview.clear()
            self.sel = None
            self.undo.clear()
            self.redo.clear()
            self.apply_sub_style(self.sub_factory())
            self.load_sub_defaults()
            self.cb_canvas.setCurrentIndex(0)
            self.load_props()
            self.after_edit()
        finally:
            self._opening = False
        self._extra, self._disk_sig = {}, None
        self.mark_clean("[]")
        self._saved_doc = self._last_doc = None
        self._conflict = False
        self.set_save_state("")
        self.track_project()

    def track_project(self):
        if self.track_session:
            store.update_session_lock(self.proj_path)

    def leave_project(self, why="open"):
        """Make the current project's changes safe before switching away or closing. False = stay here."""
        self.save_timer.stop()
        self.save_max_timer.stop()
        if self.proj_path:
            if self._conflict:
                self.resolve_conflict()
            if self.autosave(wait=True):
                self.save_thumbnail()
                return True
            r = QMessageBox.question(
                self, "Changes not saved", "Your latest changes could not be saved to the project file:\n\n"
                f"{self.save_error or 'The file was changed by another program.'}\n\nThey are kept as recovered "
                "changes, and Parrot Studio will offer them the next time you open this project. Continue anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return False
            self.keep_unsaved_as_recovery()
            return True
        if not self.is_dirty():
            return True
        if why == "open":
            return QMessageBox.question(
                self, "Open project", "Your current project has unsaved changes. Open another project anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes
        return self.ask_save_untitled("Save your project before closing?" if why == "close" else
                                      "Save this project before leaving it?")

    def ask_save_untitled(self, text):
        m = QMessageBox(self)
        m.setIcon(QMessageBox.Question)
        m.setWindowTitle("Unsaved changes")
        m.setText(text)
        sv = m.addButton("Save", QMessageBox.AcceptRole)
        ds = m.addButton("Don't save", QMessageBox.DestructiveRole)
        ds.setObjectName("danger")
        ds.style().unpolish(ds)
        ds.style().polish(ds)
        cn = m.addButton("Cancel", QMessageBox.RejectRole)
        m.setDefaultButton(sv)
        m.exec()
        if m.clickedButton() is cn:
            return False
        if m.clickedButton() is sv and not self.save_project():
            return False
        return True

    def keep_unsaved_as_recovery(self):
        """Store the current state as a 'kept' recovery snapshot (offered next time the project is opened)."""
        self.writer.wait_idle(10)
        try:
            p = store.write_recovery(self.proj_path, self.document_text())
            store.keep_recovery(store.RecoveryInfo(p, self.proj_path, time.time()))
        except Exception as e:
            store.report_problem(f"Could not keep the unsaved changes of {self.proj_path}", e)
            QMessageBox.warning(self, "Could not keep changes", f"The unsaved changes could not be stored:\n{e}")

    # ---- autosave
    def note_change(self):
        """After edits and once a second: start the autosave countdown if the project changed."""
        timer = getattr(self, "auto_timer", None)
        if self._opening or timer is None or not timer.isActive() or self.on_dashboard():
            return
        if not self.proj_path and not self.clips:
            return
        doc = self.state_doc()
        if doc == self._last_doc:
            return
        self._last_doc = doc
        self._gen += 1
        self.rec_timer.start()
        if self.proj_path and not self._conflict:
            self.save_timer.start()                       # restarts: 2 s after the last change ...
            if not self.save_max_timer.isActive():        # ... but never more than 30 s behind
                self.save_max_timer.start()
            self.set_save_state("pending")

    def autosave(self, wait=False):
        """Save changes to the project file now, in the background (wait=True: before returning). Untitled work
        goes to a recovery snapshot instead. Returns False if saving failed or is blocked by a conflict."""
        self.save_timer.stop()
        self.save_max_timer.stop()
        if self._opening:
            return True
        if not self.proj_path:
            if self.is_dirty():
                self.write_recovery()
            if wait:
                self.writer.wait_idle(10)
            self.update_title()
            return True
        if self._conflict:
            self.set_save_state("conflict")
            return False
        doc = self.state_doc()
        self._last_doc = doc
        if doc == self._saved_doc and self.save_state != "failed":
            self.set_save_state("saved")
            return True
        text, path, gen, snap = self.document_text(), self.proj_path, self._gen, self.snapshot()
        self.set_save_state("saving")

        def job():
            if self.proj_path != path:                    # project switched meanwhile: nothing to do
                return None
            sig = store.save_project_text(path, text, self._disk_sig)
            self._disk_sig = sig
            return sig
        if wait:
            self.writer.wait_idle(15)
            try:
                res, err = job(), None
            except Exception as e:
                res, err = None, e
            self.save_done(path, doc, snap, gen, res, err)
            return self.save_state == "saved"
        self.writer.submit(("save", path), job,
                           lambda res, err: self._post(lambda: self.save_done(path, doc, snap, gen, res, err)))
        return True

    def save_done(self, path, doc, snap, gen, res, err):
        if path != self.proj_path or gen < self._done_gen:
            return
        self._done_gen = gen
        if isinstance(err, store.ConflictError):
            self._conflict = True
            self.save_error = str(err)
            self.set_save_state("conflict")
            self.write_recovery(force=True)
            QTimer.singleShot(0, self.resolve_conflict)
            return
        if err is not None:
            self.save_error = str(err)
            self.set_save_state("failed")
            self.status("Autosave failed: " + str(err).replace("\n", " "), 12000)
            self.write_recovery(force=True)
            self.save_max_timer.start()                   # try again in a while
            return
        if res is None:
            return
        self._saved_doc = doc
        self.mark_clean(snap)
        if self._rec_gen <= gen:                          # the file has caught up: drop the recovery copy
            if gen == self._gen:
                self.rec_timer.stop()
            self.writer.submit(("rec", path), lambda: store.discard_recovery(path))
        self.set_save_state("saved" if doc == self._last_doc else "pending")
        self.update_title()
        if time.time() - self._last_thumb > 60:
            self.save_thumbnail()

    def write_recovery(self, force=False):
        """Write a crash-recovery snapshot of unsaved work (in the background)."""
        if self._opening or self.on_dashboard() or (not self.proj_path and not self.clips):
            return
        if not force and (self.state_doc() == self._saved_doc if self.proj_path else not self.is_dirty()):
            return
        text, path = self.document_text(), self.proj_path
        self._rec_gen = self._gen

        def done(res, err):
            if err is not None:
                store.report_problem(f"Could not write the recovery copy of {path or 'an untitled project'}", err)
                self._post(lambda: self.status(f"Couldn't write a recovery copy: {err}", 12000))
        self.writer.submit(("rec", path), lambda: store.write_recovery(path, text), done)

    SAVE_STATES = {   # text, icon, icon colour
        "": ("", None, None),
        "untitled": ("Not saved yet", None, None),
        "pending": ("Edited", None, None),
        "saving": ("Saving\u2026", None, None),
        "saved": ("Saved", "check", "#767880"),
        "failed": ("Couldn't save \u00b7 Retry", "warn", "#F07A7E"),
        "conflict": ("Changed on disk", "warn", "#E2A33B"),
    }

    def set_save_state(self, st):
        """The little save indicator next to the project name ('Saved', 'Saving…', 'Couldn't save · Retry'...).
        Public so screenshots and tests can show every state."""
        b = getattr(self, "lbl_save", None)
        now = time.monotonic()
        if b is not None and st in ("saved", "pending") and self.save_state == "saving":
            left = 0.6 - (now - getattr(self, "_saving_since", 0.0))
            if left > 0:                  # keep "Saving…" up long enough to read, so it never just flickers
                self.save_state = st
                QTimer.singleShot(int(left * 1000) + 10, lambda: self._show_save_state(self.save_state))
                return
        if st == "saving" and self.save_state != "saving":
            self._saving_since = now
        prev = self.save_state
        self.save_state = st
        self._show_save_state(st)
        if (prev == "failed") != (st == "failed"):
            self.update_title()

    def _show_save_state(self, st):
        b = getattr(self, "lbl_save", None)
        if b is None:
            return
        text, icon, col = self.SAVE_STATES[st]
        first = (self.save_error or "").strip().split("\n")[-1]
        tip = {
            "untitled": "This project isn't stored in a file yet. Click to choose where to keep it.",
            "pending": "Your changes are stored automatically in a moment.",
            "saving": "Storing your changes\u2026",
            "saved": f"All changes saved \u00b7 {time.strftime('%H:%M')}\n{self.proj_path or ''}",
            "failed": f"{first}\nYour changes are kept in a recovery copy. Click to try again.",
            "conflict": "The project file was changed by another program, so autosave is paused. "
                        "Click to choose what to do.",
        }.get(st, "")
        b.setText(text)
        b.setToolTip(tip)
        if icon:
            b.setIcon(QIcon(mk_icon(icon, col).pixmap(24, 24)))   # every state: one 24 px pixmap, same text x
        elif st == "saving":
            self._spin_step()
        else:                                    # same text position in every state: a blank 12 px icon
            pm = QPixmap(24, 24)
            pm.fill(Qt.transparent)
            b.setIcon(QIcon(pm))
        spin = getattr(self, "_spin_timer", None)
        if st == "saving" and spin is None:
            spin = self._spin_timer = QTimer(self)
            spin.setInterval(100)
            spin.timeout.connect(self._spin_step)
        if spin is not None:
            spin.start() if st == "saving" else spin.stop()
        b.setCursor(Qt.PointingHandCursor if st in ("failed", "conflict", "untitled") else Qt.ArrowCursor)
        if b.property("state") != st:
            b.setProperty("state", st)
            b.style().unpolish(b)
            b.style().polish(b)

    def _spin_step(self):
        """'Saving…' spinner: a small arc that turns."""
        self._spin_a = (getattr(self, "_spin_a", 0) + 30) % 360
        pm = QPixmap(24, 24)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#A6A8B0"), 2.4, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(4.5, 4.5, 15, 15), -self._spin_a * 16, 270 * 16)
        p.end()
        self.lbl_save.setIcon(QIcon(pm))

    def save_state_clicked(self):
        st = self.save_state
        if st == "conflict":
            self.resolve_conflict()
        elif st == "failed":
            self.status("Trying to save again\u2026")
            self.autosave()
        elif st == "untitled":
            self.save_project()
        else:
            self.status(self.lbl_save.toolTip().replace("\n", " "))

    def resolve_conflict(self):
        """The file changed on disk behind our back: ask what to do. True once resolved."""
        if not self._conflict or not self.proj_path:
            return True
        m = QMessageBox(self)
        m.setIcon(QMessageBox.Warning)
        m.setWindowTitle("Project changed on disk")
        m.setText(f"“{self.project_name()}” was changed by another program while it was open here.")
        m.setInformativeText("Your changes are safe in a recovery copy. Keep your version (it replaces the file), "
                             "load the version on disk, or save yours as a separate copy?")
        mine = m.addButton("Keep my version", QMessageBox.AcceptRole)
        disk = m.addButton("Load the version on disk", QMessageBox.ActionRole)
        copy = m.addButton("Save mine as a copy…", QMessageBox.ActionRole)
        m.addButton("Decide later", QMessageBox.RejectRole)
        m.setDefaultButton(mine)
        m.exec()
        b = m.clickedButton()
        if b is mine:
            return self._save_to(self.proj_path, force=True)
        if b is disk:
            self.keep_unsaved_as_recovery()
            self._conflict = False
            self.open_project(self.proj_path, recover=False)
            return True
        if b is copy:
            return self.save_project_as()
        return False

    # ---- explicit saving
    def _save_to(self, path, new_path=False, force=False):
        self.save_timer.stop()
        self.save_max_timer.stop()
        self.writer.wait_idle(15)
        doc, snap, old = self.state_doc(), self.snapshot(), self.proj_path
        d = self.document()
        if new_path and old != path:                       # a separate copy gets its own identity
            d["meta"] = {k: v for k, v in (d.get("meta") or {}).items() if k not in ("id", "created")}
        text = json.dumps(store.prepare_document(d, store.project_name(path)), ensure_ascii=False)
        try:
            sig = store.save_project_text(path, text, None if (new_path or force) else self._disk_sig)
        except store.ConflictError as e:
            self._conflict, self.save_error = True, str(e)
            self.set_save_state("conflict")
            return self.resolve_conflict()
        except Exception as e:
            self.save_error = str(e)
            self.set_save_state("failed")
            QMessageBox.warning(self, "Could not save", str(e) if isinstance(e, store.ProjectError)
                                else f"The project could not be saved:\n{e}")
            return False
        if old != path:
            if old is None:
                store.discard_recovery(None)
            self.proj_path = path
            self._extra = {k: v for k, v in d.items() if k not in PROJECT_KEYS}
            store.add_recent(path)
            self.track_project()
        self._disk_sig, self._saved_doc, self._last_doc = sig, doc, doc
        self.mark_clean(snap)
        self._done_gen = self._gen
        self._conflict = False
        self.rec_timer.stop()
        store.discard_recovery(path)
        self.set_save_state("saved")
        self.update_title()
        self.status("Project saved.")
        self.save_thumbnail()
        return True

    def projects_dir(self):
        d = store.get_projects_dir()
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d

    def save_project(self):
        if not self.proj_path:
            p, _ = QFileDialog.getSaveFileName(self, "Save project", os.path.join(self.projects_dir(), "Untitled.clipproj"),
                                               PROJECT_FILTER)
            if not p:
                return False
            return self._save_to(self._project_target(p), new_path=True)
        return self._save_to(self.proj_path)

    def _project_target(self, p):
        """Normalise a path from a save dialog. A new file put straight into the projects folder gets its own
        project folder there, like projects made from the dashboard."""
        p = p if p.endswith(".clipproj") else p + ".clipproj"
        root = self.projects_dir()
        if not os.path.exists(p) and os.path.normcase(os.path.dirname(os.path.abspath(p))) == os.path.normcase(root):
            nm = store.unique_name(root, store.safe_name(store.project_name(p)))
            try:
                for sub in ("recovery", "cache"):
                    os.makedirs(os.path.join(root, nm, sub))
                p = os.path.join(root, nm, nm + ".clipproj")
            except OSError:
                pass
        return p

    def save_project_as(self):
        """Save a copy under a new name and carry on editing the copy."""
        start = os.path.join(self.projects_dir(), (self.project_name() + " copy" if self.proj_path else "Untitled")
                             + ".clipproj")
        p, _ = QFileDialog.getSaveFileName(self, "Save project as", start, PROJECT_FILTER)
        if not p:
            return False
        p = self._project_target(p)
        if self.proj_path and os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(self.proj_path)):
            return self.save_project()
        if self.proj_path and not self._conflict:
            self.autosave(wait=True)                      # the original keeps everything done so far
        return self._save_to(p, new_path=True)

    # ---- project menu (header)
    def project_menu(self):
        m = QMenu(self)
        has = bool(self.proj_path)

        def act(text, fn, keys=None, enabled=True):
            a = m.addAction(text, fn)
            if keys:                      # shown in the menu only: the real shortcuts are the window's sc(...) ones
                a.setShortcut(QKeySequence(keys))
                a.setShortcutContext(Qt.WidgetShortcut)
                a.setShortcutVisibleInContextMenu(True)
            a.setEnabled(enabled)
            return a
        act("Rename\u2026", self.start_rename, "F2", has)
        act("Save", self.save_project, "Ctrl+S")
        act("Save as\u2026", self.save_project_as, "Ctrl+Shift+S")
        act("Show in folder", lambda: reveal_in_file_manager(self.proj_path), enabled=has)
        m.addSeparator()
        act("New project", self.new_project, "Ctrl+N")
        act("Open project\u2026", self.open_project, "Ctrl+O")
        m.addSeparator()
        act("Back to projects", self.show_dashboard)
        self.b_proj.setProperty("open", True)
        self.b_proj.style().unpolish(self.b_proj)
        self.b_proj.style().polish(self.b_proj)
        try:
            m.exec(self.b_proj.mapToGlobal(QPoint(0, self.b_proj.height() + 4)))
        finally:
            self.b_proj.setProperty("open", False)
            self.b_proj.style().unpolish(self.b_proj)
            self.b_proj.style().polish(self.b_proj)
            m.deleteLater()

    def start_rename(self):
        """F2 / Rename…: edit the project name right in the header."""
        if not self.proj_path:
            return self.save_project()
        if self.on_dashboard():
            return
        self.name_edit.setText(self.project_name())
        # about where the name was; it may shrink (to 80 px) rather than widen the window
        self.name_edit.setMinimumWidth(80)
        self.name_edit.setMaximumWidth(min(320, max(self.b_proj.width(), 160)))
        self.name_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.b_proj.hide()
        self.name_edit.show()
        self.name_edit.setFocus(Qt.OtherFocusReason)
        self.name_edit.selectAll()

    def finish_rename(self, commit=True):
        if not self.name_edit.isVisible():
            return
        name = self.name_edit.text().strip()
        self.name_edit.hide()
        self.b_proj.show()
        if commit and name and name != self.project_name():
            self.rename_project(name)
        self.tl.setFocus()

    NAV_KEYS = (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_Home, Qt.Key_End, Qt.Key_PageUp,
                Qt.Key_PageDown)

    def widget_wants_key(self, w, key, mods):
        """True if the focused control w uses this key itself (so the editor shortcut must not take it)."""
        from PySide6.QtWidgets import QAbstractSlider, QAbstractButton, QAbstractItemView
        if mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return False
        if key in self.NAV_KEYS:
            return isinstance(w, (QAbstractSlider, QComboBox, QAbstractItemView))
        if key == Qt.Key_Space:
            return isinstance(w, (QAbstractButton, QComboBox))
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            return isinstance(w, QAbstractItemView)    # the Library handles Delete itself (removes the item)
        return False

    def eventFilter(self, obj, ev):
        if ev.type() == ev.Type.ShortcutOverride and isinstance(obj, getattr(self, "_key_widgets", ())) \
                and obj.hasFocus() and self.widget_wants_key(obj, ev.key(), ev.modifiers()):
            ev.accept()                                # deliver it to the control as a normal key press
            return True
        if obj is getattr(self, "name_edit", None):
            if ev.type() == ev.Type.KeyPress and ev.key() == Qt.Key_Escape:
                self.finish_rename(commit=False)
                return True
            if ev.type() == ev.Type.FocusOut and obj.isVisible():
                QTimer.singleShot(0, lambda: self.finish_rename(commit=False))
        return super().eventFilter(obj, ev)

    def rename_project(self, name=None):
        """Rename the open project (and its folder). Without a name: ask with a dialog."""
        if not self.proj_path:
            return self.save_project()
        if name is None:
            name, ok = QInputDialog.getText(self, "Rename project", "New name:", QLineEdit.Normal,
                                            self.project_name())
            if not ok:
                return False
        name = (name or "").strip()
        if not name or name == self.project_name():
            return False
        if not self.leave_project("rename"):
            return False
        try:
            new = store.rename_project(self.proj_path, name)
        except store.ProjectError as e:
            QMessageBox.warning(self, "Could not rename", str(e))
            return False
        self.proj_path = new
        self._disk_sig = store.file_signature(new)
        self.track_project()
        self.set_save_state("saved")
        self.update_title()
        self.status(f"Renamed to “{self.project_name()}”.")
        return True

    def new_project(self):
        """Ctrl+N: a new project straight away ("Untitled video"), with its name ready to be typed over."""
        return self.create_and_open(self.dashboard.suggest_name(), rename=True)

    def create_and_open(self, name, data=None, rename=False):
        """Make a new project in the projects folder and open it (the current one is saved first)."""
        if not self.on_dashboard() and (self.proj_path or self.is_dirty()) and not self.leave_project("new"):
            return False
        try:
            p = store.create_project(name, data=data)
        except store.ProjectError as e:
            QMessageBox.warning(self, "Could not create the project", str(e))
            return False
        ok = self.open_project(p, recover=False)
        if ok and rename:
            QTimer.singleShot(0, self.start_rename)
        return ok

    # ---- thumbnails for the dashboard
    def render_thumbnail(self, width=384):
        """The preview at the playhead as a small picture (video frames come from the players or the timeline
        thumbnails, so nothing is decoded here)."""
        W, H = self.W, self.H
        img = QImage(width, max(1, round(width * H / W)), QImage.Format_RGB32)
        img.fill(QColor("#000000"))
        swapped = []
        for ss in self.slots.values():
            for s in ss:
                if s.cid is None or not s.vitem.isVisible():
                    continue
                sink = s.vitem.videoSink()
                fr = sink.videoFrame() if sink is not None else None
                pm = QPixmap.fromImage(fr.toImage()) if fr is not None and fr.isValid() else self._thumb_frame(s.cid)
                if pm is None or pm.isNull():
                    continue
                sz = s.vitem.size()
                s.pitem.setPixmap(pm)
                s.pitem.setTransform(QTransform.fromScale(sz.width() / pm.width(), sz.height() / pm.height()))
                s.pitem.setPos(s.vitem.pos())
                s.pitem.setOpacity(s.vitem.opacity())
                s.pitem.show()
                s.vitem.hide()
                swapped.append(s)
        hidden = [i for i in (self.xbox, self.empty_txt) if i is not None and i.isVisible()]
        for i in hidden:
            i.hide()
        pen = self.canvas.pen()
        self.canvas.setPen(QPen(Qt.NoPen))
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        try:
            self.scene.render(p, QRectF(img.rect()), QRectF(0, 0, W, H))
            if not swapped and self._blank(img):          # nothing under the playhead: use the first picture
                c = next((c for c in sorted(self.clips, key=lambda c: (c.start, -c.track))
                          if c.kind in ("video", "image") and c.track <= 2), None)
                pm = (QPixmap(c.path) if c.kind == "image" else self._thumb_frame(c.id)) if c else None
                if pm is not None and not pm.isNull():
                    sz = pm.size().scaled(img.size(), Qt.KeepAspectRatio)
                    p.drawPixmap(QRectF((img.width() - sz.width()) / 2, (img.height() - sz.height()) / 2,
                                        sz.width(), sz.height()), pm, QRectF(pm.rect()))
        finally:
            p.end()
            self.canvas.setPen(pen)
            for i in hidden:
                i.show()
            for s in swapped:
                s.pitem.hide()
                s.vitem.show()
        return img

    def _thumb_frame(self, cid):
        c = next((c for c in self.clips if c.id == cid), None)
        if c is None or c.path not in THUMBS:
            return None
        iv, files = THUMBS[c.path]
        t = c.inp + min(max(0.0, self.t - c.start), c.dur) if c.start <= self.t <= c.end else c.inp
        return pix_of(files[max(0, min(len(files) - 1, int(t / iv)))])

    @staticmethod
    def _blank(img):
        small = img.scaled(8, 8)
        return all(small.pixelColor(x, y).value() < 12 for x in range(8) for y in range(8))

    def save_thumbnail(self):
        if not self.proj_path:
            return
        try:
            dst = store.thumbnail_path(self.proj_path)
            if not self.clips:
                if os.path.exists(dst):
                    os.remove(dst)
                return
            img = self.render_thumbnail()
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".tmp"
            if img.save(tmp, "JPG", 85):
                os.replace(tmp, dst)
            self._last_thumb = time.time()
        except Exception as e:
            store.log_error(f"Could not save the thumbnail of {self.proj_path}", e)

    # ---- start-up, closing, opening
    def offer_restore(self):
        """At start-up: offer Clip Studio's old autosave (once), never-saved work and projects a crash left behind."""
        if os.path.exists(self.AUTOSAVE):
            r = QMessageBox.question(self, "Restore your work?", "Parrot Studio found unsaved work from Clip Studio "
                                     "(it was closed without saving). Restore it as a new project?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            data = None
            if r == QMessageBox.Yes:
                try:
                    data = store.load_project_file(self.AUTOSAVE)
                except store.ProjectError as e:
                    QMessageBox.warning(self, "Could not restore", f"The unsaved work could not be read:\n{e}")
            try:                                          # offered once: set it aside, never delete it
                os.replace(self.AUTOSAVE, self.AUTOSAVE + ".old")
            except OSError as e:
                store.report_problem("Could not set the old autosave file aside", e)
            if data is not None:
                self.create_and_open("Recovered project", data)
                return
        info = store.find_recovery(None)
        if info is not None:
            r = QMessageBox.question(self, "Restore your work?", "Parrot Studio found work from last time that was "
                                     "never saved to a project. Restore it as a new project?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            data = None
            if r == QMessageBox.Yes:
                try:
                    data = store.load_recovery(info)
                except store.ProjectError as e:
                    QMessageBox.warning(self, "Could not restore", f"The unsaved work could not be read:\n{e}")
            store.discard_recovery(None, info)
            if data is not None:
                self.create_and_open("Recovered project", data)
                return
        for s in self.unclean_sessions:
            p = s.get("project") if isinstance(s, dict) else None
            if p and os.path.isfile(p) and store.has_recovery(p):
                r = QMessageBox.question(self, "Parrot Studio closed unexpectedly",
                                         f"Parrot Studio didn't close properly while “{store.project_name(p)}” "
                                         "was open. Its unsaved changes were recovered. Open the project now?",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if r == QMessageBox.Yes:
                    self.open_project(p)
                break
        self.unclean_sessions = []
        probs = store.take_problems()
        if probs:
            self.status(probs[-1], 12000)

    def closeEvent(self, ev):
        if self.proj_path:
            if not self.leave_project("close"):
                ev.ignore()
                return
        elif self.is_dirty() and not self.ask_save_untitled("Save your project before closing?"):
            ev.ignore()
            return
        for proc in (getattr(self, "exp_proc", None), (self.conv_cur or {}).get("proc")):
            try:
                if proc:
                    proc.kill()
            except Exception:
                pass
        for t in (self.save_timer, self.save_max_timer, self.rec_timer):
            t.stop()
        self.writer.wait_idle(10)
        if not self.proj_path:
            store.discard_recovery(None)       # closed on purpose: never-saved work was saved or let go
        self.writer.stop()
        shutil.rmtree(self.work, ignore_errors=True)
        ev.accept()

    def offer_recovery(self, p):
        """If p has recovered unsaved changes, ask what to do. Returns (info, data) to restore, or None."""
        try:
            info = store.find_recovery(p)
        except Exception as e:
            store.report_problem(f"Could not check {p} for recovered changes", e)
            info = None
        probs = store.take_problems()
        if probs:
            self.status(probs[-1], 12000)
        if info is None:
            return None
        when = time.strftime("%d %b %Y at %H:%M", time.localtime(info.saved_at)).lstrip("0")
        m = QMessageBox(self)
        m.setIcon(QMessageBox.Question)
        m.setWindowTitle("Recover unsaved changes?")
        m.setText(f"“{store.project_name(p)}” has changes that were never saved to the project file.")
        m.setInformativeText(f"They were recovered on {when}. Restore them, or open the last saved version? "
                             "If you keep them for later, you'll be asked again next time.")
        rb = m.addButton("Restore changes", QMessageBox.AcceptRole)
        m.addButton("Keep for later", QMessageBox.RejectRole)
        db = m.addButton("Discard", QMessageBox.DestructiveRole)
        db.setObjectName("danger")
        db.style().unpolish(db)
        db.style().polish(db)
        m.setDefaultButton(rb)
        m.exec()
        b = m.clickedButton()
        try:
            if b is rb:
                return info, store.load_recovery(info)
            if b is db:
                store.discard_recovery(p, info)
            else:
                store.keep_recovery(info)
        except store.ProjectError as e:
            QMessageBox.warning(self, "Could not recover", f"The recovered changes could not be read:\n{e}")
        except OSError as e:
            store.report_problem(f"Could not update the recovery file of {p}", e)
        return None

    def open_failed(self, p, e):
        """Explain why a project didn't open; offer its backup when there is a good one. Returns data or None."""
        bak = getattr(e, "backup", None)
        if bak:
            m = QMessageBox(self)
            m.setIcon(QMessageBox.Warning)
            m.setWindowTitle("Could not open")
            m.setText(f"That project file could not be read:\n{e}")
            m.setInformativeText("Parrot Studio kept a backup from the save before. Open the backup instead? "
                                 "(The damaged file is replaced the next time the project is saved.)")
            ob = m.addButton("Open the backup", QMessageBox.AcceptRole)
            m.addButton("Cancel", QMessageBox.RejectRole)
            m.setDefaultButton(ob)
            m.exec()
            if m.clickedButton() is ob:
                try:
                    return store.load_project_file(bak)
                except store.ProjectError as e2:
                    e = e2
            else:
                return None
        QMessageBox.warning(self, "Could not open", f"That project file could not be read:\n{e}")
        return None

    def open_project(self, path=None, restore=False, recover=True):
        """Open a .clipproj (any location, old Clip Studio files too). No path: ask with a file dialog.
        restore=True loads the file as unsaved, untitled work. recover=False skips the recovered-changes prompt."""
        p = path if isinstance(path, str) else ""
        if not p:
            if self.on_dashboard():
                return self.dashboard.open_existing()
            if not self.leave_project("open"):
                return False
            start = os.path.dirname(self.proj_path) if self.proj_path else self.projects_dir()
            p, _ = QFileDialog.getOpenFileName(self, "Open project", start, PROJECT_FILTER)
        elif (not restore and self.proj_path and not self.on_dashboard()
              and os.path.normcase(os.path.abspath(p)) != os.path.normcase(os.path.abspath(self.proj_path))):
            if not self.leave_project("open"):
                return False
        if not p:
            return False
        p = p if restore else os.path.abspath(p)
        try:
            d = store.load_project_file(p)
            clips = [Clip(**c) for c in d["clips"]]
        except Exception as e:
            d = self.open_failed(p, e)
            if d is None:
                return False
            clips = [Clip(**c) for c in d["clips"]]
        saved_snap = json.dumps([c.to_dict() for c in clips])
        if recover == "restore" and not restore:     # the dashboard's Restore button: no need to ask again
            info = store.find_recovery(p)
            try:
                rec = (info, store.load_recovery(info)) if info else None
            except store.ProjectError as e:
                QMessageBox.warning(self, "Could not recover", f"The recovered changes could not be read:\n{e}")
                rec = None
        else:
            rec = self.offer_recovery(p) if recover and not restore else None
        if rec is not None:
            try:
                rclips = [Clip(**c) for c in rec[1]["clips"]]
                d, clips = rec[1], rclips
            except Exception as e:
                QMessageBox.warning(self, "Could not recover", f"The recovered changes could not be used:\n{e}")
                rec = None
        if self.playing:
            self.toggle_play()
        for t in (self.save_timer, self.save_max_timer, self.rec_timer):
            t.stop()
        self._opening = True
        try:
            self.proj_path = None if restore else p
            self.libview.clear()
            self.lib = []
            for l in d.get("lib", []):
                if not isinstance(l, dict) or not isinstance(l.get("path"), str) or not l["path"]:
                    continue
                if os.path.exists(l["path"]):
                    self.add_to_library(l["path"], ask=False, known=l)
                else:                                # offline: kept, so saving never drops it
                    self.add_offline_lib(l)
            self.clips = clips
            for c in self.clips:
                if c.nat:
                    self.rerender_caption(c)
            self.W, self.H = int(d["W"]), int(d["H"])
            if d.get("substyle"):
                self.apply_sub_style(d["substyle"])
            else:                                    # a new project starts with the user's default subtitle look
                self.apply_sub_style(self.sub_factory())
                self.load_sub_defaults()
            for name, wh in CANVAS.items():
                if wh == (self.W, self.H):
                    self.cb_canvas.blockSignals(True)
                    self.cb_canvas.setCurrentText(name)
                    self.cb_canvas.blockSignals(False)
            self.build_canvas()
            self.sel = None
            self.undo.clear()
            self.redo.clear()
            self.t = 0.0
            self.after_edit()
        finally:
            self._opening = False
        self._extra = {k: v for k, v in d.items() if k not in PROJECT_KEYS}
        self._disk_sig = None if restore else store.file_signature(p)
        self._conflict, self.save_error = False, ""
        doc = self.state_doc()
        self._last_doc = doc
        self._saved_doc = None if (restore or rec) else doc
        self._gen += 1
        self._done_gen, self._rec_gen = self._gen, 0
        self.mark_clean("[]" if restore else (saved_snap if rec else None))
        if restore or rec:
            self._saved_meta = None if restore else self._saved_meta
        self._last_thumb = 0.0
        if rec is not None:
            self.undo = [saved_snap]                  # Ctrl+Z goes back to the last saved version
            info = rec[0]
            self.write_recovery(force=True)           # a fresh snapshot of the restored state first ...
            if info.kept:                             # ... then the kept one can go
                self.writer.submit(("kept", info.path), lambda: store.discard_recovery(p, info))
            if self.auto_timer.isActive():
                self.save_timer.start()
            self.set_save_state("pending")
        else:
            self.set_save_state("" if restore else "saved")
        if not restore:
            store.add_recent(p)
            self.track_project()
        self.show_editor()
        self.update_title()
        QTimer.singleShot(0, self.zoom_fit)
        for c in self.clips:
            if c.hasaudio and c.kind in ("audio", "video"):
                threading.Thread(target=compute_wave, args=(c.path,), daemon=True).start()
        self.status("Recovered changes restored. Ctrl+Z goes back to the saved version." if rec else
                    f"Opened “{self.project_name()}”.")
        self.warn_missing()
        return True

    def warn_missing(self):
        """Tell the user which media files are gone, and offer to find them."""
        gone = self.missing_paths()
        if not gone:
            return
        names = sorted({os.path.basename(p) for p in gone})
        m = QMessageBox(self)
        m.setIcon(QMessageBox.Warning)
        m.setWindowTitle("Some media is missing")
        m.setText("These files could not be found, so their clips will be blank:\n\n" + "\n".join(names[:12])
                  + ("\n\u2026" if len(names) > 12 else ""))
        m.setInformativeText("If you moved them, locate one and Parrot Studio looks for the others in the same folder.")
        loc = m.addButton("Locate files\u2026", QMessageBox.AcceptRole)
        m.addButton("Later", QMessageBox.RejectRole)
        m.setDefaultButton(loc)
        m.exec()
        if m.clickedButton() is loc:
            self.locate_file(gone[0])

    # ------------------------------------------------------------------ export
    def export(self):
        if not self.clips:
            QMessageBox.information(self, "Export", "The timeline is empty.")
            return
        gone = sorted({os.path.basename(c.path) for c in self.clips if not c.nat and c.kind != "sub"
                       and c.path and not os.path.exists(c.path)})
        if gone:
            QMessageBox.warning(self, "Can't export yet", "These files can no longer be found:\n\n" +
                                "\n".join(gone[:12]) + "\n\nPut them back (or delete those clips) and try again.")
            return
        first = next((c.path for c in sorted(self.clips, key=lambda c: c.start) if not c.nat), "")
        start_dir = getattr(self, "last_export_dir", "") or (os.path.dirname(first) if first else "")
        base = os.path.splitext(os.path.basename(self.proj_path))[0] if self.proj_path else "export"
        out, _ = QFileDialog.getSaveFileName(self, "Export video", os.path.join(start_dir, base + ".mp4"), "MP4 (*.mp4)")
        if not out:
            return
        if not out.lower().endswith(".mp4"):
            out += ".mp4"
        self.last_export_dir = os.path.dirname(out)
        self.export_out, self.export_t0 = out, time.time()
        if self.playing:
            self.toggle_play()
        subs = None
        caps = self.sub_caps()
        if caps:
            st = self.sub_style()
            with open(os.path.join(self.work, "subs.ass"), "w", encoding="utf-8") as f:
                f.write(build_sub_ass(caps, st, self.W, self.H))
            subs = "fonts" if prep_fonts(self.work, st["font"]) else "plain"
        cmd, T = build_export(self.clips, self.W, self.H, int(self.cb_fps.currentText()), out, self.ck_nv.isChecked(), subs)
        self.cancel_flag = False
        self.exp_T = T
        self.prog = QProgressDialog("Exporting...", "Cancel", 0, 1000, self)
        self.prog.setWindowTitle("Export")
        self.prog.setLabelText(f"Exporting {os.path.basename(out)}\u2026")
        self.prog.setMinimumWidth(420)
        self.prog.setWindowModality(Qt.WindowModal)
        self.prog.canceled.connect(lambda: setattr(self, "cancel_flag", True))
        self.prog.show()
        threading.Thread(target=self.run_export, args=(cmd, T), daemon=True).start()

    def run_export(self, cmd, T):
        tail = []
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                 creationflags=LOWPRI, cwd=self.work)
            self.exp_proc = p
            for line in p.stdout:
                line = line.strip()
                m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
                if m:
                    BUS.prog.emit(min(1.0, int(m[1]) / 1e6 / T))
                elif "=" not in line and line:
                    tail.append(line)
                if self.cancel_flag:
                    p.kill()
                    BUS.done.emit("cancelled")
                    return
            p.wait()
            BUS.done.emit("" if p.returncode == 0 else "\n".join(tail[-12:]) or f"ffmpeg exit {p.returncode}")
        except Exception as e:
            BUS.done.emit(str(e))

    def on_prog(self, f):
        if self.prog:
            self.prog.setValue(int(f * 1000))
            el = time.time() - getattr(self, "export_t0", time.time())
            left = ""
            if 0.02 < f < 1 and el > 1.5:
                r = int(el / f - el)
                left = f"  \u00b7  about {r // 60}:{r % 60:02d} left"
            self.prog.setLabelText(f"Exporting {os.path.basename(getattr(self, 'export_out', ''))}\n"
                                   f"{int(f * 100)}%{left}")

    def on_export_done(self, err):
        if self.prog:
            self.prog.close()
            self.prog = None
        if err == "cancelled":
            return
        if err:
            m = QMessageBox(self)
            m.setIcon(QMessageBox.Critical)
            m.setWindowTitle("Export failed")
            m.setText("The export didn't finish.")
            m.setInformativeText("ffmpeg reported an error. The details are below; you can copy them.")
            m.setDetailedText(err)
            m.setDefaultButton(m.addButton("Close", QMessageBox.RejectRole))
            m.exec()
        else:
            secs = int(time.time() - getattr(self, "export_t0", time.time()))
            how = "NVENC (graphics card)" if self.ck_nv.isChecked() else "CPU"
            out = getattr(self, "export_out", "")
            m = QMessageBox(self)
            m.setIcon(QMessageBox.Information)
            m.setWindowTitle("Export finished")
            m.setText("Your video is ready.")
            m.setInformativeText(f"{os.path.basename(out)}  \u00b7  done in {secs // 60}m {secs % 60:02d}s using {how}")
            m.setToolTip(out)
            op = m.addButton("Show in folder", QMessageBox.AcceptRole)
            play = m.addButton("Play", QMessageBox.ActionRole)
            m.addButton("Close", QMessageBox.RejectRole)
            m.setDefaultButton(op)
            m.exec()
            if m.clickedButton() is play and out:
                QDesktopServices.openUrl(QUrl.fromLocalFile(out))
            if m.clickedButton() is op:
                out = getattr(self, "export_out", "")
                try:
                    if sys.platform.startswith("win"):
                        subprocess.Popen(["explorer", "/select,", os.path.normpath(out)])
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", "-R", out])
                    else:
                        subprocess.Popen(["xdg-open", os.path.dirname(out)])
                except Exception:
                    pass


# ----------------------------------------------------------------------------- app


UI_DIR = os.path.join(store.cache_dir(), "ui")


def make_ui_assets():
    """Arrow / check images for the stylesheet, drawn in code (QSS needs image files)."""
    d = UI_DIR
    try:
        os.makedirs(d, exist_ok=True)
        if not os.access(d, os.W_OK):
            raise OSError
    except Exception:
        d = tempfile.mkdtemp(prefix="clipstudio_ui_")
    P = QPointF

    def save(name, color, pts, width=1.5, s=36):
        pm = QPixmap(s, s)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(s / 12.0, s / 12.0)                       # draw on a 12x12 grid
        p.setPen(QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline(QPolygonF([P(*xy) for xy in pts]))
        p.end()
        pm.save(os.path.join(d, name + ".png"))

    shapes = {"chev_down": [(3, 4.5), (6, 7.5), (9, 4.5)], "chev_up": [(3, 7.5), (6, 4.5), (9, 7.5)],
              "chev_right": [(4.5, 3), (7.5, 6), (4.5, 9)], "chev_left": [(7.5, 3), (4.5, 6), (7.5, 9)]}
    for nm, pts in shapes.items():
        save(nm, "#A6A8B0", pts)
        save(nm + "_dis", "#4F5158", pts)
    chk = [(2.5, 6.2), (5, 8.6), (9.5, 3.6)]
    save("check", "#FFFFFF", chk, 1.8)
    save("check_dis", "#6F7D96", chk, 1.8)
    save("check_menu", "#E8E9EC", chk, 1.6)
    return d.replace("\\", "/")


# The whole look of the app. A plain string (not an f-string) because of the braces; @UI@ is replaced with the
# folder holding the arrow / check images made by make_ui_assets().
QSS = """
* { outline: 0; }
QMainWindow, QDialog { background: #1B1C1F; }
QToolTip { background: #2A2B30; color: #E8E9EC; border: 1px solid #3A3B42; padding: 4px 8px; font-size: 11px; }

/* ---------- named labels / containers ---------- */
QWidget#header { background: #1B1C1F; border-bottom: 1px solid #26272C; }
QWidget#transport { background: #1B1C1F; border-top: 1px solid #26272C; }
QWidget#toolbar { background: #1B1C1F; border-bottom: 1px solid #26272C; }
QLabel#title { font-size: 14px; font-weight: 600; color: #E8E9EC; padding: 0; }
QPushButton#projectName { background: transparent; border: 1px solid transparent; border-radius: 4px;
              padding: 4px 8px; color: #E8E9EC; font-size: 13px; font-weight: 600; text-align: left; }
QPushButton#projectName:hover { background: #2A2B30; }
QPushButton#projectName:pressed, QPushButton#projectName[open="true"] { background: #232428; border-color: #34353B; }
QLineEdit#projectNameEdit { font-size: 13px; font-weight: 600; border-color: #7FE5FF; background: #141518; }
QPushButton#saveState { background: transparent; border: 1px solid transparent; color: #767880; font-size: 11px;
              padding: 4px 2px; text-align: left; min-height: 18px; max-height: 18px; }
QPushButton#saveState[state="saving"] { color: #A6A8B0; }
QPushButton#saveState[state="failed"] { color: #F07A7E; }
QPushButton#saveState[state="conflict"] { color: #E2A33B; }
QPushButton#saveState[state="untitled"]:hover, QPushButton#saveState[state="failed"]:hover,
QPushButton#saveState[state="conflict"]:hover { text-decoration: underline; }
QPushButton#link { background: transparent; border: none; color: #7FE5FF; padding: 0 4px; min-height: 0; }
QPushButton#link:hover { color: #B5F0FF; text-decoration: underline; }

/* ---------- project dashboard ---------- */
QWidget#dashboard { background: #141518; }
QWidget#dashboard QScrollArea, QWidget#dashboard QScrollArea > QWidget > QWidget { background: transparent; }
QLabel#pageTitle { font-size: 20px; font-weight: 600; color: #E8E9EC; }
QLabel#projPath { color: #A6A8B0; }
QLabel#emptyTitle { font-size: 16px; font-weight: 600; color: #E8E9EC; }
QLabel#emptyBody { color: #A6A8B0; max-width: 360px; }
QPushButton#primaryLarge { background: #3A78E0; border: 1px solid #3A78E0; color: #FFFFFF; font-weight: 600;
              padding: 6px 20px; min-height: 18px; }
QPushButton#primaryLarge:hover { background: #4886EC; border-color: #4886EC; }
QPushButton#primaryLarge:pressed { background: #2F67C4; border-color: #2F67C4; }
QToolButton#cardMenu { border-radius: 4px; padding: 4px; }
QToolButton#cardMenu:hover { background: #2A2B30; }
QToolButton#cardMenu::menu-indicator { image: none; width: 0; }
QLineEdit#cardNameEdit { font-size: 13px; font-weight: 600; border-color: #7FE5FF; background: #141518;
              padding: 2px 6px; min-height: 0; max-height: 18px; }
QFrame#recoveryBanner { background: #2A2418; border: 1px solid #5A4A2A; border-radius: 8px; }
QLabel#bannerText { color: #E8E9EC; }
QLabel#time { font-family: "Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", monospace;
              font-size: 13px; color: #E8E9EC; }
QLabel#hint { color: #767880; font-size: 11px; }
QLabel#field { color: #A6A8B0; }
QLabel#eyebrow { color: #767880; font-size: 11px; }
QLabel#emptyHead { color: #A6A8B0; font-size: 13px; font-weight: 600; }
QLabel#panelTitle { color: #E8E9EC; font-size: 13px; font-weight: 600; }
QFrame#vsep { color: #2E2F35; }

/* ---------- buttons ---------- */
QPushButton { background: #2A2B30; color: #E8E9EC; border: 1px solid #34353B; border-radius: 4px;
              padding: 4px 12px; min-height: 18px; }
QPushButton:hover { background: #33343A; border-color: #45464E; }
QPushButton:pressed { background: #232428; border-color: #45464E; }
QPushButton:checked { background: #1F3253; border-color: #3A78E0; }
QPushButton:disabled { background: #222327; border-color: #2A2B30; color: #4F5158; }

QPushButton#primary { background: #3A78E0; border: 1px solid #3A78E0; color: #FFFFFF; font-weight: 600; padding: 4px 16px; }
QPushButton#primary:hover { background: #4886EC; border-color: #4886EC; }
QPushButton#primary:pressed { background: #2F67C4; border-color: #2F67C4; }
QPushButton#primary:disabled { background: #26334A; border-color: #26334A; color: #6F7D96; }

QPushButton#danger, QMessageBox QPushButton#danger { background: transparent; border: 1px solid #5A2A2D; color: #F07A7E; }
QPushButton#danger:hover { background: #3A1D20; border-color: #E5484D; color: #F59EA1; }
QPushButton#danger:pressed { background: #4A2226; }

QPushButton#ghost, QPushButton#tool { background: transparent; border: 1px solid transparent; color: #C9CBD1;
              padding: 4px 8px; }
QPushButton#ghost { padding: 4px 10px; }
QPushButton#ghost:hover, QPushButton#tool:hover { background: #2A2B30; color: #E8E9EC; border-color: transparent; }
QPushButton#ghost:pressed, QPushButton#tool:pressed { background: #232428; }
QPushButton#ghost:disabled, QPushButton#tool:disabled { background: transparent; color: #4F5158; }

QPushButton#round { background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 0; min-height: 0; }
QPushButton#round:hover { background: #2A2B30; }
QPushButton#round:pressed { background: #232428; }

QPushButton#play { background: #7FE5FF; border: none; border-radius: 18px; padding: 0; min-height: 0; }
QPushButton#play:hover { background: #A6EEFF; }
QPushButton#play:pressed { background: #5FCFEA; }

QPushButton#emoji { background: transparent; border: none; border-radius: 4px; padding: 0;
              min-height: 28px; max-height: 28px; min-width: 28px; max-width: 28px;
              font-size: 16px; font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"; }
QPushButton#emoji:hover { background: #2A2B30; }
QPushButton#emoji:pressed { background: #33343A; }

QPushButton#swatch { text-align: left; padding: 4px 8px; color: #C9CBD1; font-size: 11px;
              font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace; }

QToolButton { background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 2px; color: #C9CBD1; }
QToolButton:hover { background: #2A2B30; }
QToolButton:pressed { background: #232428; }

/* ---------- dialogs ---------- */
QMessageBox QLabel#qt_msgbox_label { min-width: 320px; color: #E8E9EC; }
QMessageBox QLabel#qt_msgbox_informativelabel { color: #A6A8B0; }
QMessageBox QPushButton, QInputDialog QPushButton, QProgressDialog QPushButton { min-width: 72px; }
QMessageBox QPushButton:default, QInputDialog QPushButton:default, QColorDialog QPushButton:default {
              background: #3A78E0; border-color: #3A78E0; color: #FFFFFF; font-weight: 600; }
QMessageBox QPushButton:default:hover, QInputDialog QPushButton:default:hover, QColorDialog QPushButton:default:hover { background: #4886EC; border-color: #4886EC; }

QInputDialog { min-width: 320px; }
QInputDialog QLabel { margin-bottom: 4px; }

/* ---------- text inputs ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox {
              background: #141518; color: #E8E9EC; border: 1px solid #34353B; border-radius: 4px;
              selection-background-color: #2F67C4; selection-color: #FFFFFF; }
QLineEdit, QAbstractSpinBox { padding: 4px 6px; min-height: 18px; }
QPlainTextEdit, QTextEdit { padding: 4px; }
QLineEdit:hover, QPlainTextEdit:hover, QAbstractSpinBox:hover, QComboBox:hover { border-color: #45464E; }
QLineEdit:focus, QPlainTextEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QComboBox:on { border-color: #7FE5FF; }
QLineEdit:disabled, QPlainTextEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {
              background: #18191C; color: #4F5158; border-color: #26272C; }

/* spin boxes */
QAbstractSpinBox { padding-right: 20px; min-height: 18px; max-height: 18px; }
QLineEdit { max-height: 18px; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
              subcontrol-origin: border; width: 18px; border: none; border-left: 1px solid #2A2B30; background: transparent; }
QAbstractSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: 4px; }
QAbstractSpinBox::down-button { subcontrol-position: bottom right; border-bottom-right-radius: 4px; }
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background: #2A2B30; }
QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed { background: #33343A; }
QAbstractSpinBox::up-arrow { image: url("@UI@/chev_up.png"); width: 8px; height: 8px; }
QAbstractSpinBox::down-arrow { image: url("@UI@/chev_down.png"); width: 8px; height: 8px; }
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off { image: url("@UI@/chev_up_dis.png"); }
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off { image: url("@UI@/chev_down_dis.png"); }

/* combo boxes (QFontComboBox inherits) */
QComboBox { padding: 4px 24px 4px 8px; min-height: 18px; combobox-popup: 0; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 20px; border: none; }
QComboBox::down-arrow { image: url("@UI@/chev_down.png"); width: 10px; height: 10px; }
QComboBox::down-arrow:disabled { image: url("@UI@/chev_down_dis.png"); }
QComboBox QAbstractItemView { background: #222327; color: #E8E9EC; border: 1px solid #34353B; padding: 4px;
              outline: 0; selection-background-color: #3A78E0; selection-color: #FFFFFF; }
QComboBox QAbstractItemView::item { min-height: 24px; padding: 0 8px; border-radius: 3px; }
QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected { background: #3A78E0; color: #FFFFFF; }

/* ---------- check boxes ---------- */
QCheckBox { spacing: 8px; color: #E8E9EC; background: transparent; }
QCheckBox:disabled { color: #4F5158; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #4A4B53; border-radius: 3px; background: #141518; }
QCheckBox::indicator:hover { border-color: #6A6C75; }
QCheckBox::indicator:checked { background: #3A78E0; border-color: #3A78E0; image: url("@UI@/check.png"); }
QCheckBox::indicator:checked:hover { background: #4886EC; border-color: #4886EC; }
QCheckBox::indicator:disabled { background: #18191C; border-color: #2A2B30; }
QCheckBox::indicator:checked:disabled { background: #2A3346; border-color: #2A3346; image: url("@UI@/check_dis.png"); }

/* ---------- sliders ---------- */
QSlider { background: transparent; min-height: 20px; }
QSlider::groove:horizontal { height: 4px; background: #34353B; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #7FE5FF; border-radius: 2px; }
QSlider::add-page:horizontal { background: #34353B; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; background: #E8E9EC; }
QSlider::handle:horizontal:hover { background: #FFFFFF; }
QSlider::handle:horizontal:pressed { background: #A6EEFF; }
QSlider::sub-page:horizontal:disabled { background: #2E4A52; }
QSlider::handle:horizontal:disabled { background: #4F5158; }

/* ---------- tabs: flat underline ---------- */
QTabWidget::pane { border: none; border-top: 1px solid #26272C; top: -1px; background: transparent; }
QTabWidget::tab-bar { left: 7px; }
QTabBar { qproperty-drawBase: 0; background: transparent; }
QTabBar::tab { background: transparent; color: #A6A8B0; border: none; border-bottom: 2px solid transparent;
              padding: 8px 5px 6px 5px; margin-right: 2px; }
QTabBar::tab:hover { color: #E8E9EC; }
QTabBar::tab:selected { color: #E8E9EC; border-bottom-color: #7FE5FF; }
QTabBar::tab:disabled { color: #4F5158; }
QTabBar QToolButton { background: #1B1C1F; border: none; border-radius: 0; width: 16px; }
QTabBar QToolButton::left-arrow { image: url("@UI@/chev_left.png"); width: 8px; height: 8px; }
QTabBar QToolButton::right-arrow { image: url("@UI@/chev_right.png"); width: 8px; height: 8px; }

/* ---------- lists ---------- */
QListWidget { background: #141518; border: 1px solid #26272C; border-radius: 6px; padding: 4px; outline: 0; color: #E8E9EC; }
QListWidget::item { border-radius: 4px; padding: 4px 6px; color: #E8E9EC; }
QListWidget::item:hover { background: #222327; }
QListWidget::item:selected { background: #1F3253; color: #E8E9EC; }
QListWidget::item:disabled { color: #4F5158; }
QListWidget#transitions::item { min-height: 22px; padding: 3px 10px; }
QListWidget#library::item { padding: 4px; margin: 2px; }

/* ---------- group boxes: section header with hairline, no box ---------- */
QGroupBox { border: none; border-top: 1px solid #26272C; margin-top: 20px; padding-top: 8px; background: transparent; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 0 4px 0; color: #E8E9EC;
              font-weight: 600; left: -2px; }

/* ---------- scroll areas & bars ---------- */
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:horizontal { height: 10px; background: transparent; margin: 0; border: none; }
QScrollBar:vertical { width: 10px; background: transparent; margin: 0; border: none; }
QScrollBar::handle:horizontal { background: #3A3B42; border-radius: 3px; min-width: 32px; margin: 2px; }
QScrollBar::handle:vertical { background: #3A3B42; border-radius: 3px; min-height: 32px; margin: 2px; }
QScrollBar::handle:hover { background: #4F5158; }
QScrollBar::handle:pressed { background: #62646C; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QScrollBar#timelineScroll:horizontal { height: 12px; background: #141518; border-top: 1px solid #26272C; }

/* ---------- menus ---------- */
QMenu { background: #222327; color: #E8E9EC; border: 1px solid #34353B; padding: 4px; }
QMenu::item { padding: 6px 32px 6px 28px; border-radius: 4px; background: transparent; }
QMenu::item:selected { background: #3A78E0; color: #FFFFFF; }
QMenu::item:disabled { color: #4F5158; background: transparent; }
QMenu::separator { height: 1px; background: #34353B; margin: 4px 8px; }
QMenu::indicator { width: 14px; height: 14px; padding-left: 8px; }
QMenu::indicator:checked { image: url("@UI@/check_menu.png"); }
QMenu::right-arrow { image: url("@UI@/chev_right.png"); width: 8px; height: 8px; margin-right: 8px; }

/* ---------- splitter ---------- */
QSplitter::handle { background: #111214; }
QSplitter::handle:horizontal { width: 4px; }
QSplitter::handle:vertical { height: 4px; }
QSplitter::handle:hover { background: #2A2B30; }
QSplitter::handle:pressed { background: #3A78E0; }

/* ---------- status bar ---------- */
QStatusBar { background: #1B1C1F; color: #A6A8B0; border-top: 1px solid #26272C; min-height: 24px; font-size: 11px; }
QStatusBar::item { border: none; }
QStatusBar QLabel { color: #767880; font-size: 11px; padding: 0 8px; }

/* ---------- progress ---------- */
QProgressBar { background: #141518; border: 1px solid #26272C; border-radius: 4px; text-align: center;
              color: #E8E9EC; min-height: 16px; max-height: 16px; font-size: 11px; }
QProgressBar::chunk { background: #7FE5FF; border-radius: 3px; }
QProgressBar#slim { min-height: 6px; max-height: 6px; border: none; background: #2A2B30; border-radius: 3px; }
QProgressBar#slim::chunk { border-radius: 3px; }
"""


def themed_icon(kind, size=32):
    """Quiet, stroke-only message-box glyphs in the theme colours (instead of Fusion's bright stock icons)."""
    col = QColor({"info": "#7FE5FF", "question": "#A6A8B0", "warning": "#E2A33B", "error": "#E5484D"}[kind])
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(col, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    s = size
    if kind == "warning":
        p.drawPolygon(QPolygonF([QPointF(s / 2, 3), QPointF(s - 2.5, s - 4), QPointF(2.5, s - 4)]))
        p.drawLine(QPointF(s / 2, s * 0.38), QPointF(s / 2, s * 0.6))
        p.drawPoint(QPointF(s / 2, s * 0.72))
    else:
        p.drawEllipse(QRectF(2, 2, s - 4, s - 4))
        f = QFont()
        f.setPixelSize(int(s * 0.55))
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.drawText(QRectF(0, 0, s, s), Qt.AlignCenter, {"info": "i", "question": "?", "error": "!"}[kind])
    p.end()
    return QIcon(pm)


class ThemedStyle(QProxyStyle):
    """Fusion, with message-box icons that match the dark theme."""
    ICONS = {QStyle.SP_MessageBoxInformation: "info", QStyle.SP_MessageBoxQuestion: "question",
             QStyle.SP_MessageBoxWarning: "warning", QStyle.SP_MessageBoxCritical: "error"}

    def standardIcon(self, sp, opt=None, widget=None):
        kind = self.ICONS.get(sp)
        if kind:
            return themed_icon(kind)
        return super().standardIcon(sp, opt, widget)


def dark_theme(app):
    global _STYLE
    from PySide6.QtWidgets import QStyleFactory
    _STYLE = ThemedStyle(QStyleFactory.create("Fusion"))      # kept referenced for the app's lifetime
    app.setStyle(_STYLE)
    f = QFont()
    f.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", "Noto Sans",
                   "DejaVu Sans"])
    f.setPixelSize(12)
    app.setFont(f)
    p = QPalette()
    c = QColor
    for role, hx in [(QPalette.Window, "#1B1C1F"), (QPalette.WindowText, "#E8E9EC"), (QPalette.Base, "#141518"),
                     (QPalette.AlternateBase, "#1F2024"), (QPalette.Text, "#E8E9EC"), (QPalette.Button, "#2A2B30"),
                     (QPalette.ButtonText, "#E8E9EC"), (QPalette.BrightText, "#FFFFFF"),
                     (QPalette.ToolTipBase, "#2A2B30"), (QPalette.ToolTipText, "#E8E9EC"),
                     (QPalette.Highlight, "#3A78E0"), (QPalette.HighlightedText, "#FFFFFF"),
                     (QPalette.Link, "#7EAAF6"), (QPalette.PlaceholderText, "#6B6D76"),
                     (QPalette.Light, "#3A3B42"), (QPalette.Midlight, "#2E2F35"), (QPalette.Mid, "#26272C"),
                     (QPalette.Dark, "#111214"), (QPalette.Shadow, "#000000")]:
        p.setColor(role, c(hx))
    for role, hx in [(QPalette.WindowText, "#4F5158"), (QPalette.Text, "#4F5158"), (QPalette.ButtonText, "#4F5158"),
                     (QPalette.PlaceholderText, "#3F4147"), (QPalette.Base, "#18191C"), (QPalette.Button, "#222327"),
                     (QPalette.Highlight, "#2A3346")]:
        p.setColor(QPalette.Disabled, role, c(hx))
    app.setPalette(p)
    app.setStyleSheet(QSS.replace("@UI@", make_ui_assets()))


def main():
    if not FFMPEG:
        print("ffmpeg not found. Install ffmpeg or: pip install imageio-ffmpeg")
        sys.exit(1)
    app = QApplication(sys.argv)

    app.setApplicationName(APP_TITLE)

    def _log_err(et, ev, tb):  # errors inside Qt callbacks are otherwise invisible on Windows
        store.log_error("Unhandled error", ev.with_traceback(tb) if ev is not None else et())
        sys.__excepthook__(et, ev, tb)
    sys.excepthook = _log_err
    dark_theme(app)
    w = MainWindow()
    w.track_session = True
    w.unclean_sessions = store.acquire_session_lock()     # earlier sessions that crashed
    app.aboutToQuit.connect(store.release_session_lock)
    w.show_dashboard()
    w.show()
    QTimer.singleShot(400, w.offer_restore)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
