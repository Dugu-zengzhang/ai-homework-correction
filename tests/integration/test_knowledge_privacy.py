# -*- coding: utf-8 -*-
"""T-E09 / D-02 集成用例：知识库统计与资料列表的敏感字段隔离。

验收：学生账号调用知识库接口不再拿到服务器绝对路径；
管理员仍可看到知识库目录与资料路径。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def j(body):
    return body.get_json(silent=True) or {}


@pytest.fixture()
def patched_engine(client, rag_engine_sandbox, monkeypatch, tmp_path):
    import app as app_module
    monkeypatch.setattr(app_module, "rag_engine", rag_engine_sandbox)
    f = tmp_path / "private_path.txt"
    f.write_text("勾股定理：直角三角形两直角边平方和等于斜边平方。", encoding="utf-8")
    rag_engine_sandbox.add_file(str(f), source="upload")
    return rag_engine_sandbox


class TestKnowledgePrivacy:
    @pytest.mark.integration
    def test_stats_student_hides_folder_and_abs_path(self, client, student_headers,
                                                     patched_engine):
        data = j(client.get("/api/knowledge/stats", headers=student_headers))["data"]
        assert "folder" not in data, "学生不应看到服务器知识库目录"
        assert data["doc_count"] == 1
        assert data["index_status"]["status"] == "ok"
        for doc in data["docs"]:
            assert "path" not in doc, "学生不应看到资料绝对路径"
            assert "filename" in doc

    @pytest.mark.integration
    def test_stats_admin_sees_folder_and_path(self, client, admin_headers, patched_engine):
        data = j(client.get("/api/knowledge/stats", headers=admin_headers))["data"]
        assert data["folder"]
        assert data["docs"][0]["path"].endswith("private_path.txt")

    @pytest.mark.integration
    def test_docs_student_has_no_abs_path(self, client, student_headers, patched_engine):
        docs = j(client.get("/api/knowledge/docs", headers=student_headers))["data"]
        assert docs
        for doc in docs:
            assert "path" not in doc
        # 响应文本中不应出现任何磁盘路径
        raw = client.get("/api/knowledge/docs", headers=student_headers).get_data(
            as_text=True
        )
        assert str(patched_engine.knowledge_dir) not in raw

    @pytest.mark.integration
    def test_docs_admin_keeps_path(self, client, admin_headers, patched_engine):
        docs = j(client.get("/api/knowledge/docs", headers=admin_headers))["data"]
        assert docs[0]["path"].endswith("private_path.txt")

    @pytest.mark.integration
    def test_knowledge_requires_login(self, client):
        assert client.get("/api/knowledge/stats").status_code == 401
        assert client.get("/api/knowledge/docs").status_code == 401
