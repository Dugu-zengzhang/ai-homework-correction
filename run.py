# -*- coding: utf-8 -*-
"""AI 智能作业批改平台 —— 启动脚本。

功能：
1. 检查 Python 版本（要求 >= 3.8）
2. 检查并安装 requirements.txt 中的必须依赖
3. 检查运行时目录结构（data/uploads、data/knowledge/uploaded）
4. 检查 DeepSeek API Key 是否配置（给出警告+配置指引，不阻塞启动）
5. 检查 5000 端口是否被占用（给出建议）
6. 初始化数据库（建表 + 默认账号）
7. 启动 Flask 后端服务
"""
import os
import sys
import socket
import subprocess
import importlib
from pathlib import Path


# ===================== 颜色输出（Windows 兼容） =====================

def _color_enable():
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


_color_enable()

R = "\033[91m"    # 红 - 错误/阻塞
G = "\033[92m"    # 绿 - 通过
Y = "\033[93m"    # 黄 - 警告/可选
B = "\033[94m"    # 蓝 - 信息
M = "\033[95m"    # 紫 - 标题
RE = "\033[0m"    # 重置

LINE = "=" * 72


def info(msg):    print(f"{B}[INFO]{RE}  {msg}")
def ok(msg):      print(f"{G}[ OK ]{RE}  {msg}")
def warn(msg):    print(f"{Y}[WARN]{RE}  {msg}")
def error(msg):   print(f"{R}[FAIL]{RE}  {msg}")
def title(msg):   print(f"\n{M}{msg}{RE}\n{'-'*len(msg)}")


# ===================== 路径定位 =====================

BASE_DIR = Path(__file__).resolve().parent
REQ_PATH = BASE_DIR / "requirements.txt"
APP_MODULE = "app"
APP_HOST = os.getenv("AI_HOMEWORK_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("AI_HOMEWORK_PORT", "5000"))


# ===================== 必须依赖 =====================

REQUIRED_PACKAGES = {
    "flask":      ("Flask>=2.3",       "Web 后端框架"),
    "requests":   ("requests>=2.31",   "HTTP 客户端（调用大模型 API）"),
    "jwt":        ("PyJWT>=2.8",       "JWT 登录鉴权"),
    "pypdf":      ("pypdf>=4.0",       "PDF 知识库解析"),
}

OPTIONAL_PACKAGES = {
    "pytesseract": ("pytesseract>=0.3.10", "本地 OCR（需额外安装 Tesseract 引擎 + 中文语言包 chi_sim）"),
    "PIL":         ("Pillow>=10.0",        "本地 OCR 图片读取"),
}


# ===================== 检查函数 =====================

def check_python_version():
    title("① 检查 Python 版本")
    ver = sys.version_info
    info(f"当前版本：Python {ver.major}.{ver.minor}.{ver.micro}")
    if ver < (3, 8):
        error("要求 Python >= 3.8（用到了 f-string、类型注解、importlib.metadata 等特性）")
        info("请升级 Python 后再试：https://www.python.org/downloads/")
        return False
    ok(f"Python {ver.major}.{ver.minor}.{ver.micro} 符合要求")
    return True


def _pkg_installed(name: str) -> bool:
    try:
        mod = importlib.import_module(name)
    except ImportError:
        return False
    return True


def check_dependencies():
    title("② 检查 Python 依赖包")
    if not REQ_PATH.exists():
        error(f"找不到 requirements.txt：{REQ_PATH}")
        return False

    missing_required = []
    missing_optional = []

    info("【必须依赖】")
    for import_name, (pip_name, desc) in REQUIRED_PACKAGES.items():
        if _pkg_installed(import_name):
            ok(f"  · {pip_name.split('>=')[0]:<16s} — {desc}")
        else:
            error(f"  · {pip_name.split('>=')[0]:<16s} — 未安装（{desc}）")
            missing_required.append(pip_name)

    info("【可选依赖（本地 OCR 功能用，未安装将自动降级为『手动录入识别文本』模式）】")
    for import_name, (pip_name, desc) in OPTIONAL_PACKAGES.items():
        if _pkg_installed(import_name):
            ok(f"  · {pip_name.split('>=')[0]:<16s} — {desc}")
        else:
            warn(f"  · {pip_name.split('>=')[0]:<16s} — 未安装（{desc}）")
            missing_optional.append(pip_name)

    if missing_required:
        warn(f"缺少 {len(missing_required)} 个必须依赖，开始自动安装……")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install",
                 "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
                 *missing_required]
            )
            ok("必须依赖安装成功！")
        except subprocess.CalledProcessError as e:
            error(f"自动安装失败（exit={e.returncode}），请手动执行：")
            print(f"       {sys.executable} -m pip install -r {REQ_PATH}")
            return False

    if missing_optional:
        warn(f"可选依赖未安装：{', '.join(p.split('>=')[0] for p in missing_optional)}")
        info("  如需本地 OCR，执行 pip install -r requirements-ocr.txt 并安装 Tesseract 引擎；")
        info("  跳过也不影响 AI 批改主链路（会自动降级为手动录入识别文本）。")

    return True


