/* ========= AI 作业批改平台 主逻辑 ========= */
const $ = (id) => document.getElementById(id);
let currentImage = null; // {image_name, image_url}

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function toast(msg, type = "") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "show " + type;
  setTimeout(() => (el.className = ""), 2800);
}

async function api(url, options = {}) {
  const opts = Object.assign({}, options);
  opts.headers = Object.assign(
    { Authorization: "Bearer " + (localStorage.getItem("token") || "") },
    options.headers || {}
  );
  if (opts.jsonBody !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.jsonBody);
    delete opts.jsonBody;
  }
  const resp = await fetch(url, opts);
  if (resp.status === 401) {
    localStorage.clear();
    location.href = "/";
    throw new Error("未登录");
  }
  const data = await resp.json().catch(() => ({ code: -1, msg: "响应解析失败" }));
  if (data.code !== 0) throw new Error(data.msg || "请求失败");
  return data.data;
}

/* ---------- 登录态 & 导航 ---------- */
const user = JSON.parse(localStorage.getItem("user") || "null");
if (!user) location.href = "/";
$("username-label").textContent = "你好，" + user.username;
$("role-tag").textContent = user.role === "admin" ? "管理员" : "学生";
$("role-tag").className = "role-tag " + (user.role === "admin" ? "admin" : "");
if (user.role === "admin") $("nav-knowledge").style.display = "";

document.querySelectorAll(".nav button").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    $("view-" + btn.dataset.view).classList.add("active");
    if (btn.dataset.view === "mistakes") loadMistakes();
    if (btn.dataset.view === "knowledge") loadKnowledge();
  };
});
$("logout-btn").onclick = () => { localStorage.clear(); location.href = "/"; };

/* ---------- 模态框 ---------- */
function showModal(title, html) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = html;
  $("modal-mask").classList.add("show");
}
$("modal-close").onclick = () => $("modal-mask").classList.remove("show");
$("modal-mask").onclick = (e) => {
  if (e.target === $("modal-mask")) $("modal-mask").classList.remove("show");
};

/* ============ 作业批改 ============ */
const uploader = $("uploader");
const imageInput = $("image-input");
uploader.onclick = () => imageInput.click();
["dragover", "dragenter"].forEach((ev) =>
  uploader.addEventListener(ev, (e) => { e.preventDefault(); uploader.classList.add("drag"); })
);
["dragleave", "drop"].forEach((ev) =>
  uploader.addEventListener(ev, (e) => { e.preventDefault(); uploader.classList.remove("drag"); })
);
uploader.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (file) handleImage(file);
});
imageInput.onchange = () => imageInput.files[0] && handleImage(imageInput.files[0]);

async function handleImage(file) {
  if (!file.type.startsWith("image/")) return toast("请选择图片文件", "error");
  const fd = new FormData();
  fd.append("image", file);
  $("ocr-note").textContent = "图片上传中，正在进行 OCR 识别…";
  try {
    const data = await api("/api/homework/upload", { method: "POST", body: fd });
    currentImage = { image_name: data.image_name, image_url: data.image_url };
    $("image-preview").src = data.image_url;
    $("image-preview").style.display = "";
    if (data.ocr_text) $("question-text").value = data.ocr_text;
    $("ocr-note").textContent =
      (data.ocr_engine === "tesseract" ? "✅ 本地 OCR 识别完成，" : "ℹ️ ") +
      (data.ocr_note || "请核对并可手动修改识别内容后再提交批改。");
  } catch (e) {
    $("ocr-note").textContent = "";
    toast(e.message, "error");
  }
}

$("grade-btn").onclick = async () => {
  const text = $("question-text").value.trim();
  if (!text) return toast("请先上传图片识别，或手动录入题目内容", "error");
  $("grading-box").style.display = "";
  $("result-area").innerHTML = "";
  $("grade-btn").disabled = true;
  try {
    const data = await api("/api/homework/grade", {
      method: "POST",
      jsonBody: {
        subject: $("subject").value,
        grade: $("grade").value,
        question_text: text,
        image_name: currentImage ? currentImage.image_name : null,
      },
    });
    renderResult(data);
    if (data.wrong_count > 0) {
      toast(`批改完成，已自动收录 ${data.wrong_count} 道错题到错题本`, "success");
    } else {
      toast("批改完成，全部正确，太棒了！", "success");
    }
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("grading-box").style.display = "none";
    $("grade-btn").disabled = false;
  }
};

