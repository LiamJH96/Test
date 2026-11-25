"""Parrot Studio project storage: folders, files, recents, settings and crash recovery.

Pure Python (no Qt), so it can be used and tested without a QApplication.

On-disk layout of a project made by Parrot Studio::

    <projects dir>/<Name>/<Name>.clipproj      the project (JSON)
                         <Name>.clipproj.bak  the previous good version, kept on every save
                         thumbnail.jpg        picture for the project dashboard
                         recovery/            crash-recovery snapshots
                         cache/               throw-away data

Imported media is never copied: a project refers to media files at their original paths.
A .clipproj opened from anywhere else keeps working where it is; its thumbnail and recovery data then live in the
config folder instead, so nothing is written next to the user's own files except the .bak backup.
"""
import datetime
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Optional

APP_NAME = "Parrot Studio"
PROJECT_EXT = ".clipproj"
SCHEMA_VERSION = 2              # files without a "schema" key are version 1 (Clip Studio)
MAX_RECENT = 30
RECOVERY_FILE = "recovery" + PROJECT_EXT

_IS_WIN = sys.platform.startswith("win")
_lock = threading.RLock()       # guards settings.json read-modify-write


class ProjectError(Exception):
    """A project (or one of its helper files) could not be read or written. The message is user-readable."""

    def __init__(self, message, path=None, backup=None):
        super().__init__(message)
        self.path = path
        self.backup = backup        # a readable .bak of the same project, when one exists


class ConflictError(ProjectError):
    """The file on disk was changed by something else since we last loaded or saved it."""


# ----------------------------------------------------------------------------- folders & settings
def config_dir():
    env = os.environ.get("PARROT_STUDIO_CONFIG_DIR")
    if env:
        return os.path.abspath(env)
    if _IS_WIN:
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, APP_NAME)
    return os.path.join(os.path.expanduser("~"), ".config", "parrot_studio")


def cache_dir():
    """Regenerable caches (emoji pictures, rendered captions, UI images): local, never roamed or backed up.
    Windows: %LOCALAPPDATA%\\Parrot Studio\\cache; elsewhere $XDG_CACHE_HOME/parrot_studio (~/.cache/parrot_studio).
    PARROT_STUDIO_CACHE_DIR overrides it; with only PARROT_STUDIO_CONFIG_DIR set (tests) it is <config>/cache."""
    env = os.environ.get("PARROT_STUDIO_CACHE_DIR")
    if env:
        return os.path.abspath(env)
    if os.environ.get("PARROT_STUDIO_CONFIG_DIR"):
        return os.path.join(config_dir(), "cache")
    if _IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, APP_NAME, "cache")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "parrot_studio")


def legacy_dir():
    """Where Clip Studio kept its settings and caches."""
    return os.path.join(os.path.expanduser("~"), ".clip_studio")


def legacy_autosave_path():
    return os.path.join(legacy_dir(), "autosave" + PROJECT_EXT)


def default_projects_dir():
    env = os.environ.get("PARROT_STUDIO_PROJECTS_DIR")
    if env:
        return os.path.abspath(env)
    home = os.environ.get("USERPROFILE") if _IS_WIN else None
    return os.path.join(home or os.path.expanduser("~"), "Videos", APP_NAME, "Projects")


def settings_path():
    return os.path.join(config_dir(), "settings.json")


def error_log_path():
    return os.path.join(config_dir(), "error.log")


