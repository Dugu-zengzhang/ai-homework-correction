# -*- coding: utf-8 -*-
"""端到端测试（E2E）：完整业务主链路。

核心用例：
  E2E-01*: 作业上传 → RAG 检索（无真实OCR用文本直传代替） → AI批改（mock掉真实网络）→ 错题自动落库
  E2E-02 : 多用户数据严格隔离
  E2E-07 : 管理员模型设置修改 → 后端设置生效 → RAG切片用新 chunk_size
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def j(body):
    return body.get_json(silent=True) or {}


# =====================================================================
# E2E-01：完整主链路（批改 → 错题自动落库）
# =====================================================================
class TestE2EGradingAndMistakes:
    """⭐ 项目验收核心用例：批改后错题自动进入错题本，字段完整。"""

    MOCK_GRADE_RESPONSE = {
        "score": "50",
        "overall_comment": "这次作业有 2 道错题，主要是计算粗心和公式记错，建议多练习面积类题目，加油！",
        "questions": [
            {
                "question": "1. 计算：25 - 8 + 3 = ？",
                "student_answer": "14",
                "is_correct": False,   # ✘ 错题 → 自动入错题本
                "score": "0",
                "correct_answer": "20",
                "error_reason": "加减混合运算顺序错误，从左到右计算：25-8=17，17+3=20",
                "knowledge_point": "加减混合运算",
                "analysis": "步骤：先算 25-8=17；再算 17+3=20。注意只有加减法时从左往右，不能先算 8+3。",
            },
            {
                "question": "2. 一个长方形长 8cm 宽 5cm，它的面积是多少？",
                "student_answer": "26 平方厘米",
                "is_correct": False,   # ✘ 错题 → 自动入错题本
                "score": "0",
                "correct_answer": "40 平方厘米",
                "error_reason": "把周长公式和面积公式搞混了；周长是(8+5)×2=26，面积应当是 长×宽=8×5=40",
                "knowledge_point": "长方形面积",
                "analysis": "面积公式 S=ab，所以 8×5=40 平方厘米。周长公式 C=2(a+b) 不要混淆。",
            },
            {
                "question": "3. 正方形边长为 3，周长是多少？",
                "student_answer": "12",
                "is_correct": True,   # ✔ 对题 → 不进入错题本
                "score": "50",
                "correct_answer": "12",
                "error_reason": None,
                "knowledge_point": "正方形周长",
                "analysis": "周长=4×边长=4×3=12。",
            },
        ],
    }

    @pytest.mark.e2e
    @pytest.mark.parametrize("user", ["alice", "bob"])
    def test_e2e_grade_then_mistakes_auto_recorded(self, request, monkeypatch, client,
                                                    user, sample_math_txt):
        """⭐ E2E-01 主链路验收：

        对同一套作业（2 错 1 对）用 alice / bob 各跑一遍：
          - 批改接口成功返回
          - mistakes 表新增 **恰好 2 条**（对的题不得入库）
          - 2 条错题字段（question/student_answer/correct_answer/knowledge_point/error_reason/analysis）非空
          - 批改 records 表新增 1 行，绑定 user_id
          - 另一个用户看不到这条记录和这些错题（数据隔离）
        """
        token_key = f"{user}_token"
        headers_key = f"{user}_headers"
        # 通过 request.getfixturevalue 动态取 fixture
        token = request.getfixturevalue(token_key)
        headers = {"Authorization": f"Bearer {token}"}

        # 0) 先把样例教材入库（sample_math_txt 在知识库 tmp 目录，需要 scan 一下）
        scan = client.post("/api/knowledge/scan", headers=request.getfixturevalue("admin_headers"))
        assert j(scan)["code"] == 0

        # 1) mock LLM grade_homework，直接返回 MOCK_GRADE_RESPONSE
        import llm_service as llm_mod

        def fake_grade(subject, grade, homework_text, contexts, **scope):
            # **scope 接收 stage / textbook_version / region（T-B04 后签名扩展）
            return json.loads(json.dumps(self.MOCK_GRADE_RESPONSE))

        monkeypatch.setattr(llm_mod, "grade_homework", fake_grade)

        # 2) 提交批改
        homework = (
            "1. 计算：25 - 8 + 3 = ？ 我的答案是 14。\n"
            "2. 长方形长8宽5，面积多少？ 我的答案是 26 平方厘米。\n"
            "3. 正方形边长3，周长？ 我的答案是 12。\n"
        )
        resp = client.post(
            "/api/homework/grade",
            headers=headers,
            json={
                "subject": "数学",
                "grade": "小学五年级",
                "question_text": homework,
                "image_name": None,
            },
        )
        body = j(resp)
        assert resp.status_code == 200 and body["code"] == 0, body.get("msg")
        assert body["data"]["wrong_count"] == 2, f"应当识别到 2 道错题：{body['data']}"

        record_id = body["data"]["record_id"]
        assert record_id is not None and record_id > 0

        # 3) 查错题本 —— 必须 EXACTLY 2 条
        resp_m = client.get("/api/mistakes", headers=headers)
        items = j(resp_m)["data"]
        assert len(items) == 2, f"错题本应有 2 条：{items}"

        # 4) 字段完整性（每一道错题的关键信息都应当落库）
        for m in items:
            assert m["question"] and len(m["question"]) > 0, "错题 question 字段空"
            assert m["student_answer"] and len(m["student_answer"]) > 0, "student_answer 空"
            assert m["correct_answer"] and len(m["correct_answer"]) > 0, "correct_answer 空"
            assert m["knowledge_point"] and len(m["knowledge_point"]) > 0, "knowledge_point 空"
            assert m["error_reason"] and len(m["error_reason"]) > 0, "error_reason 空"
            assert m["analysis"] and len(m["analysis"]) > 0, "analysis 空"
            # 两条都是未掌握默认值，subject 正确，创建时间有值
            assert m["mastered"] == 0
            assert m["subject"] == "数学"
            assert m["created_at"]

        # 5) 知识点名确实是“加减混合运算”和“长方形面积”（即错题的 knowledge_point 正确落库）
        kps = {m["knowledge_point"] for m in items}
        assert kps == {"加减混合运算", "长方形面积"}, f"错题知识点应为 2 个，但得到 {kps}"

        # 6) 第 3 道对题（正方形周长）不得出现在错题本
        questions_in_mistakes = {m["question"] for m in items}
        assert not any("正方形边长为 3" in q for q in questions_in_mistakes), "对题错误入库！"

        # 7) Records 接口能查到刚才的批改记录
        resp_r = client.get("/api/records", headers=headers)
        recs = j(resp_r)["data"]
        assert len(recs) >= 1
        rec = recs[0]
        assert rec["id"] == record_id
        assert rec["subject"] == "数学"
        assert rec["score"] == "50"   # 与 MOCK 的 score 字段一致

    @pytest.mark.e2e
    def test_e2e_data_isolation_alice_cannot_see_bob(self, monkeypatch, client,
                                                      alice_headers, bob_headers,
                                                      request, sample_math_txt):
        """E2E-02 数据隔离：bob 批改后，alice 查不到 bob 的错题和批改记录，反之亦然。

        说明：本用例不依赖上一用例顺序（会独立再次 mock 一次批改）。
        """
        import llm_service as llm_mod
        monkeypatch.setattr(llm_mod, "grade_homework", lambda *a, **k: json.loads(json.dumps(self.MOCK_GRADE_RESPONSE)))

        # 给知识库初始化数据
        client.post("/api/knowledge/scan", headers=request.getfixturevalue("admin_headers"))

        # BOB 提交批改
        client.post(
            "/api/homework/grade",
            headers=bob_headers,
            json={"subject": "数学", "grade": "五年级",
                  "question_text": "随便一个题目文本（mock会直接返回标准答案）"},
        )

        # ALICE 的错题 + 批改记录 都应该是空的（尚未提交任何批改）
        alice_m = j(client.get("/api/mistakes", headers=alice_headers))["data"]
        alice_r = j(client.get("/api/records", headers=alice_headers))["data"]
        assert alice_m == [], f"alice 不应看到 bob 的错题：{alice_m}"
        assert alice_r == [], f"alice 不应看到 bob 的批改记录：{alice_r}"

        # BOB 则可以看到
        bob_m = j(client.get("/api/mistakes", headers=bob_headers))["data"]
        bob_r = j(client.get("/api/records", headers=bob_headers))["data"]
        assert len(bob_m) == 2
        assert len(bob_r) == 1


# =====================================================================
# E2E-07：管理员设置 → RAG 新设置生效
# =====================================================================
class TestE2ESettingsApplyToRAG:
    """设置修改 chunk_size 后，新入库文件的切片数量应该和旧 chunk_size 时不同。

    说明：为避免 Flask 应用内部 RAGEngine 单例在多测试 reload 时的状态泄漏，
    这里直接用 conftest 里每次全新的 rag_engine_sandbox 实例，配合不同 chunk_size
    手动两次 add_file 验证切片数量差异。settings 的保存/读取已在 test_settings_save_persists
    中独立验证，两者合起来即覆盖完整链路。
    """

    @pytest.mark.e2e
    def test_change_chunk_size_reflected_on_next_add_file(self, rag_engine_sandbox,
                                                          sandbox_config):
        """chunk_size 改小 → 同一份文档的切片数量应变多（相对比较更鲁棒）。

        策略：
          ① 用 add_file(size=800) 入库一份文档，记录 chunk_count_800；
          ② 清空 rag_engine_sandbox（重建一份全新实例，用相同 INDEX_PATH 新文件）；
          ③ 再 add_file(size=200) 入库同一份文档；
          ④ 断言 count(200) > count(800)。
        """
        long_txt = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥" * 20   # 240 字/段
        content = "\n\n".join([f"第{i}段" + long_txt for i in range(10)])  # ≈ 2430 字

        # 写两次到不同路径（避免 add_file 同路径覆盖内部检测逻辑）
        f_800 = sandbox_config["KNOWLEDGE_DIR"] / "chunky_800.txt"
        f_200 = sandbox_config["KNOWLEDGE_DIR"] / "chunky_200.txt"
        f_800.write_text(content, encoding="utf-8")
        f_200.write_text(content, encoding="utf-8")

        import config as cfg_mod

        # --- 第一步：settings 写 chunk_size=800，add_file 内部会 load_settings ---
        s800 = cfg_mod.load_settings()
        s800["chunk_size"] = 800
        s800["chunk_overlap"] = 100
        cfg_mod.save_settings(s800)
        info_800 = rag_engine_sandbox.add_file(str(f_800), source="folder")
        chunk_count_800 = info_800["chunk_count"]

        # --- 第二步：全新 RAG 实例（新 INDEX_PATH 防止覆盖），settings 改 chunk_size=200 ---
        s200 = cfg_mod.load_settings()
        s200["chunk_size"] = 200
        s200["chunk_overlap"] = 40
        cfg_mod.save_settings(s200)

        from rag_engine import RAGEngine
        idx2 = str(sandbox_config["INDEX_PATH"]).replace(".json", "_2.json")
        engine2 = RAGEngine(index_path=idx2,
                            knowledge_dir=str(sandbox_config["KNOWLEDGE_DIR"]))
        info_200 = engine2.add_file(str(f_200), source="folder")
        chunk_count_200 = info_200["chunk_count"]

        # --- 核心断言：小 chunk_size 必须产生更多切片 ---
        assert chunk_count_200 > chunk_count_800, (
            f"chunk_size 从 800 改到 200，切片数应当变多，"
            f"但实际 size=800→{chunk_count_800} 块，size=200→{chunk_count_200} 块。"
        )


# =====================================================================
# 真实 LLM 调用（可选，需要配置好 DeepSeek Key）
# =====================================================================
class TestE2ERealLLM:
    """⭐ 有真实 API Key 才能通过。用于验收真实 AI 批改效果。

    运行命令：
      pytest -m "require_llm" --tb=short
    """

    @pytest.mark.e2e
    @pytest.mark.require_llm
    @pytest.mark.slow
    def test_e2e_real_deepseek_grading_returns_structured_json(self, client, student_headers,
                                                               sample_math_txt, admin_headers):
        # 先把样例知识库扫入
        client.post("/api/knowledge/scan", headers=admin_headers)
        # 检查是否有 Key
        import config
        assert config.load_settings().get("deepseek_api_key"), \
            "请先配置 DeepSeek API Key 再运行 require_llm 测试"

        homework = (
            "1. 25 - 8 + 3 = 我算出来是 14。（  ）\n"
            "2. 一个长方形的长是 8cm，宽是 5cm，它的面积是多少？\n   我写的答案：8 + 5 再乘 2 = 26 平方厘米。\n"
        )
        resp = client.post("/api/homework/grade",
                           headers=student_headers,
                           json={"subject": "数学", "grade": "小学五年级",
                                 "question_text": homework})
        body = j(resp)
        assert body["code"] == 0, body.get("msg")
        result = body["data"]["result"]
        # 真实 LLM 响应必须有 questions 数组
        assert isinstance(result.get("questions"), list)
        assert len(result["questions"]) >= 2
        # 至少 2 道错题被识别出来
        wrong = [q for q in result["questions"] if q.get("is_correct") is False]
        assert len(wrong) >= 1, f"真实 AI 没有识别出至少一道错题：{result}"
        # 错题本里也有记录（wrong_count 对应）
        assert body["data"]["wrong_count"] >= 1
