# -*- coding: utf-8 -*-
"""pytest 全局 fixtures：临时数据库、临时 RAG 索引、Flask test client、测试用户、JWT token 等。"""
import os
import json
import sqlite3
import tempfile
import sys
from pathlib import Path

import pytest

# ======================================================================
# 版本兼容补丁：Werkzeug 3.x 去掉了 werkzeug.__version__，
# 导致旧版 Flask 构造 test_client 时报 AttributeError。
# 自动从 importlib.metadata 读取版本号并补回属性，不影响生产代码。
# ======================================================================
try:
    import werkzeug as _wz
    if not hasattr(_wz, "__version__"):
        try:
            from importlib.metadata import version as _ver
            _wz.__version__ = _ver("werkzeug")
        except Exception:
            _wz.__version__ = "3.0.0-fallback"
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config as _config_module   # noqa: E402
from rag_engine import RAGEngine  # noqa: E402


# ======================================================================
# 1. 独立测试数据库 —— 每个测试函数自动隔离（不会污染真实 data/app.db）
# ======================================================================
@pytest.fixture()
def tmp_db_path(tmp_path):
    """返回一个全新的临时 sqlite 文件路径，用完即删。"""
    return str(tmp_path / "test_app.db")


@pytest.fixture()
def init_tmp_db(tmp_db_path, monkeypatch):
    """在临时数据库上执行 app.init_db() 的建表逻辑，并返回路径。

    D-05：建表 DDL 只在 app.py 维护一份，测试基座不再复制 schema，
    新增字段只改 app.py 即可，避免两处不同步。
    """
    import datetime
    from werkzeug.security import generate_password_hash

    import config as config_module
    monkeypatch.setattr(config_module, "DB_PATH", Path(tmp_db_path))

    import app as app_module
    app_module.init_db()

    # app.init_db() 只内置 admin / student；测试额外需要 alice / bob 做越权用例
    conn = sqlite3.connect(tmp_db_path)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def add_user(name, pwd, role):
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            (name, generate_password_hash(pwd), role, now),
        )

    add_user("alice", "123456", "user")
    add_user("bob", "123456", "user")
    conn.commit()
    conn.close()
    yield tmp_db_path


