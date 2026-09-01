"""FastAPI 앱 — Phase 1: read-only 브라우저/검색/썸네일/PDF."""
import asyncio
import json
import logging
import os
import re

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, events, scanner, thumbs, writer

log = logging.getLogger(__name__)

DEFAULT_ROOT = ""  # 미지정 — 첫 실행 시 폴더 선택 화면


def config_path() -> str:
    return os.path.join(db.app_data_dir(), "config.json")


def _legacy_settings() -> dict:
    """구 앱(PhotoOrganizer) 설정 이어받기 — photo_folder / schedule_folders."""
    p = os.path.join(os.environ.get("APPDATA", ""), "PhotoOrganizer", "settings.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_config() -> dict:
    cfg = {}
    try:
        with open(config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    legacy = _legacy_settings()
    cfg.setdefault("photo_root", legacy.get("photo_folder") or DEFAULT_ROOT)
    cfg.setdefault("schedule_folders", [os.path.normpath(x) for x in legacy.get("schedule_folders", [])])
    return cfg


cfg = load_config()
ROOT = os.environ.get("PHOTO_ROOT") or cfg["photo_root"]
SCHEDULE_FOLDERS: list[str] = cfg["schedule_folders"]

_watchers: list = []
_prewarmer = None


def save_config_file(photo_root: str, schedule_folders: list[str]) -> None:
    data = {"photo_root": photo_root, "schedule_folders": schedule_folders}
    p = config_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def start_services() -> None:
    """스캔 + 감시 + 프리웜 기동 (startup·폴더 변경 공용)."""
    global _prewarmer
    for w in _watchers:
        w.stop()
    _watchers.clear()
    if _prewarmer:
        _prewarmer.stop()
    if not os.path.isdir(ROOT):
        log.error("photo root missing: %s", ROOT)
        return
    scanner.full_scan(ROOT)
    w = scanner.Watcher([ROOT], lambda: scanner.full_scan(ROOT), name="photos")
    w.start()
    _watchers.append(w)
    if SCHEDULE_FOLDERS:
        import threading
        threading.Thread(target=scanner.scan_pdfs, args=(SCHEDULE_FOLDERS,), daemon=True).start()
        w2 = scanner.Watcher(SCHEDULE_FOLDERS, lambda: scanner.scan_pdfs(SCHEDULE_FOLDERS),
                             name="pdfs")
        w2.start()
        _watchers.append(w2)
    _prewarmer = thumbs.Prewarmer(ROOT)
    _prewarmer.start()

app = FastAPI(title="photo-app")


@app.middleware("http")
async def local_origin_guard(request: Request, call_next):
    """localhost 방어: 외부 사이트가 사용자 브라우저를 통해 쏘는 요청 차단."""
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        return JSONResponse({"detail": "bad host"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin.split("//")[-1].split(":")[0] not in ("127.0.0.1", "localhost"):
            return JSONResponse({"detail": "cross-origin write blocked"}, status_code=403)
        sfs = request.headers.get("sec-fetch-site")
        if sfs and sfs not in ("same-origin", "none"):
            return JSONResponse({"detail": "cross-site write blocked"}, status_code=403)
    return await call_next(request)


def require_root() -> None:
    if not os.path.isdir(ROOT):
        raise HTTPException(503, f"사진 루트가 없습니다 (Dropbox 미동기화?): {ROOT}")


def _auto_shutdown_loop() -> None:
    """UI(SSE) 접속이 전부 끊기고 30초 지나면 서버 종료 — 브라우저 닫으면 앱도 끝.

    첫 접속 전 90초 유예 (브라우저 뜨는 시간). PHOTO_APP_PERSIST=1이면 비활성.
    """
    import time as _t
    started = _t.monotonic()
    ever_connected = False
    while True:
        _t.sleep(5)
        if events.client_count() > 0:
            ever_connected = True
            continue
        if not ever_connected:
            if _t.monotonic() - started < 90:
                continue
            log.info("90초간 UI 미접속 — 종료")
            os._exit(0)
        if events.idle_seconds() > 30:
            log.info("UI 종료 감지 (30초 무접속) — 서버 종료")
            os._exit(0)


@app.on_event("startup")
async def startup() -> None:
    events.set_loop(asyncio.get_running_loop())
    db.connect()
    if os.environ.get("PHOTO_APP_PERSIST") != "1":
        import threading
        threading.Thread(target=_auto_shutdown_loop, daemon=True, name="auto-exit").start()
    await asyncio.to_thread(start_services)


# ── 설정: 폴더 지정 (첫 실행 온보딩 + 변경) ─────────────────────────

@app.get("/api/settings")
def get_settings() -> dict:
    return {"photo_root": ROOT, "schedule_folders": SCHEDULE_FOLDERS,
            "root_ok": os.path.isdir(ROOT)}


def _legacy_folder_dialog_ps(initial: str) -> list[str]:
    """구형 FolderBrowserDialog 인라인 명령 (ps1 스크립트가 없을 때 폴백)."""
    ps = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$f.Description = '폴더 선택';"
    )
    if initial and os.path.isdir(initial) and "'" not in initial:
        ps += f"$f.SelectedPath = '{initial}';"
    ps += (
        "$top = New-Object System.Windows.Forms.Form -Property @{TopMost=$true; "
        "WindowState='Minimized'; ShowInTaskbar=$false};"
        "if ($f.ShowDialog($top) -eq 'OK') { Write-Output $f.SelectedPath }"
    )
    return ["powershell", "-STA", "-NoProfile", "-Command", ps]


@app.post("/api/select_folder")
def select_folder(initial: str = Body(default="", embed=True)) -> dict:
    """서버(로컬 PC)에서 네이티브 폴더 선택 다이얼로그 표시 (Windows/macOS)."""
    import subprocess
    import sys
    try:
        if sys.platform == "darwin":
            script = 'POSIX path of (choose folder with prompt "폴더 선택")'
            r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=300)
            path = r.stdout.decode("utf-8", "replace").strip().rstrip("/")
        else:
            script = os.path.join(os.path.dirname(__file__), "folder_dialog.ps1")
            if os.path.isfile(script):
                args = ["powershell", "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", script]
                if initial and os.path.isdir(initial):
                    args += ["-Initial", initial]
            else:  # exe 번들 누락 등 — 구형 다이얼로그로 동작은 보장
                args = _legacy_folder_dialog_ps(initial)
            r = subprocess.run(args, capture_output=True, timeout=300)
            path = r.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        raise HTTPException(500, "폴더 선택 다이얼로그 실패")
    # PowerShell 배너/에러 텍스트가 경로로 새지 않게 — 실제 폴더일 때만 채택
    path = path.splitlines()[-1].strip() if path else ""
    return {"path": path if os.path.isdir(path) else None}


@app.post("/api/settings")
async def set_settings(photo_root: str = Body(embed=True),
                       schedule_folders: list[str] = Body(default=[], embed=True)) -> dict:
    global ROOT, SCHEDULE_FOLDERS
    photo_root = os.path.normpath(photo_root)
    if not os.path.isdir(photo_root):
        raise HTTPException(400, f"사진 폴더가 존재하지 않습니다: {photo_root}")
    schedule_folders = [os.path.normpath(f) for f in schedule_folders if f and os.path.isdir(f)]
    save_config_file(photo_root, schedule_folders)
    ROOT = photo_root
    SCHEDULE_FOLDERS = schedule_folders
    await asyncio.to_thread(start_services)  # 새 폴더로 재스캔·감시 재기동
    return {"photo_root": ROOT, "schedule_folders": SCHEDULE_FOLDERS, "root_ok": True}


@app.get("/api/health")
def health() -> dict:
    conn = db.connect()
    with db.lock:
        last = conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
        counts = conn.execute(
            "SELECT (SELECT COUNT(*) FROM patients) p, (SELECT COUNT(*) FROM folders) f,"
            " (SELECT COUNT(*) FROM files) fi, (SELECT COUNT(*) FROM pdfs) pd"
        ).fetchone()
    return {
        "root": ROOT,
        "root_ok": os.path.isdir(ROOT),
        "schedule_folders": SCHEDULE_FOLDERS,
        "last_scan": int(last["value"]) if last else None,
        "patients": counts["p"],
        "folders": counts["f"],
        "files": counts["fi"],
        "pdfs": counts["pd"],
    }


def _folder_rows_to_tree(conn, patient_ids: list[int] | None = None) -> list[dict]:
    with db.lock:
        where = ""
        args: tuple = ()
        if patient_ids is not None:
            if not patient_ids:
                return []
            where = f" WHERE p.id IN ({','.join('?' * len(patient_ids))})"
            args = tuple(patient_ids)
        patients = conn.execute(
            f"SELECT id, folder_name, patient_num, patient_name FROM patients p{where}"
            " ORDER BY folder_name", args
        ).fetchall()
        fwhere = where.replace("p.id", "f.patient_id") if where else ""
        folders = conn.execute(
            f"""
            SELECT f.id, f.patient_id, f.parent_id, f.name, f.date6, f.is_regular,
                   (SELECT COUNT(*) FROM files fi WHERE fi.folder_id=f.id AND fi.kind IN ('image','raw')) AS n_img,
                   (SELECT COUNT(*) FROM files fi WHERE fi.folder_id=f.id) AS n_all,
                   (SELECT fi.id FROM files fi WHERE fi.folder_id=f.id AND fi.kind IN ('image','raw')
                    ORDER BY fi.name LIMIT 1) AS cover
            FROM folders f{fwhere}
            """,
            args,
        ).fetchall()
        tag_rows = conn.execute(
            f"SELECT t.folder_id, t.tag FROM tags t JOIN folders f ON f.id=t.folder_id{fwhere}"
            " ORDER BY t.position", args
        ).fetchall()
    tags_by_folder: dict[int, list[str]] = {}
    for r in tag_rows:
        tags_by_folder.setdefault(r["folder_id"], []).append(r["tag"])
    by_patient: dict[int, list[dict]] = {}
    children: dict[int, list[dict]] = {}
    for r in folders:
        d = {
            "id": r["id"], "name": r["name"], "date6": r["date6"],
            "is_regular": bool(r["is_regular"]), "tags": tags_by_folder.get(r["id"], []),
            "image_count": r["n_img"], "file_count": r["n_all"], "cover": r["cover"],
            "children": [],
        }
        if r["parent_id"]:
            children.setdefault(r["parent_id"], []).append(d)
        else:
            by_patient.setdefault(r["patient_id"], []).append(d)
    for plist in by_patient.values():
        for d in plist:
            d["children"] = children.get(d["id"], [])
        plist.sort(key=lambda x: (not x["is_regular"], -(int(x["date6"]) if x["date6"] else 0), x["name"]))
    return [
        {
            "id": p["id"], "folder_name": p["folder_name"],
            "num": p["patient_num"], "name": p["patient_name"],
            "folders": by_patient.get(p["id"], []),
        }
        for p in patients
    ]


@app.get("/api/tree")
def tree() -> list[dict]:
    require_root()
    return _folder_rows_to_tree(db.connect())


@app.get("/api/search")
def search(q: str = "") -> list[dict]:
    require_root()
    q = q.strip()
    conn = db.connect()
    if not q:
        return _folder_rows_to_tree(conn)

    # 날짜 검색: 251020 / 20251020 / 2025-10-20 / 25.10.20 → 촬영일(date6) 일치
    compact = re.sub(r"[-./\s]", "", q)
    m = re.fullmatch(r"(?:20)?(\d{6})", compact)
    if m:
        date6 = m.group(1)
        with db.lock:
            ids = [r["patient_id"] for r in conn.execute(
                "SELECT DISTINCT patient_id FROM folders WHERE date6=?", (date6,))]
        if ids:  # 해당 날짜 촬영만 (다른 날짜 폴더는 제외), 없으면 일반 검색 폴백
            tree = _folder_rows_to_tree(conn, ids)
            for p in tree:
                p["folders"] = [f for f in p["folders"] if f["date6"] == date6]
            return [p for p in tree if p["folders"]]

    like = f"%{q}%"
    with db.lock:
        if q == "태그없음":
            ids = [r["id"] for r in conn.execute(
                """SELECT DISTINCT p.id FROM patients p JOIN folders f ON f.patient_id=p.id
                   WHERE f.is_regular=1 AND NOT EXISTS(SELECT 1 FROM tags t WHERE t.folder_id=f.id)"""
            )]
            return _folder_rows_to_tree(conn, ids)
        # 환자 일치(이름·진료번호) = 그 환자의 전체 폴더를 보여준다
        pat_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM patients WHERE folder_name LIKE ?", (like,))}
        # 폴더 일치(폴더명·태그·파일명) = 해당 폴더만 보여준다
        frows = conn.execute(
            """SELECT DISTINCT f.id, f.patient_id FROM folders f
               LEFT JOIN tags t ON t.folder_id = f.id
               LEFT JOIN files fi ON fi.folder_id = f.id
               WHERE f.name LIKE ? OR t.tag LIKE ? OR fi.name LIKE ?""",
            (like, like, like),
        ).fetchall()
    folder_ids = {r["id"] for r in frows}
    tree = _folder_rows_to_tree(conn, sorted(pat_ids | {r["patient_id"] for r in frows}))
    out = []
    for p in tree:
        if p["id"] not in pat_ids:  # 태그 등으로 걸린 환자 → 일치 폴더만 남긴다
            p["folders"] = [f for f in p["folders"]
                            if f["id"] in folder_ids
                            or any(c["id"] in folder_ids for c in f["children"])]
        if p["folders"]:
            out.append(p)
    return out


@app.get("/api/suspicious")
def suspicious_patients() -> dict:
    """진료번호 오타로 갈라진 듯한 환자 폴더 경고 (구 앱 기능 이식).

    case1: 이름이 같은데 번호가 1~2자리만 다름 (오타로 새 폴더가 생긴 정황)
    case2: 번호가 같은데 이름이 다름 (동명이인 오기입·개명 등)
    """
    from itertools import combinations

    conn = db.connect()
    with db.lock:
        rows = conn.execute(
            """SELECT folder_name, patient_num, patient_name FROM patients
               WHERE patient_num IS NOT NULL AND patient_name IS NOT NULL"""
        ).fetchall()
    by_name: dict[str, list] = {}
    by_num: dict[str, list] = {}
    for r in rows:
        by_name.setdefault(r["patient_name"], []).append((r["folder_name"], r["patient_num"]))
        by_num.setdefault(r["patient_num"], []).append((r["folder_name"], r["patient_name"]))

    case1 = []
    for name, items in by_name.items():
        if len(items) < 2:
            continue
        group = set()
        for (f1, n1), (f2, n2) in combinations(items, 2):
            if len(n1) == len(n2):
                dist = sum(1 for a, b in zip(n1, n2) if a != b)
                if 0 < dist <= 2:
                    group |= {f1, f2}
        if group:
            case1.append({"name": name, "folders": sorted(group)})

    case2 = [{"num": num, "folders": sorted(f for f, _ in items)}
             for num, items in by_num.items()
             if len({n for _, n in items}) > 1]

    return {"case1": case1, "case2": sorted(case2, key=lambda x: x["num"])}


@app.get("/api/tags")
def tag_list() -> list[dict]:
    conn = db.connect()
    with db.lock:
        rows = conn.execute(
            "SELECT tag, COUNT(*) n FROM tags GROUP BY tag ORDER BY n DESC, tag"
        ).fetchall()
    return [{"tag": r["tag"], "count": r["n"]} for r in rows]


@app.get("/api/tag_groups")
def tag_groups() -> list[dict]:
    """태그를 super_category별로 묶어 반환 (피커 UI용).

    어휘 = .tag_config.json(구 앱·auto_tag가 관리) ∪ 실제 사용 중 태그.
    """
    from . import tagsort

    cfg = tagsort.load_config(ROOT)
    cats = cfg.get("super_categories", {})
    conn = db.connect()
    with db.lock:
        counts = {r["tag"]: r["n"] for r in conn.execute(
            "SELECT tag, COUNT(*) n FROM tags GROUP BY tag")}
    groups: dict[str, list] = {}
    for tag in set(cfg.get("tags", {})) | set(counts):
        if tag.startswith("@"):
            continue  # 상태라벨(@Annotated 등)은 태그 피커에서 제외
        cat = tagsort.category_of(tag, cfg)
        groups.setdefault(cat, []).append({"tag": tag, "count": counts.get(tag, 0)})
    out = []
    for order, name in enumerate(tagsort.cat_order(cfg)):
        tags = groups.pop(name, [])
        if not tags:
            continue
        if name == "치식":
            tags.sort(key=lambda x: tagsort.tooth_key(x["tag"]))  # #10 #20 #30 #40 → #18~#11 …
        else:
            tags.sort(key=lambda x: (-x["count"], x["tag"]))       # 많이 쓴 순
        meta = cats.get(name, {})
        out.append({"name": name, "color": meta.get("color", "#ECECEC"),
                    "order": order, "tags": tags})
    return out


@app.post("/api/tag_category")
def set_tag_category(tag: str = Body(embed=True), category: str = Body(embed=True)) -> dict:
    """태그의 카테고리 지정 (.tag_config.json 갱신 — 구 앱과 공유)."""
    from . import tagsort
    require_root()
    try:
        tagsort.set_category(ROOT, tag, category)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"tag": tag, "category": category}


@app.post("/api/tag_delete")
async def tag_delete(tag: str = Body(embed=True)) -> dict:
    """태그를 어휘 + 사용 중인 모든 폴더에서 제거 (저널로 undo 가능)."""
    require_root()
    return await asyncio.to_thread(_write, writer.delete_tag, ROOT, tag)


@app.post("/api/tag_rename")
async def tag_rename(tag: str = Body(embed=True), new: str = Body(embed=True)) -> dict:
    """태그 이름 변경 — new가 기존 태그면 병합 (저널로 undo 가능)."""
    require_root()
    return await asyncio.to_thread(_write, writer.rename_tag, ROOT, tag, new)


@app.get("/api/folder/{folder_id}/files")
def folder_files(folder_id: int) -> list[dict]:
    conn = db.connect()
    with db.lock:
        rows = conn.execute(
            "SELECT id, name, size, mtime_ns, kind FROM files WHERE folder_id=? ORDER BY name",
            (folder_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _file_paths(file_id: int) -> tuple[str, str, str]:
    """file_id -> (abs_path, rel_path, kind). 없으면 404."""
    conn = db.connect()
    with db.lock:
        r = conn.execute(
            """
            SELECT fi.name AS file_name, fi.kind, f.name AS folder_name, f2.name AS parent_name,
                   p.folder_name AS patient_folder
            FROM files fi
            JOIN folders f ON f.id = fi.folder_id
            LEFT JOIN folders f2 ON f2.id = f.parent_id
            JOIN patients p ON p.id = f.patient_id
            WHERE fi.id = ?
            """,
            (file_id,),
        ).fetchone()
    if r is None:
        raise HTTPException(404, "unknown file id (index stale? rescan)")
    parts = [r["patient_folder"]]
    if r["parent_name"]:
        parts.append(r["parent_name"])
    parts.append(r["folder_name"])
    parts.append(r["file_name"])
    rel = "/".join(parts)
    return os.path.join(ROOT, *parts), rel, r["kind"]


@app.get("/api/thumb/{file_id}")
def thumb(file_id: int):
    abs_path, rel, kind = _file_paths(file_id)
    if kind == "video":
        raise HTTPException(415, "no video thumbs yet")
    out = thumbs.ensure_thumb(abs_path, rel)
    if out is None:
        raise HTTPException(404, "thumbnail unavailable")
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/image/{file_id}")
def image(file_id: int, full: int = 0):
    abs_path, rel, kind = _file_paths(file_id)
    if not os.path.exists(abs_path):
        raise HTTPException(404, "file moved on disk (rescan pending)")
    if kind == "raw":
        if full:
            return FileResponse(abs_path)  # 명시적 원본 다운로드만 RAW 그대로
        out = thumbs.ensure_preview(abs_path, rel)
        if out is None:
            raise HTTPException(502, "RAW preview 생성 실패")
        return FileResponse(out, media_type="image/jpeg")
    return FileResponse(abs_path)


# ── 새 사진 추가 (import 마법사) ──────────────────────────────────

@app.get("/api/import/session")
def import_session() -> dict:
    from . import importer
    s = importer.get_session()
    return s or {"status": "none"}


@app.post("/api/import/start")
async def import_start(folder: str = Body(embed=True)) -> dict:
    require_root()
    from . import importer
    try:
        return await asyncio.to_thread(importer.start, folder)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))


@app.post("/api/import/discard")
def import_discard() -> dict:
    from . import importer
    importer.discard()
    return {"status": "none"}


def _import_item_path(idx: int) -> str:
    from . import importer
    s = importer.get_session()
    if not s or not (0 <= idx < len(s["items"])):
        raise HTTPException(404)
    # 항목 인덱스 기반 — 임의 경로 입력 자체가 없음 (경로 주입 차단)
    p = os.path.normpath(os.path.join(s["folder"], s["items"][idx]["name"]))
    if not p.startswith(os.path.normpath(s["folder"])):
        raise HTTPException(403)
    return p


@app.get("/api/import/thumb/{idx}")
def import_thumb(idx: int):
    p = _import_item_path(idx)
    out = thumbs.ensure_thumb(p, f"import/{idx}/{os.path.basename(p)}")
    if out is None:
        raise HTTPException(404)
    return FileResponse(out, media_type="image/jpeg")


@app.get("/api/import/image/{idx}")
def import_image(idx: int):
    p = _import_item_path(idx)
    if p.lower().endswith((".nef", ".cr2", ".arw", ".dng")):
        out = thumbs.ensure_preview(p, f"import/{idx}/{os.path.basename(p)}")
        if out:
            return FileResponse(out, media_type="image/jpeg")
        raise HTTPException(502)
    return FileResponse(p)


@app.post("/api/import/group/{gid}")
def import_update_group(gid: int, num: str | None = Body(default=None),
                        name: str | None = Body(default=None),
                        date6: str | None = Body(default=None),
                        enabled: bool | None = Body(default=None)) -> dict:
    from . import importer
    try:
        return importer.update_group(gid, {"num": num, "name": name,
                                           "date6": date6, "enabled": enabled})
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/import/item/{idx}")
async def import_item_action(idx: int, action: str = Body(embed=True)) -> dict:
    from . import importer
    try:
        await asyncio.to_thread(importer.item_action, idx, action)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return importer.get_session() or {}


@app.post("/api/import/merge")
async def import_merge(src: int = Body(embed=True), dst: int = Body(embed=True)) -> dict:
    """그룹 src를 dst로 병합 (드래그&드롭)."""
    from . import importer
    try:
        await asyncio.to_thread(importer.merge_groups, src, dst)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return importer.get_session() or {}


@app.post("/api/import/item/{idx}/move")
async def import_item_move(idx: int, gid: int = Body(embed=True)) -> dict:
    """사진을 다른 그룹으로 이동 (드래그&드롭)."""
    from . import importer
    try:
        await asyncio.to_thread(importer.move_item, idx, gid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return importer.get_session() or {}


@app.post("/api/import/new_group")
async def import_new_group() -> dict:
    """빈 수동 묶음 추가 (➕ 새 묶음)."""
    from . import importer
    await asyncio.to_thread(importer.new_group)
    return importer.get_session() or {}


@app.post("/api/import/commit")
async def import_commit(dry_run: bool = Body(default=False, embed=True)) -> dict:
    require_root()
    from . import importer
    try:
        result = await asyncio.to_thread(importer.commit, ROOT, dry_run)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))
    if not dry_run:
        await asyncio.to_thread(scanner.full_scan, ROOT)
    return result


