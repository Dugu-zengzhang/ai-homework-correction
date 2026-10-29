# -*- coding: utf-8 -*-
"""AI 智能作业批改 Web 平台 —— Flask 后端主服务。

主链路：图片上传 → OCR 识别（可手动修正）→ RAG 知识库检索
        → DeepSeek AI 批改 → 结构化结果 → 错题自动落库。
统一响应结构：{"code": 0, "msg": "ok", "data": {...}}
"""
import datetime
from datetime import timezone as _dt_timezone
import json
import os
import sqlite3
import uuid
from functools import wraps

import jwt
from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import config
import llm_service
import ocr_service
from rag_engine import rag_engine

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ========================= 数据库 =========================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(str(config.DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(str(config.DB_PATH))
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject TEXT, grade TEXT,
            image_path TEXT, question_text TEXT,
            score TEXT, result_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mistakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            record_id INTEGER,
            subject TEXT, question TEXT, student_answer TEXT,
            correct_answer TEXT, knowledge_point TEXT,
            error_reason TEXT, analysis TEXT, image_path TEXT,
            mastered INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mistakes_user ON mistakes(user_id);
        CREATE INDEX IF NOT EXISTS idx_records_user ON records(user_id);
        """
    )

    def ensure_user(username, password, role):
        row = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            db.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
                (username, generate_password_hash(password), role,
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )

    ensure_user("admin", "admin123", "admin")
    ensure_user("student", "123456", "user")
    db.commit()
    db.close()


# ========================= 统一响应 / 鉴权 =========================

def ok(data=None, msg="ok"):
    return jsonify({"code": 0, "msg": msg, "data": data})


def fail(msg="请求失败", code=1, http_status=400):
    return jsonify({"code": code, "msg": msg, "data": None}), http_status


def make_token(user: dict) -> str:
    payload = {
        "uid": user["id"],
        "username": user["username"],
        "role": user["role"],
        "exp": datetime.datetime.now(_dt_timezone.utc)
        + datetime.timedelta(hours=config.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def auth_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token:
            return fail("未登录，请先登录", 401, 401)
        try:
            payload = jwt.decode(token, config.JWT_SECRET,
                                 algorithms=[config.JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return fail("登录已过期，请重新登录", 401, 401)
        except jwt.InvalidTokenError:
            return fail("登录凭证无效，请重新登录", 401, 401)
        g.user = {"id": payload["uid"], "username": payload["username"],
                  "role": payload["role"]}
        return func(*args, **kwargs)
    return wrapper


def admin_required(func):
    @wraps(func)
    @auth_required
    def wrapper(*args, **kwargs):
        if g.user["role"] != "admin":
            return fail("需要管理员权限", 403, 403)
        return func(*args, **kwargs)
    return wrapper


# ========================= 页面 =========================

@app.route("/")
def page_login():
    return send_from_directory(app.static_folder, "login.html")


@app.route("/app")
def page_app():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(str(config.UPLOAD_DIR), filename)


# ========================= 认证模块（Epic-07） =========================

@app.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not (3 <= len(username) <= 20) or not re_username(username):
        return fail("用户名需为 3-20 位字母、数字或下划线")
    if len(password) < 6:
        return fail("密码至少 6 位")
    db = get_db()
    if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
        return fail("该用户名已被注册")
    db.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
        (username, generate_password_hash(password), "user",
         datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.commit()
    row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    user = dict(row)
    return ok({"token": make_token(user), "user": public_user(user)}, "注册成功")


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    row = get_db().execute(
        "SELECT * FROM users WHERE username=?", (username,)
    ).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return fail("用户名或密码错误", http_status=401)
    user = dict(row)
    return ok({"token": make_token(user), "user": public_user(user)}, "登录成功")


@app.get("/api/auth/me")
@auth_required
def me():
    row = get_db().execute(
        "SELECT id, username, role, created_at FROM users WHERE id=?",
        (g.user["id"],),
    ).fetchone()
    return ok(dict(row))


def re_username(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]{3,20}", name))


def public_user(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


# ========================= 作业批改主链路（Epic-01/02/03/04） =========================

@app.post("/api/homework/upload")
@auth_required
def homework_upload():
    """第一步：上传作业图片 → 保存 → OCR 识别（结果可在前端手动修正）。"""
    file = request.files.get("image")
    if not file:
        return fail("请选择作业图片")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in config.ALLOWED_IMAGE_EXT:
        return fail("仅支持 JPG / PNG / WEBP / BMP 格式图片")

    content = file.read()
    if len(content) > config.MAX_IMAGE_MB * 1024 * 1024:
        return fail(f"图片不能超过 {config.MAX_IMAGE_MB}MB")

    filename = f"{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(str(config.UPLOAD_DIR), filename)
    with open(save_path, "wb") as f:
        f.write(content)

    ocr_result = ocr_service.ocr_image(save_path)
    return ok({
        "image_name": filename,
        "image_url": f"/uploads/{filename}",
        "ocr_text": ocr_result["text"],
        "ocr_engine": ocr_result["engine"],
        "ocr_note": ocr_result["note"],
    })


@app.post("/api/homework/grade")
@auth_required
def homework_grade():
    """第二步：RAG 检索 → AI 批改 → 保存记录 → 错题自动落库。"""
    data = request.get_json(silent=True) or {}
    subject = (data.get("subject") or "综合").strip()
    grade = (data.get("grade") or "").strip()
    # T-B05：学段 / 教材版本（五级级联需求的中期修改），用于 RAG 标签过滤；
    # 未传时降级为原有全库检索行为，保持向后兼容
    stage = (data.get("stage") or "").strip()
    textbook_version = (data.get("textbook_version") or "").strip()
    question_text = (data.get("question_text") or "").strip()
    image_name = data.get("image_name")
    if image_name:
        image_name = secure_filename(image_name)
        if not os.path.exists(os.path.join(str(config.UPLOAD_DIR), image_name)):
            image_name = None
    if not question_text:
        return fail("作业题目内容为空，请识别或手动录入后再提交")

    # RAG 召回（按学段 / 教材版本过滤，避免跨学段资料串库）
    contexts = rag_engine.search(
        question_text,
        stage=stage or None,
        textbook_version=textbook_version or None,
    )

    # AI 批改
    try:
        result = llm_service.grade_homework(subject, grade, question_text, contexts)
    except llm_service.LLMError as exc:
        return fail(str(exc))
    except Exception:
        return fail("批改服务暂时不可用，请稍后重试")

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    cur = db.execute(
        "INSERT INTO records (user_id, subject, grade, image_path, question_text,"
        " score, result_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (g.user["id"], subject, grade, image_name, question_text,
         str(result.get("score") or ""), json.dumps(result, ensure_ascii=False), now),
    )
    record_id = cur.lastrowid

    # 错题自动落库（仅判定为错误的题目；is_correct=null 不入错题）
    wrong_items = []
    for q in result.get("questions", []):
        if q.get("is_correct") is False:
            db.execute(
                "INSERT INTO mistakes (user_id, record_id, subject, question,"
                " student_answer, correct_answer, knowledge_point, error_reason,"
                " analysis, image_path, mastered, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
                (g.user["id"], record_id, subject,
                 q.get("question"), q.get("student_answer"),
                 q.get("correct_answer"), q.get("knowledge_point"),
                 q.get("error_reason"), q.get("analysis"), image_name, now),
            )
            wrong_items.append(q.get("question", "")[:30])
    db.commit()

    return ok({
        "record_id": record_id,
        "result": result,
        "contexts": contexts,
        "image_url": f"/uploads/{image_name}" if image_name else None,
        "wrong_count": len(wrong_items),
    }, "批改完成")


# ========================= 批改记录 & 错题本（Epic-05） =========================

@app.get("/api/records")
@auth_required
def list_records():
    rows = get_db().execute(
        "SELECT id, subject, grade, image_path, score, created_at FROM records"
        " WHERE user_id=? ORDER BY id DESC LIMIT 50",
        (g.user["id"],),
    ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        if item.get("image_path"):
            item["image_url"] = f"/uploads/{item['image_path']}"
        items.append(item)
    return ok(items)


@app.get("/api/mistakes")
@auth_required
def list_mistakes():
    subject = request.args.get("subject", "").strip()
    keyword = request.args.get("keyword", "").strip()
    mastered = request.args.get("mastered", "").strip()

    sql = ("SELECT id, subject, question, student_answer, correct_answer,"
           " knowledge_point, error_reason, analysis, image_path, mastered,"
           " created_at FROM mistakes WHERE user_id=?")
    params = [g.user["id"]]
    if subject:
        sql += " AND subject=?"
        params.append(subject)
    if mastered in ("0", "1"):
        sql += " AND mastered=?"
        params.append(int(mastered))
    if keyword:
        sql += " AND (question LIKE ? OR knowledge_point LIKE ?)"
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    sql += " ORDER BY id DESC LIMIT 200"

    items = []
    for r in get_db().execute(sql, params).fetchall():
        item = dict(r)
        if item.get("image_path"):
            item["image_url"] = f"/uploads/{item['image_path']}"
        items.append(item)
    return ok(items)


@app.put("/api/mistakes/<int:mistake_id>")
@auth_required
def update_mistake(mistake_id):
    data = request.get_json(silent=True) or {}
    mastered = 1 if data.get("mastered") else 0
    db = get_db()
    row = db.execute(
        "SELECT id FROM mistakes WHERE id=? AND user_id=?",
        (mistake_id, g.user["id"]),
    ).fetchone()
    if not row:
        return fail("错题不存在或无权操作", 403, 403)
    db.execute("UPDATE mistakes SET mastered=? WHERE id=?", (mastered, mistake_id))
    db.commit()
    return ok(msg="已更新掌握状态")


@app.delete("/api/mistakes/<int:mistake_id>")
@auth_required
def delete_mistake(mistake_id):
    db = get_db()
    row = db.execute(
        "SELECT id FROM mistakes WHERE id=? AND user_id=?",
        (mistake_id, g.user["id"]),
    ).fetchone()
    if not row:
        return fail("错题不存在或无权操作", 403, 403)
    db.execute("DELETE FROM mistakes WHERE id=?", (mistake_id,))
    db.commit()
    return ok(msg="已删除")


# ========================= RAG 知识库管理（Epic-03，管理员） =========================

def _public_doc(doc: dict) -> dict:
    """对外可见的资料字段（D-02）：去掉服务器绝对路径等敏感信息。"""
    return {k: v for k, v in doc.items() if k not in ("path", "source")}


@app.get("/api/knowledge/stats")
@auth_required
def knowledge_stats():
    """知识库统计：所有登录用户可读基础统计；
    服务器目录（folder）与资料绝对路径仅管理员可见（D-02）。"""
    stats = rag_engine.stats()
    is_admin = g.user["role"] == "admin"
    payload = {
        "doc_count": stats.get("doc_count", 0),
        "chunk_count": stats.get("chunk_count", 0),
        "index_status": stats.get("index_status"),
        "llm": llm_service.llm_status(),
        "docs": stats.get("docs", []) if is_admin
        else [_public_doc(d) for d in stats.get("docs", [])],
    }
    if is_admin:
        payload["folder"] = stats.get("folder")
    return ok(payload)


@app.get("/api/knowledge/docs")
@auth_required
def knowledge_docs():
    """资料列表：管理员可读完整字段；普通用户不返回绝对路径（D-02）。"""
    docs = rag_engine.stats()["docs"]
    if g.user["role"] != "admin":
        docs = [_public_doc(d) for d in docs]
    return ok(docs)


@app.post("/api/knowledge/upload")
@admin_required
def knowledge_upload():
    """上传电子版教材/习题资料（txt/md/pdf/docx），自动切片向量化入库。"""
    files = request.files.getlist("files")
    if not files:
        return fail("请选择要上传的资料文件")
    # T-B05：上传时可携带学段 / 教材版本标签（可选），入库时打在文档与切片上
    stage = (request.form.get("stage") or "").strip()
    textbook_version = (request.form.get("textbook_version") or "").strip()
    upload_dir = os.path.join(str(config.KNOWLEDGE_DIR), "uploaded")
    os.makedirs(upload_dir, exist_ok=True)
    results = []
    for file in files:
        original = secure_filename(file.filename) or "unnamed"
        ext = os.path.splitext(original)[1].lower()
        if ext not in config.ALLOWED_DOC_EXT:
            results.append({"filename": file.filename, "ok": False,
                            "msg": "不支持的类型（支持 txt/md/pdf/docx）"})
            continue
        # 防止同名覆盖：加短 uuid
        safe_name = f"{uuid.uuid4().hex[:8]}_{original}"
        path = os.path.join(upload_dir, safe_name)
        file.save(path)
        try:
            doc = rag_engine.add_file(path, source="upload",
                                      stage=stage or None,
                                      textbook_version=textbook_version or None)
            results.append({"filename": original, "ok": True,
                            "msg": f"已入库，{doc['chunk_count']} 个切片"})
        except Exception as exc:
            results.append({"filename": original, "ok": False, "msg": str(exc)})
    return ok({"results": results})


@app.post("/api/knowledge/scan")
@admin_required
def knowledge_scan():
    """扫描知识库文件夹：把新放入的电子书/资料自动入库。"""
    try:
        report = rag_engine.scan_folder()
    except Exception as exc:
        return fail(f"扫描失败：{exc}")
    return ok(report)


@app.delete("/api/knowledge/<int:doc_id>")
@admin_required
def knowledge_delete(doc_id):
    rag_engine.delete_doc(doc_id)
    return ok(msg="资料及其切片已删除")


@app.post("/api/knowledge/search")
@auth_required
def knowledge_search():
    """检索测试 / 供预览 RAG 召回效果。支持学段 / 教材版本标签过滤（T-B05）。"""
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return fail("请输入检索内容")
    return ok(rag_engine.search(
        query,
        stage=(data.get("stage") or "").strip() or None,
        textbook_version=(data.get("textbook_version") or "").strip() or None,
    ))


@app.put("/api/knowledge/<int:doc_id>/labels")
@admin_required
def knowledge_update_labels(doc_id):
    """T-B05：为已入库资料补打 / 更新学段、教材版本标签（旧数据治理）。"""
    data = request.get_json(silent=True) or {}
    try:
        doc = rag_engine.update_doc_labels(
            doc_id,
            stage=data.get("stage"),
            textbook_version=data.get("textbook_version"),
        )
    except KeyError:
        return fail("资料不存在", 404, 404)
    return ok({
        "id": doc["id"], "filename": doc["filename"],
        "stage": doc.get("stage") or None,
        "textbook_version": doc.get("textbook_version") or None,
    }, "标签已更新")


# ========================= 模型设置（管理员） =========================

@app.get("/api/settings")
@admin_required
def get_settings():
    s = config.load_settings()
    key = s.get("deepseek_api_key") or ""
    masked = (key[:6] + "****" + key[-4:]) if len(key) > 12 else ("****" if key else "")
    return ok({
        "deepseek_api_key_masked": masked,
        "deepseek_base_url": s.get("deepseek_base_url"),
        "deepseek_model": s.get("deepseek_model"),
        "chunk_size": s.get("chunk_size"),
        "top_k": s.get("top_k"),
        "min_score": s.get("min_score"),
    })


@app.put("/api/settings")
@admin_required
def update_settings():
    data = request.get_json(silent=True) or {}
    patch = {}
    if data.get("deepseek_api_key"):
        patch["deepseek_api_key"] = data["deepseek_api_key"].strip()
    if data.get("deepseek_base_url"):
        patch["deepseek_base_url"] = data["deepseek_base_url"].strip()
    if data.get("deepseek_model"):
        patch["deepseek_model"] = data["deepseek_model"].strip()
    for key in ("chunk_size", "top_k", "min_score"):
        if data.get(key) is not None:
            try:
                patch[key] = float(data[key]) if key == "min_score" else int(data[key])
            except (TypeError, ValueError):
                return fail(f"参数 {key} 格式不正确")
    config.save_settings(patch)
    return ok(msg="设置已保存")


@app.get("/api/llm/status")
@auth_required
def llm_status_api():
    return ok(llm_service.llm_status())


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
