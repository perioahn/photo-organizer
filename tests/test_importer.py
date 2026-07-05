"""import 마법사: 분류→그룹→커밋→undo."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image

from backend import db, importer, scanner, writer


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: str(tmp_path / "index.db"))
    monkeypatch.setattr(db, "app_data_dir", lambda: str(tmp_path / "appdata"))
    os.makedirs(tmp_path / "appdata", exist_ok=True)
    db.close()
    importer._session = None
    root = tmp_path / "root"
    (root / "12345678_홍길동").mkdir(parents=True)
    scanner.full_scan(str(root))
    src = tmp_path / "sdcard"
    src.mkdir()
    # 시간순: info(12345678) → clin×2 → info(OCR실패) → clin → info(임상없음, junk)
    metas = {}
    for i, (name, kind, num) in enumerate([
        ("a1.jpg", "info", "12345678"), ("a2.jpg", "clin", None), ("a3.jpg", "clin", None),
        ("b1.jpg", "info", None), ("b2.jpg", "clin", None),
        ("c1.jpg", "info", "87654321"),
    ]):
        Image.new("RGB", (60, 60), "gray").save(src / name)
        metas[name] = (5.0 if kind == "info" else 22.0, 1_700_000_000 + i * 60)
    monkeypatch.setattr(importer, "_read_meta", lambda p: metas[os.path.basename(p)])
    ocr_map = {"a1.jpg": "12345678", "b1.jpg": None, "c1.jpg": "87654321"}
    monkeypatch.setattr(importer, "_ocr_number",
                        lambda p, rel: (ocr_map.get(os.path.basename(p)), "raw", "local"))
    yield str(root), str(src)
    importer._session = None
    db.close()


def _wait_review():
    import time
    for _ in range(100):
        s = importer.get_session()
        if s and s["status"] in ("review", "error"):
            return s
        time.sleep(0.05)
    raise TimeoutError


def test_scan_and_grouping(env):
    root, src = env
    importer.start(src)
    s = _wait_review()
    assert s["status"] == "review"
    groups = [g for g in s["groups"] if not g.get("unassigned")]
    assert len(groups) == 3
    g1, g2, g3 = groups
    assert g1["num"] == "12345678" and g1["name"] == "홍길동"   # 인덱스에서 이름 자동
    assert len(g1["item_idxs"]) == 3 and g1["enabled"]
    assert g2["num"] is None and g2["enabled"]                  # OCR 실패 → 수동 입력 대상
    assert g3["num"] == "87654321" and not g3["enabled"]        # 임상사진 0 → 기본 제외


def test_commit_and_undo(env):
    root, src = env
    importer.start(src)
    _wait_review()
    importer.update_group(2, {"num": "99999999", "name": "신규환자"})
    r = importer.commit(root)
    assert r["copied"] == 5  # g1: 3장 + g2: 2장 (g3는 제외)
    assert os.path.exists(os.path.join(root, "12345678_홍길동", "231114", "a2.jpg")) or \
        any(os.path.exists(os.path.join(root, "12345678_홍길동", d, "a2.jpg"))
            for d in os.listdir(os.path.join(root, "12345678_홍길동")))
    new_pat = os.path.join(root, "99999999_신규환자")
    assert os.path.isdir(new_pat)
    # 원본 불변
    assert len(os.listdir(src)) == 6
    # undo → 복사본 삭제 + 빈 폴더 정리
    u = writer.undo(root, r["run_id"])
    assert not os.path.isdir(new_pat)


def test_commit_requires_num_or_name(env):
    root, src = env
    importer.start(src)
    _wait_review()
    with pytest.raises(ValueError):
        importer.commit(root)  # g2 num 없음 → 거부


def test_promote_demote(env):
    root, src = env
    importer.start(src)
    s = _wait_review()
    # a3(임상, idx2)을 정보사진으로 승격 → 새 그룹 시작
    importer.item_action(2, "promote")
    s = importer.get_session()
    groups = [g for g in s["groups"] if not g.get("unassigned")]
    assert len(groups) == 4
    importer.item_action(2, "demote")
    s = importer.get_session()
    assert len([g for g in s["groups"] if not g.get("unassigned")]) == 3