@app.get("/api/patient_name/{num}")
def patient_name(num: str) -> dict:
    from . import importer
    return {"num": num, "name": importer.lookup_name(num)}


# ── 스케줄 PDF ────────────────────────────────────────────────────

@app.get("/api/patient/{patient_num}/pdfs")
def patient_pdfs(patient_num: str, date6: str = "") -> list[dict]:
    """환자번호가 등장하는 스케줄 PDF 목록.

    date6(YYMMDD) 주어지면 차트 기록 위치 우선순위로 정렬:
    다음 내원(그날 처치가 기록된 차트) > 당일 > 이전.
    """
    conn = db.connect()
    with db.lock:
        rows = conn.execute(
            """SELECT h.pdf_id, h.page, d.filename, d.date8
               FROM pdf_hits h JOIN pdfs d ON d.id=h.pdf_id
               WHERE h.patient_num=? ORDER BY d.date8 DESC""",
            (patient_num,),
        ).fetchall()
    hits = [dict(r) for r in rows]
    if date6 and len(date6) == 6:
        target = "20" + date6
        for h in hits:
            d8 = h["date8"] or ""
            h["kind"] = "다음 내원" if d8 > target else ("당일" if d8 == target else "이전")
        after = sorted([h for h in hits if (h["date8"] or "") > target], key=lambda x: x["date8"])
        same = [h for h in hits if (h["date8"] or "") == target]
        before = sorted([h for h in hits if (h["date8"] or "") < target],
                        key=lambda x: x["date8"] or "", reverse=True)
        hits = after + same + before
    else:
        for h in hits:
            h["kind"] = "최신순"
    return hits


