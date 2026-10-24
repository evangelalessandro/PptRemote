#!/usr/bin/env python3
"""PptRemote - comanda le presentazioni del PC (PowerPoint, LibreOffice, WPS)
da Android via Wi-Fi.

Come funziona: sul PC gira questo piccolo server; il telefono apre la pagina
servita qui (o l'app Android), tu scegli la finestra della presentazione e da
quel momento i comandi sono F5, Esc, avanti e indietro. La prima volta il PC
chiede conferma prima di accettare il telefono.

Solo libreria standard di Python: nessuna dipendenza da installare.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import io
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

import winapi

VERSION = "1.0"
APP_NAME = "PptRemote"
DEFAULT_PORT = 8787
DISCOVERY_PORT = 8788
PAIR_TIMEOUT = 90
WEB_DIR = Path(__file__).resolve().parent / "web"
TEST_MODE = False
APP_DIR = Path(os.environ.get("PPTREMOTE_DATA") or (Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME))
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "ppremote.log"

APP_LABELS = {
    "powerpnt.exe": "PowerPoint",
    "soffice.bin": "LibreOffice",
    "soffice.exe": "LibreOffice",
    "wpp.exe": "WPS Office",
    "wppview.exe": "WPS Office",
    "acrobat.exe": "Acrobat",
    "acrord32.exe": "Acrobat",
    "foxitpdfreader.exe": "Foxit",
    "sumatrapdf.exe": "SumatraPDF",
}
PRES_EXES = set(APP_LABELS)
PRES_EXTS = (".ppt", ".pptx", ".pps", ".ppsx", ".pptm", ".odp", ".pdf", ".key")
SHOW_CLASSES = {"screenclass"}

LOG = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------- #
# configurazione e dispositivi
# --------------------------------------------------------------------------- #
class Store:
    """config.json: porta e telefoni autorizzati (token)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.port = DEFAULT_PORT
        self.devices: list[dict] = []
        self.folder = ""
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        with self._lock:
            self.port = int(raw.get("port") or DEFAULT_PORT)
            self.devices = [
                d for d in raw.get("devices", []) if isinstance(d, dict) and d.get("token")
            ]
            folder = raw.get("folder")
            if isinstance(folder, str) and folder.strip():
                self.folder = folder

    def save(self) -> None:
        with self._lock:
            data = {"port": self.port, "devices": self.devices, "folder": self.folder}
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CONFIG_PATH)
        except OSError:
            LOG.exception("salvataggio config fallito")

    # -- dispositivi --
    def find_by_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._lock:
            for d in self.devices:
                if secrets.compare_digest(d["token"], token):
                    return d
        return None

    def add_device(self, name: str, ip: str) -> dict:
        dev = {
            "id": uuid.uuid4().hex[:12],
            "name": (name or "Dispositivo Android")[:60],
            "ip": ip,
            "token": secrets.token_urlsafe(32),
            "created": int(time.time()),
            "lastSeen": 0,
        }
        with self._lock:
            self.devices.append(dev)
        self.save()
        return dev

    def touch(self, dev: dict) -> None:
        dev["lastSeen"] = int(time.time())

    def remove(self, device_id: str) -> None:
        with self._lock:
            self.devices = [d for d in self.devices if d["id"] != device_id]
        self.save()


STORE = Store()


# --------------------------------------------------------------------------- #
# pairing: richieste in attesa + coda verso la finestra Tk
# --------------------------------------------------------------------------- #
class Pairing:
    """Un telefono chiede di collegarsi -> il PC mostra un codice -> il telefono
    lo digita -> il telefono riceve un token che conserva per le volte successive."""

    MAX_ATTEMPTS = 5

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.pending: dict[str, dict] = {}
        self.ui_queue: queue.Queue[dict] = queue.Queue()

    def create(self, device_name: str, ip: str) -> dict:
        req = {
            "id": uuid.uuid4().hex[:16],
            "device": (device_name or "Dispositivo Android")[:60],
            "ip": ip,
            "created": time.time(),
            "status": "pending",
            "token": None,
            "code": f"{secrets.randbelow(1_000_000):06d}",
            "attempts": 0,
        }
        with self._lock:
            self._prune()
            self.pending[req["id"]] = req
        self.ui_queue.put(req)
        return req

    def get(self, req_id: str) -> dict | None:
        with self._lock:
            return self.pending.get(req_id)

    def confirm(self, req_id: str, code: str) -> tuple[str, dict]:
        """Confronta il codice digitato sul telefono.

        Ritorna (esito, payload) con esito in: approved | wrong | unknown |
        expired | denied | locked.
        """
        self.expire_stale()
        with self._lock:
            req = self.pending.get(req_id)
            if not req:
                return "unknown", {}
            if req["status"] == "approved":
                return "approved", {"token": req["token"], "deviceId": req.get("deviceId")}
            if req["status"] in ("expired", "denied", "locked"):
                return req["status"], {}
            code = "".join(ch for ch in str(code or "") if ch.isdigit())
            if not secrets.compare_digest(code, req["code"]):
                req["attempts"] += 1
                left = self.MAX_ATTEMPTS - req["attempts"]
                if left <= 0:
                    req["status"] = "locked"
                    LOG.warning("pairing bloccato dopo troppi codici errati: %s", req["id"])
                    return "locked", {}
                return "wrong", {"attemptsLeft": left}
            dev = STORE.add_device(req["device"], req["ip"])
            req["status"] = "approved"
            req["token"] = dev["token"]
            req["deviceId"] = dev["id"]
            LOG.info("pairing approvato con codice: %s (%s)", req["device"], req["ip"])
            return "approved", {"token": dev["token"], "deviceId": dev["id"]}

    def deny(self, req_id: str) -> None:
        with self._lock:
            req = self.pending.get(req_id)
            if req and req["status"] == "pending":
                req["status"] = "denied"

    def expire_stale(self) -> None:
        now = time.time()
        with self._lock:
            for req in self.pending.values():
                if req["status"] == "pending" and now - req["created"] > PAIR_TIMEOUT:
                    req["status"] = "expired"

    def _prune(self) -> None:
        now = time.time()
        for key in [
            k for k, r in self.pending.items() if now - r["created"] > max(PAIR_TIMEOUT * 3, 300)
        ]:
            self.pending.pop(key, None)


PAIRING = Pairing()


# --------------------------------------------------------------------------- #
# finestre delle presentazioni
# --------------------------------------------------------------------------- #
def _is_presentation_window(w: dict) -> bool:
    exe = (w.get("exe") or "").lower()
    title = (w.get("title") or "").lower()
    cls = (w.get("class") or "").lower()
    if exe in PRES_EXES or cls in SHOW_CLASSES or cls == "salframe":
        return True
    return title.endswith(PRES_EXTS)


