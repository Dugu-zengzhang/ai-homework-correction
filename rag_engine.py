# -*- coding: utf-8 -*-
"""RAG 知识库引擎（对应 Epic-03）。

能力：
1. 文档解析：txt / md / pdf / docx 电子版教材、习题资料；
2. 切片入库：按段落 + 定长滑窗切片（带重叠），防止知识点被截断；
3. 向量化：本地 TF-IDF（中英文混合分词：英文按词、中文按字 + 二元词组），
   完全离线可用；如后续需要语义向量，可替换 Embedder 而业务层不变；
4. 检索：余弦相似度 TopN 召回 + 低分过滤，供大模型 Prompt 组装上下文；
5. 标签过滤（T-B05）：入库时为切片打学段（stage）/ 教材版本（textbook_version）标签，
   检索时先按标签过滤再做相似度计算，避免小学题召回初中资料。

索引持久化在 data/rag_index.json，向量在加载/更新时重建，标签随索引一并持久化。

旧数据兼容策略：无标签（或标签为空）的切片视为「通用资料」，在任何过滤条件下
都参与召回；只有显式带了不匹配标签的切片才会被过滤掉。这样旧索引升级后
不报错、不退化为空库，管理员可通过 update_doc_labels 逐步补打标签。
"""
import json
import logging
import math
import os
import re
import threading
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime

import config

logger = logging.getLogger(__name__)

# WordprocessingML 命名空间
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ----------------------------- 文档解析 -----------------------------

def _read_txt(path: str) -> str:
    raw = None
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                raw = f.read()
            break
        except UnicodeDecodeError:
            continue
    return raw or ""


def _docx_node_text(node) -> str:
    """拼接节点内全部 <w:t> 文本（含 run / 超链接等嵌套结构）。"""
    return "".join(t.text or "" for t in node.iter(f"{_W}t")).strip()


def _read_docx(path: str) -> str:
    """解析 docx 正文与表格（D-09）。

    用 XML 解析而非正则：正则会误把 `<w:tblPr>` 等标签当成 `<w:t>` 文本，
    且无法区分「段落」与「表格行」。这里按 body 顺序输出：
    - 普通段落：一行文本；
    - 表格：每行输出 `单元格1 | 单元格2 | ...`，保证表格内容可被切片入库并检索命中。
    """
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        # 极端损坏的文档：退化为纯文本兜底，避免整份资料无法入库
        return "\n".join(
            line.strip()
            for line in re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S)
            if line.strip()
        )

    body = root.find(f"{_W}body")
    if body is None:
        body = root

    lines = []
    for child in body:
        if child.tag == f"{_W}p":
            text = _docx_node_text(child)
            if text:
                lines.append(text)
        elif child.tag == f"{_W}tbl":
            for row in child.findall(f"{_W}tr"):
                cells = []
                for cell in row.findall(f"{_W}tc"):
                    cell_text = " ".join(
                        t for t in (_docx_node_text(p) for p in cell.iter(f"{_W}p")) if t
                    )
                    if cell_text:
                        cells.append(cell_text)
                if cells:
                    lines.append(" | ".join(cells))
    return "\n".join(lines)


def _read_pdf(path: str) -> str:
    try:
        import pypdf  # type: ignore
    except ImportError:
        try:
            import PyPDF2 as pypdf  # type: ignore
        except ImportError:
            raise RuntimeError("解析 PDF 需要安装 pypdf（pip install pypdf），或先转换为 txt/docx")
    parts = []
    with open(path, "rb") as f:
        reader = pypdf.PdfReader(f)
        for page in reader.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def parse_file(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".txt", ".md"):
        text = _read_txt(path)
    elif ext == ".docx":
        text = _read_docx(path)
    elif ext == ".pdf":
        text = _read_pdf(path)
    else:
        raise RuntimeError(f"暂不支持的文件类型：{ext}")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ----------------------------- 切片 -----------------------------