def log_error(message, exc=None):
    """Append to the error log. Never raises (logging must not turn a problem into a crash)."""
    try:
        os.makedirs(config_dir(), exist_ok=True)
        text = time.strftime("%Y-%m-%d %H:%M:%S ") + message
        if exc is not None:
            text += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with open(error_log_path(), "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except Exception:
        pass


_problems = []


def report_problem(message, exc=None):
    """Log a problem that was handled (e.g. a damaged recovery file that was skipped) and keep it for the UI."""
    log_error(message, exc)
    with _lock:
        _problems.append(message)


def take_problems():
    """Problems reported since the last call (for the UI to show), oldest first."""
    with _lock:
        out = list(_problems)
        _problems.clear()
    return out


def load_settings():
    p = settings_path()
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        report_problem(f"Settings file {p} could not be read; using defaults.", e)
        return {}


def save_settings(d):
    atomic_write_json(settings_path(), d)


def update_settings(fn):
    """Read-modify-write settings.json under a lock. fn(dict) changes the dict in place."""
    with _lock:
        d = load_settings()
        fn(d)
        save_settings(d)
        return d


def get_projects_dir():
    d = load_settings().get("projects_dir")
    return d if isinstance(d, str) and d else default_projects_dir()


def set_projects_dir(path):
    path = os.path.abspath(os.path.expanduser(path))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise ProjectError(f"The folder \"{path}\" could not be created:\n{e.strerror or e}", path) from e
    if not os.access(path, os.W_OK):
        raise ProjectError(f"Parrot Studio can't save into \"{path}\" (no permission to write there).", path)
    update_settings(lambda d: d.__setitem__("projects_dir", path))
    return path


def migrate_legacy_config(names=("sub_defaults.json",)):
    """Copy Clip Studio settings files into the new config folder, once. Returns the names copied."""
    copied = []
    try:
        st = load_settings()
        if st.get("legacy_config_migrated"):
            return copied
        for n in names:
            src, dst = os.path.join(legacy_dir(), n), os.path.join(config_dir(), n)
            if os.path.isfile(src) and not os.path.exists(dst):
                os.makedirs(config_dir(), exist_ok=True)
                shutil.copy2(src, dst)
                copied.append(n)
        update_settings(lambda d: d.__setitem__("legacy_config_migrated", True))
    except Exception as e:
        report_problem("Old Clip Studio settings could not be copied to the new settings folder.", e)
    return copied


# ----------------------------------------------------------------------------- safe writing
def _fsync_dir(d):
    if _IS_WIN:
        return
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _replace(src, dst):
    # On Windows a virus scanner or indexer can hold the target for a moment: retry briefly.
    for i in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not _IS_WIN or i == 9:
                raise
            time.sleep(0.05 * (i + 1))


def atomic_write_text(path, text):
    """Write text to path so that path is always either the old complete file or the new complete file."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
        _fsync_dir(d)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False))


def file_signature(path):
    """(mtime_ns, size) of a file, or None if it does not exist. Used to notice changes made by others."""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# ----------------------------------------------------------------------------- names
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME = 80


def safe_name(name):
    """A folder/file name that is valid on Windows, macOS and Linux, as close to name as possible."""
    s = str(name or "").replace(": ", " - ").replace(":", "-")
    s = _ILLEGAL.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ").lstrip()
    s = s[:MAX_NAME].rstrip(". ")
    if not s or set(s) <= {"_", "."}:
        s = "Untitled project"
    if s.split(".")[0].upper().rstrip() in _RESERVED:
        s = "_" + s
    return s


def unique_name(parent, name, ext=""):
    """name, or "name (2)", "name (3)"... so that parent/<result><ext> does not exist (case-insensitively)."""
    try:
        taken = {n.lower() for n in os.listdir(parent)}
    except OSError:
        taken = set()
    cand, i = name, 2
    while (cand + ext).lower() in taken:
        suffix = f" ({i})"
        cand = name[:MAX_NAME - len(suffix)].rstrip(". ") + suffix
        i += 1
    return cand


def project_name(path):
    return os.path.splitext(os.path.basename(path))[0]


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def is_managed(path):
    """True for a project in the Parrot Studio folder layout (<Name>/<Name>.clipproj with recovery/)."""
    folder = os.path.dirname(os.path.abspath(path))
    if os.path.basename(folder).lower() != project_name(path).lower():
        return False
    parent = os.path.dirname(folder)
    return _norm(parent) == _norm(get_projects_dir()) or os.path.isdir(os.path.join(folder, "recovery"))


def _ext_cache_dir(kind, path):
    key = hashlib.sha1(_norm(path).encode("utf-8")).hexdigest()[:16]
    return os.path.join(config_dir(), kind, key)


def thumbnail_path(path):
    if is_managed(path):
        return os.path.join(os.path.dirname(os.path.abspath(path)), "thumbnail.jpg")
    return _ext_cache_dir("thumbnails", path) + ".jpg"


def recovery_dir(path):
    """Folder holding recovery snapshots for a project (path None = the untitled, never-saved project)."""
    if not path:
        return os.path.join(config_dir(), "recovery", "untitled")
    if is_managed(path):
        return os.path.join(os.path.dirname(os.path.abspath(path)), "recovery")
    return _ext_cache_dir("recovery", path)


# ----------------------------------------------------------------------------- documents
def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def new_project_data(name=""):
    return {"schema": SCHEMA_VERSION, "meta": {"name": name, "id": uuid.uuid4().hex, "created": _now_iso(),
                                              "modified": _now_iso(), "app": APP_NAME},
            "W": 1080, "H": 1920, "lib": [], "clips": []}


def migrate(d):
    """Bring a project dict up to SCHEMA_VERSION. Returns a new dict; unknown keys are always kept."""
    if not isinstance(d, dict):
        raise ProjectError("This isn't a Parrot Studio project (the file holds no project data).")
    v = d.get("schema", 1)
    if not isinstance(v, int) or v < 1:
        raise ProjectError(f"The project has an unknown format version ({v!r}).")
    if v > SCHEMA_VERSION:
        raise ProjectError("This project was made with a newer version of Parrot Studio. "
                           "Update Parrot Studio to open it.")
    d = dict(d)
    if v < 2:       # Clip Studio file: same content, plus a version number and metadata
        meta = dict(d.get("meta") or {})
        meta.setdefault("name", "")
        meta.setdefault("id", uuid.uuid4().hex)
        meta.setdefault("created", "")
        meta.setdefault("modified", "")
        meta.setdefault("app", APP_NAME)
        meta["migrated_from"] = v
        d["meta"] = meta
        d["schema"] = 2
    return d


def validate(d):
    """Raise ProjectError unless d has the structure the editor needs."""
    if not isinstance(d.get("clips"), list) or not all(isinstance(c, dict) for c in d["clips"]):
        raise ProjectError("The project's clip list is missing or damaged.")
    if not isinstance(d.get("lib", []), list):
        raise ProjectError("The project's media library list is damaged.")
    for k in ("W", "H"):
        if not isinstance(d.get(k), (int, float)) or isinstance(d[k], bool) or d[k] <= 0:
            raise ProjectError("The project's video size is missing or damaged.")
    for i, c in enumerate(d["clips"], 1):
        problem = _clip_problem(c)
        if problem:
            raise ProjectError(f"Clip {i} of the project is damaged ({problem}).")
    return d


_CLIP_NUMBERS = ("start", "inp", "dur", "srcdur", "w", "h", "fin", "fout", "vol", "scale", "x", "y", "tdur", "csize",
                 "wdur", "cbold", "nat")
_CLIP_TEXTS = ("id", "name", "path", "kind", "ttype", "text", "cfont", "ccol", "cbox")


def _clip_problem(c):
    """Why a stored clip can't be used, or "" if it is fine. Only the keys present are checked (older files have
    fewer); unknown keys are allowed."""
    import math
    for k in _CLIP_NUMBERS:
        if k in c:
            v = c[k]
            if isinstance(v, bool) and k != "nat" or not isinstance(v, (int, float)) or not math.isfinite(v):
                return f"\u201c{k}\u201d should be a number"
    for k in ("start", "inp", "fin", "fout"):
        if k in c and c[k] < 0:
            return f"\u201c{k}\u201d is negative"
    if "dur" in c and c["dur"] <= 0:
        return "its length is zero"
    if "track" in c and (not isinstance(c["track"], int) or isinstance(c["track"], bool) or not 0 <= c["track"] <= 5):
        return "it is on a track that doesn't exist"
    for k in _CLIP_TEXTS:
        if k in c and not isinstance(c[k], str):
            return f"\u201c{k}\u201d should be text"
    if c.get("kind", "video") not in ("video", "image", "audio", "sub"):
        return "unknown clip type"
    if "words" in c and not isinstance(c["words"], list):
        return "its subtitle words are damaged"
    return ""


def _read_document(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError as e:
        raise ProjectError(f"The project file was not found:\n{path}", path) from e
    except OSError as e:
        raise ProjectError(f"The project file could not be read:\n{e.strerror or e}", path) from e
    if not raw.strip():
        raise ProjectError("The project file is empty (it may have been cut off while saving).", path)
    try:
        d = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as e:
        raise ProjectError("The project file is damaged (it isn't text).", path) from e
    except json.JSONDecodeError as e:
        raise ProjectError(f"The project file is damaged (line {e.lineno}, column {e.colno}: {e.msg}).", path) from e
    try:
        return validate(migrate(d))
    except ProjectError as e:
        e.path = path
        raise


def load_project_file(path):
    """Read, check and migrate a project. Raises ProjectError (with .backup set when a good .bak exists)."""
    try:
        return _read_document(path)
    except ProjectError as e:
        bak = path + ".bak"
        if os.path.isfile(bak):
            try:
                _read_document(bak)
                e.backup = bak
            except ProjectError:
                pass
        log_error(f"Could not open project {path}: {e}")
        raise


def prepare_document(data, name=None):
    """A copy of data stamped with the schema version and up-to-date metadata, ready to be written."""
    d = dict(data)
    meta = dict(d.get("meta") or {})
    meta.setdefault("id", uuid.uuid4().hex)
    meta.setdefault("created", _now_iso())
    if name is not None:
        meta["name"] = name
    meta["modified"] = _now_iso()
    meta["app"] = APP_NAME
    d["meta"] = meta
    d["schema"] = SCHEMA_VERSION
    return d


def _keep_backup(path):
    """Copy the current file to <path>.bak, but only if it is a readable project (never back up garbage)."""
    if not os.path.isfile(path):
        return
    try:
        _read_document(path)
    except ProjectError:
        return
    tmp = path + ".bak.tmp"
    shutil.copy2(path, tmp)
    _replace(tmp, path + ".bak")


def save_project_text(path, text, expect=None):
    """Write an already-serialised project. expect: the file_signature() we last saw; if the file has changed
    since, ConflictError is raised and nothing is written. Returns the new signature."""
    if expect is not None:
        cur = file_signature(path)
        if cur is not None and cur != tuple(expect):
            raise ConflictError("The project file was changed by another program since it was opened here.", path)
    try:
        _keep_backup(path)
        atomic_write_text(path, text)
    except OSError as e:
        log_error(f"Could not save project {path}", e)
        raise ProjectError(f"The project could not be saved:\n{e.strerror or e}", path) from e
    return file_signature(path)


def save_project_file(path, data, expect=None):
    return save_project_text(path, json.dumps(prepare_document(data, project_name(path)), ensure_ascii=False), expect)


# ----------------------------------------------------------------------------- projects
@dataclass
class ProjectInfo:
    name: str
    path: str
    folder: str
    modified: float                     # seconds since the epoch (0 if missing)
    thumbnail: Optional[str] = None
    missing: bool = False               # in the recent list but the file is gone
    managed: bool = True                # lives in the Parrot Studio folder layout
    has_recovery: bool = False          # unsaved changes were recovered and can be restored
    extra: dict = field(default_factory=dict)


def create_project(name, parent=None, data=None):
    """Make <parent>/<safe name>/<safe name>.clipproj (unique) and return its path."""
    parent = os.path.abspath(parent or get_projects_dir())
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        raise ProjectError(f"The projects folder could not be created:\n{parent}\n{e.strerror or e}", parent) from e
    nm = unique_name(parent, safe_name(name))
    folder = os.path.join(parent, nm)
    path = os.path.join(folder, nm + PROJECT_EXT)
    try:
        os.makedirs(os.path.join(folder, "recovery"))
        os.makedirs(os.path.join(folder, "cache"))
        doc = dict(data) if data else new_project_data(nm)
        atomic_write_json(path, prepare_document(doc, nm))
    except OSError as e:
        raise ProjectError(f"The project could not be created in {parent}:\n{e.strerror or e}", path) from e
    add_recent(path)
    return path


def _recent_list():
    r = load_settings().get("recent", [])
    return [p for p in r if isinstance(p, str)] if isinstance(r, list) else []


def add_recent(path):
    path = os.path.abspath(path)

    def f(d):
        r = [p for p in d.get("recent", []) if isinstance(p, str) and _norm(p) != _norm(path)]
        d["recent"] = [path] + r[:MAX_RECENT - 1]
    update_settings(f)


def remove_recent(path):
    update_settings(lambda d: d.__setitem__(
        "recent", [p for p in d.get("recent", []) if isinstance(p, str) and _norm(p) != _norm(path)]))


def _find_in_folder(folder):
    own = os.path.join(folder, os.path.basename(folder) + PROJECT_EXT)
    if os.path.isfile(own):
        return own
    try:
        files = sorted(f for f in os.listdir(folder) if f.lower().endswith(PROJECT_EXT))
    except OSError:
        return None
    return os.path.join(folder, files[0]) if len(files) == 1 else None


def _info(path, missing=False):
    path = os.path.abspath(path)
    managed = not missing and is_managed(path)
    th = thumbnail_path(path) if not missing else None
    try:
        mod = os.path.getmtime(path)
    except OSError:
        mod, missing = 0.0, True
    return ProjectInfo(name=project_name(path), path=path, folder=os.path.dirname(path), modified=mod,
                       thumbnail=th if th and os.path.isfile(th) else None, missing=missing, managed=managed,
                       has_recovery=(not missing) and has_recovery(path))


def list_projects():
    """Projects in the projects folder plus recently opened ones elsewhere, newest first; missing ones last."""
    seen, out = set(), []
    root = get_projects_dir()
    try:
        entries = os.listdir(root)
    except OSError:
        entries = []
    for e in entries:
        folder = os.path.join(root, e)
        if e.startswith(".") or not os.path.isdir(folder):
            continue
        p = _find_in_folder(folder)
        if p and _norm(p) not in seen:
            seen.add(_norm(p))
            out.append(_info(p))
    for p in _recent_list():
        if _norm(p) in seen:
            continue
        seen.add(_norm(p))
        out.append(_info(p, missing=not os.path.isfile(p)))
    out.sort(key=lambda i: (i.missing, -i.modified))
    return out


def rename_project(path, new_name):
    """Rename a project (its folder too, for managed projects). Returns the new .clipproj path."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ProjectError("The project file can't be found, so it can't be renamed.", path)
    nm = safe_name(new_name)
    old_rec, old_th = recovery_dir(path), thumbnail_path(path)
    managed = is_managed(path)
    try:
        if managed:
            folder = os.path.dirname(path)
            parent = os.path.dirname(folder)
            if nm.lower() != os.path.basename(folder).lower():
                nm = unique_name(parent, nm)
            new_folder = os.path.join(parent, nm)
            if new_folder != folder:
                os.rename(folder, new_folder)
            old_inside = os.path.join(new_folder, os.path.basename(path))
            new_path = os.path.join(new_folder, nm + PROJECT_EXT)
            if old_inside != new_path:
                os.rename(old_inside, new_path)
                if os.path.isfile(old_inside + ".bak"):
                    os.replace(old_inside + ".bak", new_path + ".bak")
        else:
            d = os.path.dirname(path)
            if nm.lower() != project_name(path).lower():
                nm = unique_name(d, nm, PROJECT_EXT)
            new_path = os.path.join(d, nm + PROJECT_EXT)
            if new_path != path:
                os.rename(path, new_path)
                if os.path.isfile(path + ".bak"):
                    os.replace(path + ".bak", new_path + ".bak")
            for old, new in ((old_rec, recovery_dir(new_path)), (old_th, thumbnail_path(new_path))):
                if os.path.exists(old) and old != new:
                    os.makedirs(os.path.dirname(new), exist_ok=True)
                    os.replace(old, new)
    except OSError as e:
        log_error(f"Could not rename project {path}", e)
        raise ProjectError(f"The project could not be renamed:\n{e.strerror or e}\n\n"
                           "If its folder is open in another program, close that and try again.", path) from e
    try:                                    # keep the stored name in step (best effort: the rename itself worked)
        d = load_project_file(new_path)
        d.setdefault("meta", {})["name"] = nm
        atomic_write_json(new_path, d)
    except Exception as e:
        log_error(f"Renamed {new_path} but could not update its stored name", e)

    def f(st):
        st["recent"] = [new_path if _norm(p) == _norm(path) else p for p in st.get("recent", []) if isinstance(p, str)]
    update_settings(f)
    return new_path