@app.get("/api/pdf/{pdf_id}")
def serve_pdf(pdf_id: int):
    conn = db.connect()
    with db.lock:
        r = conn.execute("SELECT path, filename FROM pdfs WHERE id=?", (pdf_id,)).fetchone()
    if r is None:
        raise HTTPException(404)
    path = r["path"]
    if not any(os.path.normpath(path).startswith(os.path.normpath(f) + os.sep) or
               os.path.normpath(os.path.dirname(path)) == os.path.normpath(f)
               for f in SCHEDULE_FOLDERS):
        raise HTTPException(403, "path outside schedule folders")
    if not os.path.exists(path):
        raise HTTPException(404, "pdf moved on disk")
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": "inline"})


@app.post("/api/pdfs/rescan")
async def pdfs_rescan() -> dict:
    return await asyncio.to_thread(scanner.scan_pdfs, SCHEDULE_FOLDERS)


@app.post("/api/rescan")
async def rescan() -> dict:
    require_root()
    return await asyncio.to_thread(scanner.full_scan, ROOT)


@app.post("/api/cleanup_thumbnails")
async def cleanup_thumbnails(dry_run: bool = Body(default=True, embed=True)) -> dict:
    """구버전 앱이 원본 옆에 만든 thumbnail_*.jpg 잔재 삭제.

    dry_run=true = 개수만 집계. 실제 삭제는 영구 삭제(저널 없음)라 프론트에서 확인 후 호출.
    """
    require_root()

    def _run() -> dict:
        targets = []
        for base, _dirs, files in os.walk(ROOT):
            for n in files:
                if n.startswith("thumbnail_") and n.lower().endswith(".jpg"):
                    targets.append(os.path.join(base, n))
        if dry_run:
            return {"found": len(targets), "deleted": 0}
        deleted = 0
        with writer.write_lock, db.fs_lock, db.ProcessLock():
            for p in targets:
                try:
                    os.remove(p)
                    deleted += 1
                except OSError:
                    log.warning("썸네일 삭제 실패: %s", p)
        return {"found": len(targets), "deleted": deleted}

    return await asyncio.to_thread(_run)


