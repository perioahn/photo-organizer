"""태그 정규 순서: 치식 시퀀스 + 카테고리 등장 순서."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import tagsort

CFG = {"tags": {
    "GTR": {"super_category": "술식"}, "식립": {"super_category": "술식"},
    "네오": {"super_category": "임플란트"}, "인터오스": {"super_category": "이식재"},
    "#36": {"super_category": "치식"}, "#30": {"super_category": "치식"},
}}


def test_tooth_sequence():
    # 사분면 마커 → #18~#11 → #21~#28 → #48~#41 → #31~#38(오름차순)
    ordered = sorted(["#31", "#17", "#10", "#47", "#21", "#40", "#11", "#36"],
                     key=tagsort.tooth_key)
    assert ordered == ["#10", "#40", "#17", "#11", "#21", "#47", "#31", "#36"]


def test_category_then_tooth_order():
    tags = ["인터오스", "#36", "네오", "#30", "식립", "GTR"]
    assert tagsort.sort_tags(tags, CFG) == ["식립", "GTR", "#30", "#36", "네오", "인터오스"]


def test_stable_within_category():
    # 술식끼리는 원래 순서 유지 (GTR이 식립보다 앞에 있었으면 그대로)
    assert tagsort.sort_tags(["GTR", "식립"], CFG) == ["GTR", "식립"]


def test_unknown_tag_goes_last():
    assert tagsort.sort_tags(["수련", "식립", "#36"], CFG) == ["식립", "#36", "수련"]


def test_register_new_tags(tmp_path):
    root = str(tmp_path)
    added = tagsort.register_new_tags(root, ["#55", "새술식태그", "네오"])
    assert set(added) == {"#55", "새술식태그", "네오"}
    cfg = tagsort.load_config(root)
    assert cfg["tags"]["#55"]["super_category"] == "치식"
    assert cfg["tags"]["네오"]["super_category"] == "임플란트"
    assert cfg["tags"]["새술식태그"]["super_category"] is None
    # 재등록 없음
    assert tagsort.register_new_tags(root, ["#55"]) == []
