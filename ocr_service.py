# -*- coding: utf-8 -*-
"""OCR 适配层。

优先使用本地 Tesseract（需自行安装引擎与中文语言包）；
不可用时优雅降级为空文本，由用户在页面对识别结果进行手动录入/修正
（对应需求 T0304：识别结果支持手动编辑）。

如需接入百度 OCR 等云服务，可在此文件新增 provider，业务代码无需改动。
"""
import re


def clean_ocr_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def ocr_image(image_path: str) -> dict:
    """返回 {text, engine, note}。

    D-06：区分「缺依赖」「缺引擎」「识别失败」三种情况，note 给出可读原因，
    不再笼统吞掉异常。engine 取值：tesseract / manual。
    """
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:
        missing = getattr(exc, "name", "pytesseract/Pillow")
        return {
            "text": "",
            "engine": "manual",
            "note": f"未启用自动 OCR（缺少依赖 {missing}），请手动录入题目，或执行 pip install -r requirements-ocr.txt。",
        }

    try:
        text = pytesseract.image_to_string(
            Image.open(image_path), lang="chi_sim+eng"
        )
    except pytesseract.TesseractNotFoundError:
        return {
            "text": "",
            "engine": "manual",
            "note": "未检测到 Tesseract 引擎，请手动录入题目；或安装引擎并配置 chi_sim 中文语言包后重试。",
        }
    except Exception as exc:  # noqa: BLE001 —— 识别失败需转成可读提示
        return {
            "text": "",
            "engine": "manual",
            "note": f"OCR 识别失败（{exc}），请手动录入题目内容。",
        }

    text = clean_ocr_text(text)
    if text:
        return {"text": text, "engine": "tesseract", "note": ""}
    return {
        "text": "",
        "engine": "tesseract",
        "note": "未从图片中识别到文字，请手动录入题目内容。",
    }
