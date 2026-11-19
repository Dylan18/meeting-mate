/* 桌面助理 · 前端逻辑 */
(function () {
  "use strict";
  function report(msg) {
    try {
      window.__errs = (window.__errs || []).concat([String(msg)]);
      // 后端日志留痕：界面出错时不能一点痕迹都没有
      const api = window.pywebview && window.pywebview.api;
      if (api && api.js_error) api.js_error(String(msg).slice(0, 500));
    } catch (e) {}
  }
  window.addEventListener("error", function (e) {
    report((e.message || "error") + " @ " + (e.filename || "").split("/").pop() + ":" + (e.lineno || 0));
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    report("promise: " + ((r && (r.message || r)) || "unknown"));
  });
  window.__report = report;
})();

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const py = () => (window.pywebview && window.pywebview.api) || null;

  let seq = 0;
  let state = { listening: false, meeting: null, count: 0, busy: "", asr: {}, llm_ready: false };
  let lastText = "";
  let tickTimer = null;
  let pollTimer = null;
  let histItems = [];
  let histSelected = null;

  // ---------------------------------------------------------------- 工具
  function hhmm(ts) {
    const d = new Date(ts * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  let toastTimer = null;
  function toast(msg, kind) {
    const t = $("toast");
    if (!msg) { t.classList.remove("show"); return; }
    t.textContent = msg.length > 70 ? msg.slice(0, 70) + "…" : msg;
    t.className = "toast show" + (kind === "err" ? " err" : kind === "ok" ? " ok" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 4200);
  }

  // ---------------------------------------------------------------- 字幕
  function addLine(rec) {
    const feed = $("feed");
    const empty = $("empty");
    if (empty) empty.remove();

    const line = el("div", "line" + (rec.scope === "meeting" ? " meeting" : ""));
    line.appendChild(el("span", "t", hhmm(rec.ts)));
    line.appendChild(el("span", "c", rec.text || ""));

    const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90;
    feed.appendChild(line);
    while (feed.children.length > 400) feed.removeChild(feed.firstChild);
    if (nearBottom) feed.scrollTop = feed.scrollHeight;
    lastText = rec.text || "";
  }

  function showEmpty() {
    const feed = $("feed");
    feed.innerHTML = "";
    const box = el("div", "empty");
    box.id = "empty";
    box.appendChild(el("div", "empty-ico")).innerHTML = '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#CBD2DB" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.5" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0"/><path d="M12 17.5V21"/><path d="M9 21h6"/></svg>';
    box.appendChild(el("div", "empty-t1", "还没有记录"));
    box.appendChild(el("div", "empty-t2", "点击「开始监听」，识别到的内容会实时出现在这里"));
    feed.appendChild(box);
  }

  // ---------------------------------------------------------------- 状态渲染
  function applyState(s) {
    state = s;

    // 状态点与文案
    const dot = $("dot");
    const st = $("statusText");
    dot.className = "dot";
    if (s.busy) {
      dot.classList.add("busy");
      st.textContent = s.busy;
    } else if (s.meeting) {
      dot.classList.add("meeting");
      st.textContent = "会议记录中";
    } else if (s.listening) {
      dot.classList.add("on");
      st.textContent = "正在监听";
    } else {
      dot.classList.add("idle");
      st.textContent = "未开始";
    }

    // 按钮
    const bl = $("btnListen");
    bl.textContent = s.listening ? "暂停监听" : "开始监听";
    bl.className = "btn primary" + (s.listening ? " stop" : "");

    const bm = $("btnMeeting");
    bm.textContent = s.meeting ? "结束会议" : "开始会议";
    bm.className = "btn " + (s.meeting ? "danger" : "outline-danger");

    const lock = !!s.busy;
    bl.disabled = lock;
    bm.disabled = lock;
    $("btnDaily").disabled = lock;

    // 计数
    $("count").textContent = "今天 " + (s.count || 0) + " 条";

    // 页脚
    const asr = s.asr || {};
    const map = { ready: "就绪", loading: "加载中", error: "异常", idle: "未启动" };
    const fa = $("footAsr");
    fa.textContent = "识别 · " + (map[asr.status] || "未启动");
    fa.style.color = asr.status === "ready" ? "#12A150"
      : asr.status === "error" ? "#E5484D" : "";
    fa.title = asr.text || "";
    const fl = $("footLlm");
    fl.textContent = s.llm_ready ? "LLM 已配置" : "LLM 未配置";
    fl.style.color = s.llm_ready ? "#12A150" : "#E8A33D";

    updateClock();
  }

  function updateClock() {
    const c = $("clock");
    if (state.busy) { c.textContent = ""; return; }
    if (state.meeting && state.meeting.start) {
      const sec = Math.max(0, Math.floor((Date.now() - new Date(state.meeting.start).getTime()) / 1000));
      c.textContent = "会议 " + String(Math.floor(sec / 60)).padStart(2, "0") + ":" + String(sec % 60).padStart(2, "0");
    } else if (state.listening) {
      const d = new Date();
      c.textContent = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    } else {
      c.textContent = "";
    }
  }

  // ---------------------------------------------------------------- 事件
  function handleEvent(ev) {
    const d = ev.data;
    switch (ev.kind) {
      case "text":
        addLine(d);
        break;
      case "level":
        setLevel($("mMic"), d && d[0]);
        setLevel($("mSys"), d && d[1]);
        break;
      case "asr_status":
        if (d && d.status === "error") toast(d.text, "err");
        break;
      case "notice":
        toast(String(d || ""));
        break;
      case "error":
        toast(String(d || ""), "err");
        break;
      case "report_ready":
        toast((d.kind === "meeting" ? "会议纪要" : "日报") + "已生成", "ok");
        openPath(d.path);
        if ($("sheetHistory").classList.contains("open")) loadHistory();
        break;
      case "meeting_started":
        toast("会议记录已开始", "ok");
        break;
      case "meeting_ended":
        toast("会议已结束，正在生成纪要…");
        break;
      default:
        break;
    }
  }

  const levelSmooth = { mic: 0, sys: 0 };
  function setLevel(node, v) {
    if (!node) return;
    const key = node.classList.contains("mic") ? "mic" : "sys";
    const val = Math.max(0, Math.min(1, v || 0));
    const mapped = val <= 0.00001 ? 0 : Math.min(1, (Math.log10(val) + 4) / 4);
    levelSmooth[key] = levelSmooth[key] * 0.55 + mapped * 0.45;
    node.style.width = (levelSmooth[key] * 100).toFixed(1) + "%";
  }

  // ---------------------------------------------------------------- 轮询
  let pollFails = 0;
  function markBroken(msg) {
    const fa = $("footAsr");
    if (fa) { fa.textContent = msg; fa.style.color = "#E5484D"; fa.title = "界面与后台通信失败"; }
  }

  async function pollLoop() {
    const api = py();
    if (!api) {
      pollFails++;
      if (pollFails === 12) markBroken("未连接后台");
    } else {
      try {
        const r = await api.poll(seq);
        if (r) {
          seq = r.seq;
          (r.events || []).forEach(handleEvent);
          applyState(r.state);
        }
        pollFails = 0;
      } catch (e) {
        pollFails++;
        // 连续失败才提示，避免模型加载时的瞬时异常刷屏
        if (pollFails === 12) markBroken("通信异常");
      }
    }
    clearTimeout(pollTimer);
    pollTimer = setTimeout(pollLoop, 260);
  }

  // ---------------------------------------------------------------- 动作
  async function toggleListen() {
    const api = py();
    if (!api) { toast("后台还没连上，请稍后重试", "err"); return; }
    try { applyState(await api.toggle_listen()); } catch (e) { toast("操作失败", "err"); }
  }
  async function toggleMeeting() {
    const api = py();
    if (!api) { toast("后台还没连上，请稍后重试", "err"); return; }
    try { applyState(await api.toggle_meeting()); } catch (e) { toast("操作失败", "err"); }
  }
  async function makeDaily() {
    const api = py();
    if (!api) { toast("后台还没连上，请稍后重试", "err"); return; }
    toast("正在整理今天的记录…");
    try { await api.make_daily(null); } catch (e) { toast("生成失败", "err"); }
  }
  function openPath(p) {
    const api = py(); if (api && p) api.open_path(p);
  }

  // ---------------------------------------------------------------- 设置
  async function openSettings() {
    const api = py(); if (!api) return;
    const s = await api.get_settings();
    const dev = await api.get_devices();

    $("cbMic").checked = !!s.mic_enabled;
    $("cbSys").checked = !!s.system_audio;
    $("selModel").value = s.asr_model || "sensevoice";
    $("selLang").value = s.asr_language || "auto";
    $("cbDaily").checked = !!s.daily_report_enabled;
    $("inTime").value = s.daily_report_time || "18:30";
    $("cbTop").checked = !!s.always_on_top;

    const sel = $("selMic");
    sel.innerHTML = "";
    sel.appendChild(new Option("自动选择", ""));
    (dev.mic || []).forEach((d) => sel.appendChild(new Option(d.name, d.id)));
    sel.value = s.mic_device_id || "";

    const llm = s.llm || {};
    $("inBase").value = llm.base_url || "";
    $("inKey").value = llm.api_key || "";
    $("inModel").value = llm.model || "";
    $("testMsg").textContent = "";

    openSheet("sheetSettings");
  }

  async function saveSettings() {
    const api = py(); if (!api) return;
    if (!$("cbMic").checked && !$("cbSys").checked) {
      toast("至少要启用一个音源", "err");
      return;
    }
    const patch = {
      mic_enabled: $("cbMic").checked,
      system_audio: $("cbSys").checked,
      mic_device_id: $("selMic").value || null,
      asr_model: $("selModel").value,
      asr_language: $("selLang").value,
      daily_report_enabled: $("cbDaily").checked,
      daily_report_time: $("inTime").value.trim() || "18:30",
      always_on_top: $("cbTop").checked,
      llm: {
        base_url: $("inBase").value.trim(),
        api_key: $("inKey").value.trim(),
        model: $("inModel").value.trim(),
      },
    };
    const r = await api.save_settings(patch);
    if (r && r.state) applyState(r.state);
    closeSheet("sheetSettings");
    toast("设置已保存", "ok");
  }

  async function testLLM() {
    const api = py(); if (!api) return;
    const msg = $("testMsg");
    const btn = $("btnTest");
    msg.textContent = "测试中…";
    msg.style.color = "#7A8494";
    btn.disabled = true;
    try {
      const r = await api.test_llm({
        base_url: $("inBase").value.trim(),
        api_key: $("inKey").value.trim(),
        model: $("inModel").value.trim(),
      });
      msg.textContent = r.msg || (r.ok ? "连接成功" : "失败");
      msg.style.color = r.ok ? "#12A150" : "#E5484D";
    } catch (e) {
      msg.textContent = "测试失败";
      msg.style.color = "#E5484D";
    }
    btn.disabled = false;
  }

  // ---------------------------------------------------------------- 简易 Markdown 渲染
  function escHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function inlineMd(s) {
    return escHtml(s)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }
  function mdToHtml(md) {
    const lines = String(md || "").split(/\r?\n/);
    let out = [], listOpen = false, tableRows = [];

    const closeList = () => { if (listOpen) { out.push("</ul>"); listOpen = false; } };
    const flushTable = () => {
      if (!tableRows.length) return;
      let t = "<table>";
      tableRows.forEach((row, i) => {
        const cells = row.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        if (i === 0) {
          t += "<thead><tr>" + cells.map((c) => `<th>${inlineMd(c)}</th>`).join("") + "</tr></thead><tbody>";
        } else if (!cells.every((c) => /^:?-{2,}:?$/.test(c))) {
          t += "<tr>" + cells.map((c) => `<td>${inlineMd(c)}</td>`).join("") + "</tr>";
        }
      });
      t += "</tbody></table>";
      out.push(t);
      tableRows = [];
    };

    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (/^\|/.test(line)) { closeList(); tableRows.push(line); continue; }
      flushTable();
      if (!line.trim()) { closeList(); continue; }
      let m;
      if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
        closeList();
        const lv = Math.min(m[1].length, 3);
        out.push(`<h${lv}>${inlineMd(m[2])}</h${lv}>`);
      } else if ((m = line.match(/^>\s?(.*)$/))) {
        closeList();
        out.push(`<blockquote>${inlineMd(m[1])}</blockquote>`);
      } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
        if (!listOpen) { out.push("<ul>"); listOpen = true; }
        out.push(`<li>${inlineMd(m[1]).replace(/^\[ \]\s*/, "☐ ").replace(/^\[x\]\s*/i, "☑ ")}</li>`);
      } else if (/^\s*-{3,}\s*$/.test(line)) {
        closeList();
        out.push("<hr>");
      } else {
        closeList();
        out.push(`<p>${inlineMd(line)}</p>`);
      }
    }
    closeList();
    flushTable();
    return out.join("");
  }

  // ---------------------------------------------------------------- 历史
  async function openHistory() {
    await loadHistory();
    openSheet("sheetHistory");
  }

  async function loadHistory() {
    const api = py(); if (!api) return;
    histItems = (await api.list_reports()) || [];
    const list = $("histList");
    list.innerHTML = "";
    if (!histItems.length) {
      list.appendChild(el("div", "hist-empty", "还没有生成过记录"));
      return;
    }
    histItems.forEach((it, i) => {
      const row = el("div", "hist-item" + (histSelected === it.path ? " active" : ""));
      row.dataset.path = it.path;
      const top = el("div", "hi-top");
      top.appendChild(el("span", "badge " + it.kind, it.kind === "meeting" ? "会议" : "日报"));
      top.appendChild(el("span", "hi-title", String(it.name).replace(/^(日报|会议)_/, "")));
      row.appendChild(top);
      row.appendChild(el("span", "hi-time", it.mtime));
      row.addEventListener("click", () => selectReport(it.path, row));
      list.appendChild(row);
    });
    if (!histSelected && histItems.length) {
      selectReport(histItems[0].path, list.firstChild);
    }
  }

  async function selectReport(path, row) {
    const api = py(); if (!api) return;
    histSelected = path;
    document.querySelectorAll(".hist-item").forEach((n) => n.classList.remove("active"));
    if (row) row.classList.add("active");
    const r = await api.read_report(path);
    const pv = $("histPreview");
    if (r.ok) {
      pv.innerHTML = mdToHtml(r.text);
    } else {
      pv.innerHTML = `<div class="hist-empty">${escHtml(r.text || "读取失败")}</div>`;
    }
    pv.scrollTop = 0;
  }

  // ---------------------------------------------------------------- 抽屉
  function openSheet(id) { $(id).classList.add("open"); }
  function closeSheet(id) { $(id).classList.remove("open"); }

  // ---------------------------------------------------------------- 窗口拖动
  // WebView2 平台不支持 pywebview 的 easy_drag，这里只在标题栏自己实现
  let drag = null;
  function initDrag() {
    const bar = document.querySelector(".titlebar");
    if (!bar) return;
    bar.addEventListener("mousedown", async (e) => {
      if (e.button !== 0 || e.target.closest("button")) return;
      const api = py();
      if (!api) return;
      e.preventDefault();
      try {
        const p = await api.win_pos();
        drag = { sx: e.screenX, sy: e.screenY, wx: p.x, wy: p.y, last: 0 };
      } catch (err) { drag = null; }
    });
    window.addEventListener("mousemove", (e) => {
      if (!drag) return;
      const now = performance.now();
      if (now - drag.last < 26) return;
      drag.last = now;
      const api = py();
      if (api) api.win_move(drag.wx + (e.screenX - drag.sx), drag.wy + (e.screenY - drag.sy));
    });
    window.addEventListener("mouseup", () => { drag = null; });
    window.addEventListener("blur", () => { drag = null; });
  }

  // ---------------------------------------------------------------- 初始化
  function bind() {
    initDrag();
    $("btnListen").addEventListener("click", toggleListen);
    $("btnMeeting").addEventListener("click", toggleMeeting);
    $("btnDaily").addEventListener("click", makeDaily);
    $("btnHistory").addEventListener("click", openHistory);
    $("btnSettings").addEventListener("click", openSettings);
    $("btnMin").addEventListener("click", () => { const a = py(); if (a) a.minimize(); });
    $("btnClose").addEventListener("click", () => {
      if (state.listening || state.meeting) {
        if (!confirm("监听或会议仍在进行，确定退出吗？\n（已记录的内容会自动保存）")) return;
      }
      const a = py(); if (a) a.quit();
    });

    document.querySelectorAll("[data-close]").forEach((b) => {
      b.addEventListener("click", () => closeSheet(b.dataset.close));
    });

    $("presets").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      $("inBase").value = b.dataset.url;
      $("inModel").value = b.dataset.model;
      $("testMsg").textContent = "";
    });

    $("btnSave").addEventListener("click", saveSettings);
    $("btnTest").addEventListener("click", testLLM);
    $("btnOpenDir").addEventListener("click", () => { const a = py(); if (a) a.open_reports_dir(); });
    $("btnOpenFile").addEventListener("click", () => {
      if (histSelected) openPath(histSelected);
    });

    document.querySelectorAll(".titlebar").forEach((n) => n.classList.add("pywebview-drag-region"));

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        document.querySelectorAll(".sheet.open").forEach((s) => s.classList.remove("open"));
      }
    });
  }

  let booted = false;
  async function boot() {
    if (booted) return;
    booted = true;
    try {
      bind();
    } catch (e) {
      report("bind 失败: " + (e && e.message));
      toast("界面初始化失败：" + (e && e.message), "err");
    }
    const api = py();
    if (api) {
      try {
        const recent = await api.get_recent(40);
        if (recent && recent.length) {
          const empty = $("empty");
          if (empty) empty.remove();
          recent.forEach(addLine);
          $("feed").scrollTop = $("feed").scrollHeight;
        }
      } catch (e) { /* ignore */ }
    }
    tickTimer = setInterval(updateClock, 1000);
    pollLoop();
  }

  // 看门狗：8 秒还没桥接成功，就把问题摆到界面上，不留给用户"点了没反应"
  setTimeout(() => {
    if (!py()) {
      const st = $("statusText");
      if (st) st.textContent = "界面未连接后台";
      markBroken("未连接后台");
      toast("界面没能连上后台，请关闭后重新打开", "err");
    }
  }, 8000);

  window.addEventListener("pywebviewready", boot);
  if (window.pywebview) boot();
})();