def chunk_text(text: str, size: int = 500, overlap: int = 100) -> list:
    """按自然段聚合成块；超长段落使用定长滑窗（带重叠）。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    current = ""
    step = max(size - overlap, 1)

    def flush():
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 <= size:
            current = (current + "\n\n" + para).strip()
            continue
        flush()
        if len(para) <= size:
            current = para
        else:
            for i in range(0, len(para), step):
                piece = para[i:i + size]
                if piece:
                    chunks.append(piece)
    flush()

    # 过短的末块并入上一块，避免碎片
    if len(chunks) >= 2 and len(chunks[-1]) < 80:
        chunks[-2] = chunks[-2] + "\n\n" + chunks[-1]
        chunks.pop()
    return chunks


# ----------------------------- 分词与向量化 -----------------------------

_TOKEN_LATIN = re.compile(r"[a-z0-9]+")
_TOKEN_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list:
    text = text.lower()
    tokens = _TOKEN_LATIN.findall(text)
    cjk = _TOKEN_CJK.findall(text)
    tokens.extend(cjk)                                   # 中文单字
    tokens.extend(a + b for a, b in zip(cjk, cjk[1:]))   # 中文二元词组
    return tokens


class TfidfEmbedder:
    """纯 Python TF-IDF：无外部服务依赖，对中文检索友好。"""

    def __init__(self):
        self.idf = {}

    def fit(self, docs_tokens: list) -> None:
        n = len(docs_tokens)
        df = Counter()
        for toks in docs_tokens:
            for term in set(toks):
                df[term] += 1
        self.idf = {
            term: math.log((n + 1) / (freq + 1)) + 1.0
            for term, freq in df.items()
        }

    def vectorize(self, tokens: list) -> dict:
        if not tokens:
            return {}
        tf = Counter(tokens)
        total = len(tokens)
        vec = {}
        for term, count in tf.items():
            weight = self.idf.get(term)
            if weight is None:
                continue
            vec[term] = (count / total) * weight
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            vec = {k: v / norm for k, v in vec.items()}
        return vec

    @staticmethod
    def cosine(a: dict, b: dict) -> float:
        if not a or not b:
            return 0.0
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(k, 0.0) for k, v in a.items())


# ----------------------------- RAG 引擎 -----------------------------

def _norm_label(value) -> str:
    """标签归一化：None / 空白字符串统一为 ''（表示未打标签）。"""
    return (value or "").strip()


def _label_match(chunk_label: str, filter_value: str) -> bool:
    """过滤规则：不过滤（filter 为空）→ 放行；切片无标签 → 通用资料放行；
    切片有标签 → 必须与过滤值完全一致。"""
    if not filter_value:
        return True
    if not chunk_label:
        return True
    return chunk_label == filter_value


class RAGEngine:
    def __init__(self, index_path: str = None, knowledge_dir: str = None):
        self.index_path = index_path or str(config.INDEX_PATH)
        self.knowledge_dir = knowledge_dir or str(config.KNOWLEDGE_DIR)
        self._lock = threading.RLock()
        self.docs = []
        self.chunks = []
        self.vectors = []
        self.embedder = TfidfEmbedder()
        self.index_status = {"status": "unknown", "message": "索引尚未加载"}
        self._load()
        self._rebuild_vectors()

    # -------- 持久化 --------
    def _load(self) -> None:
        """加载索引，并把加载结果写入 self.index_status（D-06）。

        索引损坏时不再静默回退空库：记录 corrupt 状态与原因（stats 可见），
        同时保持空库可启动，且不覆盖损坏文件，便于管理员排查/替换。
        """
        if not os.path.exists(self.index_path):
            self.index_status = {
                "status": "missing",
                "message": "索引文件不存在，按空库启动（首次入库后自动创建）",
            }
            return
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("索引顶层结构不是 JSON 对象")
            docs = data.get("docs", [])
            chunks = data.get("chunks", [])
            if not isinstance(docs, list) or not isinstance(chunks, list):
                raise ValueError("docs / chunks 字段不是数组")
            self.docs, self.chunks = docs, chunks
            self.index_status = {
                "status": "ok",
                "message": f"已加载 {len(docs)} 份资料 / {len(chunks)} 个切片",
            }
        except Exception as exc:  # 损坏的 JSON / 结构异常
            self.docs, self.chunks = [], []
            detail = f"{type(exc).__name__}: {exc}"
            self.index_status = {
                "status": "corrupt",
                "message": f"索引文件损坏，已按空库启动（未覆盖原文件）：{detail}",
            }
            logger.warning("RAG 索引加载失败：%s（文件：%s）", detail, self.index_path)

    def _save(self) -> None:
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"docs": self.docs, "chunks": self.chunks},
                f, ensure_ascii=False,
            )
        os.replace(tmp, self.index_path)
        self.index_status = {
            "status": "ok",
            "message": f"已加载 {len(self.docs)} 份资料 / {len(self.chunks)} 个切片",
        }

    def _rebuild_vectors(self) -> None:
        tokens = [tokenize(c["content"]) for c in self.chunks]
        self.embedder.fit(tokens)
        self.vectors = [self.embedder.vectorize(t) for t in tokens]

    # -------- 入库 --------
    def add_file(self, path: str, source: str = "upload",
                 stage: str = None, textbook_version: str = None) -> dict:
        """解析 + 切片 + 入库。同一路径重复导入会覆盖旧版本。

        stage / textbook_version：学段、教材版本标签（T-B05），同时写在
        文档与其全部切片上，随索引持久化；不传则为无标签「通用资料」。
        """
        stage = _norm_label(stage)
        textbook_version = _norm_label(textbook_version)
        with self._lock:
            path = os.path.abspath(path)
            settings = config.load_settings()
            existing = next((d for d in self.docs if d["path"] == path), None)
            if existing:
                self._remove_doc_unlocked(existing["id"], delete_file=False)

            text = parse_file(path)
            if not text:
                raise RuntimeError("文件内容为空或无法解析")
            pieces = chunk_text(
                text,
                size=int(settings.get("chunk_size", 500)),
                overlap=int(settings.get("chunk_overlap", 100)),
            )
            doc_id = (max((d["id"] for d in self.docs), default=0) + 1)
            doc = {
                "id": doc_id,
                "filename": os.path.basename(path),
                "path": path,
                "source": source,
                "stage": stage,
                "textbook_version": textbook_version,
                "chunk_count": len(pieces),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            base_chunk_id = max((c["id"] for c in self.chunks), default=0) + 1
            for idx, piece in enumerate(pieces):
                self.chunks.append({
                    "id": base_chunk_id + idx,
                    "doc_id": doc_id,
                    "chunk_index": idx,
                    "content": piece,
                    "stage": stage,
                    "textbook_version": textbook_version,
                })
            self.docs.append(doc)
            self._save()
            self._rebuild_vectors()
            return doc

    def scan_folder(self) -> dict:
        """扫描知识库文件夹：新增文件自动入库，已删除文件自动移除索引。"""
        added, skipped, failed, removed = [], [], [], []
        with self._lock:
            found = set()
            for root, _dirs, files in os.walk(self.knowledge_dir):
                for name in files:
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in config.ALLOWED_DOC_EXT:
                        continue
                    full = os.path.abspath(os.path.join(root, name))
                    found.add(full)
                    known = next((d for d in self.docs if d["path"] == full), None)
                    if known:
                        skipped.append(name)
                        continue
                    try:
                        doc = self.add_file(full, source="folder")
                        added.append(f"{name}（{doc['chunk_count']} 个切片）")
                    except Exception as exc:
                        failed.append(f"{name}：{exc}")
            for doc in list(self.docs):
                if doc.get("source") == "folder" and doc["path"] not in found:
                    self._remove_doc_unlocked(doc["id"], delete_file=False)
                    removed.append(doc["filename"])
            return {
                "added": added, "skipped": len(skipped),
                "failed": failed, "removed": removed,
            }

    def delete_doc(self, doc_id: int) -> None:
        with self._lock:
            self._remove_doc_unlocked(doc_id, delete_file=True)
            self._save()
            self._rebuild_vectors()

    def _remove_doc_unlocked(self, doc_id: int, delete_file: bool) -> None:
        doc = next((d for d in self.docs if d["id"] == doc_id), None)
        if not doc:
            return
        if delete_file and os.path.exists(doc["path"]):
            try:
                os.remove(doc["path"])
            except OSError:
                pass
        self.docs = [d for d in self.docs if d["id"] != doc_id]
        self.chunks = [c for c in self.chunks if c["doc_id"] != doc_id]

    # -------- 标签维护（T-B05） --------
    def update_doc_labels(self, doc_id: int, stage: str = None,
                          textbook_version: str = None) -> dict:
        """为已入库的文档（及其全部切片）补打 / 更新学段、教材版本标签。

        用于旧索引无标签数据的治理：管理员在知识库管理页选定资料后补标，
        标签随索引持久化，重载后不丢失。传空字符串表示清除标签（恢复为
        通用资料）。文档不存在时抛 KeyError。
        """
        stage = _norm_label(stage)
        textbook_version = _norm_label(textbook_version)
        with self._lock:
            doc = next((d for d in self.docs if d["id"] == doc_id), None)
            if not doc:
                raise KeyError(f"文档不存在：doc_id={doc_id}")
            doc["stage"] = stage
            doc["textbook_version"] = textbook_version
            for chunk in self.chunks:
                if chunk["doc_id"] == doc_id:
                    chunk["stage"] = stage
                    chunk["textbook_version"] = textbook_version
            self._save()
            return doc

    # -------- 检索 --------
    def search(self, query: str, top_k: int = None, min_score: float = None,
               stage: str = None, textbook_version: str = None) -> list:
        """检索：先按 stage / textbook_version 标签过滤，再做 TF-IDF 相似度计算。

        标签过滤规则见 _label_match：无标签切片视为通用资料，任何过滤
        条件下均可召回；过滤参数为空时行为与旧版本完全一致（全库检索）。
        """
        stage = _norm_label(stage)
        textbook_version = _norm_label(textbook_version)
        with self._lock:
            if not self.chunks:
                return []
            settings = config.load_settings()
            top_k = top_k or int(settings.get("top_k", 6))
            min_score = min_score if min_score is not None else float(
                settings.get("min_score", 0.05)
            )
            qv = self.embedder.vectorize(tokenize(query))
            scored = []
            for chunk, vec in zip(self.chunks, self.vectors):
                # 先按标签过滤（T-B05）：不匹配的切片不参与相似度计算
                if not _label_match(_norm_label(chunk.get("stage")), stage):
                    continue
                if not _label_match(
                    _norm_label(chunk.get("textbook_version")), textbook_version
                ):
                    continue
                score = TfidfEmbedder.cosine(qv, vec)
                if score >= min_score:
                    scored.append((score, chunk))
            scored.sort(key=lambda x: x[0], reverse=True)

            doc_map = {d["id"]: d["filename"] for d in self.docs}
            results = []
            for score, chunk in scored[:top_k]:
                results.append({
                    "doc_id": chunk["doc_id"],
                    "doc_name": doc_map.get(chunk["doc_id"], "未知资料"),
                    "chunk_index": chunk["chunk_index"],
                    "content": chunk["content"],
                    "score": round(float(score), 4),
                    "stage": _norm_label(chunk.get("stage")) or None,
                    "textbook_version": (
                        _norm_label(chunk.get("textbook_version")) or None
                    ),
                })
            return results

    def stats(self) -> dict:
        with self._lock:
            return {
                "doc_count": len(self.docs),
                "chunk_count": len(self.chunks),
                "folder": self.knowledge_dir,
                "docs": self.docs,
                "index_status": dict(self.index_status),
            }


# 单例
rag_engine = RAGEngine()