# ======================================================================
# 2. 覆盖 config.py 的路径 —— 全部指向 tmp_path，实现测试沙箱
# ======================================================================
@pytest.fixture()
def sandbox_config(monkeypatch, tmp_path, init_tmp_db):
    """
    把 config 模块里所有路径（DB_PATH / UPLOAD_DIR / KNOWLEDGE_DIR / INDEX_PATH / SETTINGS_PATH）
    重定向到 tmp_path；初始化 settings.json 为空配置；返回一个 dict 包含全部新路径。
    """
    new_data = tmp_path / "data"
    new_uploads = new_data / "uploads"
    new_knowledge = new_data / "knowledge"
    new_knowledge_uploaded = new_knowledge / "uploaded"
    new_settings = new_data / "settings.json"
    new_index = new_data / "rag_index.json"
    new_secret = new_data / ".secret_key"
    new_api_key = tmp_path / "api_key.txt"

    for d in (new_data, new_uploads, new_knowledge, new_knowledge_uploaded):
        d.mkdir(parents=True, exist_ok=True)

    new_settings.write_text(
        json.dumps({
            "deepseek_api_key": "",
            "deepseek_base_url": "https://api.deepseek.com",
            "deepseek_model": "deepseek-chat",
            "chunk_size": 500,
            "chunk_overlap": 100,
            "top_k": 6,
            "min_score": 0.05,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    new_secret.write_text("test-secret-key-" + "a" * 32, encoding="utf-8")
    new_api_key.write_text("", encoding="utf-8")

    overrides = {
        "DB_PATH": Path(init_tmp_db),
        "DATA_DIR": new_data,
        "UPLOAD_DIR": new_uploads,
        "KNOWLEDGE_DIR": new_knowledge,
        "SETTINGS_PATH": new_settings,
        "INDEX_PATH": new_index,
        "SECRET_PATH": new_secret,
        "API_KEY_PATH": new_api_key,
    }
    for name, val in overrides.items():
        monkeypatch.setattr(_config_module, name, Path(val) if "DIR" in name or "PATH" in name else val)

    # 同步 monkeypatch 环境变量（避免 load_settings 走环境变量覆盖）
    for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(k, raising=False)

    yield overrides


# ======================================================================
# 3. 独立 RAG 引擎 —— 每个测试独享，索引互不干扰
# ======================================================================
@pytest.fixture()
def rag_engine_sandbox(sandbox_config, tmp_path):
    """返回一个全新的 RAGEngine（索引路径在 tmp_path/rag_index.json，初始为空）。"""
    idx_path = str(sandbox_config["INDEX_PATH"])
    k_dir = str(sandbox_config["KNOWLEDGE_DIR"])
    engine = RAGEngine(index_path=idx_path, knowledge_dir=k_dir)
    yield engine


# ======================================================================
# 4. Flask 测试客户端 —— 使用 sandbox_config，数据完全隔离
# ======================================================================
@pytest.fixture()
def client(sandbox_config):
    """
    返回 Flask test_client，使用沙箱数据库/配置；
    每次用例都新启动，互不污染。
    """
    # 清掉之前可能导入的 app 模块缓存
    for m in list(sys.modules.keys()):
        if m in ("app",):
            del sys.modules[m]

    import app as app_module

    # 重新初始化 DB（会用已经被 monkeypatch 的 config.DB_PATH → 指向临时库）
    app_module.init_db()

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


# ======================================================================
# 5. 用户 & Token 辅助 fixtures —— 快速获取鉴权请求头
# ======================================================================
def _login_and_get_token(client, username, password):
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200 and resp.get_json()["code"] == 0, \
        f"测试账号 {username} 登录失败：{resp.get_json()}"
    return resp.get_json()["data"]["token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_token(client):
    return _login_and_get_token(client, "admin", "admin123")


@pytest.fixture()
def student_token(client):
    return _login_and_get_token(client, "student", "123456")


@pytest.fixture()
def alice_token(client):
    return _login_and_get_token(client, "alice", "123456")


@pytest.fixture()
def bob_token(client):
    return _login_and_get_token(client, "bob", "123456")


@pytest.fixture()
def admin_headers(admin_token):
    return _auth_headers(admin_token)


@pytest.fixture()
def student_headers(student_token):
    return _auth_headers(student_token)


@pytest.fixture()
def alice_headers(alice_token):
    return _auth_headers(alice_token)


@pytest.fixture()
def bob_headers(bob_token):
    return _auth_headers(bob_token)


# ======================================================================
# 6. 示例数据 fixtures —— 可直接复用的 RAG 知识库题目 / 作业文本
# ======================================================================
@pytest.fixture()
def sample_math_txt(tmp_path, sandbox_config):
    """在知识库目录放一份小学数学知识点 txt，返回其绝对路径。"""
    content = (
        "【长方形面积】\n"
        "长方形的面积 = 长 × 宽，用字母表示 S = a × b。\n"
        "例如：一个长方形长 8 厘米，宽 5 厘米，面积是 8×5=40 平方厘米。\n\n"
        "【正方形周长】\n"
        "正方形周长 = 边长 × 4。\n\n"
        "【分数加法】\n"
        "同分母分数相加，分母不变，分子相加，结果要约分；"
        "异分母分数相加，先通分，再按同分母分数加法的法则计算。\n\n"
        "【加减混合运算】\n"
        "算式里只有加、减法，从左往右依次计算。有括号先算括号里的。\n"
        "例如：25 - 8 + 3 = 17 + 3 = 20。\n"
    )
    p = sandbox_config["KNOWLEDGE_DIR"] / "小学数学知识点示例.txt"
    p.write_text(content, encoding="utf-8")
    return str(p)


@pytest.fixture()
def homework_2questions_text():
    """一份包含 2 道错题的作业文本，OCR 后可直接提交批改。"""
    return (
        "1. 计算：25 - 8 + 3 = 14。（  ）\n"
        "我的答案是 14。\n\n"
        "2. 一个长方形长 8cm 宽 5cm，它的面积是多少？\n"
        "我的答案：8+5×2=26 平方厘米。\n"
    )