def presentation_windows() -> list[dict]:
    """Finestre utili: documenti aperti e presentazioni a schermo intero."""
    result = []
    for w in winapi.list_windows():
        if not _is_presentation_window(w):
            continue
        exe = (w.get("exe") or "").lower()
        cls = (w.get("class") or "").lower()
        app = APP_LABELS.get(exe) or (w.get("exe") or "").replace(".exe", "").title()
        showing = bool(w["fullscreen"]) and (cls in SHOW_CLASSES or exe in PRES_EXES)
        item = dict(w)
        item["app"] = app
        item["kind"] = "show" if showing else "doc"
        item["id"] = str(w["hwnd"])
        result.append(item)
    result.sort(key=lambda w: (w["kind"] != "show", not w["foreground"], w["title"].lower()))
    return result


def show_window_for(pid: int, windows: list[dict] | None = None) -> int:
    for w in windows or presentation_windows():
        if w["pid"] == pid and w["kind"] == "show" and not w["minimized"]:
            return w["hwnd"]
    return 0


def resolve_target(hwnd: int, prefer_show: bool = True) -> int:
    """Se la stessa app ha una presentazione a schermo intero, i tasti vanno lì."""
    hwnd = int(hwnd or 0)
    wins = presentation_windows()
    if not hwnd:
        # nessuna selezione: usa la finestra in primo piano se è una presentazione
        for w in wins:
            if w["foreground"]:
                hwnd = w["hwnd"]
                break
        if not hwnd:
            for w in wins:
                if w["kind"] == "show":
                    hwnd = w["hwnd"]
                    break
        if not hwnd and wins:
            hwnd = wins[0]["hwnd"]
        if not hwnd:
            return 0
    target = next((w for w in wins if w["hwnd"] == hwnd), None)
    if target is None:
        # hwnd sconosciuto (finestra chiusa o mai esistita): niente da comandare
        return 0
    if prefer_show:
        show = show_window_for(target["pid"], wins)
        if show:
            return show
    return hwnd


def _digits_for_slide(n: int) -> list[str]:
    return [str(d) for d in str(n)]


def _press(names: list[str], target: int) -> None:
    if not winapi.press(names, target):
        raise ValueError("non riesco a portare in primo piano la finestra della presentazione")


def perform_action(payload: dict) -> dict:
    action = (payload.get("action") or "").strip().lower()
    hwnd = int(payload.get("hwnd") or 0)
    note = ""

    if action in ("refresh", "state"):
        return {"ok": True, "note": ""}

    if hwnd and not winapi.is_window(hwnd):
        raise ValueError("la finestra scelta non esiste più: aggiorna la lista")

    if action == "focus":
        if not hwnd:
            raise ValueError("nessuna finestra selezionata")
        ok = winapi.focus(hwnd)
        note = "finestra in primo piano" if ok else "Windows non ha concesso il primo piano"
        return {"ok": ok, "note": note}

    if action in ("present", "start"):
        target = resolve_target(hwnd, prefer_show=False)
        if not target:
            raise ValueError("nessuna presentazione aperta: scegli una finestra")
        winapi.focus(target)
        time.sleep(0.12)
        winapi.send_keys(["f5"])
        slide_start(target)
        return {"ok": True, "note": "F5 inviato"}

    if action == "present_current":
        target = resolve_target(hwnd, prefer_show=False)
        if not target:
            raise ValueError("nessuna presentazione aperta")
        winapi.focus(target)
        time.sleep(0.12)
        winapi.send_keys(["shift", "f5"])
        slide_forget(target)
        return {"ok": True, "note": "Shift+F5 inviato"}

    if action in ("stop", "esc"):
        target = resolve_target(hwnd, prefer_show=True)
        if not target:
            raise ValueError("nessuna presentazione aperta")
        _press(["esc"], target)
        # se è attivo il puntatore laser il primo Esc lo spegne: chiudo anche la presentazione
        time.sleep(0.45)
        left_over = [
            w for w in presentation_windows()
            if w["kind"] == "show" and w["pid"] == (winapi.get_window(target) or {}).get("pid")
        ]
        if left_over:
            _press(["esc"], left_over[0]["hwnd"])
        slide_forget(target)
        return {"ok": True, "note": "Esc inviato"}

    if action in ("next", "forward"):
        target = resolve_target(hwnd)
        if not target:
            raise ValueError("nessuna presentazione aperta")
        _press(["right"], target)
        slide_step(delta=1)
        return {"ok": True, "note": "slide avanti"}

    if action in ("prev", "back"):
        target = resolve_target(hwnd)
        if not target:
            raise ValueError("nessuna presentazione aperta")
        _press(["left"], target)
        slide_step(delta=-1)
        return {"ok": True, "note": "slide indietro"}

    if action in ("last", "end"):
        target = resolve_target(hwnd)
        if not target:
            raise ValueError("nessuna presentazione aperta")
        _press(["end"], target)
        slide_last()
        return {"ok": True, "note": "ultima slide"}

    if action == "first":
        target = resolve_target(hwnd)
        if not target:
            raise ValueError("nessuna presentazione aperta")
        _press(["home"], target)
        slide_step(absolute=1)
        return {"ok": True, "note": "prima slide"}

    if action == "goto":
        slide = int(payload.get("slide") or 0)
        if slide <= 0:
            raise ValueError("numero slide non valido")
        target = resolve_target(hwnd)
        if not target:
            raise ValueError("nessuna presentazione aperta")
        winapi.focus(target)
        time.sleep(0.08)
        winapi.send_keys(_digits_for_slide(slide) + ["enter"])
        slide_step(absolute=slide)
        return {"ok": True, "note": f"vai alla slide {slide}"}

    if action in ("black", "blank"):
        target = resolve_target(hwnd)
        if not target:
            raise ValueError("nessuna presentazione aperta")
        _press(["b"], target)
        return {"ok": True, "note": "schermo nero (B)"}

    if action == "close":
        target = resolve_target(hwnd, prefer_show=False)
        if not target:
            raise ValueError("nessuna finestra selezionata")
        window = winapi.get_window(target)
        title = name_from_title(window["title"]) if window else "finestra"
        winapi.close_window(target)
        time.sleep(0.45)
        still_open = winapi.is_window(target)
        slide_forget(target)
        if still_open:
            return {
                "ok": True,
                "closed": False,
                "note": f"ho chiesto di chiudere {title}: se ci sono modifiche non salvate rispondi sul PC",
            }
        return {"ok": True, "closed": True, "note": f"chiuso: {title}"}

    if action == "laser":
        target = resolve_target(hwnd)
        if not target:
            raise ValueError("nessuna presentazione aperta")
        if not winapi.press_combo("ctrl", "l", target):
            return {
                "ok": True,
                "note": "laser non disponibile in questa finestra (serve il primo piano)",
            }
        return {"ok": True, "note": "laser attivo: tocca l'anteprima dove vuoi puntare"}

    if action == "key":
        name = str(payload.get("key") or "").strip().lower()
        if not name:
            raise ValueError("tasto mancante")
        target = resolve_target(hwnd)
        if not target:
            raise ValueError("nessuna finestra selezionata")
        _press([name], target)
        return {"ok": True, "note": f"tasto {name}"}

    raise ValueError(f"azione sconosciuta: {action}")


