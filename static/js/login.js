// 登录 / 注册页逻辑
const $ = (id) => document.getElementById(id);

function toast(msg, type = "") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "show " + type;
  setTimeout(() => (el.className = ""), 2600);
}

let mode = "login";

function switchMode(next) {
  mode = next;
  $("tab-login").classList.toggle("active", mode === "login");
  $("tab-register").classList.toggle("active", mode === "register");
  $("submit-btn").textContent = mode === "login" ? "登 录" : "注 册并登录";
}

$("tab-login").onclick = () => switchMode("login");
$("tab-register").onclick = () => switchMode("register");

// 已登录直接进入主界面
if (localStorage.getItem("token")) {
  location.href = "/app";
}

$("auth-form").addEventListener("submit", async () => {
  const username = $("username").value.trim();
  const password = $("password").value;
  if (!username || !password) return toast("请输入用户名和密码", "error");

  const btn = $("submit-btn");
  btn.disabled = true;
  btn.textContent = "请稍候…";
  try {
    const resp = await fetch(`/api/auth/${mode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await resp.json();
    if (data.code !== 0) {
      toast(data.msg || "操作失败", "error");
      return;
    }
    localStorage.setItem("token", data.data.token);
    localStorage.setItem("user", JSON.stringify(data.data.user));
    location.href = "/app";
  } catch (e) {
    toast("网络异常，请检查服务是否启动", "error");
  } finally {
    btn.disabled = false;
    btn.textContent = mode === "login" ? "登 录" : "注 册并登录";
  }
});