def delete_project(path, trash=None):
    """Delete a project. Only files Parrot Studio made are removed; media and other files are never touched.
    trash: optional function(path) -> bool that moves a file or folder to the Recycle Bin / Trash. With it, a
    project folder (or, for a project kept elsewhere, its .clipproj and .bak) goes to the bin; if that fails,
    ProjectError is raised and nothing is deleted (the caller asks before deleting permanently).
    Returns a list of anything left behind (e.g. the user's own files inside the project folder)."""
    path = os.path.abspath(path)
    managed = is_managed(path)
    folder = os.path.dirname(path)
    rec, th = recovery_dir(path), thumbnail_path(path)
    if trash is not None:
        targets = [folder] if managed else [f for f in (path, path + ".bak") if os.path.isfile(f)]
        for t in targets:
            ok = False
            try:
                ok = bool(trash(t))
            except Exception as e:
                log_error(f"Moving {t} to the Recycle Bin failed", e)
            if not ok:
                raise ProjectError("The project could not be moved to the Recycle Bin.", path)
        try:                                # our own helper data for it (outside the folder) goes for good
            if not managed and os.path.isdir(rec):
                shutil.rmtree(rec)
            if not managed and os.path.isfile(th):
                os.remove(th)
        except OSError as e:
            report_problem(f"Could not tidy up the cached data of {path}", e)
        remove_recent(path)
        return []
    try:
        for f in (path, path + ".bak"):
            if os.path.isfile(f):
                os.remove(f)
        if os.path.isdir(rec):
            shutil.rmtree(rec)
        if os.path.isfile(th):
            os.remove(th)
        left = []
        if managed:
            cache = os.path.join(folder, "cache")
            if os.path.isdir(cache):
                shutil.rmtree(cache)
            left = [os.path.join(folder, n) for n in os.listdir(folder)] if os.path.isdir(folder) else []
            if not left:
                os.rmdir(folder)
    except OSError as e:
        log_error(f"Could not delete project {path}", e)
        raise ProjectError(f"The project could not be deleted completely:\n{e.strerror or e}", path) from e
    remove_recent(path)
    return left


