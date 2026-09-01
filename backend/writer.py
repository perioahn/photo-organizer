"""파일시스템 쓰기 경로 — 단일 write-lock + journal + retry.

규칙 (Codex 자문 확정):
- 모든 rename은 이 모듈의 전역 락 통과 (UI 동시클릭·auto_tag 경합 차단)
- 작업 직전 source/dest 재확인 (TOCTOU 최소화)
- PermissionError/FileExistsError/공유위반 = Dropbox·탐색기가 잡은 정상 상황 → retry/backoff
- 성공 즉시 journal 기록 (undo 가능) + DB 타깃 업데이트 + SSE 알림
"""
import json
import logging
import os
import re
import threading
import time

from . import db, events, scanner, tagsort

log = logging.getLogger(__name__)

write_lock = threading.RLock()  # 재진입: edit_tags → rename_folder

RETRIES = 5
BACKOFF = 0.25  # 초, 지수 증가

INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')


def journal_dir() -> str:
    d = os.path.join(db.app_data_dir(), "journal")
    os.makedirs(d, exist_ok=True)
    return d


def _write_journal(entry: dict) -> str:
    run_id = time.strftime("%Y%m%d%H%M%S") + f"_{int(time.time_ns() % 1000):03d}"
    entry = {"run_id": run_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    path = os.path.join(journal_dir(), f"run_{run_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return run_id


def list_journal(limit: int = 50) -> list[dict]:
    out = []
    try:
        names = sorted(os.listdir(journal_dir()), reverse=True)[:limit]
    except OSError:
        return []
    for n in names:
        try:
            with open(os.path.join(journal_dir(), n), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


_RETRY_WINERR = {5, 32, 33}  # ACCESS_DENIED, SHARING_VIOLATION, LOCK_VIOLATION


def _rename_with_retry(old: str, new: str) -> None:
    """os.rename + Dropbox/백신/탐색기 락 대비 backoff. 마지막 실패는 예외 전파."""
    delay = BACKOFF
    for attempt in range(RETRIES):
        try:
            os.rename(old, new)
            return
        except OSError as e:
            transient = isinstance(e, PermissionError) or getattr(e, "winerror", None) in _RETRY_WINERR
            if not transient or attempt == RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2


class WriteError(Exception):
    pass


class StaleError(WriteError):
    """편집 시작 이후 대상이 바뀜 — 덮어쓰기 방지."""


def validate_folder_name(name: str) -> None:
    if not name or name.startswith(" ") or name.endswith((" ", ".")):
        raise WriteError("폴더명이 비었거나 앞뒤 공백/마침표")
    if INVALID_CHARS.search(name):
        raise WriteError('폴더명에 금지 문자 (<>:"/\\|?*) 포함')
    if len(name) > 200:
        raise WriteError("폴더명이 너무 깁니다")


def _folder_disk_path(conn, folder_id: int) -> tuple[str, list[str]]:
    """(abs_path, parts). 인덱스 기준 — 호출측에서 존재 재확인."""
    with db.lock:
        r = conn.execute(
            """SELECT f.name, f2.name AS parent_name, p.folder_name AS patient_folder
               FROM folders f LEFT JOIN folders f2 ON f2.id=f.parent_id
               JOIN patients p ON p.id=f.patient_id WHERE f.id=?""",
            (folder_id,),
        ).fetchone()
    if r is None:
        raise WriteError("폴더가 인덱스에 없음 (rescan 필요)")
    parts = [r["patient_folder"]] + ([r["parent_name"]] if r["parent_name"] else []) + [r["name"]]
    return parts


def rename_folder(root: str, folder_id: int, new_name: str, reason: str = "manual") -> dict:
    """B폴더(또는 서브폴더) 이름 변경. 태그 편집도 결국 이 함수 하나로 수렴."""
    validate_folder_name(new_name)
    conn = db.connect()
    # 락 순서: write_lock(스레드) → fs_lock(스캐너 배타) → ProcessLock(CLI 배타)
    with write_lock, db.fs_lock, db.ProcessLock():
        parts = _folder_disk_path(conn, folder_id)
        old_name = parts[-1]
        if new_name == old_name:
            return {"run_id": None, "old": old_name, "new": new_name, "noop": True}
        old_path = os.path.join(root, *parts)
        new_path = os.path.join(root, *parts[:-1], new_name)
        # 작업 직전 디스크 재확인 — 인덱스가 아니라 실제 상태 기준
        if not os.path.isdir(old_path):
            raise WriteError(f"원본 폴더가 디스크에 없음: {old_name} (Dropbox 동기화 중?)")
        if os.path.exists(new_path):
            raise WriteError(f"대상 폴더가 이미 존재: {new_name} — 병합은 지원 전, 이름을 바꿔주세요")
        _rename_with_retry(old_path, new_path)
        run_id = _write_journal({
            "kind": "rename", "reason": reason,
            "renames": [[old_path, new_path]],
        })
        # 타깃 DB 업데이트 (watcher 재스캔을 기다리지 않고 즉시 일관화)
        date6, tags = scanner.parse_b_folder(new_name)
        with db.lock:
            conn.execute(
                "UPDATE folders SET name=?, date6=?, is_regular=? WHERE id=?",
                (new_name, date6, 1 if date6 else 0, folder_id),
            )
            conn.execute("DELETE FROM tags WHERE folder_id=?", (folder_id,))
            conn.executemany(
                "INSERT INTO tags(folder_id, tag, position) VALUES(?,?,?)",
                [(folder_id, t, i) for i, t in enumerate(tags)],
            )
            conn.commit()
    events.publish("folder", {"state": "renamed", "folder_id": folder_id,
                              "old": old_name, "new": new_name, "run_id": run_id})
    log.info("rename [%s]: %s -> %s", run_id, old_name, new_name)
    return {"run_id": run_id, "old": old_name, "new": new_name, "noop": False}


def rename_patient(root: str, patient_id: int, num: str, name: str,
                   expect_folder: str | None = None) -> dict:
    """환자 폴더(A폴더) 이름 변경 — 진료번호/이름 오타 수정용. 저널로 undo 가능.

    expect_folder: 편집 시작 시점의 폴더명. 그 사이 디스크가 바뀌었으면 StaleError
    (사용자가 보던 내용과 다른 대상을 덮어쓰지 않게 — Codex 자문).
    """
    num, name = (num or "").strip(), (name or "").strip()
    if not re.fullmatch(r"\d{8}", num):
        raise WriteError("진료번호는 숫자 8자리여야 합니다")
    if not name:
        raise WriteError("환자 이름을 입력하세요")
    new_folder = f"{num}_{name}"
    validate_folder_name(new_folder)
    conn = db.connect()
    with write_lock, db.fs_lock, db.ProcessLock():
        with db.lock:
            r = conn.execute("SELECT folder_name FROM patients WHERE id=?",
                             (patient_id,)).fetchone()
        if r is None:
            raise WriteError("환자가 인덱스에 없음 (rescan 필요)")
        old_folder = r["folder_name"]
        if expect_folder is not None and expect_folder != old_folder:
            raise StaleError(f"편집 중 폴더가 바뀌었습니다: {expect_folder} → {old_folder}")
        if old_folder == new_folder:
            return {"run_id": None, "old": old_folder, "new": new_folder, "noop": True}
        old_path = os.path.join(root, old_folder)
        new_path = os.path.join(root, new_folder)
        if not os.path.isdir(old_path):
            raise WriteError(f"원본 폴더가 디스크에 없음: {old_folder} (동기화 중?)")
        if os.path.exists(new_path):
            raise WriteError(f"같은 이름의 환자 폴더가 이미 있습니다: {new_folder}"
                             " — 합치려면 환자 폴더 합치기를 사용하세요")
        _rename_with_retry(old_path, new_path)
        run_id = _write_journal({"kind": "rename_patient",
                                 "renames": [[old_path, new_path]]})
        with db.lock:
            conn.execute(
                "UPDATE patients SET folder_name=?, patient_num=?, patient_name=? WHERE id=?",
                (new_folder, num, name, patient_id))
            conn.commit()
    events.publish("folder", {"state": "patient_renamed", "patient_id": patient_id,
                              "old": old_folder, "new": new_folder, "run_id": run_id})
    log.info("rename patient [%s]: %s -> %s", run_id, old_folder, new_folder)
    return {"run_id": run_id, "old": old_folder, "new": new_folder, "noop": False}


def _merge_token(root: str, src_folder: str, dst_folder: str) -> str:
    """합치기 검토 시점의 상태 지문 — 검토 후 폴더가 바뀌면 실행을 막는다."""
    import hashlib
    h = hashlib.sha1()
    for f in (src_folder, dst_folder):
        d = os.path.join(root, f)
        h.update(f.encode("utf-8"))
        try:
            for n in sorted(os.listdir(d)):
                h.update(b"|" + n.encode("utf-8"))
        except OSError:
            h.update(b"|<missing>")
    return h.hexdigest()[:16]


def merge_patients(root: str, src_id: int, dst_id: int, dry_run: bool = False,
                   token: str | None = None) -> dict:
    """환자 폴더 합치기 — src의 촬영일 폴더를 dst로 이동 (번호 오타로 갈라진 환자 복구).

    같은 날짜(date6) 폴더가 양쪽에 있으면 dst 폴더로 사진만 옮겨 합치고, 태그는
    양쪽 합집합으로 갱신한다. dry_run이면 계획만 반환하고 디스크는 건드리지 않는다.
    """
    from . import tagsort
    conn = db.connect()
    with write_lock, db.fs_lock, db.ProcessLock():
        with db.lock:
            rows = conn.execute(
                "SELECT id, folder_name FROM patients WHERE id IN (?,?)",
                (src_id, dst_id)).fetchall()
        by_id = {r["id"]: r["folder_name"] for r in rows}
        if src_id not in by_id or dst_id not in by_id or src_id == dst_id:
            raise WriteError("합칠 환자를 찾을 수 없습니다")
        src_dir = os.path.join(root, by_id[src_id])
        dst_dir = os.path.join(root, by_id[dst_id])
        if not os.path.isdir(src_dir) or not os.path.isdir(dst_dir):
            raise WriteError("환자 폴더가 디스크에 없습니다 (동기화 중?)")

        cfg = tagsort.load_config(root)
        dst_by_date = {}
        for n in os.listdir(dst_dir):
            if os.path.isdir(os.path.join(dst_dir, n)):
                d6, _ = scanner.parse_b_folder(n)
                if d6:
                    dst_by_date[d6] = n
        moves, merges = [], []
        for n in sorted(os.listdir(src_dir)):
            sp = os.path.join(src_dir, n)
            if not os.path.isdir(sp):
                continue
            d6, tags = scanner.parse_b_folder(n)
            twin = dst_by_date.get(d6) if d6 else None
            if twin is None and not os.path.exists(os.path.join(dst_dir, n)):
                moves.append((n, n))
            elif twin is None:
                moves.append((n, n + "_2"))  # 날짜 없는 폴더 이름 충돌 회피
            else:
                _, twin_tags = scanner.parse_b_folder(twin)
                merged = list(dict.fromkeys(twin_tags + tags))
                status = [t for t in merged if t.startswith("@")]
                rest = tagsort.sort_tags([t for t in merged if not t.startswith("@")], cfg)
                new_twin = d6 + ("_" + "_".join(status + rest) if (status + rest) else "")
                merges.append((n, twin, new_twin))
        cur_token = _merge_token(root, by_id[src_id], by_id[dst_id])
        plan = {"src": by_id[src_id], "dst": by_id[dst_id],
                "move": len(moves), "merge": len(merges), "token": cur_token,
                "details": {"moves": [m[0] for m in moves],
                            "merges": [[m[0], m[1]] for m in merges]}}
        if dry_run:
            return {**plan, "dry_run": True}
        if token is not None and token != cur_token:
            raise StaleError("검토 후 폴더 상태가 바뀌었습니다 — 다시 검토해 주세요")

        renames: list[list[str]] = []
        for src_name, dst_name in moves:
            a, b = os.path.join(src_dir, src_name), os.path.join(dst_dir, dst_name)
            _rename_with_retry(a, b)
            renames.append([a, b])
        for src_name, twin, new_twin in merges:
            sp, tp = os.path.join(src_dir, src_name), os.path.join(dst_dir, twin)
            for f in sorted(os.listdir(sp)):
                a = os.path.join(sp, f)
                stem, ext = os.path.splitext(f)
                b, k = os.path.join(tp, f), 1
                while os.path.exists(b):
                    b = os.path.join(tp, f"{stem}_{k}{ext}")
                    k += 1
                _rename_with_retry(a, b)
                renames.append([a, b])
            try:
                os.rmdir(sp)  # 비었을 때만 (남은 게 있으면 보존)
            except OSError:
                log.warning("병합 후 원본 폴더가 비지 않음: %s", sp)
            if new_twin != twin:
                np_ = os.path.join(dst_dir, new_twin)
                if not os.path.exists(np_):
                    _rename_with_retry(tp, np_)
                    renames.append([tp, np_])
        try:
            os.rmdir(src_dir)
        except OSError:
            log.warning("합치기 후 환자 폴더가 비지 않음: %s", src_dir)
        run_id = _write_journal({"kind": "merge_patients", "renames": renames})
    scanner.full_scan(root)  # 구조가 크게 바뀌므로 즉시 재스캔
    events.publish("folder", {"state": "patients_merged", "run_id": run_id, **plan})
    log.info("merge patients [%s]: %s -> %s (이동 %d·병합 %d)",
             run_id, plan["src"], plan["dst"], plan["move"], plan["merge"])
    return {**plan, "run_id": run_id, "dry_run": False}


def set_folder_date(root: str, folder_id: int, date6: str) -> dict:
    """촬영일 변경 — 태그는 그대로 두고 날짜 부분만 교체."""
    date6 = (date6 or "").strip()
    if not re.fullmatch(r"\d{6}", date6):
        raise WriteError("촬영일은 YYMMDD 6자리여야 합니다")
    if not ("01" <= date6[2:4] <= "12" and "01" <= date6[4:6] <= "31"):
        raise WriteError(f"날짜가 올바르지 않습니다: {date6}")
    conn = db.connect()
    with db.lock:
        r = conn.execute("SELECT name FROM folders WHERE id=?", (folder_id,)).fetchone()
    if r is None:
        raise WriteError("폴더가 인덱스에 없음 (rescan 필요)")
    old_d6, tags = scanner.parse_b_folder(r["name"])
    if old_d6 is None:
        raise WriteError("날짜 형식(YYMMDD…) 폴더만 촬영일을 바꿀 수 있습니다")
    new_name = date6 + ("_" + "_".join(tags) if tags else "")
    return rename_folder(root, folder_id, new_name, reason="set_date")


def edit_tags(root: str, folder_id: int, add: list[str], remove: list[str],
              reason: str = "manual") -> dict:
    """태그 추가/삭제 = 새 폴더명 계산 후 rename_folder.

    read-modify-write 전체가 write_lock 안 — 동시 편집이 서로의 태그를 덮지 않게.
    """
    conn = db.connect()
    with write_lock:
        with db.lock:
            r = conn.execute("SELECT name, date6 FROM folders WHERE id=?", (folder_id,)).fetchone()
        if r is None:
            raise WriteError("폴더가 인덱스에 없음")
        if not r["date6"]:
            raise WriteError("비정규 폴더에는 태그를 붙이지 않습니다")
        _, tags = scanner.parse_b_folder(r["name"])
        for t in remove:
            if t in tags:
                tags.remove(t)
        for t in add:
            t = t.strip().replace(" ", "")
            if t and t not in tags:
                if "_" in t:
                    raise WriteError("태그에 밑줄(_) 불가 — 폴더명 구분자입니다")
                tags.append(t)
        # 신규 태그는 tag_config에 카테고리 추정과 함께 등록 (피커 그룹에 바로 반영)
        tagsort.register_new_tags(root, [t for t in tags if t])
        # 폴더명도 정규 순서로 기록: 상태라벨(@) 먼저, 이후 술식→치식→재료…
        status = [t for t in tags if t.startswith("@")]
        rest = tagsort.sort_tags([t for t in tags if not t.startswith("@")],
                                 tagsort.load_config(root))
        tags = status + rest
        new_name = r["date6"] + ("_" + "_".join(tags) if tags else "")
        return rename_folder(root, folder_id, new_name, reason=reason)


def delete_tag(root: str, tag: str) -> dict:
    """태그를 어휘(tag_config)와 사용 중인 모든 폴더명에서 제거. 저널 1건으로 undo 가능."""
    if not tag or tag.startswith("@"):
        raise WriteError("상태라벨(@)은 삭제할 수 없습니다")
    conn = db.connect()
    renames_disk: list[list[str]] = []
    skipped: list[str] = []
    with write_lock, db.fs_lock, db.ProcessLock():
        with db.lock:
            rows = conn.execute(
                """SELECT f.id, f.name, f.date6, f2.name AS parent_name,
                          p.folder_name AS patient_folder
                   FROM folders f
                   JOIN tags t ON t.folder_id = f.id AND t.tag = ?
                   LEFT JOIN folders f2 ON f2.id = f.parent_id
                   JOIN patients p ON p.id = f.patient_id""",
                (tag,),
            ).fetchall()
        cfg = tagsort.load_config(root)
        for r in rows:
            _, tags = scanner.parse_b_folder(r["name"])
            if tag not in tags:
                continue
            tags.remove(tag)
            status = [t for t in tags if t.startswith("@")]
            rest = tagsort.sort_tags([t for t in tags if not t.startswith("@")], cfg)
            new_name = r["date6"] + ("_" + "_".join(status + rest) if (status + rest) else "")
            parts = [r["patient_folder"]] + ([r["parent_name"]] if r["parent_name"] else [])
            old_path = os.path.join(root, *parts, r["name"])
            new_path = os.path.join(root, *parts, new_name)
            if not os.path.isdir(old_path) or os.path.exists(new_path):
                skipped.append(r["name"])
                continue
            _rename_with_retry(old_path, new_path)
            renames_disk.append([old_path, new_path])
            date6, new_tags = scanner.parse_b_folder(new_name)
            with db.lock:
                conn.execute("UPDATE folders SET name=? WHERE id=?", (new_name, r["id"]))
                conn.execute("DELETE FROM tags WHERE folder_id=?", (r["id"],))
                conn.executemany(
                    "INSERT INTO tags(folder_id, tag, position) VALUES(?,?,?)",
                    [(r["id"], t, i) for i, t in enumerate(new_tags)],
                )
                conn.commit()
        run_id = None
        if renames_disk:
            run_id = _write_journal({"kind": "delete_tag", "tag": tag, "renames": renames_disk})
        # 어휘에서 제거 (피커 목록에서 사라짐)
        if tag in cfg.get("tags", {}):
            del cfg["tags"][tag]
            tagsort.save_config(root, cfg)
    events.publish("folder", {"state": "tag_deleted", "tag": tag,
                              "removed_from": len(renames_disk), "run_id": run_id})
    log.info("delete tag %s: %d folders, %d skipped", tag, len(renames_disk), len(skipped))
    return {"tag": tag, "removed_from": len(renames_disk), "skipped": skipped, "run_id": run_id}


def rename_tag(root: str, old: str, new: str) -> dict:
    """태그 이름 변경. new가 이미 있는 태그면 병합(폴더 내 중복 제거). 저널 1건으로 undo."""
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or old.startswith("@"):
        raise WriteError("상태라벨(@)은 이름을 바꿀 수 없습니다")
    if not new or new.startswith("@"):
        raise WriteError("새 이름이 비었거나 @로 시작합니다")
    if "_" in new:
        raise WriteError("태그에 밑줄(_) 불가 — 폴더명 구분자입니다")
    if INVALID_CHARS.search(new):
        raise WriteError('태그에 금지 문자 (<>:"/\\|?*) 포함')
    if new == old:
        raise WriteError("이름이 같습니다")
    conn = db.connect()
    renames_disk: list[list[str]] = []
    skipped: list[str] = []
    n_folders = 0
    with write_lock, db.fs_lock, db.ProcessLock():
        with db.lock:
            rows = conn.execute(
                """SELECT f.id, f.name, f.date6, f2.name AS parent_name,
                          p.folder_name AS patient_folder
                   FROM folders f
                   JOIN tags t ON t.folder_id = f.id AND t.tag = ?
                   LEFT JOIN folders f2 ON f2.id = f.parent_id
                   JOIN patients p ON p.id = f.patient_id""",
                (old,),
            ).fetchall()
            new_used = conn.execute("SELECT 1 FROM tags WHERE tag=? LIMIT 1", (new,)).fetchone()
        cfg = tagsort.load_config(root)
        tags_cfg = cfg.setdefault("tags", {})
        if old not in tags_cfg and not rows:
            raise WriteError(f"태그 '{old}'가 없습니다")
        merged = new in tags_cfg or new_used is not None
        old_cat = tagsort.category_of(old, cfg)  # pop 전에 — 카테고리 계승용
        old_entry = tags_cfg.pop(old, None)
        if not merged:
            # 새 이름이 old의 카테고리를 계승 (정렬·피커 그룹 유지)
            tags_cfg[new] = old_entry or {
                "super_category": None if old_cat == "기타" else old_cat}
        for r in rows:
            _, tags = scanner.parse_b_folder(r["name"])
            if old not in tags:
                continue
            tags = list(dict.fromkeys(new if t == old else t for t in tags))  # 병합 시 중복 제거
            status = [t for t in tags if t.startswith("@")]
            rest = tagsort.sort_tags([t for t in tags if not t.startswith("@")], cfg)
            new_name = r["date6"] + ("_" + "_".join(status + rest) if (status + rest) else "")
            if new_name == r["name"]:
                continue
            parts = [r["patient_folder"]] + ([r["parent_name"]] if r["parent_name"] else [])
            old_path = os.path.join(root, *parts, r["name"])
            new_path = os.path.join(root, *parts, new_name)
            case_only = old_path.lower() == new_path.lower()  # Windows FS는 대소문자 무시
            if not os.path.isdir(old_path) or (os.path.exists(new_path) and not case_only):
                skipped.append(r["name"])
                continue
            if case_only:
                # 같은 디렉터리라 직접 rename 불가 — dot 임시명 경유 (스캐너가 무시)
                tmp_path = os.path.join(root, *parts, "." + new_name)
                _rename_with_retry(old_path, tmp_path)
                _rename_with_retry(tmp_path, new_path)
                renames_disk += [[old_path, tmp_path], [tmp_path, new_path]]
            else:
                _rename_with_retry(old_path, new_path)
                renames_disk.append([old_path, new_path])
            n_folders += 1
            date6, new_tags = scanner.parse_b_folder(new_name)
            with db.lock:
                conn.execute("UPDATE folders SET name=? WHERE id=?", (new_name, r["id"]))
                conn.execute("DELETE FROM tags WHERE folder_id=?", (r["id"],))
                conn.executemany(
                    "INSERT INTO tags(folder_id, tag, position) VALUES(?,?,?)",
                    [(r["id"], t, i) for i, t in enumerate(new_tags)],
                )
                conn.commit()
        run_id = None
        if renames_disk:
            run_id = _write_journal({"kind": "rename_tag", "old": old, "new": new,
                                     "renames": renames_disk})
        if old_entry is not None or not merged:
            tagsort.save_config(root, cfg)
    events.publish("folder", {"state": "tag_renamed", "old": old, "new": new,
                              "merged": merged, "renamed": n_folders, "run_id": run_id})
    log.info("rename tag %s -> %s (merged=%s): %d folders, %d skipped",
             old, new, merged, n_folders, len(skipped))
    return {"tag": old, "new": new, "merged": merged, "renamed_folders": n_folders,
            "skipped": skipped, "run_id": run_id}


def undo(root: str, run_id: str) -> dict:
    """저널 역순 리네임. 현 위치에 있고 원래 이름이 비어있을 때만."""
    path = os.path.join(journal_dir(), f"run_{run_id}.json")
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, ValueError):
        raise WriteError(f"저널 없음: {run_id}")
    if entry.get("undone"):
        raise WriteError("이미 되돌린 작업입니다")
    done, skipped = [], []
    with write_lock, db.fs_lock, db.ProcessLock():
        for old_path, new_path in reversed(entry.get("renames", [])):
            # 폴더뿐 아니라 파일 이동(환자 합치기)도 되돌린다.
            # 합치기 때 비워져 삭제된 상위 폴더는 복원 전에 다시 만든다.
            if os.path.exists(new_path) and not os.path.exists(old_path):
                parent = os.path.dirname(old_path)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                _rename_with_retry(new_path, old_path)
                done.append([new_path, old_path])
            else:
                skipped.append([new_path, old_path])
        # import 저널: 복사본 삭제 + 그때 만든 빈 폴더 제거 (원본은 건드린 적 없음)
        for p in entry.get("copies", []):
            try:
                os.remove(p)
                done.append([p, "(deleted)"])
            except FileNotFoundError:
                pass
            except OSError:
                skipped.append([p, "(delete failed)"])
        for d in reversed(entry.get("created_dirs", [])):
            try:
                os.rmdir(d)  # 비어있을 때만 성공 — 사용자가 넣은 파일은 보존
            except OSError:
                pass
        # 전부 성공했을 때만 undone 확정 — 부분 skip은 재시도 가능하게 남김
        if not skipped:
            entry["undone"] = True
        entry["undo_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        entry["undo_skipped"] = skipped
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    scanner.full_scan(root)  # undo는 드문 작업 — 전체 재스캔으로 확실히 일관화
    return {"run_id": run_id, "reverted": len(done), "skipped": len(skipped)}
