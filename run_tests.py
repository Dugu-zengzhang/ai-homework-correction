# -*- coding: utf-8 -*-
"""AI 智能作业批改平台 —— 测试一键运行脚本。

等价于：
  python -m pytest tests -v --tb=short          # 默认全跑（require_llm / require_ocr 除外）

支持的额外命令行参数（同时兼容 pytest 原生参数）：
  --unit           只跑单元测试
  --integration    只跑接口集成测试
  --e2e            只跑端到端主链路测试（mock LLM，不花真实 Key 钱）
  --with-llm       包含需要真实 DeepSeek API Key 的测试（标记 require_llm）
  --with-ocr       包含需要本地 Tesseract OCR 的测试（标记 require_ocr）
  --slow           包含 slow 标记的测试（默认跳过）
  --all            包含一切（slow + require_llm + require_ocr），外加生成覆盖率报告

使用示例：
  python run_tests.py                       # 单元 + 集成 + E2E(mock LLM)
  python run_tests.py --unit                # 只跑 unit/
  python run_tests.py --with-llm --slow     # 连真实大模型一起跑
  python run_tests.py --e2e -k "隔离"        # 只跑名字含“隔离”的 e2e 用例
"""
import subprocess
import sys
import os


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)

    args = sys.argv[1:]
    py_opts = []
    markers = set()

    # === 解析我们自定义的“快捷参数” ===
    def has(a, short=None):
        if a in args:
            args.remove(a)
            return True
        if short and short in args:
            args.remove(short)
            return True
        return False

    only_unit = has("--unit")
    only_int  = has("--integration")
    only_e2e  = has("--e2e")
    with_llm  = has("--with-llm")
    with_ocr  = has("--with-ocr")
    with_slow = has("--slow")
    all_mode  = has("--all")

    if all_mode:
        with_llm = with_ocr = with_slow = True

    # === 选择测试路径 ===
    if only_unit:
        py_opts.append("tests/unit")
    elif only_int:
        py_opts.append("tests/integration")
    elif only_e2e:
        py_opts.append("tests/e2e")
    else:
        py_opts.append("tests")

    # === 处理 marker：最终拼成一个字符串（pytest -m 只接受 1 个参数） ===
    exclude = []
    include = []

    # 默认排除：require_llm / require_ocr / slow
    if not with_llm:  exclude.append("require_llm")
    if not with_ocr:  exclude.append("require_ocr")
    if not with_slow: exclude.append("slow")

    # 只跑某一层：加 include marker
    if only_unit:   include.append("unit")
    if only_int:    include.append("integration")
    if only_e2e:    include.append("e2e")

    if exclude or include:
        parts = []
        if include:
            parts.append("(" + " or ".join(include) + ")")
        if exclude:
            parts += [f"not {m}" for m in exclude]
        markers_expr = " and ".join(parts)
        py_opts += ["-m", markers_expr]

    # === 覆盖率（--all 才生成，需要 pytest-cov） ===
    if all_mode:
        py_opts.append("--cov=app,config,ocr_service,rag_engine,llm_service")
        py_opts.append("--cov-report=term-missing")
        py_opts.append("--cov-report=html:htmlcov")

    # === 把剩下的 pytest 原生参数（如 -k xxx、-x、--no-header）透传 ===
    py_opts += args

    print(f"\n{'='*72}")
    print(f"  🧪  AI 智能作业批改平台 · 自动化测试")
    print(f"  📁  项目目录：{here}")
    print(f"  🚀  执行命令：python -m pytest {' '.join(py_opts)}")
    print(f"{'='*72}\n")

    return subprocess.call([sys.executable, "-m", "pytest", *py_opts])


if __name__ == "__main__":
    sys.exit(main())
