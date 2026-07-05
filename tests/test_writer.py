"""Phase 2 쓰기 경로: rename/tag edit/journal/undo."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image

from backend import db, scanner, writer


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: str(tmp_path / "index.db"))
    monkeypatch.setattr(db, "app_data_dir", lambda: str(tmp_path / "appdata"))
    os.makedirs(tmp_path / "appdata", exist_ok=True)
    db.close()
    root = tmp_path / "photos"
    b = root / "12345678_홍길동" / "250116_A_GTR_#36"
    b.mkdir(parents=True)
    Image.new("RGB", (60, 60), "red").save(b / "a.jpg")
    scanner.full_scan(str(root))
    conn = db.connect()
    folder_id = conn.execute("SELECT id FROM folders WHERE name='250116_A_GTR_#36'").fetchone()["id"]
    yield str(root), folder_id, conn
    db.close()


def test_rename_and_undo(env):
    root, fid, conn = env
    r = writer.rename_folder(root, fid, "250116_A_GTR_#36_EMD")
    assert not r["noop"] and r["run_id"]
    assert os.path.isdir(os.path.join(root, "12345678_홍길동", "250116_A_GTR_#36_EMD"))
    tags = [x["tag"] for x in conn.execute(
        "SELECT tag FROM tags WHERE folder_id=? ORDER BY position", (fid,))]
    assert tags == ["A", "GTR", "#36", "EMD"]

    u = writer.undo(root, r["run_id"])
    assert u["reverted"] == 1
    assert os.path.isdir(os.path.join(root, "12345678_홍길동", "250116_A_GTR_#36"))
    # 이중 undo 거부
    with pytest.raises(writer.WriteError):
        writer.undo(root, r["run_id"])


def test_edit_tags(env):
    root, fid, conn = env
    r = writer.edit_tags(root, fid, add=["EMD"], remove=["A"])
    # 정규 순서: config 없는 테스트 환경에선 GTR/EMD가 '기타' 추정 → 치식 뒤
    assert r["new"] == "250116_#36_GTR_EMD"
    # 신규 태그가 config에 자동 등록됨
    from backend import tagsort
    cfg = tagsort.load_config(root)
    assert "#36" in cfg["tags"] and cfg["tags"]["#36"]["super_category"] == "치식"
    # 밑줄 태그 거부
    with pytest.raises(writer.WriteError):
        writer.edit_tags(root, fid, add=["bad_tag"], remove=[])


def test_rename_validation(env):
    root, fid, _ = env
    for bad in ["", "a/b", "x?", "trail. ", "a" * 250]:
        with pytest.raises(writer.WriteError):
            writer.rename_folder(root, fid, bad)


def test_rename_target_exists(env):
    root, fid, _ = env
    os.makedirs(os.path.join(root, "12345678_홍길동", "250117_new"))
    with pytest.raises(writer.WriteError):
        writer.rename_folder(root, fid, "250117_new")


def test_delete_tag(env):
    root, fid, conn = env
    r = writer.delete_tag(root, "GTR")
    assert r["removed_from"] == 1 and r["run_id"]
    # 정규 정렬: config 없는 테스트 환경에서 'A'는 기타 → 치식(#36) 뒤
    assert os.path.isdir(os.path.join(root, "12345678_홍길동", "250116_#36_A"))
    # 어휘에서도 제거
    from backend import tagsort
    assert "GTR" not in tagsort.load_config(root).get("tags", {})
    # undo로 원복
    u = writer.undo(root, r["run_id"])
    assert u["reverted"] == 1
    assert os.path.isdir(os.path.join(root, "12345678_홍길동", "250116_A_GTR_#36"))
    # 상태라벨 삭제 거부
    with pytest.raises(writer.WriteError):
        writer.delete_tag(root, "@Annotated")


def test_noop_rename(env):
    root, fid, _ = env
    r = writer.rename_folder(root, fid, "250116_A_GTR_#36")
    assert r["noop"] and r["run_id"] is None
