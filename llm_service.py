# -*- coding: utf-8 -*-
"""DeepSeek 大模型服务（对应 Epic-04）。

- 统一封装 OpenAI 兼容的 /chat/completions 调用：超时、重试、异常友好提示；
- 批改 Prompt 模板独立维护，结合 OCR 文本 + RAG 召回资料；
- 结构化解析输出（对错、得分、错误原因、知识点、参考答案、解析）。
"""
import json

import requests

import config


class LLMError(Exception):
    """对前端友好的业务异常。"""


SYSTEM_PROMPT = (
    "你是一名经验丰富、耐心细致的中小学全科教师，正在批改学生的课后作业。"
    "你的讲解语言要通俗易懂，符合中小学生认知水平，多鼓励、少批评。"
)

GRADE_PROMPT_TEMPLATE = """请批改以下{grade}{subject}作业。{scope}

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
    if not isinstance(raw, str):
        raw = str(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # D-10：从混杂文本中提取第一个完整的 JSON 对象。
    # 用括号配对而非贪婪正则可正确处理字符串内部的 '{'/'}' 与多段 JSON 场景。
    start = raw.find("{")
    if start == -1:
        raise ValueError("大模型输出中未找到 JSON 对象")

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(raw[start:i + 1])
    raise ValueError("大模型输出中的 JSON 括号不配对")


def _is_score_valid(value) -> bool:
    """得分允许 null、空串、数值，或可转数值的字符串（如 "85"）。"""
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return True
        try:
            float(text)
            return True
        except ValueError:
            return False
    return False


def _validate_result(result) -> tuple:
    """按约定 Schema 校验并清洗大模型输出，返回 (cleaned, warnings)。

    处理策略：**单题丢弃并记录**（T-C01）——单题字段缺失或类型非法时只丢弃该题，
    不影响整次批改；总分 / 总评格式非法则置空并记录。这样既不让脏数据写入错题本，
    也不会因为一道题格式异常就让整份作业批改失败。
    """
    warnings = []
    if not isinstance(result, dict):
        return ({"score": None, "overall_comment": "", "questions": []},
                ["模型输出不是 JSON 对象，已降级为空结果"])

    cleaned = dict(result)
    cleaned.setdefault("score", None)
    cleaned.setdefault("overall_comment", "")

    if not _is_score_valid(cleaned.get("score")):
        warnings.append(f"总分格式非法（{cleaned.get('score')!r}），已置空")
        cleaned["score"] = None

    if cleaned.get("overall_comment") is None:
        cleaned["overall_comment"] = ""
    elif not isinstance(cleaned.get("overall_comment"), str):
        warnings.append("总评格式非法，已置空")
        cleaned["overall_comment"] = ""

    raw_questions = cleaned.get("questions")
    if raw_questions is None:
        raw_questions = []
    if not isinstance(raw_questions, list):
        warnings.append("questions 不是数组，已置空")
        cleaned["questions"] = []
        return cleaned, warnings

    kept = []
    for idx, item in enumerate(raw_questions, 1):
        if not isinstance(item, dict):
            warnings.append(f"第 {idx} 题不是对象，已丢弃")
            continue
        if not str(item.get("question") or "").strip():
            warnings.append(f"第 {idx} 题缺少必填字段 question，已丢弃")
            continue
        if "is_correct" not in item:
            warnings.append(f"第 {idx} 题缺少必填字段 is_correct，已丢弃")
            continue
        value = item.get("is_correct")
        # 严格类型校验：不接受 1/0、"true" 等真值近似写法
        if not (isinstance(value, bool) or value is None):
            warnings.append(f"第 {idx} 题 is_correct 类型非法（{value!r}），已丢弃")
            continue
        if not _is_score_valid(item.get("score")):
            warnings.append(f"第 {idx} 题得分格式非法，已置空")
            item["score"] = None
        kept.append(item)

    cleaned["questions"] = kept
    return cleaned, warnings


def _build_scope(stage: str, textbook_version: str, region: str) -> str:
    """把学段 / 教材版本 / 地区组装成提示词里的「学情范围」段。

    三者全空时返回空串，保持与旧版提示词完全一致的兜底行为。
    """
    parts = []
    if stage:
        parts.append(f"学段：{stage}")
    if textbook_version:
        parts.append(f"教材版本：{textbook_version}")
    if region:
        parts.append(f"地区：{region}")
    if not parts:
        return ""
    return (
        "\n【学情范围】" + "；".join(parts)
        + "。请按该教材版本的知识体系、章节顺序与教学要求判断对错并组织解析。"
    )


def grade_homework(subject: str, grade: str, homework_text: str, contexts: list,
                   stage: str = "", textbook_version: str = "",
                   region: str = "") -> dict:
    """完整批改：组装 RAG 上下文 → 调用大模型 → 解析并校验结构化结果。

    stage / textbook_version / region 用于把「学段 / 教材版本 / 地区」注入提示词，
    使判题口径对齐教材（T-B04）；三者均未传时保持原有行为不变。
    """
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
        scope=_build_scope(stage, textbook_version, region),
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

    result, warnings = _validate_result(result)
    if warnings:
        # 透出校验告警，便于前端提示与问题回溯；无异常时不改变原有返回结构
        result["warnings"] = warnings
    return result


def llm_status() -> dict:
    settings = config.load_settings()
    return {
        "configured": bool(settings.get("deepseek_api_key")),
        "model": settings.get("deepseek_model"),
        "base_url": settings.get("deepseek_base_url"),
    }
