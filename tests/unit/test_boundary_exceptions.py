# -*- coding: utf-8 -*-
"""T-C02 边界与异常测试补充（对应遗留清单：
「补充 OCR 各类异常、HTTP 状态码、超时、乱码输入等边界用例」）。

全部用例在 mock 环境下可复现，不需要真实 API Key、不需要 Tesseract 环境：
  OCR-04 文件不存在 → 优雅降级 manual（不抛异常）
  OCR-05 非图片字节（伪装 .png）→ 优雅降级
  OCR-06 空图（0 字节）→ 优雅降级
  OCR-07 无 Tesseract 环境（模拟依赖缺失）→ manual 降级
  OCR-08 乱码 / 控制字符输入清洗 → 不崩溃
  AI-10  HTTP 401 → Key 无效提示
  AI-11  HTTP 402 → 余额不足提示
  AI-12  HTTP 429 → 限流提示
  AI-13  HTTP 500 → 服务异常提示
  AI-14  请求超时 → 超时提示
  AI-15  连续连接异常 → 友好失败提示
  AI-16  400 降级：JSON 模式不支持 → 自动降级普通模式重试成功
  AI-17  乱码作业文本提交批改 → 不崩溃，Prompt 正常组装
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# =====================================================================
# OCR 异常边界（遗留清单条目：OCR 各类异常）
# =====================================================================
class TestOCRBoundary:
    @pytest.mark.unit
    def test_ocr_missing_file_no_ocr_marker(self, tmp_path):
        """OCR-04：文件不存在 → 返回 manual 降级结构，不抛异常。"""
        from ocr_service import ocr_image
        result = ocr_image(str(tmp_path / "not_exists_xyz.png"))
        assert result["engine"] == "manual"
        assert result["text"] == ""
        assert "录入" in result["note"] or "手动" in result["note"]

    @pytest.mark.unit
    def test_ocr_non_image_bytes(self, tmp_path):
        """OCR-05：扩展名是 .png 但内容不是图片 → 优雅降级，不抛异常。"""
        from ocr_service import ocr_image
        fake = tmp_path / "fake.png"
        fake.write_bytes(b"this is not a real png, just plain text bytes")
        result = ocr_image(str(fake))
        assert result["engine"] in ("manual", "tesseract")
        assert "text" in result and "note" in result

    @pytest.mark.unit
    def test_ocr_empty_image_file(self, tmp_path):
        """OCR-06：0 字节空图 → 优雅降级，不抛异常。"""
        from ocr_service import ocr_image
        empty = tmp_path / "empty.png"
        empty.write_bytes(b"")
        result = ocr_image(str(empty))
        assert result["engine"] in ("manual", "tesseract")
        assert isinstance(result["text"], str)

    @pytest.mark.unit
    def test_ocr_without_tesseract_env(self, tmp_path, monkeypatch):
        """OCR-07：模拟无 Tesseract 环境（pytesseract / PIL 均不可导入）→ manual。"""
        monkeypatch.setitem(sys.modules, "pytesseract", None)
        monkeypatch.setitem(sys.modules, "PIL", None)
        monkeypatch.setitem(sys.modules, "PIL.Image", None)
        from ocr_service import ocr_image
        real = tmp_path / "real.png"
        real.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        result = ocr_image(str(real))
        assert result["engine"] == "manual"
        assert result["text"] == ""
        assert "手动" in result["note"] or "录入" in result["note"]

    @pytest.mark.unit
    def test_ocr_missing_engine_has_distinct_note(self, tmp_path, monkeypatch):
        """D-06：缺 Tesseract 引擎（而非缺依赖）时，note 应明确指向引擎安装。"""
        import types

        fake = types.ModuleType("pytesseract")

        class TesseractNotFoundError(Exception):
            pass

        fake.TesseractNotFoundError = TesseractNotFoundError

        def _raise(*_a, **_k):
            raise TesseractNotFoundError("tesseract is not installed")

        fake.image_to_string = _raise
        monkeypatch.setitem(sys.modules, "pytesseract", fake)

        from ocr_service import ocr_image
        real = tmp_path / "real.png"
        # 真实的最小 1x1 PNG，确保 PIL 能成功打开，从而进入 image_to_string 分支
        real.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
        ))
        result = ocr_image(str(real))
        assert result["engine"] == "manual"
        assert "引擎" in result["note"]

    @pytest.mark.unit
    def test_clean_ocr_text_garbled_and_control_chars(self):
        """OCR-08：乱码（锟斤拷）与控制字符输入清洗 → 不崩溃、返回字符串。"""
        from ocr_service import clean_ocr_text
        out = clean_ocr_text("锟斤拷锟斤拷\x00\x01\x02 烫烫烫\r\n\r\n\r\n1+1=2")
        assert isinstance(out, str)
        assert "1+1=2" in out
        assert "\r" not in out
        # 极端：仅控制字符
        assert isinstance(clean_ocr_text("\x00\x0b\x0c"), str)


# =====================================================================
# 大模型 HTTP 状态码 / 超时 / 网络异常（遗留清单条目：HTTP 状态码、超时）
# =====================================================================
class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(payload or {}, ensure_ascii=False)

    def json(self):
        return self._payload


@pytest.fixture()
def fake_llm_settings(monkeypatch):
    """让 _post_chat 认为已配置 Key（纯内存 mock，不写文件、不消耗真实额度）。"""
    import llm_service as llm_mod
    monkeypatch.setattr(llm_mod.config, "load_settings", lambda: {
        "deepseek_api_key": "sk-test-fake",
        "deepseek_base_url": "https://mock.local",
        "deepseek_model": "deepseek-chat",
    })
    return llm_mod


def _post_chat_with_status(fake_llm_settings, monkeypatch, status_code):
    import llm_service as llm_mod
    monkeypatch.setattr(llm_mod.requests, "post",
                        lambda *a, **k: _FakeResp(status_code, text=f"err {status_code}"))
    with pytest.raises(llm_mod.LLMError) as exc_info:
        llm_mod._post_chat([{"role": "user", "content": "hi"}])
    return str(exc_info.value)


class TestLLMHttpBoundary:
    @pytest.mark.unit
    def test_http_401_invalid_key(self, fake_llm_settings, monkeypatch):
        """AI-10：401 → 提示 Key 无效或已过期。"""
        msg = _post_chat_with_status(fake_llm_settings, monkeypatch, 401)
        assert "无效" in msg or "过期" in msg

    @pytest.mark.unit
    def test_http_402_insufficient_balance(self, fake_llm_settings, monkeypatch):
        """AI-11：402 → 提示余额不足。"""
        msg = _post_chat_with_status(fake_llm_settings, monkeypatch, 402)
        assert "余额不足" in msg

    @pytest.mark.unit
    def test_http_429_rate_limited(self, fake_llm_settings, monkeypatch):
        """AI-12：429 → 提示限流。"""
        msg = _post_chat_with_status(fake_llm_settings, monkeypatch, 429)
        assert "频繁" in msg or "限流" in msg

    @pytest.mark.unit
    def test_http_500_server_error(self, fake_llm_settings, monkeypatch):
        """AI-13：500 → 提示服务异常并带状态码。"""
        msg = _post_chat_with_status(fake_llm_settings, monkeypatch, 500)
        assert "500" in msg and "异常" in msg

    @pytest.mark.unit
    def test_request_timeout(self, fake_llm_settings, monkeypatch):
        """AI-14：requests.Timeout → 友好超时提示（不暴露堆栈）。"""
        import llm_service as llm_mod
        import requests as real_requests

        def boom(*a, **k):
            raise real_requests.Timeout("read timed out")

        monkeypatch.setattr(llm_mod.requests, "post", boom)
        with pytest.raises(llm_mod.LLMError) as exc_info:
            llm_mod._post_chat([{"role": "user", "content": "hi"}])
        assert "超时" in str(exc_info.value)

    @pytest.mark.unit
    def test_connection_error_exhausts_retry(self, fake_llm_settings, monkeypatch):
        """AI-15：两次尝试均连接异常 → 抛出友好 LLMError，不抛底层堆栈。"""
        import llm_service as llm_mod
        import requests as real_requests

        def boom(*a, **k):
            raise real_requests.ConnectionError("connection refused")

        monkeypatch.setattr(llm_mod.requests, "post", boom)
        with pytest.raises(llm_mod.LLMError) as exc_info:
            llm_mod._post_chat([{"role": "user", "content": "hi"}])
        assert "调用大模型失败" in str(exc_info.value)

    @pytest.mark.unit
    def test_json_mode_400_degrades_to_plain_and_succeeds(self, fake_llm_settings,
                                                          monkeypatch):
        """AI-16：JSON 模式 400 → 自动降级普通模式重试，第二次成功返回内容。"""
        import llm_service as llm_mod
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            if len(calls) == 1:
                return _FakeResp(400, text="response_format not supported")
            return _FakeResp(200, payload={
                "choices": [{"message": {"content": '{"score":"88","questions":[]}'}}]})

        monkeypatch.setattr(llm_mod.requests, "post", fake_post)
        out = llm_mod._post_chat([{"role": "user", "content": "hi"}])
        assert out == '{"score":"88","questions":[]}'
        assert len(calls) == 2
        assert "response_format" in calls[0] and "response_format" not in calls[1]

    @pytest.mark.unit
    def test_grade_homework_garbled_input_no_crash(self, monkeypatch):
        """AI-17：乱码作业文本（锟斤拷/emoji/控制符）→ 正常组装 Prompt 不崩溃。"""
        import llm_service as llm_mod
        captured = {}

        def fake_post(messages, timeout=60):
            captured["prompt"] = messages[1]["content"]
            return json.dumps({"score": None, "overall_comment": "", "questions": []},
                              ensure_ascii=False)

        monkeypatch.setattr(llm_mod, "_post_chat", fake_post)
        garbled = "1. 锟斤拷锟斤拷 ½+¼=？ 🤯\x00 我的答案：√￣￣￣"
        result = llm_mod.grade_homework("数学", "五年级", garbled, [])
        assert isinstance(result, dict)
        assert "questions" in result
        # 乱码内容原样进入 Prompt（不吞掉、不转义崩溃）
        assert "锟斤拷" in captured["prompt"]