# ----------------------------------------------------------------------------- crash recovery
@dataclass
class RecoveryInfo:
    path: str                           # the recovery snapshot file
    project_path: Optional[str]         # None = untitled project
    saved_at: float                     # mtime of the snapshot
    kept: bool = False                  # the user chose "keep for later" before


def write_recovery(project_path, data):
    """Store a crash-recovery snapshot of unsaved work. data: project dict or an already-serialised JSON string."""
    d = recovery_dir(project_path)
    text = data if isinstance(data, str) else json.dumps(prepare_document(data), ensure_ascii=False)
    atomic_write_text(os.path.join(d, RECOVERY_FILE), text)
    return os.path.join(d, RECOVERY_FILE)


def _valid_snapshot(p):
    try:
        _read_document(p)
        return True
    except ProjectError as e:
        bad = p + f".damaged-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(p, bad)
        except OSError:
            bad = p
        report_problem(f"A damaged recovery file was skipped and set aside as {bad}: {e}")
        return False


def has_recovery(project_path):
    """Cheap check (no file parsing) used by the dashboard."""
    d = recovery_dir(project_path)
    live = os.path.join(d, RECOVERY_FILE)
    ps = file_signature(project_path) if project_path else None
    if os.path.isfile(live) and (ps is None or os.path.getmtime(live) > ps[0] / 1e9):
        return True
    try:
        return any(n.startswith("kept-") and n.endswith(PROJECT_EXT) for n in os.listdir(d))
    except OSError:
        return False


