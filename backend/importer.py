"""새 사진 추가(import) 세션 — 분류→OCR→그룹→리뷰→커밋.

정책 (Codex 자문 확정):
- 분류는 EXIF 조리개 < 20 = 정보사진(모니터 촬영) 단일 규칙. 오분류는 리뷰 UI에서 수정.
- OCR = 로컬 RapidOCR (진료번호 8자리만, 실측 6/6·0.9s/장). 이름은 인덱스 조회 > 수동 입력.
- 리뷰 확정 전에는 아무것도 복사하지 않음. 커밋은 COPY 전용(원본 불변), 저널로 undo.
- 단일 활성 세션, %LOCALAPPDATA% JSON 저장 → 앱 재시작 후 이어하기.
"""
import json
import logging
import os
import re
import shutil
import threading
import time

from . import db, events

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".nef", ".cr2", ".arw", ".dng"}
APERTURE_INFO_MAX = 20  # 구 파이프라인 검증된 임계값: f<20 = 모니터 촬영
NUM_RE = re.compile(r"\d{8}")

_lock = threading.RLock()
_session: dict | None = None
_ocr = None


def session_path() -> str:
    return os.path.join(db.app_data_dir(), "import_session.json")


def get_session() -> dict | None:
    global _session
    with _lock:
        if _session is None and os.path.exists(session_path()):
            try:
                with open(session_path(), encoding="utf-8") as f:
                    _session = json.load(f)
            except (OSError, ValueError):
                _session = None
        return _session


def _save() -> None:
    with _lock:
        if _session is None:
            try:
                os.remove(session_path())
            except OSError:
                pass
            return
        tmp = session_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_session, f, ensure_ascii=False)
        os.replace(tmp, session_path())


def discard() -> None:
    global _session
    with _lock:
        _session = None
        _save()


def _read_meta(path: str) -> tuple[float | None, float]:
    """(aperture, timestamp). exifread는 JPG/NEF(TIFF) 모두 처리."""
    aperture = None
    ts = None
    try:
        import exifread
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
        fn = tags.get("EXIF FNumber")
        if fn:
            v = fn.values[0]
            aperture = float(v.num) / float(v.den or 1)
        dt = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
        if dt:
            ts = time.mktime(time.strptime(str(dt), "%Y:%m:%d %H:%M:%S"))
    except Exception:
        pass
    if ts is None:
        ts = os.path.getmtime(path)
    return aperture, ts


def _ocr_number(abs_path: str, rel: str) -> tuple[str | None, str, str]:
    """정보사진에서 8자리 진료번호 추출. (num, raw_text, source).

    배포판: 로컬 RapidOCR 전용 (클라우드 전송·자격증명 없음).
    인식 실패는 리뷰 화면에서 수동 입력.
    """
    global _ocr
    from . import thumbs
    src = thumbs.ensure_preview(abs_path, "import/" + rel) or abs_path
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    try:
        result, _ = _ocr(src)
        text = " ".join(x[1] for x in (result or []))
    except Exception:
        log.warning("local ocr failed: %s", abs_path, exc_info=True)
        text = ""
    m = NUM_RE.search(text)
    if m:
        return m.group(), text[:200], "local"
    return None, text[:200], "none"


def lookup_name(num: str) -> str | None:
    conn = db.connect()
    with db.lock:
        r = conn.execute("SELECT patient_name FROM patients WHERE patient_num=?", (num,)).fetchone()
    return r["patient_name"] if r else None


def _date6(ts: float) -> str:
    return time.strftime("%y%m%d", time.localtime(ts))


