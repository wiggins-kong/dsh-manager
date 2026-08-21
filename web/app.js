/* DSH 管理器前端逻辑 (pywebview jsbridge) */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  // 注意: 不能在这里固化 webview —— 桥是异步注入的, 解析时它是 undefined。
  // 必须在 pywebviewready / 轮询确认就绪后再赋值。因此用 let 且初始为 null。
  let webview = null;

  let state = {
    versions: [],
    local: {},
    selected: null,
    running: false,
  };

  const els = {
    nodeBadge: $("node-badge"),
    nodeBanner: $("node-banner"),
    nodeBannerText: $("node-banner-text"),
    versionList: $("version-list"),
    localList: $("local-list"),
    detailEmpty: $("detail-empty"),
    detail: $("detail"),
    tag: $("detail-tag"),
    status: $("detail-status"),
    path: $("detail-path"),
    btnClone: $("btn-clone"),
    btnRun: $("btn-run"),
    btnOpen: $("btn-open"),
    btnStop: $("btn-stop"),
    log: $("log"),
  };

  /* ---------- 工具 ---------- */
  function toast(msg, ms) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.add("hidden"), ms || 2600);
  }

  function log(lines) {
    const arr = Array.isArray(lines) ? lines : [lines];
    for (const l of arr) els.log.textContent += (l === null || l === undefined ? "" : String(l)) + "\n";
    els.log.scrollTop = els.log.scrollHeight;
  }

  function clearLog() { els.log.textContent = ""; }

  function setControls(cloned) {
    els.btnClone.disabled = cloned;
    els.btnRun.disabled = !cloned;
    els.btnOpen.disabled = !cloned;
    els.btnStop.disabled = !cloned || !state.running;
  }

  async function refreshVersions(force) {
    els.versionList.innerHTML = '<li class="empty">加载中…</li>';
    try {
      const api = webview.api;
      const versions = force
        ? await api.fetch_versions(true)
        : state.versions.length ? state.versions : await api.fetch_versions(false);
      state.versions = versions;
      renderVersions();
    } catch (e) {
      els.versionList.innerHTML =
        '<li class="empty">加载失败。\n若网络不通，请在「设置」中开启代理后重试。</li>';
      toast("获取版本失败：" + (e && e.message ? e.message : e));
    }
  }

  function renderVersions() {
    const ul = els.versionList;
    ul.innerHTML = "";
    if (!state.versions.length) {
      ul.innerHTML = '<li class="empty">暂无版本</li>';
      return;
    }
    state.versions.forEach((tag) => {
      const li = document.createElement("li");
      li.className = tag === state.selected ? "active" : "";
      li.dataset.tag = tag;
      li.innerHTML = `<span class="t">${tag}</span>` +
        (state.local[tag] ? '<span class="local-tick" title="已下载">&#10003;</span>' : "");
      li.addEventListener("click", () => selectVersion(tag));
      ul.appendChild(li);
    });
  }

  function renderLocal() {
    const ul = els.localList;
    ul.innerHTML = "";
    const tags = Object.keys(state.local);
    if (!tags.length) {
      ul.innerHTML = '<li class="empty">尚未下载任何源码</li>';
      return;
    }
    tags.sort().reverse().forEach((tag) => {
      const li = document.createElement("li");
      li.innerHTML = `<span class="t">${tag}</span><span class="tag-pill">本地</span>`;
      li.addEventListener("click", () => selectVersion(tag));
      ul.appendChild(li);
    });
  }

  function selectVersion(tag) {
    state.selected = tag;
    renderVersions();
    els.detailEmpty.classList.add("hidden");
    els.detail.classList.remove("hidden");
    els.tag.textContent = tag;
    const cloned = !!state.local[tag];
    els.path.textContent = cloned ? "路径：" + state.local[tag] : "路径：尚未下载（将克隆到 data/repos/" + tag + "）";
    els.status.textContent = cloned ? "已下载" : "未下载";
    els.status.className = "badge" + (cloned ? " badge-ok" : "");
    setControls(cloned);
    clearLog();
    log("已选择版本 " + tag + "。" + (cloned ? " 已就绪，可运行后端。" : " 点击「下载源码」开始拉取。"));
  }

  async function runClone(tag) {
    els.btnClone.disabled = true;
    clearLog();
    log("开始下载源码 " + tag + " …（" + (state.proxyOn ? "走代理" : "直连") + "）\n");
    try {
      const res = await webview.api.clone(tag, function (line) { log(line); });
      state.local[tag] = res.path;
      renderLocal();
      renderVersions();
      selectVersion(tag);
      toast("下载完成：" + tag);
    } catch (e) {
      toast("下载失败，详见日志");
      log("\n[错误] " + (e && e.message ? e.message : e));
    } finally {
      els.btnClone.disabled = false;
    }
  }

  async function runStart(tag) {
    els.btnRun.disabled = true;
    clearLog();
    log("正在准备并启动后端 " + tag + " …\n");
    try {
      const res = await webview.api.start(tag, function (line) { log(line); });
      state.running = true;
      toast("后端已启动 (端口 " + res.port + ")");
      setControls(true);
    } catch (e) {
      toast("启动失败，详见日志");
      log("\n[错误] " + (e && e.message ? e.message : e));
      setControls(true);
    } finally {
      els.btnRun.disabled = false;
    }
  }

  async function runStop() {
    els.btnStop.disabled = true;
    try {
      await webview.api.stop();
      state.running = false;
      toast("后端已停止");
      log("\n[已停止] DSH 后端进程已结束。");
      setControls(true);
    } catch (e) {
      toast("停止失败：" + (e && e.message ? e.message : e));
    } finally {
      els.btnStop.disabled = false;
    }
  }

  /* ---------- Node 检测渲染 ---------- */
  function renderNode(node) {
    const ok = node && node.node && node.pnpm;
    els.nodeBadge.textContent = "";
    els.nodeBadge.classList.toggle("badge-ok", !!ok);
    els.nodeBadge.classList.toggle("badge-err", !ok);
    const nv = node.node_version || "无 Node";
    const pv = node.pnpm_version ? " / pnpm " + node.pnpm_version : "";
    els.nodeBadge.insertAdjacentHTML("afterbegin",
      `<span class="dot"></span>${ok ? "Node " + nv + pv : "缺少运行环境"}`);
    els.nodeBanner.classList.toggle("hidden", ok);
    if (!ok) {
      els.nodeBannerText.textContent =
        (node.node ? "" : "未检测到 Node.js。") +
        (node.pnpm ? "" : " 未检测到 pnpm。") +
        " 运行 DSH 需要它们。";
    }
  }

  /* ---------- 主题 (深色/浅色/跟随系统) ---------- */
  function systemDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function applyTheme(theme) {
    const resolved = theme === "system" ? (systemDark() ? "dark" : "light") : theme;
    document.documentElement.dataset.theme = resolved;
  }
  // 实时跟随系统深浅色切换
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (state.theme === "system") applyTheme("system");
    });
  }

  /* ---------- 设置弹窗 ---------- */
  let currentProxy = null;
  let pendingWs = null;   // 待处理的工作区切换 {new_path, old_count}

  async function refreshStateLocal() {
    const st = await webview.api.get_state();
    state.local = {};
    (st.local || []).forEach((r) => { state.local[r.tag] = r.path; });
    state.workspace = st.workspace;
    renderLocal();
    renderVersions();
    if (state.selected) selectVersion(state.selected);
  }

  function openSettings() {
    currentProxy = state.proxy;
    $("proxy-enabled").checked = currentProxy.enabled;
    $("proxy-host").value = currentProxy.host;
    $("proxy-port").value = currentProxy.port;
    $("theme-select").value = state.theme || "dark";
    $("workspace-path").value = state.workspace || "";
    $("settings-modal").classList.remove("hidden");
    $("proxy-host").focus();
  }
  function closeSettings() { $("settings-modal").classList.add("hidden"); }

  async function openWorkspaceDialog() {
    try {
      const picked = await window.pywebview.create_file_dialog(window.pywebview.FOLDER_DIALOG);
      if (picked) $("workspace-path").value = String(picked);
    } catch (e) {
      toast("无法打开目录选择：" + (e && e.message ? e.message : e));
    }
  }

  async function doApplyWorkspace(on_old) {
    const target = pendingWs.new_path;
    pendingWs = null;
    closeWsModal();
    try {
      const res = await webview.api.apply_workspace(target, on_old);
      await refreshStateLocal();
      closeSettings();
      toast(on_old === "move" ? "已迁移源码到新路径" :
            on_old === "delete" ? "已删除旧源码并切换路径" : "已切换源码路径");
    } catch (e) {
      closeSettings();
      toast("切换失败：" + (e && e.message ? e.message : e));
    }
  }

  async function saveSettings() {
    const enabled = $("proxy-enabled").checked;
    const host = $("proxy-host").value.trim() || "127.0.0.1";
    const port = parseInt($("proxy-port").value, 10) || 7897;
    const theme = $("theme-select").value;
    const newPath = $("workspace-path").value.trim();
    try {
      await webview.api.save_proxy(enabled, host, port);
      await webview.api.set_theme(theme);
      state.theme = theme;
      state.proxy = { enabled, host, port };
      state.proxyOn = enabled;
      applyTheme(theme);

      // 工作区处理
      if (newPath) {
        const prev = await webview.api.preview_workspace(newPath);
        if (!prev.same) {
          if (prev.will_prompt && prev.old_count > 0) {
            pendingWs = prev;
            $("ws-count").textContent = prev.old_count;
            openWsModal();
            return;  // 等用户在弹窗里选
          }
          await webview.api.apply_workspace(prev.new_path, "leave");
          await refreshStateLocal();
        }
      }
      closeSettings();
      toast("设置已保存");
    } catch (e) {
      toast("保存失败：" + (e && e.message ? e.message : e));
    }
  }

  /* ---------- 旧源码处理弹窗 ---------- */
  function openWsModal() { $("ws-modal").classList.remove("hidden"); }
  function closeWsModal() {
    $("ws-modal").classList.add("hidden");
    pendingWs = null;
  }

  /* ---------- 事件绑定 ---------- */
  function bind() {
    $("btn-refresh").addEventListener("click", () => refreshVersions(true));
    $("btn-clone").addEventListener("click", () => state.selected && runClone(state.selected));
    $("btn-run").addEventListener("click", () => state.selected && runStart(state.selected));
    $("btn-stop").addEventListener("click", runStop);
    $("btn-open").addEventListener("click", () => webview.api.open_web().catch((e) => toast("打开失败：" + e.message)));
    $("btn-clear-log").addEventListener("click", clearLog);
    $("btn-settings").addEventListener("click", openSettings);
    $("btn-modal-close").addEventListener("click", closeSettings);
    $("settings-modal").addEventListener("click", (e) => {
      if (e.target === $("settings-modal")) closeSettings();
    });
    $("btn-settings-save").addEventListener("click", saveSettings);
    $("btn-workspace-browse").addEventListener("click", openWorkspaceDialog);
    $("ws-move").addEventListener("click", () => doApplyWorkspace("move"));
    $("ws-delete").addEventListener("click", () => doApplyWorkspace("delete"));
    $("ws-leave").addEventListener("click", () => doApplyWorkspace("leave"));
    $("ws-modal").addEventListener("click", (e) => {
      if (e.target === $("ws-modal")) closeWsModal();
    });
    $("btn-node-download").addEventListener("click", () => webview.api.open_node_download());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { closeWsModal(); closeSettings(); }
    });
  }

  /* ---------- 启动 ---------- */
  // pywebview 的 JS 桥 (window.pywebview) 是异步注入的, 必须以 pywebviewready
  // 事件为“就绪”信号, 否则会在桥就绪前初始化, 导致事件监听全部失效。
  let started = false;
  async function init() {
    if (started) return;
    started = true;
    if (!webview || !webview.api) {
      els.nodeBadge.innerHTML = '<span class="dot"></span>请在 pywebview 中运行';
      els.versionList.innerHTML = '<li class="empty">此页面需在 DSH 管理器窗口中运行</li>';
      return;
    }
    bind();
    try {
      const st = await webview.api.get_state();
      state.versions = st.versions || [];
      state.local = {};
      (st.local || []).forEach((r) => { state.local[r.tag] = r.path; });
      state.proxy = st.proxy || { enabled: false, host: "127.0.0.1", port: 7897 };
      state.proxyOn = state.proxy.enabled;
      state.running = !!st.running;
      state.theme = st.theme || "dark";
      state.workspace = st.workspace;
      applyTheme(state.theme);
      renderNode(st.node);
      renderVersions();
      renderLocal();
      if (st.running) log("[提示] 后端正在运行 (" + (st.running_tag || "") + ")。\n");
      $("btn-refresh").click();
    } catch (e) {
      toast("初始化失败：" + (e && e.message ? e.message : e));
    }
  }

  function boot() {
    // 关键: 不能在开局 window.pywebview 还是 undefined 时误判为“普通浏览器”。
    // 实测桥约在脚本解析后 50ms 才注入。因此总是: ① 监听 pywebviewready 事件;
    // ② 轮询直到 .api 就绪。仅当长时间仍无 pywebview(判定为真·普通浏览器)才走兜底。
    const assign = function () { if (window.pywebview && window.pywebview.api) webview = window.pywebview; };
    window.addEventListener("pywebviewready", function () { assign(); init(); });
    if (window.pywebview && window.pywebview.api) {
      assign(); init();
      return;
    }
    els.versionList.innerHTML = '<li class="empty">正在连接界面桥…</li>';
    let n = 0;
    const iv = setInterval(function () {
      if (window.pywebview && window.pywebview.api) {
        clearInterval(iv); assign(); init();
      } else {
        n++;
        // 3s 后仍无 pywebview 对象 → 基本可判定为普通浏览器
        if (n > 30 && !window.pywebview) { clearInterval(iv); init(); }
        // 有 pywebview 但 15s 桥仍未就绪 → 兜底提示
        else if (n > 150) { clearInterval(iv); init(); }
      }
    }, 100);
  }
  boot();
})();
