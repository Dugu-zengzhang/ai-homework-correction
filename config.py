# -*- coding: utf-8 -*-
"""全局配置：路径、JWT、DeepSeek 与 RAG 参数。
敏感信息（API Key）通过 data/settings.json 或环境变量管理，不硬编码。
"""
import json
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"          # 作业图片
KNOWLEDGE_DIR = DATA_DIR / "knowledge"     # 电子版教材/习题资料放入此文件夹
DB_PATH = DATA_DIR / "app.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
INDEX_PATH = DATA_DIR / "rag_index.json"
SECRET_PATH = DATA_DIR / ".secret_key"
# 五级级联字典（地区/学段/学科/教材版本/年级）：静态文件，随代码进仓库
DICT_PATH = DATA_DIR / "dict.json"
# 专门存放 DeepSeek API Key 的文本文件（项目根目录）
API_KEY_PATH = BASE_DIR / "api_key.txt"

for _d in (DATA_DIR, UPLOAD_DIR, KNOWLEDGE_DIR, KNOWLEDGE_DIR / "uploaded"):
    _d.mkdir(parents=True, exist_ok=True)


def _load_jwt_secret() -> str:
    if SECRET_PATH.exists():
        value = SECRET_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(32)
    SECRET_PATH.write_text(value, encoding="utf-8")
    return value


JWT_SECRET = _load_jwt_secret()
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 72

DEFAULT_SETTINGS = {
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-chat",
    # RAG 参数
    "chunk_size": 500,
    "chunk_overlap": 100,
    "top_k": 6,
    "min_score": 0.05,
}


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            settings.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    # 从项目根目录的 api_key.txt 读取 Key（自动识别 sk- 开头的一行）
    if not settings.get("deepseek_api_key") and API_KEY_PATH.exists():
        for line in API_KEY_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("sk-"):
                settings["deepseek_api_key"] = line
                break
    # 环境变量优先级最高
    if os.getenv("DEEPSEEK_API_KEY"):
        settings["deepseek_api_key"] = os.getenv("DEEPSEEK_API_KEY")
    if os.getenv("DEEPSEEK_BASE_URL"):
        settings["deepseek_base_url"] = os.getenv("DEEPSEEK_BASE_URL")
    if os.getenv("DEEPSEEK_MODEL"):
        settings["deepseek_model"] = os.getenv("DEEPSEEK_MODEL")
    return settings


def save_settings(patch: dict) -> dict:
    current = load_settings()
    current.update({k: v for k, v in patch.items() if v is not None})
    SETTINGS_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return current


ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
ALLOWED_DOC_EXT = {".txt", ".md", ".pdf", ".docx"}
MAX_IMAGE_MB = 10