function renderResult(data) {
  const r = data.result || {};
  const hasScore = r.score !== null && r.score !== undefined && r.score !== "";
  const scoreHtml = hasScore
    ? `<div class="score-num ${Number(r.score) < 60 ? "warn" : ""}">${escapeHtml(r.score)}</div>`
    : "";
  const contexts = (data.contexts || []).map(
    (c, i) => `<div class="context-item">
        <span class="context-src">[资料${i + 1}] 《${escapeHtml(c.doc_name)}》 相关度 ${c.score}</span><br>
        ${escapeHtml(c.content).slice(0, 220)}…
      </div>`
  ).join("");

  const questions = (r.questions || []).map((q) => {
    const correct = q.is_correct;
    const cls = correct === true ? "" : correct === false ? "wrong" : "unknown";
    const badge = correct === true
      ? '<span class="q-badge right">✔ 回答正确</span>'
      : correct === false
        ? '<span class="q-badge wrong">✘ 回答错误</span>'
        : '<span class="q-badge unknown">— 仅提供参考答案</span>';
    const field = (label, val) =>
      val ? `<p><span class="label">${label}</span>${escapeHtml(val)}</p>` : "";
    return `<div class="q-card ${cls}">
      <div class="q-head"><strong>📝 ${escapeHtml(q.question || "题目")}</strong>${badge}</div>
      <div class="q-body">
        ${field("学生作答：", q.student_answer)}
        ${field("参考答案：", q.correct_answer)}
        ${q.knowledge_point ? `<span class="kp-tag">🔖 ${escapeHtml(q.knowledge_point)}</span>` : ""}
        ${field("错误原因：", q.error_reason)}
        ${field("解析：", q.analysis)}
        ${field("得分：", q.score)}
      </div>
    </div>`;
  }).join("");

  $("result-area").innerHTML = `
    <div class="card">
      <div class="card-title">🎯 批改结果</div>
      <div class="score-hero">
        ${scoreHtml}
        <div><strong style="font-size:15px;">AI 总评：</strong>${escapeHtml(r.overall_comment || "")}</div>
      </div>
      ${contexts ? `<div class="context-box"><strong>📚 本次批改参考的知识库资料</strong>${contexts}</div>` : ""}
      ${questions || '<p style="color:var(--text-3);margin-top:10px;">模型未返回逐题结果</p>'}
    </div>`;
}

/* ============ 错题本 ============ */
$("filter-btn").onclick = loadMistakes;

async function loadMistakes() {
  const params = new URLSearchParams({
    subject: $("f-subject").value,
    mastered: $("f-mastered").value,
    keyword: $("f-keyword").value.trim(),
  });
  const listEl = $("mistake-list");
  listEl.innerHTML = '<div class="empty"><span class="ico">⏳</span>加载中…</div>';
  try {
    const items = await api("/api/mistakes?" + params.toString());
    if (!items.length) {
      listEl.innerHTML = '<div class="empty"><span class="ico">📭</span>还没有错题，去批改一份作业吧～</div>';
      return;
    }
    listEl.innerHTML = items.map((m) => `
      <div class="mistake-card">
        <div class="mistake-head">
          <div>
            <span class="kp-tag">${escapeHtml(m.subject || "综合")}</span>
            ${m.knowledge_point ? `<span class="kp-tag">🔖 ${escapeHtml(m.knowledge_point)}</span>` : ""}
            <span class="mistake-meta">${escapeHtml(m.created_at)}</span>
          </div>
          <div class="mistake-actions">
            ${m.mastered ? '<span class="mastered-stamp">✅ 已掌握</span>' : ""}
            <button class="mini-btn ${m.mastered ? "" : "done"}" onclick="toggleMastered(${m.id}, ${m.mastered ? 0 : 1})">
              ${m.mastered ? "标记未掌握" : "标记已掌握"}
            </button>
            <button class="mini-btn del" onclick="deleteMistake(${m.id})">删除</button>
          </div>
        </div>
        <p><span class="label" style="color:var(--text-3);font-size:12px;font-weight:700;">题目：</span>${escapeHtml(m.question)}</p>
        ${m.student_answer ? `<p><span class="label" style="color:var(--danger);font-size:12px;font-weight:700;">我的作答：</span>${escapeHtml(m.student_answer)}</p>` : ""}
        <p><span class="label" style="color:var(--success);font-size:12px;font-weight:700;">正确答案：</span>${escapeHtml(m.correct_answer || "")}</p>
        ${m.error_reason ? `<p><span class="label" style="color:var(--text-3);font-size:12px;font-weight:700;">错误原因：</span>${escapeHtml(m.error_reason)}</p>` : ""}
        <p><span class="label" style="color:var(--text-3);font-size:12px;font-weight:700;">解析：</span>${escapeHtml(m.analysis || "")}</p>
        ${m.image_url ? `<img class="thumb" src="${m.image_url}" onclick="showImage('${m.image_url}')" alt="原图">` : ""}
      </div>`).join("");
  } catch (e) {
    listEl.innerHTML = "";
    toast(e.message, "error");
  }
}

