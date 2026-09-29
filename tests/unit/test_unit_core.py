# -*- coding: utf-8 -*-
"""单元测试：纯函数 / 纯算法逻辑，不依赖 Flask、外部服务。
对应测试用例：OCR-03、RAG-01~05、RAG-12、AI-05/06/07/09 等纯逻辑部分。
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# =====================================================================
# OCR 服务模块 —— ocr_service.py 的纯函数
# =====================================================================
class TestOCRTextClean:
    """对应测试用例 OCR-03：文本清洗单元测试。"""

    @pytest.mark.unit
    def test_crlf_normalized_to_single_lf(self):
        from ocr_service import clean_ocr_text
        assert clean_ocr_text("a\r\nb\r\nc") == "a\nb\nc"

    @pytest.mark.unit
    def test_multiple_blank_lines_collapsed(self):
        from ocr_service import clean_ocr_text
        # 3 个以上换行 → 折叠为最多 2 个（即一个空行）
        result = clean_ocr_text("1\n\n\n\n2\n\n\n\n\n3")
        # 多个连续空行：每段最多保留一个空行
        assert "\n\n\n" not in result
        assert result.count("\n\n") <= 2

    @pytest.mark.unit
    def test_tab_and_spaces_squeezed(self):
        from ocr_service import clean_ocr_text
        assert clean_ocr_text("a\t\tb   c  \t d") == "a b c d"

    @pytest.mark.unit
    def test_empty_and_none_safe(self):
        from ocr_service import clean_ocr_text
        assert clean_ocr_text("") == ""
        assert clean_ocr_text(None) == ""  # noqa —— 故意传 None

    @pytest.mark.unit
    def test_real_mixed_noise(self):
        from ocr_service import clean_ocr_text
        raw = "  1.  计算：25 - 8 + 3 =   \r\n  \r\n  \r\n  2. 长方形面积 = 长 × 宽   "
        out = clean_ocr_text(raw)
        assert "   " not in out
        assert "\r" not in out
        assert out.startswith("1. 计算：")
        assert out.endswith("长 × 宽")

    @pytest.mark.unit
    @pytest.mark.require_ocr
    def test_ocr_image_missing_file_degrades_gracefully(self):
        """OCR-01：传不存在的文件路径也不会抛异常，会返回 manual 模式。"""
        from ocr_service import ocr_image
        result = ocr_image("definitely_not_exists_xyz.png")
        assert "text" in result and "engine" in result and "note" in result
        assert result["engine"] == "manual"
        assert len(result["text"]) == 0


# =====================================================================
# RAG 模块 —— rag_engine.py：文档解析 / 切片 / 分词 / TF-IDF
# =====================================================================
class TestDocumentParsing:
    """RAG-01 多编码、RAG-02 docx、格式清洗等。"""

    @pytest.mark.unit
    def test_txt_utf8(self, tmp_path):
        from rag_engine import parse_file
        p = tmp_path / "a.txt"
        p.write_text("长方形面积=长×宽\n正方形周长=边长×4\n", encoding="utf-8")
        text = parse_file(str(p))
        assert "长方形面积=长×宽" in text
        assert "正方形周长=边长×4" in text

    @pytest.mark.unit
    def test_txt_gbk_fallback(self, tmp_path):
        """中文 txt 常见 GBK 编码 —— parse_file 多编码自动识别。"""
        from rag_engine import parse_file
        p = tmp_path / "gbk.txt"
        p.write_text("分数加法：先通分\n", encoding="gbk")
        text = parse_file(str(p))
        assert "分数加法：先通分" in text

    @pytest.mark.unit
    def test_text_is_cleaned_after_parse(self, tmp_path):
        from rag_engine import parse_file
        p = tmp_path / "x.txt"
        p.write_text("A\r\n\r\n\r\n\r\nB\r\n   C\r\n\n\nD", encoding="utf-8")
        text = parse_file(str(p))
        assert "\r" not in text
        assert "\n\n\n" not in text
        assert "\xa0" not in text  # 不间断空格过滤

    @pytest.mark.unit
    def test_unknown_ext_raises_runtime_error(self, tmp_path):
        from rag_engine import parse_file
        p = tmp_path / "weird.xyz"
        p.write_text("abc", encoding="utf-8")
        with pytest.raises(RuntimeError):
            parse_file(str(p))


class TestChunking:
    """RAG-03 / RAG-04 / RAG-05 切片策略。"""

    @pytest.mark.unit
    def test_short_paragraphs_merged_into_one_chunk(self):
        """10 段每段 30 字，chunk_size=500 → 应合并成一个大 chunk，避免碎片。"""
        from rag_engine import chunk_text
        paras = [f"第{i}段：" + "这是一段三十字左右的中文示例文本段落哦" for i in range(10)]
        text = "\n\n".join(paras)
        chunks = chunk_text(text, size=500, overlap=100)
        assert len(chunks) == 1
        assert "第0段" in chunks[0] and "第9段" in chunks[0]

    @pytest.mark.unit
    def test_very_long_paragraph_uses_sliding_window(self):
        """单段超长 → 定长滑窗，相邻块必须有 overlap。"""
        from rag_engine import chunk_text
        long_para = "甲乙丙丁戊己庚辛壬癸" * 100   # 1000 字
        chunks = chunk_text(long_para, size=200, overlap=50)
        assert len(chunks) >= 2
        # 相邻 chunk 应该有重叠（overlap 策略）
        for i in range(len(chunks) - 1):
            tail = chunks[i][-30:]
            assert tail in chunks[i + 1]

    @pytest.mark.unit
    def test_last_short_chunk_merged_into_previous(self):
        """RAG-05：末块太短，自动并入上一块，不产生碎片 chunk。"""
        from rag_engine import chunk_text
        # 构造：chunk1 450 字 + chunk2 20 字（<80阈值） → 最终只应有 1 块
        p1 = "中文" * 225 + "\n\n"           # 450 字
        p2 = "末尾" * 10                       # 20 字 < 80 阈值
        chunks = chunk_text(p1 + p2, size=500, overlap=100)
        assert len(chunks) == 1
        assert "末尾" in chunks[0]

    @pytest.mark.unit
    def test_chunk_preserves_paragraph_break_when_possible(self):
        """合并保留 \n\n 段落分隔符。"""
        from rag_engine import chunk_text
        text = "第一段内容A\n\n第二段内容B\n\n第三段内容C"
        chunks = chunk_text(text, size=500, overlap=50)
        assert len(chunks) == 1
        assert "\n\n" in chunks[0]


class TestTokenizeAndTFIDF:
    """RAG 检索核心：分词、TF-IDF 向量、余弦相似度。"""

    @pytest.mark.unit
    def test_tokenize_english_lowercase_words(self):
        from rag_engine import tokenize
        toks = tokenize("Hello HELLO World2024")
        # 英文词 & 数字：全部出现在分词结果
        assert "hello" in toks
        assert "world2024" in toks

    @pytest.mark.unit
    def test_tokenize_chinese_single_and_bigrams(self):
        from rag_engine import tokenize
        toks = tokenize("长方形面积")
        singles = ["长", "方", "形", "面", "积"]
        for s in singles:
            assert s in toks, f"中文单字 {s} 应被加入 token"
        bigrams = ["长方", "方形", "形面", "面积"]
        for b in bigrams:
            assert b in toks, f"中文二元词组 {b} 应被加入 token（提升中文检索召回）"

    @pytest.mark.unit
    def test_tfidf_cosine_identical_is_one(self):
        from rag_engine import TfidfEmbedder, tokenize
        docs = [
            tokenize("长方形的面积等于长乘以宽 公式 S=a×b"),
            tokenize("正方形周长等于边长乘4 公式 C=4a"),
            tokenize("分数加法先通分后分子相加"),
        ]
        emb = TfidfEmbedder()
        emb.fit(docs)
        v = emb.vectorize(docs[0])
        assert abs(TfidfEmbedder.cosine(v, v) - 1.0) < 1e-6

    @pytest.mark.unit
    def test_tfidf_cosine_ordering_matches_semantic(self):
        from rag_engine import TfidfEmbedder, tokenize
        docs = [
            tokenize("长方形面积 长 乘 宽 S=ab"),
            tokenize("正方形周长 边长 乘 4 C=4a"),
            tokenize("分数加法 先通分 然后 分子 相加"),
        ]
        emb = TfidfEmbedder()
        emb.fit(docs)
        q = emb.vectorize(tokenize("一个长方形 长8 宽5 求面积 公式"))
        sims = [TfidfEmbedder.cosine(q, emb.vectorize(d)) for d in docs]
        # 最相似的文档应该是 docs[0]（长方形面积）
        assert sims.index(max(sims)) == 0, f"余弦相似度排序错误：{sims}"

    @pytest.mark.unit
    def test_empty_doc_and_query_cosine_zero(self):
        from rag_engine import TfidfEmbedder
        assert TfidfEmbedder.cosine({}, {"a": 0.5}) == 0.0
        assert TfidfEmbedder.cosine({"a": 0.5}, {}) == 0.0


class TestRAGEngineEndToEndSearch:
    """RAG-06~RAG-12：完整 RAGEngine 行为（空库、入库、检索、删除、持久化、覆盖）。"""

    @pytest.mark.unit
    def test_empty_engine_search_returns_empty(self, rag_engine_sandbox):
        assert rag_engine_sandbox.search("任何内容") == []

    @pytest.mark.unit
    def test_add_file_and_search_matches(self, rag_engine_sandbox, tmp_path):
        from rag_engine import RAGEngine
        kb = tmp_path / "知识点.txt"
        kb.write_text(
            "【长方形面积】公式 S=长×宽。例：长8cm宽5cm面积=40平方厘米。\n\n"
            "【分数加法】同分母直接加分子，异分母先通分。",
            encoding="utf-8",
        )
        rag_engine_sandbox.add_file(str(kb), source="folder")
        hits = rag_engine_sandbox.search("一个长方形长 8 厘米宽 5 厘米，面积怎么算？")
        assert len(hits) >= 1
        assert "长方形" in hits[0]["content"]
        assert 0.0 <= hits[0]["score"] <= 1.0
        assert hits[0]["doc_name"] == "知识点.txt"

    @pytest.mark.unit
    def test_delete_doc_removes_its_chunks_from_search(self, rag_engine_sandbox, tmp_path):
        """RAG-09：删除 doc 后，该 doc 的内容不应再被召回。"""
        f1 = tmp_path / "f1.txt"
        f1.write_text("AAAAA 独一无二的关键词 宇宙大爆炸 ZZZZZ", encoding="utf-8")
        f2 = tmp_path / "f2.txt"
        f2.write_text("BBBBB 正常数学题 长方形面积 CCCCC", encoding="utf-8")
        d1 = rag_engine_sandbox.add_file(str(f1))
        d2 = rag_engine_sandbox.add_file(str(f2))
        rag_engine_sandbox.delete_doc(d1["id"])
        hits = rag_engine_sandbox.search("宇宙大爆炸")
        assert all("宇宙大爆炸" not in h["content"] for h in hits)
        # f2 仍能搜出来
        hits2 = rag_engine_sandbox.search("长方形面积")
        assert any("长方形面积" in h["content"] for h in hits2)

    @pytest.mark.unit
    def test_low_score_filter_drops_irrelevant(self, rag_engine_sandbox, tmp_path):
        """RAG-07：搜完全无关的关键词（min_score=0.05 默认阈值）→ 空列表。"""
        f = tmp_path / "x.txt"
        f.write_text("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥", encoding="utf-8")
        rag_engine_sandbox.add_file(str(f))
        hits = rag_engine_sandbox.search("量子力学 弦理论 爱因斯坦相对论", min_score=0.3)
        assert hits == []

    @pytest.mark.unit
    def test_same_path_added_twice_overwrites_instead_of_duplicate(self, rag_engine_sandbox, tmp_path):
        """RAG-12：重复入库同路径文件 → doc 总数不增加，内容更新为新版本。"""
        f = tmp_path / "same.txt"
        f.write_text("版本1：1+1=2", encoding="utf-8")
        d1 = rag_engine_sandbox.add_file(str(f), source="folder")
        old_total = rag_engine_sandbox.stats()["doc_count"]

        f.write_text("版本2：1+1=2，2+2=4，3+3=6", encoding="utf-8")
        d2 = rag_engine_sandbox.add_file(str(f), source="folder")
        new_total = rag_engine_sandbox.stats()["doc_count"]
        assert new_total == old_total, "同路径重复添加不应增加 doc 总数"
        hits = rag_engine_sandbox.search("2+2=4")
        assert len(hits) >= 1 and "版本2" in hits[0]["content"]


# =====================================================================
# LLM 服务模块 —— 纯逻辑部分（不发起真实 HTTP 请求）
# =====================================================================
class TestLLMPureLogic:
    """AI-05 JSON 模式降级、AI-06 非JSON重试解析、AI-09 空作业拦截。"""

    @pytest.mark.unit
    def test_extract_json_strict_valid(self):
        from llm_service import _extract_json
        data = _extract_json('{"score": "80", "questions": []}')
        assert data["score"] == "80"
        assert data["questions"] == []

    @pytest.mark.unit
    def test_extract_json_surrounded_by_extra_text(self):
        """AI-06：模型输出了前言后语，_extract_json 用正则抓出中间 JSON 块。"""
        from llm_service import _extract_json
        raw = "好的老师，下面是批改结果～\n```json\n{\"score\":\"90\",\"overall_comment\":\"做得不错\"}\n```\n好的加油！"
        data = _extract_json(raw)
        assert data["score"] == "90"
        assert data["overall_comment"] == "做得不错"

    @pytest.mark.unit
    def test_extract_json_garbage_raises(self):
        from llm_service import _extract_json
        with pytest.raises(Exception):
            _extract_json("这根本不是一个 json 啊 12345")

    @pytest.mark.unit
    def test_grade_homework_raises_on_empty_text(self):
        """AI-09：作业内容为空 → 直接抛 LLMError，不发起真实 HTTP 请求。"""
        from llm_service import grade_homework, LLMError
        with pytest.raises(LLMError):
            grade_homework("数学", "五年级", "", [])

    @pytest.mark.unit
    def test_grade_homework_context_assembly(self, monkeypatch):
        """验证 grade_homework 组装 Prompt 时，RAG 上下文被正确拼接到模板里。"""
        import llm_service as llm_mod

        calls = []

        def fake_post(messages, timeout=60):
            calls.append(messages)
            return json.dumps({
                "score": "100",
                "overall_comment": "全部正确！",
                "questions": [{"question": "题1", "is_correct": True,
                               "student_answer": "A", "correct_answer": "A",
                               "knowledge_point": "加法", "analysis": "对的",
                               "score": "10"}]
            }, ensure_ascii=False)

        monkeypatch.setattr(llm_mod, "_post_chat", fake_post)

        contexts = [
            {"doc_name": "数学教材.pdf", "score": 0.88,
             "content": "1+1=2，2+2=4，3+3=6"},
        ]
        result = llm_mod.grade_homework(
            "数学", "小学三年级",
            "1. 1+1=？我算的是 2",
            contexts,
        )
        assert len(calls) == 1
        user_prompt = calls[0][1]["content"]
        # 用户 Prompt 必须包含 RAG 资料
        assert "[资料1]" in user_prompt
        assert "《数学教材.pdf》" in user_prompt
        assert "1+1=2" in user_prompt
        # 解析后的返回结构完整
        assert result["score"] == "100"
        assert result["overall_comment"] == "全部正确！"
        assert len(result["questions"]) == 1
        assert result["questions"][0]["knowledge_point"] == "加法"

    @pytest.mark.unit
    def test_grade_homework_no_knowledge_uses_note(self, monkeypatch):
        """当 RAG 未召回资料时，prompt 里应该出现“知识库未检索到相关资料”提示。"""
        import llm_service as llm_mod

        def fake_post(messages, timeout=60):
            return json.dumps({"score": null if False else "60", "overall_comment": "", "questions": []})

        monkeypatch.setattr(llm_mod, "_post_chat", fake_post)
        # 先触发一次调用以便断言
        prompts = {}

        def capture_post(messages, timeout=60):
            prompts["user"] = messages[1]["content"]
            return fake_post(messages, timeout)

        monkeypatch.setattr(llm_mod, "_post_chat", capture_post)
        llm_mod.grade_homework("数学", "五年级", "题1 xxxx", [])
        assert "知识库未检索到相关资料" in prompts["user"]