def find_recovery(project_path):
    """The recovery snapshot to offer for a project, or None. A live snapshot counts only if it is newer than the
    project file; snapshots the user kept for later are always offered. Damaged snapshots are set aside."""
    d = recovery_dir(project_path)
    live = os.path.join(d, RECOVERY_FILE)
    ps = file_signature(project_path) if project_path else None
    if os.path.isfile(live):
        mt = os.path.getmtime(live)
        if (ps is None or mt > ps[0] / 1e9) and _valid_snapshot(live):
            return RecoveryInfo(live, project_path, mt)
    try:
        kept = sorted((n for n in os.listdir(d) if n.startswith("kept-") and n.endswith(PROJECT_EXT)), reverse=True)
    except OSError:
        kept = []
    for n in kept:
        p = os.path.join(d, n)
        if _valid_snapshot(p):
            return RecoveryInfo(p, project_path, os.path.getmtime(p), kept=True)
    return None


def load_recovery(info):
    return load_project_file(info.path)


def keep_recovery(info):
    """'Keep for later': move a live snapshot aside so new snapshots don't replace it. Returns the new info."""
    if info.kept:
        return info
    new = os.path.join(os.path.dirname(info.path), f"kept-{time.strftime('%Y%m%d-%H%M%S')}{PROJECT_EXT}")
    os.replace(info.path, new)
    return RecoveryInfo(new, info.project_path, info.saved_at, kept=True)


