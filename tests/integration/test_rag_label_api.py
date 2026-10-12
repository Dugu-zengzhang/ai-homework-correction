# -*- coding: utf-8 -*-
"""T-B05 集成测试：标签过滤在 Flask API 层的完整链路。

覆盖：
  上传接口携带标签 → docs 列表可见；
  检索接口按学段过滤 → 不返回其他学段；
  标签补标接口（admin 权限 / 404）；
  批改接口携带 stage → RAG 召回只含匹配学段资料（mock LLM，不消耗真实额度）。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def j(body):
    return body.get_json(silent=True) or {}


@pytest.fixture()
def patched_engine(client, rag_engine_sandbox, monkeypatch):
    """把 app 模块内的 rag_engine 单例替换为沙箱引擎，保证测试隔离与确定性。"""
    import app as app_module
    monkeypatch.setattr(app_module, "rag_engine", rag_engine_sandbox)
    return rag_engine_sandbox


class TestRagLabelAPI:
    @pytest.mark.integration
    def test_upload_with_labels_visible_in_docs(self, client, admin_headers,
                                                patched_engine, tmp_path):
        """上传时携带 stage/textbook_version → 文档列表能看到标签。"""
        f = tmp_path / "labeled_upload.txt"
        f.write_text("平行线判定：同位角相等，两直线平行。", encoding="utf-8")
        with open(str(f), "rb") as fh:
            resp = client.post(
                "/api/knowledge/upload",
                headers=admin_headers,
                data={"files": (fh, "labeled_upload.txt"),
                      "stage": "初中", "textbook_version": "人教版"},
                content_type="multipart/form-data",
            )
        assert j(resp)["code"] == 0
        docs = j(client.get("/api/knowledge/docs", headers=admin_headers))["data"]
        target = next(d for d in docs if "labeled_upload" in d["filename"])
        assert target["stage"] == "初中"
        assert target["textbook_version"] == "人教版"

    @pytest.mark.integration
    def test_search_api_filters_by_stage(self, client, admin_headers,
                                         patched_engine, tmp_path):
        """检索接口传 stage → 不返回其他学段资料；无标签资料仍返回。"""
        p1 = tmp_path / "a_primary.txt"
        p1.write_text("简易方程入门，等式性质一。", encoding="utf-8")
        p2 = tmp_path / "a_junior.txt"
        p2.write_text("简易方程拓展，配方法初步。", encoding="utf-8")
        patched_engine.add_file(str(p1), source="folder", stage="小学")
        patched_engine.add_file(str(p2), source="folder", stage="初中")

        resp = client.post("/api/knowledge/search", headers=admin_headers,
                           json={"query": "简易方程", "stage": "小学"})
        items = j(resp)["data"]
        assert len(items) >= 1
        assert all(i["stage"] in (None, "小学") for i in items)

    @pytest.mark.integration
    def test_labels_endpoint_update_404_and_forbidden(self, client, admin_headers,
                                                      student_headers, patched_engine,
                                                      tmp_path):
        """补标接口：admin 可更新并立即影响检索；不存在文档 404；学生 403。"""
        p = tmp_path / "legacy.txt"
        p.write_text("勾股定理与逆定理综合应用。", encoding="utf-8")
        doc = patched_engine.add_file(str(p), source="folder")  # 无标签旧数据

        # 学生无权补标
        resp_forbidden = client.put(f"/api/knowledge/{doc['id']}/labels",
                                    headers=student_headers,
                                    json={"stage": "初中"})
        assert resp_forbidden.status_code == 403

        # admin 补标
        resp = client.put(f"/api/knowledge/{doc['id']}/labels",
                          headers=admin_headers,
                          json={"stage": "初中", "textbook_version": "北师大版"})
        assert j(resp)["code"] == 0
        assert j(resp)["data"]["stage"] == "初中"

        # 补标后学段过滤生效
        items = j(client.post("/api/knowledge/search", headers=admin_headers,
                              json={"query": "勾股定理", "stage": "小学"}))["data"]
        assert all(i["doc_id"] != doc["id"] for i in items)

        # 不存在的文档 → 404
        resp404 = client.put("/api/knowledge/99999/labels",
                             headers=admin_headers, json={"stage": "初中"})
        assert resp404.status_code == 404

    @pytest.mark.integration
    def test_grade_passes_stage_to_rag_filter(self, client, student_headers,
                                              patched_engine, monkeypatch, tmp_path):
        """批改接口携带 stage → 召回 contexts 不含其他学段资料（mock LLM）。"""
        import llm_service as llm_mod
        monkeypatch.setattr(
            llm_mod, "grade_homework",
            lambda *a, **k: {"score": "90", "overall_comment": "好",
                             "questions": []})

        p1 = tmp_path / "g_primary.txt"
        p1.write_text("长方形面积等于长乘宽，小学数学重点。", encoding="utf-8")
        p2 = tmp_path / "g_junior.txt"
        p2.write_text("长方形面积在初中几何证明中的应用。", encoding="utf-8")
        patched_engine.add_file(str(p1), source="folder", stage="小学")
        patched_engine.add_file(str(p2), source="folder", stage="初中")

        resp = client.post("/api/homework/grade", headers=student_headers,
                           json={"subject": "数学", "grade": "五年级", "stage": "小学",
                                 "question_text": "长方形面积怎么算？"})
        body = j(resp)
        assert body["code"] == 0, body.get("msg")
        ctx_names = {c["doc_name"] for c in body["data"]["contexts"]}
        assert "g_junior.txt" not in ctx_names, "指定小学后不应召回初中资料"
