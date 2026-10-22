"""
桥接 ue-callchain-explorer 的索引 / caller 查询能力，供 CodeSage 当工具用。

**Schema 自适应**：老版 indexer 生成的 db 里 symbols 表可能没有 parent_class 列，
这里直接自己写 SQL 查询，检测列存在性后降级 —— 而不是依赖 explorer 的 searcher.py。
"""
import os, sys, shutil, sqlite3
from typing import List, Dict, Optional

# ---- 自动注入 ripgrep 目录 ----
_RG_CANDIDATES = [
    os.environ.get("RG_BIN_DIR", ""),
    r"G:\tools\ripgrep\ripgrep-14.1.1-x86_64-pc-windows-msvc",
]
if shutil.which("rg") is None:
    for _d in _RG_CANDIDATES:
        if _d and os.path.exists(os.path.join(_d, "rg.exe")):
            os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
            break

# ---- 路径接入（拿 explorer 的 callers.find_callers 和 file_context） ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPLORER_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "ue-callchain-explorer"))
if _EXPLORER_ROOT not in sys.path and os.path.isdir(_EXPLORER_ROOT):
    sys.path.insert(0, _EXPLORER_ROOT)

from ue_explorer.index.callers import find_callers as _find_callers
from ue_explorer.index.searcher import file_context as _file_ctx


# ---- db 路径解析 ----
# 现在我们的默认 db 是用户提供的全量库，不再依赖 explorer 的 latest_db
DEFAULT_DB = r"g:\tools\SourceCode_db\symbols_20260702_1913_e-Source_g-Source.db"


def resolve_db_path(configured: str = "") -> str:
    """
    优先级: 显式配置 > DEFAULT_DB > 报错
    """
    if configured:
        if not os.path.exists(configured):
            raise FileNotFoundError(f"配置的 db_path 不存在: {configured}")
        return configured
    if os.path.exists(DEFAULT_DB):
        return DEFAULT_DB
    raise FileNotFoundError(f"未找到默认索引 db: {DEFAULT_DB}")


# ---- schema 自适应 ----
_schema_cache: Dict[str, set] = {}

def _cols(db: sqlite3.Connection, table: str = "symbols") -> set:
    """列出 symbols 表的实际列名，缓存到进程内。"""
    key = getattr(db, "_perf_sleuth_db_id", None) or str(id(db))
    if key in _schema_cache:
        return _schema_cache[key]
    cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    _schema_cache[key] = cols
    return cols


# ---- 核心查询 ----
def lookup_symbol(name: str, type_filter: Optional[str] = None,
                  only_impl: bool = False, limit: int = 30,
                  db_path: Optional[str] = None) -> List[Dict]:
    """
    查符号定义。返回 [{type, name, file, line, parent_class?, is_impl}, ...]

    parent_class 只有在 db schema 里存在该列时才填。
    """
    p = db_path or resolve_db_path()
    db = sqlite3.connect(p)
    cols = _cols(db)
    has_parent = "parent_class" in cols
    has_ret = "return_type" in cols

    select_cols = ["type", "name"]
    if has_ret: select_cols.append("return_type")
    if has_parent: select_cols.append("parent_class")
    select_cols += ["is_impl", "file", "line"]
    sel_sql = ", ".join(select_cols)

    where = ["name = ?"]
    params: list = [name]
    if type_filter:
        where.append("type = ?")
        params.append(type_filter)
    if only_impl:
        where.append("is_impl = 1")

    sql = f"SELECT {sel_sql} FROM symbols WHERE {' AND '.join(where)} LIMIT {int(limit)}"
    rows = db.execute(sql, params).fetchall()

    # 未命中则降级 LIKE
    if not rows:
        where[0] = "name LIKE ?"
        params[0] = f"%{name}%"
        sql = f"SELECT {sel_sql} FROM symbols WHERE {' AND '.join(where)} LIMIT {int(limit)}"
        rows = db.execute(sql, params).fetchall()

    out: List[Dict] = []
    for r in rows:
        d = dict(zip(select_cols, r))
        d["is_impl"] = bool(d.get("is_impl", 0))
        if not has_parent:
            d["parent_class"] = None
        out.append(d)
    return out


def class_hierarchy(class_name: str, db_path: Optional[str] = None,
                    max_depth: int = 10) -> List[Dict]:
    """
    向上查继承链。老 schema 无 parent_class 时返回空 + 说明。
    """
    p = db_path or resolve_db_path()
    db = sqlite3.connect(p)
    if "parent_class" not in _cols(db):
        return [{
            "name": class_name,
            "parent": None,
            "note": "当前索引 db 无 parent_class 列，无法追继承链；可用 ripgrep 搜 `class ClassName :` 手动查",
        }]

    chain = []
    seen = set()
    current = class_name
    for _ in range(max_depth):
        if current in seen:
            break
        seen.add(current)
        row = db.execute(
            "SELECT name, parent_class, file, line FROM symbols "
            "WHERE type='class' AND name=? "
            "ORDER BY (parent_class IS NULL), line LIMIT 1", (current,)
        ).fetchone()
        if not row:
            break
        chain.append({"name": row[0], "parent": row[1], "file": row[2], "line": row[3]})
        if not row[1]:
            break
        current = row[1].split("::")[-1].split("<")[0]
    return chain


def subclasses(base: str, db_path: Optional[str] = None) -> List[Dict]:
    """反向查子类。老 schema 无 parent_class 时返回空。"""
    p = db_path or resolve_db_path()
    db = sqlite3.connect(p)
    if "parent_class" not in _cols(db):
        return []
    rows = db.execute(
        "SELECT type,name,is_impl,file,line FROM symbols "
        "WHERE type='class' AND (parent_class=? OR parent_class LIKE ?)",
        (base, f"%::{base}")
    ).fetchall()
    return [
        {"type": r[0], "name": r[1], "is_impl": bool(r[2]),
         "file": r[3], "line": r[4]}
        for r in rows
    ]


def find_callers(symbol: str, roots: List[str], max_hits: int = 60) -> List[Dict]:
    """ripgrep 找调用点。"""
    return _find_callers(symbol, roots=roots, max_hits=max_hits)


def read_file_window(filepath: str, line_num: int, context: int = 30) -> str:
    """读取文件片段（带行号）。"""
    return _file_ctx(filepath, line_num, context=context)