def discard_recovery(project_path, info=None):
    """Remove the live snapshot (and, if info is given, that particular snapshot)."""
    paths = [os.path.join(recovery_dir(project_path), RECOVERY_FILE)]
    if info is not None:
        paths.append(info.path)
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as e:
            report_problem(f"Could not remove recovery file {p}", e)


# ----------------------------------------------------------------------------- session locks (unclean shutdown)
def _sessions_dir():
    return os.path.join(config_dir(), "sessions")


def _pid_alive(pid):
    if pid == os.getpid():
        return True
    if _IS_WIN:
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return code.value == 259                     # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _lock_path(pid=None):
    return os.path.join(_sessions_dir(), f"{pid or os.getpid()}.lock")


def acquire_session_lock():
    """Mark this app session as running. Returns info dicts of earlier sessions that did not shut down cleanly
    (each has "pid", "started" and "project": the project that was open, or None)."""
    unclean = []
    d = _sessions_dir()
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for n in names:
        if not n.endswith(".lock"):
            continue
        p = os.path.join(d, n)
        try:
            with open(p, encoding="utf-8") as f:
                info = json.load(f)
            pid = int(info.get("pid", 0))
        except Exception:
            info, pid = {"pid": 0, "project": None}, 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            continue                     # another Parrot Studio is running right now
        if pid != os.getpid():
            unclean.append(info)
            try:
                os.remove(p)
            except OSError:
                pass
    update_session_lock(None)
    return unclean


