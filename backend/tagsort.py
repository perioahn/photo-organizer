"""태그 정규 순서 — 화면 표기와 폴더명 기록 공용.

카테고리 등장 순서: 술식 → 치식 → 임플란트 → 이식재 → 차폐막 → 기타
치식 내부 순서: 사분면 마커(#10 #20 #30 #40) → #18~#11 → #21~#28 → #48~#41 → #31~#38 → 유치(숫자순)
카테고리 내부(치식 외): 기존 상대 순서 유지(stable) — 폴더명 churn 최소화.
"""
import json
import os
import time

CAT_ORDER = ["술식", "치식", "임플란트", "이식재", "차폐막", "기타"]
_CAT_IDX = {c: i for i, c in enumerate(CAT_ORDER)}
ETC_IDX = _CAT_IDX["기타"]

BRANDS = {"네오", "오스템", "덴티움", "스트라우만", "신흥"}
GRAFTS = {"바이오오스", "바이오트리", "본트리", "인터오스", "오스테온"}
MEMBRANES = {"바이오가이드", "티타늄메쉬", "ePTFE"}


def _tooth_seq() -> dict[str, int]:
    seq = ["#10", "#20", "#30", "#40"]                      # 사분면 마커 먼저
    seq += [f"#1{i}" for i in range(8, 0, -1)]              # #18 → #11
    seq += [f"#2{i}" for i in range(1, 9)]                  # #21 → #28
    seq += [f"#4{i}" for i in range(8, 0, -1)]              # #48 → #41
    seq += [f"#3{i}" for i in range(1, 9)]                  # #31 → #38
    return {t: i for i, t in enumerate(seq)}


TOOTH_SEQ = _tooth_seq()


def tooth_key(tag: str) -> int:
    return TOOTH_SEQ.get(tag, len(TOOTH_SEQ) + int(tag[1:] or 0) if tag[1:].isdigit() else 9999)


def guess_category(tag: str) -> str:
    if tag.startswith("#"):
        return "치식"
    if tag in BRANDS:
        return "임플란트"
    if tag in GRAFTS:
        return "이식재"
    if tag in MEMBRANES:
        return "차폐막"
    return "기타"


def config_path(root: str) -> str:
    return os.path.join(root, ".tag_config.json")


def load_config(root: str) -> dict:
    try:
        with open(config_path(root), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"super_categories": {}, "tags": {}}


def category_of(tag: str, cfg: dict) -> str:
    cat = (cfg.get("tags", {}).get(tag) or {}).get("super_category")
    return cat if cat in _CAT_IDX else guess_category(tag)


def sort_tags(tags: list[str], cfg: dict) -> list[str]:
    """카테고리 순 + 치식 내부 순서. 그 외 카테고리 내부는 원래 순서 유지(stable)."""
    def key(item):
        i, t = item
        cat = category_of(t, cfg)
        ci = _CAT_IDX.get(cat, ETC_IDX)
        return (ci, tooth_key(t) if cat == "치식" else 0, i)
    return [t for _, t in sorted(enumerate(tags), key=lambda x: key(x))]


def save_config(root: str, cfg: dict) -> None:
    p = config_path(root)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    delay = 0.25
    for attempt in range(5):  # Dropbox/백신이 직전 쓰기를 동기화하며 잠깐 잡는 경우 재시도
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(delay)
            delay *= 2


def set_category(root: str, tag: str, category: str | None) -> None:
    """태그의 super_category 지정 (None = 기타). 구 앱과 같은 config 파일에 기록."""
    if category is not None and category not in _CAT_IDX:
        raise ValueError(f"알 수 없는 카테고리: {category}")
    cfg = load_config(root)
    entry = cfg.setdefault("tags", {}).setdefault(tag, {})
    entry["super_category"] = None if category in (None, "기타") else category
    save_config(root, cfg)


def register_new_tags(root: str, tags: list[str]) -> list[str]:
    """config에 없는 태그를 카테고리 추정과 함께 등록. 반환 = 새로 등록된 태그."""
    cfg = load_config(root)
    known = cfg.setdefault("tags", {})
    added = []
    for t in tags:
        if t and t not in known:
            g = guess_category(t)
            known[t] = {"super_category": g if g != "기타" else None}
            added.append(t)
    if added:
        save_config(root, cfg)
    return added