# ── 쓰기 (Phase 2): 모든 파일시스템 변경은 writer 경유 ─────────────

def _write(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except writer.WriteError as e:
        raise HTTPException(409, str(e))
    except PermissionError:
        raise HTTPException(423, "파일이 잠겨 있습니다 (Dropbox/탐색기 사용 중) — 잠시 후 재시도")


@app.post("/api/folder/{folder_id}/rename")
async def rename_folder(folder_id: int, new_name: str = Body(embed=True)) -> dict:
    require_root()
    return await asyncio.to_thread(_write, writer.rename_folder, ROOT, folder_id, new_name)


@app.post("/api/patient/{patient_id}/rename")
async def rename_patient(patient_id: int, num: str = Body(embed=True),
                         name: str = Body(embed=True)) -> dict:
    """환자 폴더 진료번호·이름 수정 (오타 교정)."""
    require_root()
    return await asyncio.to_thread(_write, writer.rename_patient, ROOT, patient_id, num, name)


@app.post("/api/patients/merge")
async def merge_patients(src: int = Body(embed=True), dst: int = Body(embed=True),
                         dry_run: bool = Body(default=False, embed=True)) -> dict:
    """환자 폴더 합치기 — src의 촬영일 폴더를 dst로 이동 (dry_run이면 계획만)."""
    require_root()
    return await asyncio.to_thread(_write, writer.merge_patients, ROOT, src, dst, dry_run)


@app.post("/api/folder/{folder_id}/date")
async def set_folder_date(folder_id: int, date6: str = Body(embed=True)) -> dict:
    """촬영일(YYMMDD) 수정 — 태그는 유지."""
    require_root()
    return await asyncio.to_thread(_write, writer.set_folder_date, ROOT, folder_id, date6)


@app.post("/api/folder/{folder_id}/tags")
async def edit_tags(folder_id: int, add: list[str] = Body(default=[]),
                    remove: list[str] = Body(default=[])) -> dict:
    require_root()
    return await asyncio.to_thread(_write, writer.edit_tags, ROOT, folder_id, add, remove)


@app.get("/api/journal")
def journal() -> list[dict]:
    entries = writer.list_journal()
    for e in entries:  # 경로 전체 대신 폴더명만 노출 (UI 표시용)
        e["renames"] = [[os.path.basename(o), os.path.basename(n)] for o, n in e.get("renames", [])]
    return entries


@app.post("/api/undo/{run_id}")
async def undo(run_id: str) -> dict:
    require_root()
    return await asyncio.to_thread(_write, writer.undo, ROOT, run_id)


@app.post("/api/open_folder/{folder_id}")
def open_folder(folder_id: int) -> dict:
    """탐색기로 폴더 열기 (read-only 단계의 유일한 OS 액션)."""
    conn = db.connect()
    with db.lock:
        r = conn.execute(
            """SELECT f.name, f2.name AS parent_name, p.folder_name AS patient_folder
               FROM folders f LEFT JOIN folders f2 ON f2.id=f.parent_id
               JOIN patients p ON p.id=f.patient_id WHERE f.id=?""",
            (folder_id,),
        ).fetchone()
    if r is None:
        raise HTTPException(404)
    parts = [r["patient_folder"]] + ([r["parent_name"]] if r["parent_name"] else []) + [r["name"]]
    path = os.path.join(ROOT, *parts)
    if not os.path.isdir(path):
        raise HTTPException(404, "folder moved on disk")
    import sys
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 — 로컬 단일 사용자 앱
    else:
        import subprocess
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
    return {"opened": path}


@app.get("/api/events")
async def sse():
    q = events.subscribe()

    async def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


import sys as _sys

# PyInstaller frozen 빌드에서는 번들 데이터 경로(_MEIPASS) 기준
_BASE = getattr(_sys, "_MEIPASS",
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIST = os.path.join(_BASE, "frontend", "dist")
if os.path.isdir(DIST):
    app.mount("/", StaticFiles(directory=DIST, html=True), name="static")
else:
    @app.get("/")
    def no_frontend() -> JSONResponse:
        return JSONResponse({"status": "backend ok", "note": "frontend/dist 없음 — npm run build 필요"})
