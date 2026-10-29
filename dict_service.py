# -*- coding: utf-8 -*-
"""五级级联字典服务（地区 / 学段 / 学科 / 教材版本 / 年级）。

数据来源：data/dict.json —— 静态字典文件，随代码进仓库，不落数据库表。
设计理由见《第四次实验课任务卡片》T-B01「存储方案（已定）」。

职责：
  1. 加载并缓存字典文件（文件被修改后自动重新加载）；
  2. 按上级维度返回下级候选，供 GET /api/dict 做级联；
  3. 校验「学段-学科-教材版本-年级」组合的合法性，供批改接口入参校验。
"""
import json

import config

# 简单缓存：记录文件 mtime，字典文件被改动后自动失效重载
_cache = {"mtime": None, "data": None}


def load_dict() -> dict:
    """读取字典文件；文件缺失或格式错误时返回空结构，不抛异常打断主链路。"""
    path = config.DICT_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] == mtime and _cache["data"] is not None:
        return _cache["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    _cache["mtime"] = mtime
    _cache["data"] = data
    return data


def regions() -> dict:
    """地区（省市两级）：{"福建省": ["福州市", ...]}"""
    return load_dict().get("regions") or {}


def cities_of(province: str):
    """返回省份下的城市列表；省份不存在时返回 None。"""
    return regions().get(province)


def stage_list() -> list:
    return list(load_dict().get("stages") or [])


def subjects_of(stage: str) -> list:
    """学科按学段分层——小学不含物理/化学/生物。"""
    return list((load_dict().get("subjects") or {}).get(stage) or [])


def textbooks_of(stage: str) -> list:
    return list((load_dict().get("textbooks") or {}).get(stage) or [])


def grades_of(stage: str) -> list:
    return list((load_dict().get("grades") or {}).get(stage) or [])


def defaults() -> dict:
    return dict(load_dict().get("defaults") or {})


def stage_of_grade(grade: str) -> str:
    """从年级写法推断学段，兼容「五年级」「五年级上」「小学五年级」「七年级上」等。

    无法判断时返回空串。用于老数据回填与「年级-学段」自洽校验。
    """
    text = (grade or "").strip()
    if not text:
        return ""
    for prefix in ("小学", "初中", "高中"):
        if text.startswith(prefix):
            return prefix
    if text.startswith("高"):
        return "高中"
    head = text[0]
    if head in "一二三四五六":
        return "小学"
    if head in "七八九":
        return "初中"
    return ""


def validate_selection(stage: str = "", subject: str = "",
                       textbook: str = "", grade: str = "",
                       region: str = "") -> tuple:
    """校验五级选择的合法性，返回 (ok, msg)。

    兼容策略（Web 前端 T-B03 完成前不断链）：
      - 字典文件缺失 → 整体降级为宽松模式，一律放行；
      - 未提交 stage（旧版前端行为）→ 放行；
      - 一旦提交 stage，则启用严格校验，字典外的取值一律拒绝。
    """
    stages = stage_list()
    if not stages:
        return True, ""

    stage = (stage or "").strip()
    if not stage:
        return True, ""

    if stage not in stages:
        return False, f"学段「{stage}」不在字典范围内"

    region = (region or "").strip()
    if region and region not in regions():
        return False, f"地区「{region}」不在字典范围内"

    subject = (subject or "").strip()
    if subject and subject not in subjects_of(stage):
        return False, f"「{stage}」不含学科「{subject}」"

    textbook = (textbook or "").strip()
    if textbook and textbook not in textbooks_of(stage):
        return False, f"「{stage}」不含教材版本「{textbook}」"

    grade = (grade or "").strip()
    if grade:
        # 年级写法在不同教材/地区不统一（五年级 / 五年级上），因此不做精确枚举匹配，
        # 只校验「年级推断出的学段」与所选学段是否自洽。
        inferred = stage_of_grade(grade)
        if inferred and inferred != stage:
            return False, f"年级「{grade}」与学段「{stage}」不匹配"

    return True, ""
