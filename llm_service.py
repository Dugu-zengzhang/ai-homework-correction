# -*- coding: utf-8 -*-
"""DeepSeek 大模型服务（对应 Epic-04）。

- 统一封装 OpenAI 兼容的 /chat/completions 调用：超时、重试、异常友好提示；
- 批改 Prompt 模板独立维护，结合 OCR 文本 + RAG 召回资料；
- 结构化解析输出（对错、得分、错误原因、知识点、参考答案、解析）。
"""
import json
import re

import requests

import config


class LLMError(Exception):
    """对前端友好的业务异常。"""


SYSTEM_PROMPT = (
    "你是一名经验丰富、耐心细致的中小学全科教师，正在批改学生的课后作业。"
    "你的讲解语言要通俗易懂，符合中小学生认知水平，多鼓励、少批评。"
)

GRADE_PROMPT_TEMPLATE = """请批改以下{grade}{subject}作业。

【学生作业内容】（由图片 OCR 识别得到，可能包含一道或多道题目及学生作答，也可能存在识别误差）
{homework}

【知识库检索到的参考资料】（来自电子教材/习题库，优先以此作为判题依据）
{context}

批改要求：
1. 逐题判断学生作答是否正确；若资料中有标准答案/解题步骤，请严格依据资料判断。
2. 参考资料未覆盖的题目，可依据你的学科知识判断，但要在解析中说明。
3. 若某题只有题目、识别不到学生作答，is_correct 设为 null，并直接给出参考答案与解析。
4. 解析要写出关键解题步骤；错题要指出错误原因和对应知识点。
5. 只输出 JSON，不要输出任何额外文字，JSON 结构如下：
{{
  "score": "本次作业总体得分（百分制，无法评分可填 null）",
  "overall_comment": "总体评语，含鼓励与改进建议，50-150字",
  "questions": [
    {{
      "question": "题目内容",
      "student_answer": "学生作答，没有则填 null",
      "is_correct": true,
      "score": "该题得分，无法评分填 null",
      "correct_answer": "参考答案",
      "error_reason": "错误原因，答对填 null",
      "knowledge_point": "对应知识点",
      "analysis": "解题步骤与解析"
    }}
  ]
}}"""


def _post_chat(messages: dict, timeout: int = 60):
    settings = config.load_settings()
    api_key = settings.get("deepseek_api_key")
    if not api_key:
        raise LLMError("尚未配置 DeepSeek API Key，请联系管理员在「知识库 → 模型设置」中填写。")

    url = settings["deepseek_base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    base_payload = {
        "model": settings.get("deepseek_model") or "deepseek-chat",
        "messages": messages,
        "temperature": 0.2,
    }

    last_error = None
    # 第一次尝试 JSON 模式；若兼容端点不支持 response_format，则降级重试一次
    for use_json_mode in (True, False):
        payload = dict(base_payload)
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.Timeout:
            raise LLMError("大模型服务响应超时，请稍后重试。")
        except requests.RequestException as exc:
            last_error = str(exc)
            continue

        if resp.status_code == 200:
            try:
                return resp.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError):
                raise LLMError("大模型返回内容格式异常，请稍后重试。")

        if resp.status_code in (400, 422) and use_json_mode:
            last_error = resp.text[:300]
            continue  # 降级为普通模式重试

        if resp.status_code == 401:
            raise LLMError("DeepSeek API Key 无效或已过期，请管理员检查设置。")
        if resp.status_code == 402:
            raise LLMError("DeepSeek 账户余额不足，请充值后再试。")
        if resp.status_code == 429:
            raise LLMError("大模型调用过于频繁（限流），请稍后再试。")
        raise LLMError(f"大模型服务异常（HTTP {resp.status_code}），请稍后重试。")

    raise LLMError(f"调用大模型失败：{last_error or '未知错误'}")


def _extract_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            return json.loads(match.group(0))
        raise


def grade_homework(subject: str, grade: str, homework_text: str, contexts: list) -> dict:
    """完整批改：组装 RAG 上下文 → 调用大模型 → 解析结构化结果。"""
    homework_text = (homework_text or "").strip()
    if not homework_text:
        raise LLMError("作业内容为空，请先上传图片识别或手动录入题目。")

    if contexts:
        context_blocks = []
        for idx, item in enumerate(contexts, 1):
            context_blocks.append(
                f"[资料{idx}] 来源：《{item['doc_name']}》（相关度 {item['score']}）\n"
                f"{item['content']}"
            )
        context_text = "\n\n".join(context_blocks)
    else:
        context_text = "（知识库未检索到相关资料，请依据你的学科知识谨慎判断并在解析中注明）"

    prompt = GRADE_PROMPT_TEMPLATE.format(
        grade=grade or "中小学",
        subject=subject or "综合",
        homework=homework_text,
        context=context_text,
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    raw = _post_chat(messages)
    try:
        result = _extract_json(raw)
    except Exception:
        # 容错重试一次：要求模型仅输出 JSON
        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": "你上一次的输出不是合法 JSON。请严格只输出符合约定结构的 JSON，不要输出其他任何内容。",
        })
        raw = _post_chat(messages)
        result = _extract_json(raw)

    result.setdefault("questions", [])
    result.setdefault("overall_comment", "")
    result.setdefault("score", None)
    return result


def llm_status() -> dict:
    settings = config.load_settings()
    return {
        "configured": bool(settings.get("deepseek_api_key")),
        "model": settings.get("deepseek_model"),
        "base_url": settings.get("deepseek_base_url"),
    }
