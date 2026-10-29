# -*- coding: utf-8 -*-
"""接口集成测试：Flask API + SQLite 沙箱数据库 + RAG 沙箱。
覆盖测试用例：AUTH-01~11、（批改接口参数校验）、错题 CRUD、知识库上传/删除、管理员权限等。
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def j(body):
    """取 Flask 响应的 JSON body（dict）。"""
    return body.get_json(silent=True) or {}


# =====================================================================
# 认证模块 Epic-07 —— 注册 / 登录 / 权限 / 越权
# =====================================================================
class TestAuth:
    """AUTH-01 ~ AUTH-11 全量覆盖。"""

    # --- AUTH-01 注册 ---
    @pytest.mark.integration
    def test_register_success_returns_token_and_user(self, client):
        resp = client.post("/api/auth/register",
                           json={"username": "newuser01", "password": "123456"})
        data = j(resp)
        assert resp.status_code == 200
        assert data["code"] == 0, data.get("msg")
        assert "token" in data["data"]
        assert data["data"]["user"]["username"] == "newuser01"
        assert data["data"]["user"]["role"] == "user"
        assert data["data"]["user"]["id"] > 0

    # --- AUTH-02 重复注册拦截 ---
    @pytest.mark.integration
    def test_register_duplicate_username_blocked(self, client):
        client.post("/api/auth/register",
                    json={"username": "dup01", "password": "123456"})
        resp2 = client.post("/api/auth/register",
                            json={"username": "dup01", "password": "xxxxxx"})
        data = j(resp2)
        assert data["code"] != 0, "相同用户名不应能重复注册"
        assert "已被注册" in data.get("msg", "")

    # --- AUTH-03 密码不足6位 ---
    @pytest.mark.integration
    def test_register_short_password_rejected(self, client):
        resp = client.post("/api/auth/register",
                           json={"username": "shortpw", "password": "12345"})
        assert j(resp)["code"] != 0

    # --- AUTH-04 用户名格式（太短） ---
    @pytest.mark.integration
    def test_register_username_format_length(self, client):
        resp = client.post("/api/auth/register",
                           json={"username": "ab", "password": "123456"})
        assert j(resp)["code"] != 0

    # --- AUTH-05 正常登录 ---
    @pytest.mark.integration
    def test_login_success_returns_token(self, client):
        client.post("/api/auth/register",
                    json={"username": "loginok", "password": "123456"})
        resp = client.post("/api/auth/login",
                           json={"username": "loginok", "password": "123456"})
        assert j(resp)["code"] == 0
        assert len(j(resp)["data"]["token"]) > 30

    # --- AUTH-06 密码错误 ---
    @pytest.mark.integration
    def test_login_wrong_password_401(self, client):
        client.post("/api/auth/register",
                    json={"username": "wrongpw", "password": "123456"})
        resp = client.post("/api/auth/login",
                           json={"username": "wrongpw", "password": "wrongwrong"})
        assert resp.status_code == 401
        assert j(resp)["code"] != 0

    # --- AUTH-07 未登录访问业务接口 ---
    @pytest.mark.integration
    def test_access_mistakes_without_token_is_401(self, client):
        resp = client.get("/api/mistakes")
        assert resp.status_code == 401
        assert j(resp)["code"] == 401

    # --- AUTH-07 未登录访问批改接口 ---
    @pytest.mark.integration
    def test_access_grade_without_token_is_401(self, client):
        resp = client.post("/api/homework/grade",
                           json={"question_text": "题1"})
        assert resp.status_code == 401

    # --- AUTH-09 越权：A 的错题 B 不能修改 ---
    @pytest.mark.integration
    def test_mistake_cross_user_modify_blocked(self, client, alice_headers, bob_headers):
        """AUTH-09 核心安全：跨用户数据隔离（越权修改）。

        步骤：
          1) 先用 alice 身份手动往 DB 插一条属于她的错题（不依赖批改链路，避免 LLM 调用）
          2) bob 尝试对同一条 mistake_id 调用 PUT（标记已掌握）和 DELETE
          3) 必须全部 403 被拦截，不能成功
        """
        # 直接从沙箱 DB 查 alice 的 id，再插入一条错题
        import config as _cfg
        import sqlite3
        db_path = _cfg.DB_PATH if isinstance(_cfg.DB_PATH, str) else str(_cfg.DB_PATH)
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE username='alice'")
        alice_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO mistakes (user_id, subject, question, student_answer,"
            " correct_answer, knowledge_point, error_reason, analysis, mastered, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,0,'2026-01-01 00:00:00')",
            (alice_id, "数学", "1+1=？", "3", "2",
             "加法", "粗心", "1+1等于2，不是3"),
        )
        mistake_id = cur.lastrowid
        conn.commit()
        conn.close()
        assert mistake_id > 0

        # bob 尝试修改 → 必须 403
        resp_put = client.put(f"/api/mistakes/{mistake_id}",
                              headers=bob_headers,
                              json={"mastered": True})
        assert resp_put.status_code == 403, "越权 PUT 必须被后端拦截"

        # bob 尝试删除 → 必须 403
        resp_del = client.delete(f"/api/mistakes/{mistake_id}",
                                 headers=bob_headers)
        assert resp_del.status_code == 403, "越权 DELETE 必须被后端拦截"

        # alice 自己操作 → 应当成功
        resp_ok = client.put(f"/api/mistakes/{mistake_id}",
                             headers=alice_headers,
                             json={"mastered": True})
        assert resp_ok.status_code == 200 and j(resp_ok)["code"] == 0

    # --- AUTH-10 管理员权限拦截 ---
    @pytest.mark.integration
    def test_student_cannot_call_admin_endpoint(self, client, student_headers):
        """学生身份调用 /api/settings（仅 admin 可用） → 403。"""
        resp = client.get("/api/settings", headers=student_headers)
        assert resp.status_code == 403
        assert j(resp)["code"] == 403

    @pytest.mark.integration
    def test_admin_can_access_settings(self, client, admin_headers):
        resp = client.get("/api/settings", headers=admin_headers)
        assert resp.status_code == 200
        assert j(resp)["code"] == 0
        body = j(resp)["data"]
        assert "deepseek_model" in body
        assert "chunk_size" in body
        assert "top_k" in body
        assert "min_score" in body

    # --- AUTH-11 登录态：刷新 token 后 /me 可查 ---
    @pytest.mark.integration
    def test_auth_me_endpoint_returns_user_info(self, client, admin_headers):
        resp = client.get("/api/auth/me", headers=admin_headers)
        assert resp.status_code == 200
        d = j(resp)["data"]
        assert d["username"] == "admin"
        assert d["role"] == "admin"
        assert "created_at" in d


# =====================================================================
# 作业批改接口 Epic-01/02/03/04 —— 参数校验、无 Key 友好报错
# =====================================================================
class TestHomeworkGradeAPI:
    """作业批改 API 的非 LLM 调用相关部分。"""

    @pytest.mark.integration
    def test_upload_no_file_blocked(self, client, student_headers):
        """上传接口无 image 字段 → 提示请选择作业图片。"""
        resp = client.post("/api/homework/upload", headers=student_headers)
        assert j(resp)["code"] != 0
        assert "请选择" in j(resp).get("msg", "")

    @pytest.mark.integration
    def test_upload_bad_ext_blocked(self, client, student_headers, tmp_path):
        """上传非图片文件（exe）→ 格式拦截。"""
        bad = tmp_path / "bad.exe"
        bad.write_bytes(b"MZ not an image")
        with open(str(bad), "rb") as f:
            resp = client.post("/api/homework/upload",
                               headers=student_headers,
                               data={"image": (f, "bad.exe")},
                               content_type="multipart/form-data")
        assert j(resp)["code"] != 0
        assert "格式" in j(resp).get("msg", "") or "支持" in j(resp).get("msg", "")

    @pytest.mark.integration
    def test_grade_empty_question_blocked(self, client, student_headers):
        """批改接口 question_text 为空 → 拒绝。"""
        resp = client.post("/api/homework/grade",
                           headers=student_headers,
                           json={"subject": "数学", "grade": "五年级", "question_text": "   "})
        assert j(resp)["code"] != 0
        assert "空" in j(resp).get("msg", "") or "录入" in j(resp).get("msg", "")

    @pytest.mark.integration
    def test_grade_without_deepseek_key_returns_friendly_error(self, client, student_headers,
                                                                 sandbox_config, monkeypatch):
        """无 API Key → LLMError 被 app 捕获为 fail，提示清晰，不抛堆栈。"""
        # 确保当前 settings 里没有 key
        import config
        s = config.load_settings()
        s["deepseek_api_key"] = ""
        config.save_settings(s)

        resp = client.post("/api/homework/grade",
                           headers=student_headers,
                           json={"subject": "数学", "grade": "五年级",
                                 "question_text": "1+1=？ 学生答案=2"})
        data = j(resp)
        assert data["code"] != 0, "无 Key 时批改不能成功"
        assert "DeepSeek" in data.get("msg", "") or "API Key" in data.get("msg", "")

    @pytest.mark.integration
    def test_grade_invalid_region_rejected(self, client, student_headers):
        """字典外的地区一律拒绝（整改项 CR-15：region 此前未参与校验）。"""
        resp = client.post("/api/homework/grade",
                           headers=student_headers,
                           json={"subject": "数学", "grade": "七年级上", "stage": "初中",
                                 "textbook_version": "人教版", "region": "火星省",
                                 "question_text": "1+1=？ 学生答案=2"})
        data = j(resp)
        assert data["code"] != 0
        assert "地区" in data.get("msg", "")


# =====================================================================
# 错题 CRUD Epic-05
# =====================================================================
class TestMistakesCRUD:
    """E2E-03~05 筛选/更新/删除。"""

    @staticmethod
    def _insert_mistakes(db_path, user_id, rows):
        """rows 列表：每一项 (subject, question, mastered, knowledge_point)。"""
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        out = []
        for (subj, q, mastered, kp) in rows:
            cur.execute(
                "INSERT INTO mistakes (user_id, subject, question, student_answer,"
                " correct_answer, knowledge_point, error_reason, analysis, mastered, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (user_id, subj, q, "错答", "正答", kp, "原因", "解析",
                 mastered, "2026-01-01 00:00:00"),
            )
            out.append(cur.lastrowid)
        conn.commit()
        conn.close()
        return out

    @pytest.mark.integration
    def test_list_mistakes_empty(self, client, student_headers):
        resp = client.get("/api/mistakes", headers=student_headers)
        assert j(resp)["code"] == 0 and j(resp)["data"] == []

    @pytest.mark.integration
    def test_filter_by_subject(self, client, student_headers, sandbox_config):
        """E2E-03：学科筛选。"""
        import config, sqlite3
        db_path = str(config.DB_PATH)
        conn = sqlite3.connect(db_path)
        uid = conn.execute("SELECT id FROM users WHERE username='student'").fetchone()[0]
        conn.close()
        self._insert_mistakes(db_path, uid, [
            ("数学", "数学题1", 0, "分数"),
            ("语文", "语文题1", 0, "拼音"),
            ("数学", "数学题2", 1, "加法"),
        ])
        # 仅查数学
        resp = client.get("/api/mistakes?subject=数学", headers=student_headers)
        items = j(resp)["data"]
        assert len(items) == 2
        assert all(x["subject"] == "数学" for x in items)

    @pytest.mark.integration
    def test_filter_by_mastered(self, client, student_headers, sandbox_config):
        import config, sqlite3
        db_path = str(config.DB_PATH)
        conn = sqlite3.connect(db_path)
        uid = conn.execute("SELECT id FROM users WHERE username='student'").fetchone()[0]
        conn.close()
        self._insert_mistakes(db_path, uid, [
            ("数学", "未掌握", 0, "X"),
            ("数学", "已掌握", 1, "X"),
        ])
        resp = client.get("/api/mistakes?mastered=0", headers=student_headers)
        items = j(resp)["data"]
        assert len(items) == 1
        assert items[0]["question"] == "未掌握"

    @pytest.mark.integration
    def test_filter_by_keyword_on_question_and_kp(self, client, student_headers, sandbox_config):
        """E2E-04：关键词同时在题目文本和知识点中命中。"""
        import config, sqlite3
        db_path = str(config.DB_PATH)
        conn = sqlite3.connect(db_path)
        uid = conn.execute("SELECT id FROM users WHERE username='student'").fetchone()[0]
        conn.close()
        self._insert_mistakes(db_path, uid, [
            ("数学", "长方形面积题", 0, "长方形"),
            ("数学", "分数加法题", 0, "分数加法"),
            ("语文", "拼音题a a a", 0, "拼音"),
        ])
        resp = client.get("/api/mistakes?keyword=分数", headers=student_headers)
        items = j(resp)["data"]
        assert len(items) == 1 and items[0]["question"] == "分数加法题"

    @pytest.mark.integration
    def test_filter_by_textbook_version(self, client, student_headers, sandbox_config):
        """E2E-05：教材版本筛选（整改项 CR-13：前端发 textbook_version，后端曾只读 textbook）。"""
        import config, sqlite3
        db_path = str(config.DB_PATH)
        conn = sqlite3.connect(db_path)
        uid = conn.execute("SELECT id FROM users WHERE username='student'").fetchone()[0]
        for name, tb in (("人教版题目", "人教版"), ("苏科版题目", "苏科版")):
            conn.execute(
                "INSERT INTO mistakes (user_id, subject, grade, stage, textbook_version,"
                " question, correct_answer, knowledge_point, mastered, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,0,?)",
                (uid, "数学", "七年级上", "初中", tb, name, "参考答案", "X",
                 "2026-01-01 00:00:00"),
            )
        conn.commit()
        conn.close()

        # 前端实际提交的参数名
        items = j(client.get("/api/mistakes?textbook_version=人教版",
                             headers=student_headers))["data"]
        assert len(items) == 1, f"教材版本筛选失效：{items}"
        assert items[0]["question"] == "人教版题目"

        # 旧参数名仍兼容
        items_legacy = j(client.get("/api/mistakes?textbook=苏科版",
                                    headers=student_headers))["data"]
        assert len(items_legacy) == 1
        assert items_legacy[0]["question"] == "苏科版题目"

    @pytest.mark.integration
    def test_toggle_mastered_updates_db(self, client, student_headers, sandbox_config):
        import config, sqlite3
        db_path = str(config.DB_PATH)
        conn = sqlite3.connect(db_path)
        uid = conn.execute("SELECT id FROM users WHERE username='student'").fetchone()[0]
        conn.close()
        ids = self._insert_mistakes(db_path, uid, [("数学", "一题", 0, "X")])
        mid = ids[0]
        client.put(f"/api/mistakes/{mid}", headers=student_headers,
                   json={"mastered": True})
        # 直接查 DB 验证
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT mastered FROM mistakes WHERE id=?", (mid,)).fetchone()
        conn.close()
        assert row[0] == 1

    @pytest.mark.integration
    def test_delete_mistake_removes_from_db(self, client, student_headers, sandbox_config):
        import config, sqlite3
        db_path = str(config.DB_PATH)
        conn = sqlite3.connect(db_path)
        uid = conn.execute("SELECT id FROM users WHERE username='student'").fetchone()[0]
        conn.close()
        ids = self._insert_mistakes(db_path, uid, [("数学", "待删", 0, "X")])
        mid = ids[0]
        client.delete(f"/api/mistakes/{mid}", headers=student_headers)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT COUNT(*) FROM mistakes WHERE id=?", (mid,)).fetchone()
        conn.close()
        assert row[0] == 0


# =====================================================================
# 知识库 API Epic-03 管理员
# =====================================================================
class TestKnowledgeAPI:
    """RAG-08 / RAG-09 / RAG-10 / 设置保存。"""

    @pytest.mark.integration
    def test_upload_txt_as_admin_creates_doc(self, client, admin_headers, tmp_path):
        """RAG-08：管理员上传 txt → stats.doc_count +1。"""
        f = tmp_path / "uploaded_math.txt"
        f.write_text("平行四边形面积 = 底 × 高\n三角形面积 = 底 × 高 ÷ 2\n", encoding="utf-8")
        stats_before = j(client.get("/api/knowledge/stats", headers=admin_headers))["data"]
        with open(str(f), "rb") as fh:
            resp = client.post(
                "/api/knowledge/upload",
                headers=admin_headers,
                data={"files": (fh, "uploaded_math.txt")},
                content_type="multipart/form-data",
            )
        assert j(resp)["code"] == 0
        stats_after = j(client.get("/api/knowledge/stats", headers=admin_headers))["data"]
        assert stats_after["doc_count"] == stats_before["doc_count"] + 1

    @pytest.mark.integration
    def test_upload_bad_ext_rejected(self, client, admin_headers, tmp_path):
        """上传 .zip（不在 ALLOWED_DOC_EXT） → 单条结果 ok=False。"""
        f = tmp_path / "weird.zip"
        f.write_bytes(b"PK fake zip")
        with open(str(f), "rb") as fh:
            resp = client.post(
                "/api/knowledge/upload",
                headers=admin_headers,
                data={"files": (fh, "weird.zip")},
                content_type="multipart/form-data",
            )
        results = j(resp)["data"]["results"]
        assert len(results) == 1
        assert results[0]["ok"] is False

    @pytest.mark.integration
    def test_delete_doc_removes_from_stats(self, client, admin_headers, tmp_path):
        """RAG-09：删除文档后 stats 下降。"""
        # 先上传 2 个
        for i in range(2):
            f = tmp_path / f"d{i}.txt"
            f.write_text(f"文档{i}内容 ABCDEFGHIJKLMNOPQRSTUVWXYZ", encoding="utf-8")
            with open(str(f), "rb") as fh:
                client.post(
                    "/api/knowledge/upload",
                    headers=admin_headers,
                    data={"files": (fh, f"d{i}.txt")},
                    content_type="multipart/form-data",
                )
        docs = j(client.get("/api/knowledge/docs", headers=admin_headers))["data"]
        target_id = docs[0]["id"]
        client.delete(f"/api/knowledge/{target_id}", headers=admin_headers)
        docs2 = j(client.get("/api/knowledge/docs", headers=admin_headers))["data"]
        assert len(docs2) == len(docs) - 1
        assert target_id not in {d["id"] for d in docs2}

    @pytest.mark.integration
    def test_settings_save_persists(self, client, admin_headers):
        """设置接口修改模型名和 chunk_size → 再次读取能拿到新值。"""
        client.put("/api/settings", headers=admin_headers,
                   json={"deepseek_model": "deepseek-v3-turbo",
                         "chunk_size": 600, "top_k": 10})
        got = j(client.get("/api/settings", headers=admin_headers))["data"]
        assert got["deepseek_model"] == "deepseek-v3-turbo"
        assert got["chunk_size"] == 600
        assert got["top_k"] == 10

    @pytest.mark.integration
    def test_knowledge_search_returns_expected_structure(self, client, admin_headers, rag_engine_sandbox,
                                                          sandbox_config, tmp_path):
        """知识库检索测试接口结构：{doc_id, doc_name, chunk_index, content, score}。"""
        # 先往 sandbox 知识库塞一个文件（通过 add_file 直连 RAGEngine 或 upload 接口）
        f = sandbox_config["KNOWLEDGE_DIR"] / "search_demo.txt"
        f.write_text("一元二次方程求根公式：x = (-b ± √(b²-4ac)) / 2a", encoding="utf-8")
        # 直接通过扫描接口入库
        resp_scan = client.post("/api/knowledge/scan", headers=admin_headers)
        assert j(resp_scan)["code"] == 0
        resp = client.post("/api/knowledge/search", headers=admin_headers,
                           json={"query": "一元二次方程 求根公式"})
        items = j(resp)["data"]
        if len(items) > 0:
            first = items[0]
            for k in ("doc_id", "doc_name", "chunk_index", "content", "score"):
                assert k in first, f"检索结果缺少字段 {k}"
            assert 0.0 <= first["score"] <= 1.0


# =====================================================================
# 作业原图访问控制（整改项 CR-14：/uploads 曾完全公开）
# =====================================================================
class TestUploadImageAccess:
    """作业原图必须登录后可读，且仅本人与管理员可读。"""

    IMAGE = "audit_cr14.png"

    @staticmethod
    def _prepare(sandbox_config):
        """在沙箱上传目录放一张图，并给 student 落一条批改记录指向它。"""
        import config, sqlite3

        (Path(sandbox_config["UPLOAD_DIR"]) / TestUploadImageAccess.IMAGE).write_bytes(
            b"\x89PNG\r\n\x1a\n fake image bytes"
        )
        conn = sqlite3.connect(str(config.DB_PATH))
        uid = conn.execute("SELECT id FROM users WHERE username='student'").fetchone()[0]
        conn.execute(
            "INSERT INTO records (user_id, subject, grade, stage, textbook_version,"
            " image_path, question_text, score, result_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uid, "数学", "七年级上", "初中", "人教版", TestUploadImageAccess.IMAGE,
             "题目", "", "{}", "2026-01-01 00:00:00"),
        )
        conn.commit()
        conn.close()

    @pytest.mark.integration
    def test_anonymous_access_rejected(self, client, sandbox_config):
        self._prepare(sandbox_config)
        resp = client.get(f"/uploads/{self.IMAGE}")
        assert resp.status_code == 401

    @pytest.mark.integration
    def test_owner_can_read_image(self, client, student_headers, sandbox_config):
        self._prepare(sandbox_config)
        resp = client.get(f"/uploads/{self.IMAGE}", headers=student_headers)
        assert resp.status_code == 200
        assert resp.data.startswith(b"\x89PNG")

    @pytest.mark.integration
    def test_other_user_forbidden(self, client, bob_headers, sandbox_config):
        self._prepare(sandbox_config)
        resp = client.get(f"/uploads/{self.IMAGE}", headers=bob_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_admin_can_read_image(self, client, admin_headers, sandbox_config):
        self._prepare(sandbox_config)
        resp = client.get(f"/uploads/{self.IMAGE}", headers=admin_headers)
        assert resp.status_code == 200

    @pytest.mark.integration
    def test_token_query_param_accepted(self, client, student_token, sandbox_config):
        """<img src> 无法带请求头，因此 ?token= 也必须可用。"""
        self._prepare(sandbox_config)
        resp = client.get(f"/uploads/{self.IMAGE}?token={student_token}")
        assert resp.status_code == 200