async function toggleMastered(id, mastered) {
  try {
    await api(`/api/mistakes/${id}`, { method: "PUT", jsonBody: { mastered } });
    loadMistakes();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteMistake(id) {
  if (!confirm("确定删除这条错题吗？")) return;
  try {
    await api(`/api/mistakes/${id}`, { method: "DELETE" });
    toast("已删除", "success");
    loadMistakes();
  } catch (e) { toast(e.message, "error"); }
}

function showImage(url) {
  showModal("作业原图", `<img src="${url}" alt="作业原图">`);
}

/* ============ 知识库管理 ============ */
async function loadKnowledge() {
  try {
    const stats = await api("/api/knowledge/stats");
    $("folder-path").textContent = "📂 " + stats.folder;
    $("kb-stats").innerHTML = `
      <div class="stat-card"><div class="num">${stats.doc_count}</div><div class="lbl">入库资料数</div></div>
      <div class="stat-card"><div class="num">${stats.chunk_count}</div><div class="lbl">知识切片数（向量）</div></div>
      <div class="stat-card"><div class="num">${stats.llm.configured ? "✅" : "⚠️"}</div>
        <div class="lbl">DeepSeek ${stats.llm.configured ? "已配置" : "未配置 Key"}</div></div>
      <div class="stat-card"><div class="num" style="font-size:15px;">${escapeHtml(stats.llm.model || "-")}</div><div class="lbl">当前模型</div></div>`;
    renderDocs(stats.docs || []);
    const s = await api("/api/settings");
    $("set-baseurl").value = s.deepseek_base_url || "";
    $("set-model").value = s.deepseek_model || "";
    $("set-apikey").placeholder = s.deepseek_api_key_masked
      ? "已配置：" + s.deepseek_api_key_masked + "（留空不修改）"
      : "sk-...";
  } catch (e) { toast(e.message, "error"); }
}

function renderDocs(docs) {
  $("doc-count-tip").textContent = `共 ${docs.length} 份`;
  if (!docs.length) {
    $("doc-list").innerHTML = '<div class="empty"><span class="ico">📚</span>知识库还是空的，快上传教材或扫描文件夹吧</div>';
    return;
  }
  $("doc-list").innerHTML = docs.map((d) => `
    <div class="doc-row">
      <div class="doc-info">
        <div class="doc-name">📄 ${escapeHtml(d.filename)}
          <span class="source-tag ${d.source === "folder" ? "folder" : ""}">
            ${d.source === "folder" ? "文件夹" : "上传"}</span>
        </div>
        <div class="doc-meta">${d.chunk_count} 个切片 · 入库时间 ${escapeHtml(d.created_at)}</div>
      </div>
      <button class="mini-btn del" onclick="deleteDoc(${d.id})">删除</button>
    </div>`).join("");
}

async function deleteDoc(id) {
  if (!confirm("删除该资料会同时删除其全部切片，确定吗？")) return;
  try {
    await api(`/api/knowledge/${id}`, { method: "DELETE" });
    toast("已删除", "success");
    loadKnowledge();
  } catch (e) { toast(e.message, "error"); }
}

$("scan-btn").onclick = async () => {
  const btn = $("scan-btn");
  btn.disabled = true; btn.textContent = "扫描中…";
  try {
    const r = await api("/api/knowledge/scan", { method: "POST" });
    const parts = [];
    if (r.added.length) parts.push("新增 " + r.added.length + " 份：" + r.added.join("；"));
    if (r.removed.length) parts.push("移除 " + r.removed.length + " 份");
    if (r.failed.length) parts.push("失败：" + r.failed.join("；"));
    parts.push(`已存在 ${r.skipped} 份跳过`);
    toast(parts.join("；") || "没有发现新文件", r.failed.length ? "error" : "success");
    loadKnowledge();
  } catch (e) { toast(e.message, "error"); }
  finally { btn.disabled = false; btn.textContent = "🔄 扫描文件夹并入库"; }
};

/* 知识库拖拽 / 点选上传 */
const kbUploader = $("kb-uploader");
const kbInput = $("kb-file-input");
kbUploader.onclick = () => kbInput.click();
["dragover", "dragenter"].forEach((ev) =>
  kbUploader.addEventListener(ev, (e) => { e.preventDefault(); kbUploader.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) =>
  kbUploader.addEventListener(ev, (e) => { e.preventDefault(); kbUploader.classList.remove("drag"); }));
kbUploader.addEventListener("drop", (e) => e.dataTransfer.files.length && uploadKbFiles(e.dataTransfer.files));
kbInput.onchange = () => kbInput.files.length && uploadKbFiles(kbInput.files);

async function uploadKbFiles(fileList) {
  const fd = new FormData();
  [...fileList].forEach((f) => fd.append("files", f));
  $("kb-upload-result").innerHTML = "<p style='color:var(--text-3);padding:8px 0;'>解析、切片、向量化中，请稍候…</p>";
  try {
    const data = await api("/api/knowledge/upload", { method: "POST", body: fd });
    $("kb-upload-result").innerHTML = data.results.map((r) =>
      `<p style="padding:4px 0;color:${r.ok ? "var(--success)" : "var(--danger)"}">
        ${r.ok ? "✅" : "❌"} ${escapeHtml(r.filename)} — ${escapeHtml(r.msg)}</p>`
    ).join("");
    if (data.results.some((r) => r.ok)) { toast("资料入库成功", "success"); loadKnowledge(); }
  } catch (e) {
    $("kb-upload-result").innerHTML = "";
    toast(e.message, "error");
  }
}

$("search-btn").onclick = async () => {
  const q = $("search-query").value.trim();
  if (!q) return toast("请输入要检索的题目或知识点", "error");
  $("search-result").innerHTML = "<p style='color:var(--text-3);padding:8px 0;'>检索中…</p>";
  try {
    const items = await api("/api/knowledge/search", {
      method: "POST", jsonBody: { query: q },
    });
    if (!items.length) {
      $("search-result").innerHTML = '<p style="padding:8px 0;color:var(--warning);">未检索到相关资料（可尝试更换关键词，或先向知识库添加教材）</p>';
      return;
    }
    $("search-result").innerHTML =
      '<div class="context-box">' + items.map((c, i) =>
        `<div class="context-item">
          <span class="context-src">[Top ${i + 1}] 《${escapeHtml(c.doc_name)}》 相似度 ${c.score}</span><br>
          ${escapeHtml(c.content)}
        </div>`).join("") + "</div>";
  } catch (e) {
    $("search-result").innerHTML = "";
    toast(e.message, "error");
  }
};

$("save-settings-btn").onclick = async () => {
  const body = {
    deepseek_api_key: $("set-apikey").value.trim(),
    deepseek_base_url: $("set-baseurl").value.trim(),
    deepseek_model: $("set-model").value.trim(),
  };
  try {
    await api("/api/settings", { method: "PUT", jsonBody: body });
    toast("设置已保存", "success");
    $("set-apikey").value = "";
    loadKnowledge();
  } catch (e) { toast(e.message, "error"); }
};
