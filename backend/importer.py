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
            if _session and _session.get("status") == "review":
                for _k in ("name_cache", "name_cache_v3"):
                    _session.pop(_k, None)  # 이전 규칙의 오인 결과 폐기
                _rebuild_groups()  # 재로딩 시 자동 이름 재해석
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


_NAME_STOP = {"원장", "치과", "선생님", "위생사", "실장", "진료번호", "환자명", "환자",
              "이름", "번호", "차트", "예약", "접수", "수납", "메모", "시간", "오전", "오후",
              "보험", "일반", "공통", "초진", "재진", "신환", "구환", "스케일링", "임플란트",
              "발치", "크라운", "브릿지", "체크", "소독", "수정", "수진자명", "생년월일",
              "진료의", "담당의", "주치의", "메모사항", "성명"}  # 번호 근처 머리글·비이름 토큰


def _nearest_name(text: str, num: str, max_dist: int = 40) -> str | None:
    """텍스트에서 num 주변 가장 가까운 한글 2~4자 토큰 (머리글 제외, max_dist 이내)."""
    best = None
    for m in re.finditer(num, text):
        lo = max(0, m.start() - max_dist)
        for t in re.finditer(r"[가-힣]{2,4}", text[lo:m.end() + max_dist]):
            if t.group() in _NAME_STOP:
                continue
            pos = lo + t.start()
            dist = m.start() - (lo + t.end()) if pos < m.start() else pos - m.end()
            if dist > max_dist:
                continue
            if best is None or dist < best[0]:
                best = (dist, t.group())
    return best[1] if best else None


def name_from_schedule(num: str) -> str | None:
    """일정표 PDF에서 진료번호로 이름 조회 — 라벨 증거 전용 (코덱스 확정안).

    번호가 등장하는 페이지에서 '수진자명:' 류 라벨 바로 뒤 단어만 증거로 수집.
    거리 기반 추측은 라벨('진료번호'·'수진자명')이나 의사 이름('진료의: …')을
    이름으로 오인한 실사고 2회로 폐기 — 증거가 없거나 문서 간 상충하면 None(NA).
    """
    conn = db.connect()
    with db.lock:
        rows = conn.execute(
            """SELECT d.path, h.page FROM pdf_hits h JOIN pdfs d ON d.id = h.pdf_id
               WHERE h.patient_num = ? ORDER BY d.date8 DESC LIMIT 8""",
            (num,)).fetchall()
    evidence: dict[str, set] = {}  # 이름 -> {pdf path}
    for r in rows:
        try:
            import fitz
            with fitz.open(r["path"]) as doc:
                words = doc[r["page"] - 1].get_text("words")
        except Exception:
            continue
        for n in _label_names(words, num):
            evidence.setdefault(n, set()).add(r["path"])
    if len(evidence) == 1:  # 상충 없이 하나로 수렴할 때만 채택
        return next(iter(evidence))
    return None


_NAME_LABELS = ("수진자명", "환자성명", "환자명", "성명")


def _plausible_name(t: str) -> bool:
    return bool(re.fullmatch(r"[가-힣]{2,6}", t)) and t not in _NAME_STOP


def _label_names(words: list, num: str) -> set[str]:
    """번호가 있는 줄에서 이름 라벨 바로 뒤 단어만 수집.

    실측 형식: `진료번호: 12505169 || 수진자명: 노병희|| 생년월일: … 진료의: 안현성||`
    """
    out: set[str] = set()
    for w in words:
        if num not in w[4]:
            continue
        cy = (w[1] + w[3]) / 2
        h = max(w[3] - w[1], 1)
        line = sorted((x for x in words if abs((x[1] + x[3]) / 2 - cy) < h * 0.6),
                      key=lambda x: x[0])
        toks = [re.sub(r"[^가-힣]", "", x[4]) for x in line]
        for i, t in enumerate(toks):
            for lab in _NAME_LABELS:
                if t == lab and i + 1 < len(toks) and _plausible_name(toks[i + 1]):
                    out.add(toks[i + 1])
                elif t.startswith(lab) and _plausible_name(t[len(lab):]):
                    out.add(t[len(lab):])  # '수진자명홍길동' 붙은 추출 대응
    return out


