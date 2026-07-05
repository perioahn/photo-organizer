"""Phase 1 백엔드 스모크 테스트: 파싱, 스캔, 썸네일 원자성."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image

from backend import db, scanner, thumbs


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: str(tmp_path / "index.db"))
    monkeypatch.setattr(db, "app_data_dir", lambda: str(tmp_path))
    db.close()
    yield
    db.close()


def test_parse_patient():
    assert scanner.parse_patient("12345678_홍길동") == ("12345678", "홍길동")
    assert scanner.parse_patient("no_id_250101") == (None, "no_id_250101")


def test_parse_b_folder():
    assert scanner.parse_b_folder("250116_A_GTR_#36") == ("250116", ["A", "GTR", "#36"])
    assert scanner.parse_b_folder("250116") == ("250116", [])
    assert scanner.parse_b_folder("CT") == (None, [])


def test_classify_file():
    assert scanner.classify_file("DSC_0001.jpg") == "image"
    assert scanner.classify_file("DSC_0001.NEF") == "raw"
    assert scanner.classify_file("thumbnail_DSC_0001.jpg") is None
    assert scanner.classify_file("notes.txt") is None


def _make_tree(root):
    b = root / "12345678_홍길동" / "250116_A_GTR_#36"
    b.mkdir(parents=True)
    Image.new("RGB", (100, 80), "red").save(b / "DSC_0001.jpg")
    (b / "thumbnail_DSC_0001.jpg").write_bytes(b"legacy")
    sub = b / "CT"
    sub.mkdir()
    Image.new("RGB", (50, 50), "blue").save(sub / "ct1.png")
    (root / "12345678_홍길동" / "원본").mkdir()
    (root / "others").mkdir()


def test_full_scan(tmp_path, tmp_db):
    root = tmp_path / "photos"
    _make_tree(root)
    stats = scanner.full_scan(str(root))
    assert stats["patients"] == 2  # 환자 + others
    conn = db.connect()
    f = conn.execute("SELECT * FROM folders WHERE name='250116_A_GTR_#36'").fetchone()
    assert f["date6"] == "250116" and f["is_regular"] == 1
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM tags WHERE folder_id=? ORDER BY position", (f["id"],))]
    assert tags == ["A", "GTR", "#36"]
    files = conn.execute("SELECT name FROM files WHERE folder_id=?", (f["id"],)).fetchall()
    assert [r["name"] for r in files] == ["DSC_0001.jpg"]  # legacy thumbnail 제외
    ct = conn.execute("SELECT * FROM folders WHERE name='CT'").fetchone()
    assert ct["parent_id"] == f["id"] and ct["is_regular"] == 0
    # 재스캔해도 중복 없이 동일 + id 보존 (프론트가 든 id가 무효화되지 않아야 함)
    ids_before = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM files")}
    stats2 = scanner.full_scan(str(root))
    assert stats2["patients"] == stats["patients"]
    assert stats2["files"] == stats["files"] == 2
    ids_after = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM files")}
    assert ids_before == ids_after


def test_scan_removes_deleted(tmp_path, tmp_db):
    root = tmp_path / "photos"
    _make_tree(root)
    scanner.full_scan(str(root))
    import shutil
    shutil.rmtree(root / "12345678_홍길동" / "250116_A_GTR_#36" / "CT")
    scanner.full_scan(str(root))
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM folders WHERE name='CT'").fetchone()["c"] == 0
    # cascade로 CT 안의 파일도 정리
    assert conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 1


def test_thumb_roundtrip(tmp_path, tmp_db):
    src = tmp_path / "big.jpg"
    Image.new("RGB", (1600, 1200), "green").save(src)
    out = thumbs.ensure_thumb(str(src), "pat/250101/big.jpg")
    assert out and os.path.exists(out)
    img = Image.open(out)
    assert max(img.size) <= thumbs.THUMB_SIZE
    # 같은 입력 → 같은 캐시 경로 (재생성 없음)
    assert thumbs.ensure_thumb(str(src), "pat/250101/big.jpg") == out
    # mtime 바뀌면 새 키
    os.utime(src, (1, 1))
    out2 = thumbs.ensure_thumb(str(src), "pat/250101/big.jpg")
    assert out2 != out


def test_thumb_missing_file(tmp_db):
    assert thumbs.ensure_thumb("Z:/nope/x.jpg", "x.jpg") is None