def update_session_lock(project_path):
    """Record which project this session has open (so a crash can point at it)."""
    try:
        atomic_write_json(_lock_path(), {"pid": os.getpid(), "started": time.time(), "project": project_path})
    except OSError as e:
        log_error("Could not write the session lock", e)


def release_session_lock():
    try:
        os.remove(_lock_path())
    except OSError:
        pass


# ----------------------------------------------------------------------------- background writer
class AsyncWriter:
    """One worker thread that runs write jobs in order. Jobs with the same key replace a queued, not yet started
    job (only the newest state matters). on_done(result, error) runs on the worker thread."""

    def __init__(self):
        self._q = queue.Queue()
        self._pending = {}
        self._mx = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._busy = 0
        self._t = threading.Thread(target=self._loop, name="parrot-writer", daemon=True)
        self._t.start()

    def submit(self, key, fn, on_done=None):
        with self._mx:
            replaced = key in self._pending
            self._pending[key] = (fn, on_done)
            if not replaced:
                self._busy += 1
                self._idle.clear()
                self._q.put(key)

    def _loop(self):
        while True:
            key = self._q.get()
            if key is None:
                return
            with self._mx:
                fn, on_done = self._pending.pop(key)
            res, err = None, None
            try:
                res = fn()
            except BaseException as e:          # reported to on_done, never lost
                err = e
            if on_done:
                try:
                    on_done(res, err)
                except Exception as e:
                    log_error("Save callback failed", e)
            with self._mx:
                self._busy -= 1
                if self._busy == 0:
                    self._idle.set()

    def wait_idle(self, timeout=10.0):
        """Block until every queued job has finished. Returns False on timeout."""
        return self._idle.wait(timeout)

    def stop(self):
        self._q.put(None)


# ----------------------------------------------------------------------------- display helpers
def relative_time(ts, now=None):
    """'Edited just now' / '5 minutes ago' / '2 hours ago' / 'yesterday' / '3 days ago' / '12 Mar 2026'."""
    if not ts:
        return ""
    now = time.time() if now is None else now
    s = max(0, now - ts)
    if s < 60:
        return "Edited just now"
    if s < 3600:
        m = int(s // 60)
        return f"Edited {m} minute{'s' if m != 1 else ''} ago"
    then, today = datetime.datetime.fromtimestamp(ts), datetime.datetime.fromtimestamp(now)
    days = (today.date() - then.date()).days
    if days == 0:
        h = int(s // 3600)
        return f"Edited {h} hour{'s' if h != 1 else ''} ago"
    if days == 1:
        return "Edited yesterday"
    if days < 7:
        return f"Edited {days} days ago"
    fmt = "%d %b" if then.year == today.year else "%d %b %Y"
    return "Edited " + then.strftime(fmt).lstrip("0")
