# -*- coding: utf-8 -*-
"""RAG 知识库引擎（对应 Epic-03）。

能力：
1. 文档解析：txt / md / pdf / docx 电子版教材、习题资料；
2. 切片入库：按段落 + 定长滑窗切片（带重叠），防止知识点被截断；
3. 向量化：本地 TF-IDF（中英文混合分词：英文按词、中文按字 + 二元词组），
   完全离线可用；如后续需要语义向量，可替换 Embedder 而业务层不变；
4. 检索：余弦相似度 TopN 召回 + 低分过滤，供大模型 Prompt 组装上下文。

索引持久化在 data/rag_index.json，向量在加载/更新时重建。
"""
import json
import math
import os
import re
import threading
import zipfile
from collections import Counter
from datetime import datetime

import config


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


def _read_docx(path: str) -> str:
    paragraphs = []
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    for para in re.split(r"</w:p>", xml):
        texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S)
        line = "".join(texts).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


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

class RAGEngine:
    def __init__(self, index_path: str = None, knowledge_dir: str = None):
        self.index_path = index_path or str(config.INDEX_PATH)
        self.knowledge_dir = knowledge_dir or str(config.KNOWLEDGE_DIR)
        self._lock = threading.RLock()
        self.docs = []
        self.chunks = []
        self.vectors = []
        self.embedder = TfidfEmbedder()
        self._load()
        self._rebuild_vectors()

    # -------- 持久化 --------
    def _load(self) -> None:
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.docs = data.get("docs", [])
                self.chunks = data.get("chunks", [])
            except Exception:
                self.docs, self.chunks = [], []

    def _save(self) -> None:
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"docs": self.docs, "chunks": self.chunks},
                f, ensure_ascii=False,
            )
        os.replace(tmp, self.index_path)

    def _rebuild_vectors(self) -> None:
        tokens = [tokenize(c["content"]) for c in self.chunks]
        self.embedder.fit(tokens)
        self.vectors = [self.embedder.vectorize(t) for t in tokens]

    # -------- 入库 --------
    def add_file(self, path: str, source: str = "upload") -> dict:
        """解析 + 切片 + 入库。同一路径重复导入会覆盖旧版本。"""
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

    # -------- 检索 --------
    def search(self, query: str, top_k: int = None, min_score: float = None) -> list:
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
                })
            return results

    def stats(self) -> dict:
        with self._lock:
            return {
                "doc_count": len(self.docs),
                "chunk_count": len(self.chunks),
                "folder": self.knowledge_dir,
                "docs": self.docs,
            }


# 单例
rag_engine = RAGEngine()