# --------------------------------------------------------------------------- #
# stato
# --------------------------------------------------------------------------- #
# cartella dei file, conteggio slide, anteprima live
# --------------------------------------------------------------------------- #
SEARCH_SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "appdata", "node_modules", ".git",
    "__pycache__", "onedrivetemp",
}
SLIDE_COUNT_CACHE: dict[tuple[str, int], int] = {}
FOLDER_CACHE: dict = {"stamp": 0.0, "folder": "", "by_name": {}}
SLIDES: dict[int, dict] = {}
SLIDES_LOCK = threading.Lock()
PREVIEW_CACHE: dict = {"stamp": 0.0, "hwnd": 0, "data": b""}


DEFAULT_FOLDER_NAMES = ("Documents", "Downloads", "Desktop")
_AUTO_FOLDER: dict = {"value": ""}
FILES_CACHE: dict = {"stamp": 0.0, "folder": "", "items": [], "truncated": False}


def _iter_presentations(folder: Path, limit: int = 4000, budget: float = 2.0, stats=None):
    deadline = time.time() + budget
    seen = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d.lower() not in SEARCH_SKIP_DIRS
        ]
        for name in files:
            if name.startswith("~$") or name.startswith("."):
                continue  # file di blocco di Office e file nascosti
            if not name.lower().endswith(PRES_EXTS):
                continue
            yield Path(root) / name
            seen += 1
            if seen >= limit or time.time() > deadline:
                if stats is not None:
                    stats["truncated"] = True
                return


def _count_presentations(folder: Path, budget: float = 1.5, limit: int = 800) -> int:
    count = 0
    for _ in _iter_presentations(folder, limit=limit, budget=budget):
        count += 1
    return count


def detect_folder() -> Path:
    """Se non è stata scelta una cartella, uso quella che contiene più presentazioni."""
    home = Path.home()
    candidates = [home / name for name in DEFAULT_FOLDER_NAMES]
    one_drive = home / "OneDrive"
    if one_drive.is_dir():
        candidates += [
            one_drive / "Documenti",
            one_drive / "Documents",
            one_drive / "Desktop",
        ]
    best: Path | None = None
    best_count = 0
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        count = _count_presentations(candidate)
        LOG.info("cartella candidata %s: %d presentazioni", candidate, count)
        if count > best_count:
            best, best_count = candidate, count
    if best is None:
        best = home / "Documents" if (home / "Documents").is_dir() else home
    return best


def folder_root() -> Path:
    configured = (STORE.folder or "").strip()
    if configured:
        path = Path(configured)
        if path.is_dir():
            return path
    if _AUTO_FOLDER["value"]:
        return Path(_AUTO_FOLDER["value"])
    chosen = detect_folder()
    _AUTO_FOLDER["value"] = str(chosen)
    STORE.folder = str(chosen)
    STORE.save()
    FILES_CACHE["stamp"] = 0.0
    LOG.info("cartella di ricerca scelta automaticamente: %s", chosen)
    return chosen


def _folder_items(force: bool = False) -> tuple[list[dict], bool]:
    """Elenco (in cache) delle presentazioni nella cartella configurata."""
    folder = folder_root()
    now = time.time()
    if (
        not force
        and FILES_CACHE["folder"] == str(folder)
        and now - FILES_CACHE["stamp"] < 30
        and FILES_CACHE["items"]
    ):
        return FILES_CACHE["items"], FILES_CACHE["truncated"]
    items: list[dict] = []
    stats = {"truncated": False}
    for path in _iter_presentations(folder, limit=8000, budget=8.0, stats=stats):
        try:
            stat = path.stat()
        except OSError:
            continue
        try:
            rel = str(path.parent.relative_to(folder))
        except ValueError:
            rel = str(path.parent)
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "rel": "" if rel == "." else rel,
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
            }
        )
    FILES_CACHE.update(
        {
            "stamp": now,
            "folder": str(folder),
            "items": items,
            "truncated": stats["truncated"],
        }
    )
    return items, stats["truncated"]


def count_slides(path: Path) -> int:
    """Numero di slide leggendo il file (pptx/odp); 0 se non è possibile."""
    try:
        mtime = int(path.stat().st_mtime)
    except OSError:
        return 0
    key = (str(path), mtime)
    if key in SLIDE_COUNT_CACHE:
        return SLIDE_COUNT_CACHE[key]
    total = 0
    try:
        suffix = path.suffix.lower()
        if suffix in (".pptx", ".pptm", ".ppsx"):
            with zipfile.ZipFile(path) as archive:
                total = sum(
                    1
                    for n in archive.namelist()
                    if n.startswith("ppt/slides/slide") and n.endswith(".xml")
                )
        elif suffix == ".odp":
            with zipfile.ZipFile(path) as archive:
                total = archive.read("content.xml").decode("utf-8", "replace").count("<draw:page ")
    except (OSError, KeyError, zipfile.BadZipFile):
        total = 0
    SLIDE_COUNT_CACHE[key] = total
    return total


def folder_index() -> dict[str, Path]:
    """Nome file -> percorso, per dedurre il totale slide di una finestra aperta."""
    now = time.time()
    if now - FOLDER_CACHE["stamp"] < 120 and FOLDER_CACHE["folder"] == STORE.folder:
        return FOLDER_CACHE["by_name"]
    mapping: dict[str, Path] = {}
    for path in _iter_presentations(folder_root(), limit=3000, budget=1.5):
        mapping.setdefault(path.name.lower(), path)
    FOLDER_CACHE.update({"stamp": now, "folder": STORE.folder, "by_name": mapping})
    return mapping


def name_from_title(title: str) -> str:
    t = (title or "").strip()
    t = re.sub(r"^(Presentazione|PowerPoint Slide Show)\s*[-–]\s*", "", t, flags=re.I)
    t = re.sub(r"^\[(.*)\]$", r"\1", t)
    t = re.sub(
        r"\s*[-–]\s*(PowerPoint|LibreOffice Impress|LibreOffice|WPS Presentation|WPS Office)\s*$",
        "", t, flags=re.I,
    )
    return t.strip()


def total_slides_for(hwnd: int) -> int:
    window = winapi.get_window(hwnd)
    if not window:
        return 0
    name = name_from_title(window["title"]).lower()
    path = folder_index().get(name)
    return count_slides(path) if path else 0


def slide_info(pid: int) -> dict | None:
    with SLIDES_LOCK:
        info = SLIDES.get(pid)
        return dict(info) if info else None


def slide_start(hwnd: int, total: int = 0, name: str = "") -> None:
    window = winapi.get_window(hwnd)
    if not window:
        return
    with SLIDES_LOCK:
        SLIDES[window["pid"]] = {
            "current": 1,
            "total": total or total_slides_for(hwnd),
            "name": name or name_from_title(window["title"]),
        }


