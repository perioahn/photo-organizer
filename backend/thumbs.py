"""썸네일/프리뷰: %LOCALAPPDATA% 캐시, 온디맨드 생성 + 백그라운드 프리웜.

키 = sha1(relpath|size|mtime_ns). 쓰기는 temp → os.replace라 부분 파일 서빙 불가능.
"""
import hashlib
import io
import logging
import os
import tempfile
import threading
import time

from PIL import Image, ImageOps

from . import db, events

log = logging.getLogger(__name__)

THUMB_SIZE = 400
PREVIEW_SIZE = 2000

_gen_locks: dict[str, threading.Lock] = {}
_gen_locks_guard = threading.Lock()


def cache_dir() -> str:
    d = os.path.join(db.app_data_dir(), "thumbs")
    os.makedirs(d, exist_ok=True)
    return d


def cache_key(rel_path: str, size: int, mtime_ns: int, variant: str) -> str:
    h = hashlib.sha1(f"{rel_path}|{size}|{mtime_ns}|{variant}".encode("utf-8")).hexdigest()
    return h


def cache_path(key: str) -> str:
    d = os.path.join(cache_dir(), key[:2])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, key + ".jpg")


def _load_image(abs_path: str) -> Image.Image:
    ext = os.path.splitext(abs_path)[1].lower()
    if ext in (".nef", ".cr2", ".arw", ".dng"):
        import rawpy

        with rawpy.imread(abs_path) as raw:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    return Image.open(io.BytesIO(thumb.data)).convert("RGB")
            except Exception:
                pass
            rgb = raw.postprocess(half_size=True, use_camera_wb=True)
            return Image.fromarray(rgb)
    img = Image.open(abs_path)
    return ImageOps.exif_transpose(img).convert("RGB")


def ensure_variant(abs_path: str, rel_path: str, max_px: int, variant: str) -> str | None:
    """캐시에 있으면 경로 반환, 없으면 생성. 실패 시 None."""
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    out = cache_path(cache_key(rel_path, st.st_size, st.st_mtime_ns, variant))
    if os.path.exists(out):
        return out
    # 같은 파일 동시 생성 방지 (탐색 중 그리드가 같은 썸네일 여러 번 요청)
    with _gen_locks_guard:
        flock = _gen_locks.setdefault(out, threading.Lock())
    with flock:
        if os.path.exists(out):
            return out
        try:
            img = _load_image(abs_path)
            img.thumbnail((max_px, max_px), Image.LANCZOS)
            fd, tmp = tempfile.mkstemp(suffix=".jpg", dir=os.path.dirname(out))
            try:
                with os.fdopen(fd, "wb") as f:
                    img.save(f, "JPEG", quality=85)
                os.replace(tmp, out)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return out
        except Exception:
            log.warning("thumbnail failed: %s", abs_path, exc_info=True)
            return None
        finally:
            with _gen_locks_guard:
                _gen_locks.pop(out, None)


def ensure_thumb(abs_path: str, rel_path: str) -> str | None:
    return ensure_variant(abs_path, rel_path, THUMB_SIZE, "t")


def ensure_preview(abs_path: str, rel_path: str) -> str | None:
    return ensure_variant(abs_path, rel_path, PREVIEW_SIZE, "p")


class Prewarmer:
    """정규 폴더 최신 날짜순으로 썸네일 미리 생성. 온디맨드가 항상 우선이라 없어도 동작."""

    def __init__(self, root: str, workers: int = 2):
        self.root = root
        self.workers = workers
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="prewarm").start()

    def stop(self) -> None:
        self._stop.set()

    def _pending(self) -> list[tuple[str, str]]:
        conn = db.connect()
        with db.lock:
            rows = conn.execute(
                """
                SELECT p.folder_name, f2.name AS parent_name, f.name AS folder_name2,
                       fi.name AS file_name, fi.size, fi.mtime_ns
                FROM files fi
                JOIN folders f ON f.id = fi.folder_id
                LEFT JOIN folders f2 ON f2.id = f.parent_id
                JOIN patients p ON p.id = f.patient_id
                WHERE fi.kind IN ('image','raw')
                ORDER BY f.is_regular DESC, f.date6 DESC
                """
            ).fetchall()
        out = []
        for r in rows:
            parts = [r["folder_name"]]
            if r["parent_name"]:
                parts.append(r["parent_name"])
            parts.append(r["folder_name2"])
            parts.append(r["file_name"])
            rel = "/".join(parts)
            key = cache_key(rel, r["size"], r["mtime_ns"], "t")
            if not os.path.exists(cache_path(key)):
                out.append((os.path.join(self.root, *parts), rel))
        return out

    def _run(self) -> None:
        time.sleep(2)  # 서버 기동·첫 스캔에 CPU 양보
        pending = self._pending()
        total = len(pending)
        if not total:
            return
        events.publish("prewarm", {"state": "start", "total": total})
        done = 0
        idx = threading.Lock()
        it = iter(pending)

        def worker():
            nonlocal done
            while not self._stop.is_set():
                with idx:
                    item = next(it, None)
                if item is None:
                    return
                ensure_thumb(*item)
                with idx:
                    done += 1
                    if done % 50 == 0 or done == total:
                        events.publish("prewarm", {"state": "progress", "done": done, "total": total})
                time.sleep(0.02)  # CPU 양보

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        events.publish("prewarm", {"state": "done", "done": done, "total": total})
