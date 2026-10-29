# -*- coding: utf-8 -*-
"""T-E09 回归用例：D-05 / D-06（索引）/ D-09（docx 表格）。

- D-05：测试基座改为调用 app.init_db()，conftest 不再维护第二份建表 DDL；
- D-06：索引文件损坏时暴露 index_status=corrupt，不静默空库、不影响启动；
- D-09：docx 表格文本（w:tbl / w:tr / w:tc）可被解析、入库并检索命中。
"""
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# =====================================================================
# D-05 测试基座：建表 DDL 单一来源
# =====================================================================
class TestTestBaseNoDuplicateDDL:
    @pytest.mark.unit
    def test_conftest_contains_no_create_table_ddl(self):
        """conftest 不得复制建表语句（D-05）。"""
        source = Path(__file__).resolve().parent.parent.joinpath("conftest.py").read_text(
            encoding="utf-8"
        )
        assert "CREATE TABLE" not in source.upper()

    @pytest.mark.unit
    def test_schema_created_by_app_init_db(self, init_tmp_db):
        """临时库的表结构来自 app.init_db()，且保留测试专用账号。"""
        conn = sqlite3.connect(init_tmp_db)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"users", "records", "mistakes"} <= tables

        user_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        assert {"id", "username", "password_hash", "role", "created_at"} <= user_cols

        usernames = {row[0] for row in conn.execute("SELECT username FROM users")}
        assert {"admin", "student", "alice", "bob"} <= usernames
        conn.close()


# =====================================================================
# D-06 索引状态：损坏不再静默
# =====================================================================
class TestIndexStatus:
    @pytest.mark.unit
    def test_missing_index_reports_missing(self, tmp_path):
        from rag_engine import RAGEngine
        engine = RAGEngine(index_path=str(tmp_path / "absent.json"),
                           knowledge_dir=str(tmp_path))
        assert engine.index_status["status"] == "missing"
        assert engine.stats()["index_status"]["status"] == "missing"
        assert engine.stats()["doc_count"] == 0

    @pytest.mark.unit
    def test_corrupt_index_reports_corrupt_and_keeps_file(self, tmp_path):
        from rag_engine import RAGEngine
        idx = tmp_path / "broken.json"
        idx.write_text("{ this is not json", encoding="utf-8")

        engine = RAGEngine(index_path=str(idx), knowledge_dir=str(tmp_path))

        assert engine.index_status["status"] == "corrupt"
        assert "损坏" in engine.index_status["message"]
        assert engine.stats()["doc_count"] == 0
        # 不覆盖损坏文件，便于管理员排查
        assert idx.read_text(encoding="utf-8") == "{ this is not json"

    @pytest.mark.unit
    def test_wrong_structure_reports_corrupt(self, tmp_path):
        from rag_engine import RAGEngine
        idx = tmp_path / "wrong.json"
        idx.write_text(json.dumps({"docs": "not-a-list"}), encoding="utf-8")
        engine = RAGEngine(index_path=str(idx), knowledge_dir=str(tmp_path))
        assert engine.index_status["status"] == "corrupt"

    @pytest.mark.unit
    def test_valid_index_reports_ok_and_search_works(self, tmp_path):
        from rag_engine import RAGEngine
        idx = tmp_path / "ok.json"
        idx.write_text(json.dumps({
            "docs": [{"id": 1, "filename": "a.txt", "path": "a.txt", "source": "folder",
                      "chunk_count": 1, "created_at": "2026-10-29 10:00:00"}],
            "chunks": [{"id": 1, "doc_id": 1, "chunk_index": 0,
                        "content": "长方形面积等于长乘宽"}],
        }, ensure_ascii=False), encoding="utf-8")
        engine = RAGEngine(index_path=str(idx), knowledge_dir=str(tmp_path))
        assert engine.index_status["status"] == "ok"
        hits = engine.search("长方形面积")
        assert hits and hits[0]["doc_name"] == "a.txt"


# =====================================================================
# D-09 docx 表格解析与检索
# =====================================================================
_DOCX_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _make_docx(path: Path, body_xml: str) -> Path:
    """构造最小 docx（只需 word/document.xml 即可被 parse_file 解析）。"""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {_DOCX_NS}><w:body>{body_xml}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return path


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _row(cells) -> str:
    return "<w:tr>" + "".join(
        f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in cells
    ) + "</w:tr>"


class TestDocxTableParsing:
    @pytest.fixture()
    def table_docx(self, tmp_path):
        body = (
            _p("以下是常用图形面积公式：")
            + "<w:tbl><w:tblPr/><w:tblGrid/>"
            + _row(["图形", "面积公式"])
            + _row(["长方形", "长×宽"])
            + _row(["正方形", "边长×边长"])
            + "</w:tbl>"
            + _p("表格结束。")
        )
        return _make_docx(tmp_path / "table.docx", body)

    @pytest.mark.unit
    def test_table_cells_extracted(self, table_docx):
        from rag_engine import parse_file
        text = parse_file(str(table_docx))
        assert "图形 | 面积公式" in text
        assert "长方形 | 长×宽" in text
        assert "正方形 | 边长×边长" in text
        # 不再把 <w:tblPr> 等标签当文本
        assert "w:tblPr" not in text
        assert "<w:" not in text

    @pytest.mark.unit
    def test_paragraph_still_extracted(self, table_docx):
        from rag_engine import parse_file
        text = parse_file(str(table_docx))
        assert "以下是常用图形面积公式：" in text
        assert "表格结束。" in text

    @pytest.mark.unit
    def test_table_text_can_be_indexed_and_searched(self, rag_engine_sandbox, table_docx):
        """docx 表格文字入库后可被 RAG 检索命中（D-09 验收④）。"""
        rag_engine_sandbox.add_file(str(table_docx), source="upload")
        hits = rag_engine_sandbox.search("正方形面积公式")
        assert hits, "表格内的知识点未被检索命中"
        assert any("正方形" in h["content"] for h in hits)