def slide_step(delta: int | None = None, absolute: int | None = None,
               total: int | None = None) -> None:
    """Aggiorna il contatore delle presentazioni a schermo intero."""
    for window in presentation_windows():
        if window["kind"] != "show":
            continue
        with SLIDES_LOCK:
            info = SLIDES.get(window["pid"])
            if info is None:
                info = {"current": 0, "total": 0, "name": name_from_title(window["title"])}
                SLIDES[window["pid"]] = info
            if not info.get("total"):
                info["total"] = total_slides_for(window["hwnd"])
            if total is not None:
                info["total"] = total
            if absolute is not None:
                info["current"] = absolute
            elif delta is not None:
                info["current"] = info["current"] + delta if info["current"] > 0 else 1
            if info["total"] > 0:
                info["current"] = max(1, min(info["current"], info["total"]))


def slide_clear() -> None:
    with SLIDES_LOCK:
        SLIDES.clear()


def slide_forget(hwnd: int) -> None:
    window = winapi.get_window(hwnd)
    if window:
        with SLIDES_LOCK:
            SLIDES.pop(window["pid"], None)


def slide_last() -> None:
    for window in presentation_windows():
        if window["kind"] != "show":
            continue
        with SLIDES_LOCK:
            info = SLIDES.setdefault(
                window["pid"],
                {"current": 0, "total": 0, "name": name_from_title(window["title"])},
            )
            if not info.get("total"):
                info["total"] = total_slides_for(window["hwnd"])
            if info["total"]:
                info["current"] = info["total"]


try:  # JPEG opzionale: se c'è Pillow le immagini pesano ~20 volte meno
    from PIL import Image as _PILImage

    HAVE_PIL = True
except ImportError:  # pragma: no cover - senza Pillow si usa il BMP
    _PILImage = None
    HAVE_PIL = False


def _to_jpeg(bmp: bytes | None, quality: int = 55) -> tuple[bytes | None, str]:
    """Da BMP a JPEG (se possibile). Ritorna (dati, content-type)."""
    if not bmp:
        return None, "image/bmp"
    if not HAVE_PIL:
        return bmp, "image/bmp"
    try:
        image = _PILImage.open(io.BytesIO(bmp)).convert("RGB")
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:  # pragma: no cover
        LOG.debug("conversione JPEG fallita", exc_info=True)
        return bmp, "image/bmp"


def screen_frame(monitor: int = 0, width: int = 520) -> tuple[bytes | None, str]:
    return _to_jpeg(winapi.capture_screen_bmp(monitor, width), 55)


def preview_bytes(hwnd: int = 0, max_width: int = 320) -> bytes | None:
    target = int(hwnd or 0)
    if not target:
        for window in presentation_windows():
            if window["kind"] == "show":
                target = window["hwnd"]
                break
    if not target:
        return None
    now = time.time()
    if PREVIEW_CACHE["hwnd"] == target and now - PREVIEW_CACHE["stamp"] < 0.7:
        return PREVIEW_CACHE["data"]
    data, mime = _to_jpeg(winapi.capture_window_bmp(target, max_width), 60)
    if data:
        PREVIEW_CACHE.update({"stamp": now, "hwnd": target, "data": data, "mime": mime})
    return data


def preview_frame(hwnd: int = 0, max_width: int = 320) -> tuple[bytes | None, str]:
    """Anteprima come (dati, content-type): JPEG se disponibile."""
    target = int(hwnd or 0)
    if not target:
        for window in presentation_windows():
            if window["kind"] == "show":
                target = window["hwnd"]
                break
    if not target:
        return None, "image/jpeg"
    now = time.time()
    if PREVIEW_CACHE["hwnd"] == target and now - PREVIEW_CACHE["stamp"] < 0.7 and PREVIEW_CACHE["data"]:
        return PREVIEW_CACHE["data"], PREVIEW_CACHE.get("mime", "image/jpeg")
    data, mime = _to_jpeg(winapi.capture_window_bmp(target, max_width), 60)
    if data:
        PREVIEW_CACHE.update({"stamp": now, "hwnd": target, "data": data, "mime": mime})
    return data, mime


TYPE_PRIORITY = {
    ".pptx": 0, ".pptm": 0, ".ppsx": 0, ".ppt": 1, ".pps": 1,
    ".odp": 1, ".pdf": 2, ".key": 3,
}


def _norm(text: str) -> str:
    """Minuscolo, senza accenti, con _ - . trattati come spazi."""
    flat = unicodedata.normalize("NFKD", str(text or "").lower())
    flat = "".join(ch for ch in flat if not unicodedata.combining(ch))
    flat = re.sub(r"[_\-.,;:]+", " ", flat)
    return re.sub(r"\s+", " ", flat).strip()


def _tokens(query_text: str) -> list[str]:
    return [tok for tok in _norm(query_text).split(" ") if tok]


def search_files(query_text: str, limit: int = 40) -> tuple[list[dict], bool, int]:
    """Cerca nella cartella configurata: (risultati, troncato, totale_in_cartella).

    Le parole cercate vanno bene in qualsiasi ordine e in qualsiasi punto del nome:
    "goli ubbi" trova "Davide_e_Golia_ubbidienza.pptx".
    """
    items, truncated = _folder_items()
    tokens = _tokens(query_text)
    phrase = _norm(query_text)

    scored: list[tuple[int, dict]] = []
    for item in items:
        name_norm = _norm(item["name"])
        haystack = name_norm + " " + _norm(item["rel"])
        if tokens and not all(tok in haystack for tok in tokens):
            continue
        if not tokens:
            score = 0
        elif phrase and name_norm.startswith(phrase):
            score = 0
        elif all(tok in name_norm for tok in tokens):
            score = 1
        else:
            score = 2
        scored.append((score, item))

    scored.sort(
        key=lambda pair: (
            pair[0],
            TYPE_PRIORITY.get(Path(pair[1]["name"]).suffix.lower(), 4),
            -pair[1]["modified"],
        )
    )
    total = len(scored)
    results = []
    for _score, item in scored[:limit]:
        entry = dict(item)
        entry["slides"] = count_slides(Path(item["path"]))
        results.append(entry)
    return results, truncated or total > limit, len(items)


