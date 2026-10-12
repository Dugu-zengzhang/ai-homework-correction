# -*- coding: utf-8 -*-
"""T-B05 单元测试：RAG 按学段 / 教材版本标签过滤检索。

覆盖测试用例（对应遗留清单「知识库来源扩充 / 检索口径对齐」）：
  RAG-13 入库时切片打标签（doc 与 chunk 双层）
  RAG-14 按学段过滤：指定学段后不含其他学段资料
  RAG-15 按教材版本过滤
  RAG-16 旧索引无标签数据兼容：视为通用资料，任何过滤下可召回，不报错
  RAG-17 标签随索引持久化，引擎重载后不丢失
  RAG-18 update_doc_labels 补打 / 清除标签；不存在的文档抛 KeyError
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


class TestRagLabels:
    """RAG-13 ~ RAG-18：标签入库、过滤检索、旧数据兼容、持久化、补标。"""

    @pytest.mark.unit
    def test_add_file_stamps_labels_on_doc_and_chunks(self, rag_engine_sandbox, tmp_path):
        """RAG-13：入库时传入 stage / textbook_version → 文档与全部切片都带标签。"""
        f = _write(tmp_path, "初中数学.txt",
                   "一元二次方程求根公式 x=(-b±√(b²-4ac))/2a\n\n配方法解方程步骤示例。")
        doc = rag_engine_sandbox.add_file(
            f, source="upload", stage="初中", textbook_version="人教版")
        assert doc["stage"] == "初中"
        assert doc["textbook_version"] == "人教版"
        chunks = [c for c in rag_engine_sandbox.chunks if c["doc_id"] == doc["id"]]
        assert len(chunks) >= 1
        assert all(c["stage"] == "初中" for c in chunks)
        assert all(c["textbook_version"] == "人教版" for c in chunks)
        # 检索结果中暴露标签字段
        hits = rag_engine_sandbox.search("一元二次方程")
        assert hits and hits[0]["stage"] == "初中"
        assert hits[0]["textbook_version"] == "人教版"

    @pytest.mark.unit
    def test_search_filters_by_stage(self, rag_engine_sandbox, tmp_path):
        """RAG-14：指定学段检索 → 结果不包含其他学段资料（核心验收标准）。"""
        f_primary = _write(tmp_path, "小学方程初步.txt",
                           "简易方程：用字母表示数，等式的性质，解简易方程入门。")
        f_junior = _write(tmp_path, "初中方程.txt",
                          "简易方程进阶与一元二次方程，解简易方程的拓展。")
        rag_engine_sandbox.add_file(f_primary, source="folder", stage="小学")
        rag_engine_sandbox.add_file(f_junior, source="folder", stage="初中")

        hits = rag_engine_sandbox.search("简易方程", stage="小学")
        assert len(hits) >= 1
        assert all(h["stage"] in (None, "小学") for h in hits), \
            f"指定小学后召回了其他学段资料：{hits}"
        assert any(h["doc_name"] == "小学方程初步.txt" for h in hits)

    @pytest.mark.unit
    def test_search_filters_by_textbook_version(self, rag_engine_sandbox, tmp_path):
        """RAG-15：同学段不同教材版本 → 按 textbook_version 过滤只留匹配版本。"""
        f_rj = _write(tmp_path, "人教版物理.txt", "光的折射定律，折射角与入射角的关系。")
        f_sk = _write(tmp_path, "苏科版物理.txt", "光的折射现象，折射角和入射角比较。")
        rag_engine_sandbox.add_file(f_rj, source="folder",
                                    stage="初中", textbook_version="人教版")
        rag_engine_sandbox.add_file(f_sk, source="folder",
                                    stage="初中", textbook_version="苏科版")

        hits = rag_engine_sandbox.search("光的折射", textbook_version="人教版")
        assert len(hits) >= 1
        assert all(h["textbook_version"] in (None, "人教版") for h in hits)
        assert any(h["doc_name"] == "人教版物理.txt" for h in hits)

    @pytest.mark.unit
    def test_untagged_legacy_data_recalled_under_any_filter(self, rag_engine_sandbox, tmp_path):
        """RAG-16：旧索引无标签切片 = 通用资料，任何过滤条件下均可召回、不报错；
        显式带了不匹配标签的切片才被过滤。"""
        f_legacy = _write(tmp_path, "旧版通用资料.txt",
                          "简易方程通用讲解：等式两边同时加减同一个数，等式仍然成立。")
        f_tagged = _write(tmp_path, "高中专属资料.txt",
                          "简易方程在高中导数中的应用（高中专属内容）。")
        rag_engine_sandbox.add_file(f_legacy, source="folder")          # 无标签（旧数据）
        rag_engine_sandbox.add_file(f_tagged, source="folder", stage="高中")

        hits = rag_engine_sandbox.search("简易方程", stage="小学")
        names = {h["doc_name"] for h in hits}
        assert "旧版通用资料.txt" in names, "无标签旧数据应在任何过滤下可召回"
        assert "高中专属资料.txt" not in names, "高中标签与小学过滤不匹配，应被过滤"
        # 不带过滤参数时行为与旧版一致：两者都可召回
        hits_all = rag_engine_sandbox.search("简易方程")
        names_all = {h["doc_name"] for h in hits_all}
        assert {"旧版通用资料.txt", "高中专属资料.txt"} <= names_all

    @pytest.mark.unit
    def test_labels_persist_across_engine_reload(self, rag_engine_sandbox,
                                                 sandbox_config, tmp_path):
        """RAG-17：索引重载后标签不丢失，过滤仍然生效。"""
        f = _write(tmp_path, "持久化验证.txt", "浮力大小等于排开液体的重力，阿基米德原理。")
        rag_engine_sandbox.add_file(f, source="folder",
                                    stage="初中", textbook_version="人教版")

        from rag_engine import RAGEngine
        engine2 = RAGEngine(index_path=str(sandbox_config["INDEX_PATH"]),
                            knowledge_dir=str(sandbox_config["KNOWLEDGE_DIR"]))
        doc = engine2.stats()["docs"][0]
        assert doc["stage"] == "初中"
        assert doc["textbook_version"] == "人教版"
        hits = engine2.search("阿基米德原理", stage="初中")
        assert len(hits) >= 1 and hits[0]["stage"] == "初中"
        # 学段不匹配时查不到该资料
        assert engine2.search("阿基米德原理", stage="高中") == []

    @pytest.mark.unit
    def test_update_doc_labels_relabel_clear_and_missing(self, rag_engine_sandbox, tmp_path):
        """RAG-18：旧数据补打标签 → 过滤生效；清空标签 → 恢复通用；不存在文档抛 KeyError。"""
        f = _write(tmp_path, "待补标.txt", "勾股定理：直角三角形两直角边的平方和等于斜边的平方。")
        doc = rag_engine_sandbox.add_file(f, source="folder")  # 旧数据无标签

        # 补打标签 → 学段过滤开始约束该资料
        updated = rag_engine_sandbox.update_doc_labels(
            doc["id"], stage="初中", textbook_version="北师大版")
        assert updated["stage"] == "初中"
        assert rag_engine_sandbox.search("勾股定理", stage="小学") == []
        hits = rag_engine_sandbox.search("勾股定理", stage="初中",
                                         textbook_version="北师大版")
        assert len(hits) >= 1

        # 清除标签 → 恢复为通用资料
        cleared = rag_engine_sandbox.update_doc_labels(doc["id"], stage="",
                                                       textbook_version="")
        assert cleared["stage"] == ""
        hits2 = rag_engine_sandbox.search("勾股定理", stage="小学")
        assert len(hits2) >= 1

        # 不存在的文档 → KeyError
        with pytest.raises(KeyError):
            rag_engine_sandbox.update_doc_labels(99999, stage="初中")