def start(folder: str) -> dict:
    global _session
    if not os.path.isdir(folder):
        raise ValueError(f"폴더가 없습니다: {folder}")
    with _lock:
        s = get_session()
        if s and s.get("status") in ("scanning", "review", "committing"):
            raise RuntimeError("진행 중인 가져오기 세션이 있습니다 — 이어하거나 폐기하세요")
        _session = {"folder": folder, "status": "scanning", "items": [], "groups": [],
                    "progress": {"done": 0, "total": 0}, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _save()
    threading.Thread(target=_scan, args=(folder,), daemon=True, name="import-scan").start()
    return _session


def _scan(folder: str) -> None:
    global _session
    try:
        files = []
        for n in sorted(os.listdir(folder)):
            p = os.path.join(folder, n)
            if os.path.isfile(p) and os.path.splitext(n)[1].lower() in IMAGE_EXT \
                    and not n.startswith("thumbnail"):
                files.append(n)
        items = []
        for n in files:
            ap, ts = _read_meta(os.path.join(folder, n))
            kind = "info" if (ap is not None and ap < APERTURE_INFO_MAX) else "clinical"
            items.append({"name": n, "aperture": ap, "ts": ts, "kind": kind,
                          "num": None, "ocr_text": ""})
        items.sort(key=lambda x: x["ts"])
        n_info = sum(1 for i in items if i["kind"] == "info")
        with _lock:
            _session["items"] = items
            _session["progress"] = {"done": 0, "total": n_info}
            _save()
        events.publish("import", {"state": "classified", "files": len(items), "info": n_info})
        for idx, it in enumerate(items):
            if it["kind"] != "info":
                continue
            num, text, src = _ocr_number(os.path.join(folder, it["name"]), it["name"])
            with _lock:
                it["num"] = num
                it["ocr_text"] = text
                it["ocr_src"] = src
                _session["progress"]["done"] += 1
                _save()
            events.publish("import", {"state": "ocr", **_session["progress"]})
        _rebuild_groups()
        with _lock:
            _session["status"] = "review"
            _save()
        events.publish("import", {"state": "review", "groups": len(_session["groups"])})
    except Exception as e:
        log.exception("import scan failed")
        with _lock:
            if _session:
                _session["status"] = "error"
                _session["error"] = str(e)
                _save()
        events.publish("import", {"state": "error", "detail": str(e)})


def _rebuild_groups() -> None:
    """items의 kind/순서 기준으로 그룹 재구성 (info가 새 그룹 시작).

    _session["merges"] = {src_info_idx: dst_info_idx} — 드래그 병합 기록.
    재구성 후 src 그룹의 항목들을 dst 그룹으로 옮기고 src 그룹은 제거.
    """
    with _lock:
        items = _session["items"]
        groups = []
        cur = None
        unassigned = {"id": 0, "num": None, "name": None, "date6": None, "enabled": False,
                      "info_idx": None, "item_idxs": [], "unassigned": True}
        for i, it in enumerate(items):
            if it["kind"] == "info":
                cur = {"id": len(groups) + 1, "num": it["num"],
                       "name": lookup_name(it["num"]) if it["num"] else None,
                       "date6": None, "enabled": True, "info_idx": i, "item_idxs": [i],
                       "unassigned": False}
                groups.append(cur)
            elif cur is not None:
                cur["item_idxs"].append(i)
            else:
                unassigned["item_idxs"].append(i)
        # 병합 적용 (info가 demote되면 그 병합 기록은 자연 소멸)
        merges = {int(k): int(v) for k, v in (_session.get("merges") or {}).items()}
        by_info = {g["info_idx"]: g for g in groups}
        for src_i, dst_i in merges.items():
            src_g, dst_g = by_info.get(src_i), by_info.get(dst_i)
            if src_g is None or dst_g is None or src_g is dst_g:
                continue
            dst_g["item_idxs"] = sorted(dst_g["item_idxs"] + src_g["item_idxs"])
            groups.remove(src_g)
            by_info.pop(src_i)
        for k, g in enumerate(groups):
            g["id"] = k + 1
        for g in groups:
            clin = [items[i] for i in g["item_idxs"] if items[i]["kind"] == "clinical"]
            base = clin[0] if clin else items[g["info_idx"]]  # 날짜 = 첫 임상사진 (Codex 확정)
            g["date6"] = _date6(base["ts"])
            if not clin:
                g["enabled"] = False  # 연속 정보사진 등 임상 없는 묶음 = 기본 제외
        if unassigned["item_idxs"]:
            unassigned["date6"] = _date6(items[unassigned["item_idxs"][0]]["ts"])
            groups.insert(0, unassigned)
        _session["groups"] = groups
        _save()


def merge_groups(src_gid: int, dst_gid: int) -> None:
    """src 그룹을 dst로 병합 (드래그&드롭). 재구성 시에도 유지되게 merges에 기록."""
    with _lock:
        groups = _session["groups"]
        src = next((g for g in groups if g["id"] == src_gid), None)
        dst = next((g for g in groups if g["id"] == dst_gid), None)
        if src is None or dst is None:
            raise ValueError("그룹 없음")
        if src is dst or src.get("unassigned") or dst.get("unassigned"):
            raise ValueError("병합할 수 없는 그룹입니다")
        merges = _session.setdefault("merges", {})
        # src로 이미 병합된 그룹들도 함께 dst를 가리키게
        for k, v in list(merges.items()):
            if int(v) == src["info_idx"]:
                merges[k] = dst["info_idx"]
        merges[str(src["info_idx"])] = dst["info_idx"]
        _rebuild_groups()


def update_group(gid: int, fields: dict) -> dict:
    with _lock:
        g = next((g for g in _session["groups"] if g["id"] == gid), None)
        if g is None:
            raise ValueError("그룹 없음")
        for k in ("num", "name", "date6", "enabled"):
            if k in fields and fields[k] is not None:
                g[k] = fields[k]
        if "num" in fields and fields["num"] and not fields.get("name"):
            known = lookup_name(fields["num"])
            if known:
                g["name"] = known
        _save()
        return g


def item_action(idx: int, action: str) -> None:
    """promote(정보사진 승격=새 묶음 시작) / demote / to_prev / to_next / to_unassigned."""
    with _lock:
        items = _session["items"]
        if not (0 <= idx < len(items)):
            raise ValueError("항목 없음")
        it = items[idx]
        if action == "promote":
            it["kind"] = "info"
            if not it["num"]:
                num, text, src = _ocr_number(os.path.join(_session["folder"], it["name"]), it["name"])
                it["num"], it["ocr_text"], it["ocr_src"] = num, text, src
        elif action == "demote":
            it["kind"] = "clinical"
        elif action in ("to_prev", "to_next", "to_unassigned"):
            # 그룹 재배치는 ts 조작으로 단순화하지 않고 kind 기반 재구성이라,
            # 단일 항목 이동은 promote/demote 조합으로 처리 불가한 경우만 필요 —
            # v1: 인접 정보사진 kind 조정으로 해결되므로 미지원. (요청 시 확장)
            raise ValueError("v1에서는 승격/해제만 지원합니다")
        else:
            raise ValueError("알 수 없는 액션")
        _rebuild_groups()


def commit(root: str) -> dict:
    """enabled 그룹을 사진정리 루트로 COPY. 저널 기록(undo=복사본 삭제)."""
    from . import writer
    with _lock:
        s = _session
        if s is None or s["status"] != "review":
            raise RuntimeError("리뷰 상태의 세션이 없습니다")
        s["status"] = "committing"
        _save()
    items = s["items"]
    copies: list[str] = []
    created_dirs: list[str] = []
    report = []
    try:
        with writer.write_lock, db.fs_lock, db.ProcessLock():
            existing = {n.split("_")[0]: n for n in os.listdir(root)
                        if os.path.isdir(os.path.join(root, n)) and re.match(r"^\d{8}_", n)}
            for g in s["groups"]:
                if not g["enabled"] or g.get("unassigned"):
                    continue
                if not g["num"] or not re.fullmatch(r"\d{8}", str(g["num"])):
                    raise ValueError(f"그룹 {g['id']}: 8자리 진료번호 필요")
                pdir_name = existing.get(g["num"])
                if pdir_name is None:
                    if not g["name"]:
                        raise ValueError(f"그룹 {g['id']} ({g['num']}): 신규 환자 — 이름 필요")
                    pdir_name = f"{g['num']}_{g['name']}"
                pdir = os.path.join(root, pdir_name)
                if not os.path.isdir(pdir):
                    os.makedirs(pdir)
                    created_dirs.append(pdir)
                # 같은 날짜(prefix) 폴더가 이미 있으면 거기에 합류 (태그 붙은 폴더 포함)
                date_dir = next((d for d in os.listdir(pdir)
                                 if os.path.isdir(os.path.join(pdir, d))
                                 and d.startswith(g["date6"])), None)
                if date_dir is None:
                    date_dir = g["date6"]
                    os.makedirs(os.path.join(pdir, date_dir))
                    created_dirs.append(os.path.join(pdir, date_dir))
                ddir = os.path.join(pdir, date_dir)
                n_copied = 0
                for i in g["item_idxs"]:
                    src = os.path.join(s["folder"], items[i]["name"])
                    dst = os.path.join(ddir, items[i]["name"])
                    stem, ext = os.path.splitext(items[i]["name"])
                    k = 1
                    while os.path.exists(dst):
                        dst = os.path.join(ddir, f"{stem}_{k}{ext}")
                        k += 1
                    tmp = dst + ".importing"
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dst)  # 부분 복사본이 최종 이름으로 노출되지 않게
                    copies.append(dst)
                    n_copied += 1
                report.append({"group": g["id"], "patient": pdir_name,
                               "folder": date_dir, "copied": n_copied})
            run_id = writer._write_journal({"kind": "import", "source": s["folder"],
                                            "copies": copies, "created_dirs": created_dirs,
                                            "renames": []}) if copies else None
    except Exception:
        with _lock:
            s["status"] = "review"  # 실패 → 리뷰로 복귀, 재시도 가능
            _save()
        raise
    with _lock:
        s["status"] = "done"
        s["report"] = report
        s["run_id"] = run_id
        _save()
    events.publish("import", {"state": "committed", "run_id": run_id,
                              "copied": len(copies), "groups": len(report)})
    return {"run_id": run_id, "copied": len(copies), "report": report}