def open_presentation(raw_path: str, present: bool) -> dict:
    root = folder_root().resolve()
    try:
        path = Path(raw_path).expanduser().resolve()
        path.relative_to(root)
    except (ValueError, OSError):
        raise ValueError("il file non è dentro la cartella configurata")
    if not path.is_file():
        raise ValueError("file non trovato")
    if path.suffix.lower() not in PRES_EXTS:
        raise ValueError("formato non supportato")
    total = count_slides(path)
    try:
        os.startfile(str(path))  # noqa: S606 - apertura file locale scelto dall'utente
    except OSError as exc:
        raise ValueError(f"Windows non ha aperto il file: {exc}")

    if not present:
        return {"note": "file aperto", "name": path.name, "slides": total}

    target = None
    for _ in range(48):
        time.sleep(0.25)
        target = next(
            (w for w in presentation_windows() if path.name.lower() in w["title"].lower()),
            None,
        )
        if target:
            break
    if not target:
        raise ValueError("file aperto, ma la finestra non è comparsa: toccala dall'elenco")
    title = target["title"].lower()
    protected = "protected view" in title or "visualizzazione protetta" in title
    winapi.press(["f5"], target["hwnd"])
    slide_start(target["hwnd"], total, path.name)
    result = {"note": "aperto e presentazione avviata", "name": path.name, "slides": total}
    if protected:
        result["warning"] = (
            "PowerPoint ha aperto il file in Visualizzazione protetta: se i comandi "
            "non rispondono, sposta il file in Documenti (o aggiungi la cartella "
            "alle posizioni attendibili di PowerPoint)."
        )
    return result


# --------------------------------------------------------------------------- #
def build_state() -> dict:
    wins = presentation_windows()
    for window in wins:
        info = slide_info(window["pid"])
        if info:
            window["slide"] = info
    return {
        "pc": os.environ.get("COMPUTERNAME") or socket.gethostname(),
        "version": VERSION,
        "monitors": winapi.monitors(),
        "windows": wins,
        "folder": str(folder_root()),
        "devices": [
            {
                "id": d["id"],
                "name": d["name"],
                "ip": d["ip"],
                "online": int(time.time()) - int(d.get("lastSeen") or 0) < 20,
            }
            for d in STORE.devices
        ],
        "serverTime": int(time.time()),
    }


# --------------------------------------------------------------------------- #
# API HTTP
# --------------------------------------------------------------------------- #
def _token_from(headers, query) -> str | None:
    token = (query.get("token") or [None])[0]
    if token:
        return token
    header = headers.get("X-Token")
    return header or None


def api_dispatch(method: str, path: str, query: dict, body: dict, ip: str, headers=None):
    if path == "/api/info":
        return 200, {
            "app": APP_NAME,
            "version": VERSION,
            "pc": os.environ.get("COMPUTERNAME") or socket.gethostname(),
            "paired": len(STORE.devices) > 0,
        }

    if path == "/api/pair":
        if method == "POST":
            req = PAIRING.create(str(body.get("device") or ""), ip)
            LOG.info("richiesta di pairing da %s (%s): codice mostrato sul PC", req["device"], ip)
            return 200, {"id": req["id"], "status": "pending", "timeout": PAIR_TIMEOUT}
        req_id = (query.get("id") or [""])[0]
        PAIRING.expire_stale()
        req = PAIRING.get(req_id)
        if not req:
            return 404, {"status": "unknown"}
        payload = {"id": req["id"], "status": req["status"]}
        if req["status"] == "approved":
            payload["token"] = req["token"]
            payload["deviceId"] = req.get("deviceId")
        return 200, payload

    if path == "/api/pair/confirm":
        if method != "POST":
            return 405, {"error": "usa POST"}
        req_id = str(body.get("id") or "")
        outcome, payload = PAIRING.confirm(req_id, str(body.get("code") or ""))
        if outcome == "approved":
            return 200, {"status": "approved", **payload}
        if outcome == "wrong":
            return 403, {"status": "wrong", "error": "codice errato", **payload}
        if outcome == "unknown":
            return 404, {"status": "unknown"}
        if outcome == "locked":
            return 429, {"status": "locked", "error": "troppi tentativi: riprova dal telefono"}
        return 410, {"status": outcome, "error": "richiesta scaduta"}

    if path == "/api/pair/code":
        # solo per i test automatici: la finestra del PC è l'unica a mostrare il codice
        if not TEST_MODE:
            return 404, {"error": "endpoint sconosciuto"}
        req = PAIRING.get((query.get("id") or [""])[0])
        if not req:
            return 404, {"status": "unknown"}
        return 200, {"code": req["code"], "status": req["status"]}

    # da qui in poi serve un token valido
    token = _token_from(headers, query)
    device = STORE.find_by_token(token)
    if not device:
        return 401, {"error": "non autorizzato", "code": "unauthorized"}
    STORE.touch(device)

    if path == "/api/state":
        return 200, build_state()

    if path == "/api/action":
        if method != "POST":
            return 405, {"error": "usa POST"}
        try:
            result = perform_action(body)
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
        state = build_state()
        return 200, {"ok": True, **result, "state": state}

    if path == "/api/unlink":
        STORE.remove(device["id"])
        return 200, {"ok": True}

    if path == "/api/device":
        return 200, {"device": {"id": device["id"], "name": device["name"]}}

    if path == "/api/pointer":
        if method != "POST":
            return 405, {"error": "usa POST"}
        action = str(body.get("action") or "move").strip().lower()
        try:
            if action == "move":
                dx = int(body.get("dx") or 0)
                dy = int(body.get("dy") or 0)
                if dx or dy:
                    winapi.move_mouse(dx, dy)
            elif action == "abs":
                fx = float(body.get("x") or 0)
                fy = float(body.get("y") or 0)
                if str(body.get("space") or "window") == "screen":
                    rect = winapi.monitor_rect(int(body.get("monitor") or 0))
                    if not rect:
                        return 400, {"error": "monitor non disponibile"}
                    left, top, right, bottom = rect
                    px = left + int((right - left) * min(max(fx, 0.0), 1.0))
                    py = top + int((bottom - top) * min(max(fy, 0.0), 1.0))
                    winapi.move_mouse_abs(px, py)
                    return 200, {"ok": True}
                target = resolve_target(0, prefer_show=True)
                window = winapi.get_window(target) if target else None
                if not window:
                    return 400, {"error": "nessuna presentazione aperta"}
                left, top, right, bottom = window["rect"]
                px = left + int((right - left) * min(max(fx, 0.0), 1.0))
                py = top + int((bottom - top) * min(max(fy, 0.0), 1.0))
                winapi.move_mouse_abs(px, py)
            elif action == "click":
                winapi.click_mouse()
            else:
                return 400, {"error": f"azione puntatore sconosciuta: {action}"}
        except (TypeError, ValueError):
            return 400, {"error": "coordinate non valide"}
        return 200, {"ok": True}

    if path == "/api/files":
        files, truncated, total = search_files((query.get("q") or [""])[0])
        return 200, {
            "folder": str(folder_root()),
            "files": files,
            "truncated": truncated,
            "total": total,
        }

    if path == "/api/files/open":
        if method != "POST":
            return 405, {"error": "usa POST"}
        try:
            info = open_presentation(
                str(body.get("path") or ""), bool(body.get("present"))
            )
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
        return 200, {"ok": True, **info, "state": build_state()}

    if path == "/api/config":
        if method == "POST":
            folder = str(body.get("folder") or "").strip()
            if folder:
                candidate = Path(folder).expanduser()
                if not candidate.is_dir():
                    return 400, {"error": "cartella inesistente"}
                STORE.folder = str(candidate)
                STORE.save()
                FOLDER_CACHE["stamp"] = 0.0
                FILES_CACHE["stamp"] = 0.0
                _AUTO_FOLDER["value"] = str(candidate)
            return 200, {"folder": STORE.folder}
        return 200, {"folder": STORE.folder}

    return 404, {"error": "endpoint sconosciuto"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/{VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        LOG.debug("%s - %s", self.client_address[0], fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1_000_000))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/preview":
                self._serve_preview(query)
                return
            if path == "/api/screen":
                self._serve_screen(query)
                return
            if path.startswith("/api/"):
                body = self._read_body() if method == "POST" else {}
                status, payload = api_dispatch(
                    method, path, query, body, self.client_address[0], self.headers
                )
                self._send_json(status, payload)
            else:
                self._serve_static(path)
        except Exception as exc:  # pragma: no cover - rete/IO
            LOG.exception("errore gestendo %s %s", method, path)
            self._send_json(500, {"error": str(exc)})

    def _serve_screen(self, query: dict) -> None:
        device = STORE.find_by_token(_token_from(self.headers, query))
        if not device:
            self._send_json(401, {"error": "non autorizzato"})
            return
        STORE.touch(device)
        try:
            monitor = int((query.get("monitor") or ["0"])[0] or 0)
        except ValueError:
            monitor = 0
        try:
            width = int((query.get("w") or ["520"])[0] or 520)
        except ValueError:
            width = 520
        data, mime = screen_frame(monitor, max(200, min(width, 1200)))
        if not data:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_preview(self, query: dict) -> None:
        device = STORE.find_by_token(_token_from(self.headers, query))
        if not device:
            self._send_json(401, {"error": "non autorizzato"})
            return
        STORE.touch(device)
        try:
            hwnd = int((query.get("hwnd") or ["0"])[0] or 0)
        except ValueError:
            hwnd = 0
        data, mime = preview_frame(hwnd)
        if not data:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        candidate = (WEB_DIR / rel).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        data = candidate.read_bytes()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".webmanifest": "application/manifest+json; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
        }.get(candidate.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Cache-Control",
            "no-store" if candidate.suffix.lower() in (".html", ".js", ".css") else "max-age=86400",
        )
        self.end_headers()
        self.wfile.write(data)