def _resolve_name(num: str | None, ocr_text: str | None = None) -> tuple[str | None, str | None]:
    """(이름, 출처) — 인덱스 > 정보사진 OCR(차트화면에 이름 표기) > 일정표 PDF(캐시)."""
    if not num:
        return None, None
    n = lookup_name(num)
    if n:
        return n, "index"
    if ocr_text:
        n = _nearest_name(ocr_text, num, max_dist=10)  # 차트화면 '번호 / 이름' 인접만
        if n:
            return n, "ocr"
    cache = _session.setdefault("name_cache_v3", {}) if _session is not None else {}
    if num in cache:
        n = cache[num]
    else:
        n = name_from_schedule(num)
        cache[num] = n
    return (n, "schedule") if n else (None, None)


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
    _session["manual_groups"] = [{key:"m1", ...}] — ➕ 새 묶음 (빈 그룹도 유지).
    항목의 "grp" = 드래그 이동 override (파생 그룹 key=info_idx, 수동 "m<n>", 미분류 "u").
    항목의 "edit" = 사용자가 수정한 그룹 필드 (재구성 후에도 유지, update_group이 기록).
    """
    with _lock:
        items = _session["items"]
        groups = []
        cur = None
        moved = []  # (item_idx, target_key)
        unassigned = {"id": 0, "key": "u", "num": None, "name": None, "date6": None,
                      "enabled": False, "info_idx": None, "item_idxs": [], "unassigned": True}
        for i, it in enumerate(items):
            if it["kind"] == "info":
                it.pop("grp", None)  # 정보사진은 항상 자기 그룹 소속
                ed = it.get("edit")
                if ed and ed.get("name") and ed.get("name_source") in (None, "session"):
                    name, name_src = ed["name"], ed.get("name_source")  # 사람 입력만 보존
                else:
                    name, name_src = _resolve_name(it["num"], it.get("ocr_text"))
                cur = {"id": len(groups) + 1, "key": i, "num": it["num"],
                       "name": name, "name_source": name_src,
                       "date6": None, "enabled": ed.get("enabled", True) if ed else True,
                       "info_idx": i, "item_idxs": [i],
                       "unassigned": False}
                groups.append(cur)
            elif it.get("grp") is not None:
                moved.append((i, it["grp"]))
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
        for g in groups:
            ed = items[g["info_idx"]].get("edit") or {}
            clin = [items[i] for i in g["item_idxs"] if items[i]["kind"] == "clinical"]
            base = clin[0] if clin else items[g["info_idx"]]  # 날짜 = 첫 임상사진 (Codex 확정)
            g["date6"] = ed.get("date6") or _date6(base["ts"])
            if not clin:
                g["enabled"] = False  # 연속 정보사진 등 임상 없는 묶음 = 기본 제외
        # 수동 그룹 실체화 (빈 그룹도 표시) — 날짜는 자동 추정 없이 사용자 입력만
        for mg in _session.get("manual_groups") or []:
            groups.append({"id": 0, "key": mg["key"], "num": mg.get("num"),
                           "name": mg.get("name"), "name_source": mg.get("name_source"),
                           "date6": mg.get("date6"), "enabled": mg.get("enabled", True),
                           "info_idx": None, "item_idxs": [], "unassigned": False,
                           "manual": True})
        # 이동 override 적용 — 대상이 병합됐으면 따라가고, 사라졌으면 폐기(미분류로)
        by_key = {g["key"]: g for g in groups}
        by_key["u"] = unassigned
        for i, tkey in moved:
            seen = set()
            while isinstance(tkey, int) and tkey in merges and tkey not in seen:
                seen.add(tkey)
                tkey = merges[tkey]
            g = by_key.get(tkey)
            if g is None:
                items[i].pop("grp", None)
                g = unassigned
            g["item_idxs"].append(i)
        for g in groups:
            g["item_idxs"].sort()
        for k, g in enumerate(groups):
            g["id"] = k + 1
        if unassigned["item_idxs"]:
            unassigned["item_idxs"].sort()
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
        if src.get("manual") or dst.get("manual"):
            raise ValueError("수동 묶음은 병합 대신 사진을 드래그해 옮기세요")
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
        num_before = g.get("num")
        for k in ("num", "name", "date6", "enabled"):
            if k in fields and fields[k] is not None:
                g[k] = fields[k]
        if fields.get("name"):
            g["name_source"] = None  # 직접 입력 — 자동 추정 아님
            # 같은 세션의 동일 진료번호 그룹(다른 날짜 등)에도 이름 전파
            if g.get("num"):
                for o in _session["groups"]:
                    if o is not g and o.get("num") == g["num"] and not o.get("name")                             and not o.get("unassigned"):
                        o["name"], o["name_source"] = g["name"], "session"
                        _persist_group(o)
        elif fields.get("num") and fields["num"] != num_before:
            ocr = (_session["items"][g["info_idx"]].get("ocr_text")
                   if g.get("info_idx") is not None else None)
            known, src = _resolve_name(fields["num"], ocr)
            if not known:  # 인덱스·일정표에 없어도 같은 세션에 이름 있으면 채용
                other = next((o for o in _session["groups"]
                              if o is not g and o.get("num") == fields["num"] and o.get("name")), None)
                if other:
                    known, src = other["name"], "session"
            if known:
                g["name"], g["name_source"] = known, src
        _persist_group(g)
        _save()
        return g


def _persist_group(g: dict) -> None:
    """그룹 편집을 재구성 원본(items/manual_groups)에 반영 — rebuild 후에도 유지."""
    if g.get("unassigned"):
        return
    if g.get("manual"):
        mg = next((m for m in _session.get("manual_groups") or [] if m["key"] == g["key"]), None)
        if mg:
            for k in ("num", "name", "name_source", "date6", "enabled"):
                mg[k] = g.get(k)
    elif g.get("info_idx") is not None:
        it = _session["items"][g["info_idx"]]
        it["num"] = g.get("num")
        it["edit"] = {"name": g.get("name"), "name_source": g.get("name_source"),
                      "date6": g.get("date6"), "enabled": g.get("enabled")}


def move_item(idx: int, gid: int) -> None:
    """드래그&드롭: 사진(임상)을 다른 그룹으로 이동 — items[idx]["grp"] override."""
    with _lock:
        items = _session["items"]
        if not (0 <= idx < len(items)):
            raise ValueError("항목 없음")
        if items[idx]["kind"] == "info":
            raise ValueError("정보사진은 이동할 수 없습니다 — 먼저 ↩임상으로 바꾸세요")
        g = next((g for g in _session["groups"] if g["id"] == gid), None)
        if g is None:
            raise ValueError("그룹 없음")
        items[idx]["grp"] = g["key"]
        _rebuild_groups()


def new_group() -> None:
    """➕ 새 묶음 — 사진을 드래그해 담는 빈 수동 그룹."""
    with _lock:
        mgs = _session.setdefault("manual_groups", [])
        n = 1 + max((int(m["key"][1:]) for m in mgs), default=0)
        mgs.append({"key": f"m{n}", "num": None, "name": None, "name_source": None,
                    "date6": None, "enabled": True})
        _rebuild_groups()


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
            it.pop("edit", None)  # 그룹 소멸 — 그룹 편집 기록도 함께
        elif action in ("to_prev", "to_next", "to_unassigned"):
            # 그룹 재배치는 ts 조작으로 단순화하지 않고 kind 기반 재구성이라,
            # 단일 항목 이동은 promote/demote 조합으로 처리 불가한 경우만 필요 —
            # v1: 인접 정보사진 kind 조정으로 해결되므로 미지원. (요청 시 확장)
            raise ValueError("v1에서는 승격/해제만 지원합니다")
        else:
            raise ValueError("알 수 없는 액션")
        _rebuild_groups()


def _same_file(a: str, b: str) -> bool:
    """이름 충돌 파일의 내용 동일성 — 크기 먼저, 같으면 sha1."""
    import hashlib
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        digests = []
        for p in (a, b):
            h = hashlib.sha1()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            digests.append(h.digest())
        return digests[0] == digests[1]
    except OSError:
        return False


def commit(root: str, dry_run: bool = False) -> dict:
    """enabled 그룹을 사진정리 루트로 COPY. 저널 기록(undo=복사본 삭제).

    검증→계획→복사 순서: 전 그룹의 문제를 한 번에 모두 보고하고,
    대상 폴더에 같은 이름·같은 내용 파일이 있으면 dup으로 건너뜀.
    dry_run=True → 검증+계획만 반환 (복사·상태 변경 없음).
    """
    from . import writer
    with _lock:
        s = _session
        if s is None or s["status"] != "review":
            raise RuntimeError("리뷰 상태의 세션이 없습니다")
        if not dry_run:
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
            targets = [g for g in s["groups"]
                       if g["enabled"] and not g.get("unassigned") and g["item_idxs"]]
            # 1) 전 그룹 일괄 검증 — 문제를 모아 한 번에 보고
            errors = []
            for g in targets:
                if not g["num"] or not re.fullmatch(r"\d{8}", str(g["num"])):
                    errors.append(f"그룹 {g['id']}: 8자리 진료번호 필요")
                elif existing.get(g["num"]) is None and not g["name"]:
                    errors.append(f"그룹 {g['id']} ({g['num']}): 신규 환자 — 이름 필요")
                if not g["date6"] or not re.fullmatch(r"\d{6}", str(g["date6"])):
                    errors.append(f"그룹 {g['id']}: 날짜(YYMMDD) 필요")
            if errors:
                raise ValueError(" / ".join(errors))
            # 2) 계획 — 대상 폴더와 이름 충돌(dup/rename) 판정을 복사 전에 확정
            plan = []
            for g in targets:
                pdir_name = existing.get(g["num"]) or f"{g['num']}_{g['name']}"
                pdir = os.path.join(root, pdir_name)
                # 같은 날짜(prefix) 폴더가 이미 있으면 거기에 합류 (태그 붙은 폴더 포함)
                date_dir = None
                if os.path.isdir(pdir):
                    date_dir = next((d for d in os.listdir(pdir)
                                     if os.path.isdir(os.path.join(pdir, d))
                                     and d.startswith(g["date6"])), None)
                date_dir = date_dir or g["date6"]
                ddir = os.path.join(pdir, date_dir)
                entries = []
                for i in g["item_idxs"]:
                    src = os.path.join(s["folder"], items[i]["name"])
                    dst = os.path.join(ddir, items[i]["name"])
                    stem, ext = os.path.splitext(items[i]["name"])
                    kind, k = "new", 1
                    while os.path.exists(dst):
                        if _same_file(src, dst):
                            kind = "dup"  # 같은 이름·같은 내용 = 이미 복사됨
                            break
                        dst = os.path.join(ddir, f"{stem}_{k}{ext}")
                        kind, k = "renamed", k + 1
                    entries.append({"src": src, "dst": dst, "kind": kind})
                plan.append({"gid": g["id"], "pdir": pdir, "ddir": ddir,
                             "patient": pdir_name, "folder": date_dir, "entries": entries})
            if dry_run:
                gs = [{"id": p["gid"], "patient": p["patient"], "folder": p["folder"],
                       **{f"n_{k}": sum(1 for e in p["entries"] if e["kind"] == k)
                          for k in ("new", "dup", "renamed")}} for p in plan]
                return {"dry_run": True, "groups": gs,
                        "totals": {k: sum(x[f"n_{k}"] for x in gs)
                                   for k in ("new", "dup", "renamed")}}
            # 3) 복사 (dup은 건너뜀)
            dup_skipped = 0
            for p in plan:
                n_copied = n_dup = 0
                for e in p["entries"]:
                    if e["kind"] == "dup":
                        n_dup += 1
                        continue
                    for d in (p["pdir"], p["ddir"]):
                        if not os.path.isdir(d):
                            os.makedirs(d)
                            created_dirs.append(d)
                    tmp = e["dst"] + ".importing"
                    shutil.copy2(e["src"], tmp)
                    os.replace(tmp, e["dst"])  # 부분 복사본이 최종 이름으로 노출되지 않게
                    copies.append(e["dst"])
                    n_copied += 1
                dup_skipped += n_dup
                report.append({"group": p["gid"], "patient": p["patient"],
                               "folder": p["folder"], "copied": n_copied, "dup": n_dup})
            run_id = writer._write_journal({"kind": "import", "source": s["folder"],
                                            "copies": copies, "created_dirs": created_dirs,
                                            "renames": []}) if copies else None
    except Exception:
        if not dry_run:
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
    return {"run_id": run_id, "copied": len(copies), "dup_skipped": dup_skipped,
            "report": report}
