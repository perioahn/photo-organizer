"""파일시스템 → SQLite 인덱스 스캐너 + watchfiles 감시.

설계 원칙:
- 디스크 walk는 DB 락 없이 스냅샷 수집 → DB 쓰기는 짧은 트랜잭션으로 분리
- 모든 entry 접근은 per-entry OSError 허용 (Dropbox가 옮기는 중이어도 스캔 계속)
- upsert + gen 세대표식으로 id 보존 (프론트가 든 id가 다른 파일을 가리키지 않게)
- symlink/junction 미추적 (ROOT 밖 유출 방지)
"""
import logging
import os
import re
import threading
import time

from . import db, events

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
RAW_EXT = {".nef", ".cr2", ".arw", ".dng"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mts"}

PATIENT_RE = re.compile(r"^(\d{8})_(.+)$")
DATE6_RE = re.compile(r"^(\d{6})(?:_(.*))?$")
NUM8_RE = re.compile(r"\d{8}")

SKIP_DIRS = {"_auto_tag_journal", "__pycache__", ".dropbox.cache"}


def parse_patient(name: str) -> tuple[str | None, str]:
    m = PATIENT_RE.match(name)
    if m:
        return m.group(1), m.group(2)
    return None, name


def parse_b_folder(name: str) -> tuple[str | None, list[str]]:
    """'250116_A_GTR_#36' -> ('250116', ['A','GTR','#36']). 비정규면 (None, [])."""
    m = DATE6_RE.match(name)
    if not m:
        return None, []
    tags = [t for t in (m.group(2) or "").split("_") if t]
    return m.group(1), tags


def classify_file(name: str) -> str | None:
    if name.startswith("thumbnail_") or name.startswith("."):
        return None  # 구 앱이 폴더 안에 심어둔 썸네일은 인덱싱 제외
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in RAW_EXT:
        return "raw"
    if ext in VIDEO_EXT:
        return "video"
    return None


def _safe_dirs(path: str) -> list:
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError:
        return []
    out = []
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS and not e.name.startswith("."):
                out.append(e)
        except OSError:
            continue
    return out


def _safe_files(path: str) -> list[tuple[str, int, int]]:
    """[(name, size, mtime_ns)] — 사라지는 중인 파일은 스킵."""
    try:
        entries = list(os.scandir(path))
    except OSError:
        return []
    out = []
    for e in entries:
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            st = e.stat()
            out.append((e.name, st.st_size, st.st_mtime_ns))
        except OSError:
            continue
    return sorted(out)


def _walk_snapshot(root: str) -> list[dict]:
    """DB 락 없이 전체 트리 스냅샷 수집."""
    snap = []
    for a in _safe_dirs(root):
        num, pname = parse_patient(a.name)
        pat = {"folder_name": a.name, "num": num, "name": pname, "folders": []}
        for b in _safe_dirs(a.path):
            date6, tags = parse_b_folder(b.name)
            folder = {
                "name": b.name, "date6": date6, "tags": tags,
                "files": [(n, s, m, classify_file(n)) for n, s, m in _safe_files(b.path)],
                "children": [],
            }
            for sub in _safe_dirs(b.path):
                folder["children"].append({
                    "name": sub.name,
                    "files": [(n, s, m, classify_file(n)) for n, s, m in _safe_files(sub.path)],
                })
            pat["folders"].append(folder)
        snap.append(pat)
    return snap


def _upsert_folder(cur, patient_id: int, parent_id: int, name: str,
                   date6: str | None, is_regular: int, gen: int) -> int:
    return cur.execute(
        """INSERT INTO folders(patient_id, parent_id, name, date6, is_regular, gen)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(patient_id, parent_id, name)
           DO UPDATE SET date6=excluded.date6, is_regular=excluded.is_regular, gen=excluded.gen
           RETURNING id""",
        (patient_id, parent_id, name, date6, is_regular, gen),
    ).fetchone()[0]


def _upsert_files(cur, folder_id: int, files: list, gen: int) -> int:
    n = 0
    for name, size, mtime_ns, kind in files:
        if kind is None:
            continue
        cur.execute(
            """INSERT INTO files(folder_id, name, size, mtime_ns, kind, gen) VALUES(?,?,?,?,?,?)
               ON CONFLICT(folder_id, name)
               DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns,
                             kind=excluded.kind, gen=excluded.gen""",
            (folder_id, name, size, mtime_ns, kind, gen),
        )
        n += 1
    return n


def full_scan(root: str) -> dict:
    t0 = time.time()
    with db.fs_lock:  # 스캔(walk+DB쓰기) 동안 리네임 배타 — 스테일 스냅샷 덮어쓰기 방지
        return _full_scan_locked(root, t0)


def _full_scan_locked(root: str, t0: float) -> dict:
    snap = _walk_snapshot(root)
    conn = db.connect()
    n_pat = n_folder = n_file = 0
    with db.lock:
        cur = conn.cursor()
        cur.execute("BEGIN")
        try:
            gen = db.next_gen(conn, "scan_gen")
            for pat in snap:
                patient_id = cur.execute(
                    """INSERT INTO patients(folder_name, patient_num, patient_name, gen)
                       VALUES(?,?,?,?)
                       ON CONFLICT(folder_name)
                       DO UPDATE SET patient_num=excluded.patient_num,
                                     patient_name=excluded.patient_name, gen=excluded.gen
                       RETURNING id""",
                    (pat["folder_name"], pat["num"], pat["name"], gen),
                ).fetchone()[0]
                n_pat += 1
                for f in pat["folders"]:
                    folder_id = _upsert_folder(
                        cur, patient_id, 0, f["name"], f["date6"], 1 if f["date6"] else 0, gen)
                    n_folder += 1
                    cur.execute("DELETE FROM tags WHERE folder_id=?", (folder_id,))
                    for pos, t in enumerate(f["tags"]):
                        cur.execute("INSERT INTO tags(folder_id, tag, position) VALUES(?,?,?)",
                                    (folder_id, t, pos))
                    n_file += _upsert_files(cur, folder_id, f["files"], gen)
                    for c in f["children"]:
                        cid = _upsert_folder(cur, patient_id, folder_id, c["name"], None, 0, gen)
                        n_folder += 1
                        n_file += _upsert_files(cur, cid, c["files"], gen)
            # 이번 세대에 안 보인 행 = 디스크에서 사라짐 → 삭제 (cascade)
            cur.execute("DELETE FROM patients WHERE gen<>?", (gen,))
            cur.execute("DELETE FROM folders WHERE gen<>?", (gen,))
            cur.execute("DELETE FROM files WHERE gen<>?", (gen,))
            cur.execute("INSERT OR REPLACE INTO meta VALUES('last_scan', ?)", (str(int(time.time())),))
            cur.execute("INSERT OR REPLACE INTO meta VALUES('root_path', ?)", (root,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    stats = {"patients": n_pat, "folders": n_folder, "files": n_file,
             "seconds": round(time.time() - t0, 2)}
    log.info("full scan done: %s", stats)
    events.publish("index", {"state": "refreshed", **stats})
    return stats


# ── 스케줄 PDF 인덱스 ──────────────────────────────────────────────

def scan_pdfs(schedule_folders: list[str]) -> dict:
    """PDF 목록 diff 후 새/변경 파일만 텍스트 추출 (페이지별 8자리 진료번호)."""
    t0 = time.time()
    found: dict[str, tuple[str, str | None, int, int]] = {}
    for folder in schedule_folders:
        if not os.path.isdir(folder):
            continue
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if not fn.lower().endswith(".pdf"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                m = re.match(r"(\d{8})", fn)
                found[p] = (fn, m.group(1) if m else None, st.st_size, st.st_mtime_ns)

    conn = db.connect()
    with db.lock:
        existing = {r["path"]: r for r in conn.execute("SELECT id, path, size, mtime_ns FROM pdfs")}
    n_new = 0
    for path, (fn, date8, size, mtime_ns) in found.items():
        old = existing.get(path)
        if old and old["size"] == size and old["mtime_ns"] == mtime_ns:
            continue
        hits = _extract_pdf_hits(path)  # 락 밖에서 파싱 (PDF당 수십 ms)
        with db.lock:
            cur = conn.cursor()
            cur.execute("BEGIN")
            try:
                pdf_id = cur.execute(
                    """INSERT INTO pdfs(path, filename, date8, size, mtime_ns) VALUES(?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET filename=excluded.filename,
                           date8=excluded.date8, size=excluded.size, mtime_ns=excluded.mtime_ns
                       RETURNING id""",
                    (path, fn, date8, size, mtime_ns),
                ).fetchone()[0]
                cur.execute("DELETE FROM pdf_hits WHERE pdf_id=?", (pdf_id,))
                cur.executemany(
                    "INSERT INTO pdf_hits(pdf_id, page, patient_num) VALUES(?,?,?)",
                    [(pdf_id, page, num) for page, num in hits],
                )
                conn.commit()
                n_new += 1
            except BaseException:
                conn.rollback()
                raise
    removed = set(existing) - set(found)
    if removed:
        with db.lock:
            conn.executemany("DELETE FROM pdfs WHERE path=?", [(p,) for p in removed])
            conn.commit()
    stats = {"pdfs": len(found), "indexed": n_new, "removed": len(removed),
             "seconds": round(time.time() - t0, 2)}
    log.info("pdf scan done: %s", stats)
    if n_new or removed:
        events.publish("pdf_index", {"state": "refreshed", **stats})
    return stats


def _extract_pdf_hits(path: str) -> list[tuple[int, str]]:
    """[(page 1-indexed, patient_num)]"""
    import fitz

    out = []
    try:
        with fitz.open(path) as doc:
            for i, page in enumerate(doc):
                nums = set(NUM8_RE.findall(page.get_text()))
                out.extend((i + 1, n) for n in nums)
    except Exception:
        log.warning("pdf parse failed: %s", path, exc_info=True)
    return out


class Watcher:
    """watchfiles 기반 변경 감시 → 디바운스 후 rescan 콜백."""

    def __init__(self, paths: list[str], on_change, debounce_sec: float = 3.0, name: str = "fs"):
        self.paths = [p for p in paths if os.path.isdir(p)]
        self.on_change = on_change
        self.debounce_sec = debounce_sec
        self.name = name
        self._stop = threading.Event()
        self._dirty = threading.Event()

    def start(self) -> None:
        if not self.paths:
            return
        threading.Thread(target=self._run, daemon=True, name=f"watch-{self.name}").start()
        threading.Thread(target=self._rescan_loop, daemon=True, name=f"rescan-{self.name}").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from watchfiles import watch

        try:
            for changes in watch(*self.paths, stop_event=self._stop, debounce=1600):
                if changes:
                    self._dirty.set()
        except Exception:
            log.exception("watcher(%s) died; manual rescan only", self.name)

    def _rescan_loop(self) -> None:
        while not self._stop.is_set():
            if self._dirty.wait(timeout=0.5):
                time.sleep(self.debounce_sec)  # Dropbox 연속 변경 흡수
                self._dirty.clear()
                try:
                    self.on_change()
                except Exception:
                    log.exception("rescan(%s) failed", self.name)