# --------------------------------------------------------------------------- #
# rete: indirizzo LAN + discovery UDP
# --------------------------------------------------------------------------- #
def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def lan_addresses() -> list[str]:
    """Indirizzi IPv4 del PC raggiungibili dal telefono (esclude APIPA e schede virtuali)."""
    candidates: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in candidates and not ip.startswith("127."):
                candidates.append(ip)
    except OSError:
        pass

    usable: list[str] = []
    for ip in candidates:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((ip, 0))
            probe.connect(("8.8.8.8", 80))
            usable.append(ip)
        except OSError:
            continue
        finally:
            probe.close()

    primary = lan_ip()
    if primary in usable:
        usable.remove(primary)
    usable.insert(0, primary)
    return usable


def firewall_open(port: int) -> bool:
    """True se esiste una regola di Windows Firewall per la porta del server."""
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={APP_NAME} {port}"],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def start_discovery() -> None:
    """Risponde su UDP 8788: permette all'app Android di trovare il PC da sola."""

    def run() -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
        except OSError:
            LOG.warning("discovery UDP %s occupata: proseguo senza", DISCOVERY_PORT)
            return
        reply = json.dumps(
            {
                "app": APP_NAME,
                "version": VERSION,
                "pc": os.environ.get("COMPUTERNAME") or socket.gethostname(),
                "port": SERVER_PORT,
            }
        ).encode("utf-8")
        while True:
            try:
                data, addr = sock.recvfrom(1024)
            except OSError:
                break
            if b"PPTDISCOVER" in data:
                try:
                    sock.sendto(reply, addr)
                except OSError:
                    pass

    threading.Thread(target=run, daemon=True, name="discovery").start()


# --------------------------------------------------------------------------- #
# finestra sul PC
# --------------------------------------------------------------------------- #
import tkinter as tk  # noqa: E402  (import dopo per non rallentare l'avvio in console)
from tkinter import ttk  # noqa: E402

BG = "#0f141a"
CARD = "#182027"
FG = "#e8eef5"
MUTED = "#95a3b3"
ACCENT = "#2f81f7"
OK = "#2ea043"
DANGER = "#da3633"