def check_directories():
    title("③ 检查运行时目录")
    dirs_to_check = [
        BASE_DIR / "data",
        BASE_DIR / "data" / "uploads",
        BASE_DIR / "data" / "knowledge",
        BASE_DIR / "data" / "knowledge" / "uploaded",
    ]
    for d in dirs_to_check:
        if d.exists() and d.is_dir():
            ok(f"  · {d.relative_to(BASE_DIR).as_posix()}/  已存在")
        else:
            d.mkdir(parents=True, exist_ok=True)
            ok(f"  · {d.relative_to(BASE_DIR).as_posix()}/  自动创建成功")
    return True


def check_api_key():
    title("④ 检查 DeepSeek API Key 配置")
    import config as _cfg
    settings = _cfg.load_settings()
    key = settings.get("deepseek_api_key", "")

    if not key:
        warn("尚未配置 DeepSeek API Key，AI 批改功能暂时不可用。")
        info("配置方式（任选其一，优先级从高到低）：")
        info("  1) 环境变量：  set DEEPSEEK_API_KEY=sk-xxxx       （Windows cmd）")
        info("                 $env:DEEPSEEK_API_KEY='sk-xxxx'   （PowerShell）")
        info(f"  2) 文本文件：  把 sk-xxxx 一行写入  {(_cfg.API_KEY_PATH).relative_to(BASE_DIR)}")
        info(f"  3) 后台界面：  启动后使用 admin/admin123 登录 → 知识库管理 → DeepSeek 模型设置")
        info("  4) 配置文件：  data/settings.json 中 deepseek_api_key 字段（不推荐，会被 .gitignore 忽略）")
        return False
    else:
        masked = key[:6] + "****" + key[-4:] if len(key) > 12 else "****"
        ok(f"已检测到 API Key：{masked}  （模型：{settings.get('deepseek_model','-')}  端点：{settings.get('deepseek_base_url','-')}）")
        return True


def check_port():
    title(f"⑤ 检查端口占用（{APP_HOST}:{APP_PORT}）")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((APP_HOST if APP_HOST != "0.0.0.0" else "127.0.0.1", APP_PORT))
        s.close()
        ok(f"端口 {APP_PORT} 空闲可用")
        return True
    except OSError:
        s.close()
        error(f"端口 {APP_PORT} 已被占用！")
        info("解决办法（任选其一）：")
        info(f"  1) 临时换端口启动：set AI_HOMEWORK_PORT=5001   然后再次运行本脚本")
        info(f"  2) Windows 查找占用进程：netstat -ano | findstr :{APP_PORT}，然后 taskkill /PID <pid> /F")
        info(f"  3) 直接编辑 run.py 顶部 APP_PORT 修改默认端口")
        return False


