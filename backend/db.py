"""SQLite 운영 인덱스. 폴더명이 source of truth, DB는 읽기/검색용 캐시(재생성 가능)."""
import logging
import os
import sqlite3
import threading

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3

_conn: sqlite3.Connection | None = None
# ponytail: 전역 단일 커넥션 + RLock. 로컬 단일 사용자 앱이라 충분. 병목이면 read pool로 승급.
lock = threading.RLock()

# 파일시스템 변경(리네임)과 전체 스캔의 상호배타 — 스캔 중 리네임이 끼어들어
# 스테일 스냅샷으로 DB를 덮는 경합 방지. writer/scanner 공용.
fs_lock = threading.RLock()


class ProcessLock:
    """프로세스 간 쓰기 락 (photo_app 서버 ↔ auto_tag CLI).

    lock 파일 O_CREAT|O_EXCL 획득, 5분 이상 된 stale lock은 파기.
    """

    def __init__(self, name: str = "write.lock", timeout: float = 20.0):
        self.path = os.path.join(app_data_dir(), name)
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self):
        import time
        deadline = time.time() + self.timeout
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 300:
                        os.unlink(self.path)  # stale (크래시 잔재)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise TimeoutError("다른 프로세스가 사진 폴더를 변경 중입니다 (write.lock)")
                time.sleep(0.2)

    def __exit__(self, *exc):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.unlink(self.path)
        except OSError:
            pass


def app_data_dir() -> str:
    import sys
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    d = os.path.join(base, "PhotoApp")
    os.makedirs(d, exist_ok=True)
    return d


def db_path() -> str:
    return os.path.join(app_data_dir(), "index.db")


# id는 AUTOINCREMENT: 삭제된 rowid 재사용 금지 — 프론트가 들고 있는 stale id가
# 다른 파일을 가리키는 사고 방지. gen 컬럼은 스캔 세대 표식(안 보인 행 삭제용).
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS patients(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_name TEXT NOT NULL UNIQUE,
    patient_num TEXT,
    patient_name TEXT,
    gen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS folders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    parent_id INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    date6 TEXT,
    is_regular INTEGER NOT NULL DEFAULT 0,
    gen INTEGER NOT NULL DEFAULT 0,
    UNIQUE(patient_id, parent_id, name)
);
CREATE TABLE IF NOT EXISTS tags(
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    kind TEXT NOT NULL,
    gen INTEGER NOT NULL DEFAULT 0,
    UNIQUE(folder_id, name)
);
CREATE TABLE IF NOT EXISTS pdfs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    date8 TEXT,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    gen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pdf_hits(
    pdf_id INTEGER NOT NULL REFERENCES pdfs(id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    patient_num TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_folders_patient ON folders(patient_id);
CREATE INDEX IF NOT EXISTS idx_folders_date ON folders(date6);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE INDEX IF NOT EXISTS idx_tags_folder ON tags(folder_id);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
CREATE INDEX IF NOT EXISTS idx_pdf_hits_num ON pdf_hits(patient_num);
CREATE TABLE IF NOT EXISTS suggestions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_folder TEXT NOT NULL,
    b_folder TEXT NOT NULL,
    date6 TEXT,
    kind TEXT NOT NULL,
    add_tags TEXT NOT NULL,      -- JSON array
    remove_tags TEXT NOT NULL,   -- JSON array
    note TEXT,
    provenance TEXT,             -- JSON
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/accepted/rejected/stale
    created TEXT NOT NULL,
    resolved TEXT,
    UNIQUE(patient_folder, b_folder, kind, add_tags, remove_tags)
);
"""


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def connect(path: str | None = None) -> sqlite3.Connection:
    global _conn
    with lock:
        if _conn is not None:
            return _conn
        p = path or db_path()
        conn = _open(p)
        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if ver is None:
            conn.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            conn.commit()
        elif int(ver["value"]) != SCHEMA_VERSION:
            # 인덱스는 재생성 가능한 캐시 — 구버전이면 지우고 새로 만든다
            log.warning("index.db schema %s != %s; rebuilding", ver["value"], SCHEMA_VERSION)
            conn.close()
            os.remove(p)
            for suffix in ("-wal", "-shm"):
                try:
                    os.remove(p + suffix)
                except OSError:
                    pass
            conn = _open(p)
            conn.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            conn.commit()
        _conn = conn
        return conn


def next_gen(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    gen = (int(row["value"]) if row else 0) + 1
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, str(gen)))
    return gen


def close() -> None:
    global _conn
    with lock:
        if _conn is not None:
            _conn.close()
            _conn = None