class PcWindow:
    def __init__(self, port: int) -> None:
        self.port = port
        self.addresses = lan_addresses()
        self.url = f"http://{self.addresses[0]}:{port}"
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} — telecomando presentazioni")
        self.root.geometry("560x760")
        self.root.minsize(520, 620)
        self.root.configure(bg=BG)
        self.dialog: tk.Toplevel | None = None
        self.waiting: list[dict] = []
        self._build()
        self.root.after(250, self._poll_pairing)
        self.root.after(1000, self._refresh_lists)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # -- costruzione interfaccia --
    def _build(self) -> None:
        pad = {"padx": 18}
        tk.Label(
            self.root,
            text=APP_NAME,
            bg=BG,
            fg=FG,
            font=("Segoe UI Semibold", 22),
        ).pack(anchor="w", pady=(18, 0), **pad)
        tk.Label(
            self.root,
            text="PowerPoint / LibreOffice / WPS — F5, Esc, avanti e indietro",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w", **pad)

        card = tk.Frame(self.root, bg=CARD, highlightthickness=0)
        card.pack(fill="x", pady=14, **pad)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=16, pady=14)
        self.status_dot = tk.Label(
            inner, text="●", bg=CARD, fg=OK, font=("Segoe UI", 14)
        )
        self.status_dot.grid(row=0, column=0, sticky="w")
        tk.Label(
            inner,
            text=f"Server attivo sulla porta {self.port}",
            bg=CARD,
            fg=FG,
            font=("Segoe UI Semibold", 12),
        ).grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.url_label = tk.Label(
            inner,
            text=self.url,
            bg=CARD,
            fg=ACCENT,
            font=("Consolas", 15),
            cursor="hand2",
        )
        self.url_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 2))
        tk.Label(
            inner,
            text="Apri questo indirizzo dal telefono (stessa rete Wi-Fi).",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        buttons = tk.Frame(inner, bg=CARD)
        buttons.grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self._button(buttons, "Copia indirizzo", self.copy_url).pack(side="left")
        self._button(buttons, "Apri nel browser", self.open_browser).pack(
            side="left", padx=(8, 0)
        )
        self._button(buttons, "Apri firewall", self.firewall).pack(side="left", padx=(8, 0))

        if len(self.addresses) > 1:
            tk.Label(
                inner,
                text="Altri indirizzi del PC: " + ", ".join(self.addresses[1:]),
                bg=CARD,
                fg=MUTED,
                font=("Segoe UI", 8),
                wraplength=470,
                justify="left",
            ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        qr = self._qr_widget(inner, self.url)
        if qr is not None:
            qr.grid(row=5, column=0, columnspan=2, pady=(12, 0))
            tk.Label(
                inner,
                text="Inquadra con la fotocamera del telefono per aprire la pagina",
                bg=CARD,
                fg=MUTED,
                font=("Segoe UI", 8),
            ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.fw_label = tk.Label(
            inner, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8),
            wraplength=470, justify="left",
        )
        self.fw_label.grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._update_firewall_label()
        self._refresh_folder_count()

        folder_row = tk.Frame(self.root, bg=BG)
        folder_row.pack(fill="x", pady=(2, 6), **pad)
        tk.Label(
            folder_row, text="Cartella file:", bg=BG, fg=MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        self.folder_label = tk.Label(
            folder_row, text=self._folder_text(), bg=BG, fg=FG,
            font=("Segoe UI", 9), anchor="w",
        )
        self.folder_label.pack(side="left", padx=(6, 8), fill="x", expand=True)
        self._button(folder_row, "Scegli…", self.pick_folder).pack(side="right")

        tk.Label(
            self.root,
            text="Telefoni autorizzati",
            bg=BG,
            fg=FG,
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", pady=(6, 4), **pad)
        self.devices_box = tk.Frame(self.root, bg=BG)
        self.devices_box.pack(fill="both", expand=True, **pad)
        self.devices_empty = tk.Label(
            self.devices_box,
            text="Nessuno. La prima connessione dal telefono chiederà conferma qui.",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            wraplength=460,
            justify="left",
        )
        self.devices_empty.pack(anchor="w")

        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", pady=(0, 16), **pad)
        tk.Label(
            footer,
            text="Telefono e PC devono stare sulla stessa rete Wi-Fi (non serve l'hotspot). "
            "Se Windows chiede il permesso del firewall, scegli Consenti.",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
            wraplength=380,
            justify="left",
        ).pack(side="left")
        self._button(footer, "Chiudi", self.quit).pack(side="right")

    def _qr_widget(self, parent, text: str):
        """QR dell'indirizzo, se la libreria qrcode è disponibile (facoltativa)."""
        try:
            import qrcode
        except ImportError:
            return None
        try:
            qr = qrcode.QRCode(border=2, box_size=1)
            qr.add_data(text)
            qr.make(fit=True)
            matrix = qr.get_matrix()
        except Exception:  # pragma: no cover - libreria presente ma diversa
            LOG.debug("QR non generato", exc_info=True)
            return None
        module = 4
        side = len(matrix) * module
        canvas = tk.Canvas(
            parent, width=side, height=side, bg="white", highlightthickness=0
        )
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    canvas.create_rectangle(
                        x * module, y * module, (x + 1) * module, (y + 1) * module,
                        fill="#0b1016", outline="",
                    )
        return canvas

    def _update_firewall_label(self) -> None:
        if not hasattr(self, "fw_label"):
            return
        if firewall_open(self.port):
            self.fw_label.config(
                text=f"Firewall: regola per la porta {self.port} presente. Il telefono può collegarsi.",
                fg=OK,
            )
        else:
            self.fw_label.config(
                text=f"Firewall: nessuna regola per la porta {self.port}. "
                     "Se il telefono non apre la pagina, premi «Apri firewall».",
                fg="#e3b341",
            )

    def _button(self, parent, text: str, command, accent: bool = False):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=ACCENT if accent else "#232f3a",
            fg="white",
            activebackground="#1f6fdd" if accent else "#2c3a46",
            activeforeground="white",
            relief="flat",
            padx=12,
            pady=6,
            font=("Segoe UI", 9),
            cursor="hand2",
            borderwidth=0,
        )

    # -- azioni --
    def copy_url(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)
        self._toast("Indirizzo copiato")

    def open_browser(self) -> None:
        os.startfile(self.url)  # noqa: S606 - link locale

    def firewall(self) -> None:
        cmd = (
            f'netsh advfirewall firewall add rule name="{APP_NAME} {self.port}" '
            f"dir=in action=allow protocol=TCP localport={self.port}"
        )
        try:
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd.exe", f"/c {cmd}", None, 1
            )
            self._toast("Conferma la richiesta di Windows per aprire la porta")
            self.root.after(7000, self._update_firewall_label)
            self.root.after(20000, self._update_firewall_label)
        except OSError:
            self._toast("Non riesco ad aprire il prompt di Windows")

    # -- cartella dei file --
    def _folder_text(self) -> str:
        path = str(STORE.folder or "")
        if not path:
            return "(automatica: scelgo la cartella con più presentazioni)"
        return path if len(path) <= 46 else "…" + path[-44:]

    def _set_folder_count(self, count: int, truncated: bool = False) -> None:
        base = self._folder_text()
        if count < 0:
            self.folder_label.config(text=base, fg=FG)
        elif count == 0:
            self.folder_label.config(
                text=base + "  ·  nessuna presentazione: premi Scegli…", fg="#e3b341"
            )
        else:
            extra = "  ·  " + str(count) + " presentazioni"
            if truncated:
                extra += " (parziale)"
            self.folder_label.config(text=base + extra, fg=FG)

    def _refresh_folder_count(self) -> None:
        if getattr(self, "_counting", False):
            return
        self._counting = True

        def work() -> None:
            try:
                items, truncated = _folder_items()
                count = len(items)
            except Exception:  # pragma: no cover - permessi/disco
                count, truncated = -1, False

            def done() -> None:
                self._counting = False
                self._set_folder_count(count, truncated)

            try:
                self.root.after(0, done)
            except Exception:  # finestra già chiusa
                pass

        threading.Thread(target=work, daemon=True, name="folder-count").start()

    def pick_folder(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(
            initialdir=STORE.folder or str(Path.home()),
            title="Cartella dove cercare le presentazioni",
        )
        if chosen:
            STORE.folder = chosen
            STORE.save()
            FOLDER_CACHE["stamp"] = 0.0
            FILES_CACHE["stamp"] = 0.0
            _AUTO_FOLDER["value"] = chosen
            self.folder_label.config(text=self._folder_text(), fg=FG)
            self._refresh_folder_count()
            self._toast("Cartella impostata")

    def _toast(self, text: str) -> None:
        self.status_dot.config(fg=ACCENT)
        self.root.after(1200, lambda: self.status_dot.config(fg=OK))

    # -- lista dispositivi --
    def _refresh_lists(self) -> None:
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self._ticks % 10 == 1:
            self._update_firewall_label()
        if self._ticks % 10 == 2:
            self._refresh_folder_count()
        for child in self.devices_box.winfo_children():
            child.destroy()
        if not STORE.devices:
            tk.Label(
                self.devices_box,
                text="Nessuno. La prima connessione dal telefono chiederà conferma qui.",
                bg=BG,
                fg=MUTED,
                font=("Segoe UI", 9),
                wraplength=460,
                justify="left",
            ).pack(anchor="w")
        now = time.time()
        for dev in STORE.devices:
            online = now - (dev.get("lastSeen") or 0) < 20
            row = tk.Frame(self.devices_box, bg=CARD)
            row.pack(fill="x", pady=3)
            tk.Label(row, text="●", bg=CARD, fg=OK if online else "#4a5763",
                     font=("Segoe UI", 11)).pack(side="left", padx=(10, 6), pady=8)
            tk.Label(
                row,
                text=dev["name"],
                bg=CARD,
                fg=FG,
                font=("Segoe UI", 10),
            ).pack(side="left")
            tk.Label(
                row,
                text=("collegato" if online else f"visto {dev['ip']}"),
                bg=CARD,
                fg=MUTED,
                font=("Segoe UI", 8),
            ).pack(side="left", padx=(8, 0))
            tk.Button(
                row,
                text="Revoca",
                command=lambda d=dev: self.revoke(d["id"]),
                bg=CARD,
                fg="#ff8b8b",
                relief="flat",
                borderwidth=0,
                cursor="hand2",
                font=("Segoe UI", 8),
            ).pack(side="right", padx=8)
        self.root.after(1500, self._refresh_lists)

    def revoke(self, device_id: str) -> None:
        STORE.remove(device_id)
        self._toast("Telefono revocato")

    # -- dialogo di conferma --
    def _poll_pairing(self) -> None:
        try:
            while True:
                self.waiting.append(PAIRING.ui_queue.get_nowait())
        except queue.Empty:
            pass
        if self.dialog is None or not self.dialog.winfo_exists():
            self.dialog = None
            while self.waiting:
                req = self.waiting.pop(0)
                if req["status"] == "pending" and time.time() - req["created"] < PAIR_TIMEOUT:
                    self._show_pair_dialog(req)
                    break
        self.root.after(200, self._poll_pairing)

    def _show_pair_dialog(self, req: dict) -> None:
        dlg = tk.Toplevel(self.root)
        self.dialog = dlg
        dlg.title("Collega un telefono")
        dlg.configure(bg=BG)
        w, h = 440, 322
        x = max(0, (dlg.winfo_screenwidth() - w) // 2)
        y = max(0, (dlg.winfo_screenheight() - h) // 3)
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        dlg.protocol("WM_DELETE_WINDOW", lambda: self._cancel(req["id"]))
        dlg.bind("<Escape>", lambda _e: self._cancel(req["id"]))

        tk.Label(
            dlg, text="Collega un telefono", bg=BG, fg=FG,
            font=("Segoe UI Semibold", 16),
        ).pack(pady=(16, 2))
        tk.Label(
            dlg, text=req["device"] + " · " + req["ip"], bg=BG, fg=ACCENT,
            font=("Segoe UI", 10), wraplength=380,
        ).pack(pady=(0, 10))

        code = req["code"]
        shown = code[:3] + " " + code[3:]
        code_box = tk.Frame(dlg, bg=CARD)
        code_box.pack()
        tk.Label(
            code_box, text=shown, bg=CARD, fg=FG,
            font=("Consolas", 34, "bold"), padx=24, pady=10,
        ).pack()

        tk.Label(
            dlg, text="Digita questo codice nell'app del telefono.",
            bg=BG, fg=FG, font=("Segoe UI", 10),
        ).pack(pady=(12, 2))

        countdown = tk.Label(dlg, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        countdown.pack(pady=(2, 10))

        tk.Button(
            dlg, text="Annulla", command=lambda: self._cancel(req["id"]),
            bg="#232f3a", fg=FG, activebackground="#2c3a46", activeforeground="white",
            relief="flat", borderwidth=0, padx=18, pady=7,
            font=("Segoe UI", 9), cursor="hand2",
        ).pack()

        def tick(remaining: int) -> None:
            if not dlg.winfo_exists():
                return
            if remaining <= 0:
                PAIRING.deny(req["id"])
                dlg.destroy()
                self.dialog = None
                return
            countdown.config(text=f"Il codice scade tra {remaining} secondi")
            dlg.after(1000, tick, remaining - 1)

        def watch() -> None:
            """Chiude il riquadro appena il telefono conferma il codice."""
            if not dlg.winfo_exists():
                return
            status = (PAIRING.get(req["id"]) or {}).get("status")
            if status != "pending":
                if status == "approved":
                    self._toast("Telefono collegato")
                dlg.destroy()
                self.dialog = None
                return
            dlg.after(300, watch)

        tick(PAIR_TIMEOUT)
        watch()
        dlg.lift()
        dlg.focus_force()
        # Windows non concede il primo piano a un processo in background: lo forziamo
        try:
            winapi.focus(winapi.root_window(dlg.winfo_id()))
        except Exception:  # pragma: no cover - non deve mai bloccare il pairing
            LOG.debug("focus del dialogo non riuscito", exc_info=True)

    def _cancel(self, req_id: str) -> None:
        PAIRING.deny(req_id)
        LOG.info("pairing annullato sul PC: %s", req_id)
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.destroy()
        self.dialog = None

    def quit(self) -> None:
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------- #
# avvio
# --------------------------------------------------------------------------- #
SERVER_PORT = DEFAULT_PORT


def find_port(preferred: int) -> int:
    """Usa la porta preferita; se è occupata da un altro programma, prova le successive."""
    for candidate in [preferred, *range(preferred + 1, preferred + 12)]:
        try:
            test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test.bind(("0.0.0.0", candidate))
            test.close()
            return candidate
        except OSError:
            continue
    raise SystemExit(f"nessuna porta libera tra {preferred} e {preferred + 11}")


def setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    LOG.addHandler(file_handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    LOG.addHandler(console)
    LOG.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    global SERVER_PORT, TEST_MODE
    parser = argparse.ArgumentParser(
        prog="ppremote", description="Telecomando presentazioni per il telefono"
    )
    parser.add_argument("--port", type=int, help="porta HTTP (default: 8787)")
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="solo per i test automatici: rende leggibile il codice di pairing via API",
    )
    args = parser.parse_args(argv)
    TEST_MODE = bool(args.test_mode)
    setup_logging()
    if TEST_MODE:
        LOG.warning("TEST MODE: il codice di pairing è leggibile da /api/pair/code")
    port = find_port(args.port or STORE.port)
    if port != STORE.port and not args.port:
        STORE.port = port
        STORE.save()
    SERVER_PORT = port

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    start_discovery()
    LOG.info("in ascolto su http://%s:%s", lan_ip(), port)

    window = PcWindow(port)
    try:
        window.run()
    finally:
        httpd.shutdown()
        LOG.info("chiuso")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