def init_database():
    title("⑥ 初始化数据库（建表 + 默认账号）")
    try:
        sys.path.insert(0, str(BASE_DIR))
        from app import init_db
        init_db()
        ok("数据库初始化完成：users / records / mistakes 三张表已就绪")
        info("默认账号：")
        info("  · 管理员：admin  /  admin123   （可管理知识库、修改模型设置）")
        info("  · 学生  ：student / 123456    （批改作业、查看错题本）")
        return True
    except Exception as e:
        error(f"数据库初始化失败：{e}")
        import traceback
        traceback.print_exc()
        return False


def print_summary(py_ok, dep_ok, dir_ok, key_ok, port_ok, db_ok):
    def s(ok, yes="✅ OK", no="❌ FAIL"):
        return yes if ok else no

    print(f"\n{LINE}")
    print(f"  📘 AI 智能作业批改 Web 平台  V1.0 MVP  启动环境检查报告")
    print(f"{LINE}")
    print(f"  Python 版本    : {s(py_ok)}")
    print(f"  必须依赖       : {s(dep_ok)}")
    print(f"  目录结构       : {s(dir_ok)}")
    print(f"  DeepSeek Key   : {'✅ 已配置' if key_ok else '⚠️  未配置（AI批改暂不可用，启动后可在后台补）'}")
    print(f"  端口 {APP_PORT:<4d} 占用  : {s(port_ok, yes='✅ 空闲', no='❌ 占用')}")
    print(f"  数据库         : {s(db_ok, yes='✅ 已初始化')}")
    print(f"{LINE}")
    print(f"  🌐 访问地址：")
    print(f"     本机：  http://127.0.0.1:{APP_PORT}")
    print(f"     局域网：http://<你的IP>:{APP_PORT}")
    print(f"  💡 提示：首次请用 admin/admin123 登录，到「知识库管理」中上传教材文件")
    print(f"{LINE}\n")


# ===================== 启动服务 =====================

def run_server():
    sys.path.insert(0, str(BASE_DIR))
    from app import app
    info(f"启动 Flask 服务：{APP_HOST}:{APP_PORT}")
    try:
        app.run(host=APP_HOST, port=APP_PORT, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        info("\n收到 Ctrl+C，服务已停止。")
        sys.exit(0)


# ===================== 主流程 =====================

def main():
    print(f"\n{LINE}")
    print(f"  📘  AI 智能作业批改 Web 平台  ·  一键启动脚本")
    print(f"  📂  项目目录：{BASE_DIR}")
    print(f"{LINE}")

    # === 阻塞性检查：不通过直接退出 ===
    py_ok = check_python_version()
    if not py_ok:
        sys.exit(1)

    dep_ok = check_dependencies()
    if not dep_ok:
        error("必须依赖安装失败，请手动执行后再启动：")
        print(f"       {sys.executable} -m pip install -r {REQ_PATH}")
        info("如遇网络问题可切换镜像：")
        print(f"       {sys.executable} -m pip install -r {REQ_PATH} -i https://pypi.tuna.tsinghua.edu.cn/simple")
        sys.exit(1)

    dir_ok = check_directories()
    if not dir_ok:
        sys.exit(1)

    # === 非阻塞 / 部分阻塞检查 ===
    key_ok = check_api_key()        # 未配置 Key：只警告，不阻塞（可后台补）

    port_ok = check_port()
    if not port_ok:
        sys.exit(1)

    db_ok = init_database()
    if not db_ok:
        sys.exit(1)

    print_summary(py_ok, dep_ok, dir_ok, key_ok, port_ok, db_ok)

    if not key_ok:
        warn("⚠️  当前未配置 DeepSeek API Key，登录后 AI 批改接口会返回提示。")
        info("   建议先进入「知识库管理 → 模型设置」填写 Key，再开始使用批改功能。\n")

    run_server()


if __name__ == "__main__":
    main()
