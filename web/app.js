/* CQA frontend — plain JavaScript, no dependencies (Dynamics 365 web resource friendly). */
(function () {
  "use strict";

  // ---------- config / state ----------
  const qs = new URLSearchParams(location.search);
  const API = (qs.get("api") || "").replace(/\/$/, "") + "/api";
  const store = {
    get(k, d) { try { const v = localStorage.getItem("cqa." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("cqa." + k, JSON.stringify(v)); } catch (e) { /* ignore */ } },
  };
  const state = {
    role: qs.get("role") || store.get("role", "operator"),
    checklistId: store.get("checklist", "freedom_default"),
    group: store.get("group", ""),
    samples: store.get("samples", 3),
    provider: store.get("provider", "primary"),
    job: null,
    checklists: [],
    health: null,
  };
  if (qs.get("embed") === "1") document.body.classList.add("embed");

  const LABEL = { yes: "Да", no: "Нет", na: "N/A" };
  const TYPE_LABEL = { binary: "Да / Нет", scale: "Шкала", flag: "Флаг (без баллов)" };
  const SPEAKER = { client: "Клиент", operator: "Оператор", bot: "Бот", system: "Система" };
  const speakerName = (m) => (SPEAKER[m.speaker] || m.speaker)
    + (m.role_needs_review ? " · роль требует проверки" : "");
  const CHANNEL = { "agent chat": "Чат", "voice call": "Звонок" };

  // ---------- helpers ----------
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k === "html") el.innerHTML = v; // only used with static strings
        else if (k === "value") el.value = v;
        else if (k === "checked") el.checked = !!v;
        else el.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  const $ = (s, r) => (r || document).querySelector(s);
  const fmt = (x, d) => (x == null ? "—" : Number(x).toLocaleString("ru-RU", { maximumFractionDigits: d == null ? 2 : d }));
  const gnum = (x) => (x == null ? "" : String(+x));

  async function api(path, opts) {
    const o = Object.assign({ headers: {} }, opts || {});
    if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
    const r = await fetch(API + path, o);
    let data = null;
    try { data = await r.json(); } catch (e) { /* empty */ }
    if (!r.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || r.statusText);
    return data;
  }

  let toastTimer;
  function toast(msg, err) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast show" + (err ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.className = "toast"), err ? 6000 : 3000);
  }
  const fail = (e) => toast(e.message || String(e), true);

  function answerBadge(answer, value, type) {
    if (answer == null) return h("span", { class: "badge b-warn" }, "нет ответа");
    if (answer === "score") return h("span", { class: "badge b-score" }, gnum(value));
    if (type === "flag") return h("span", { class: "badge " + (answer === "yes" ? "b-gross" : "b-yes") }, answer === "yes" ? "Выявлено" : "Нет");
    return h("span", { class: "badge b-" + answer }, LABEL[answer] || answer);
  }
  const evLabel = (e) => (e.from_id === e.to_id ? e.from_id : e.from_id + "–" + e.to_id);

  function modal(content) {
    const back = h("div", { class: "modal-back", onclick: (e) => { if (e.target === back) close(); } }, h("div", { class: "modal" }, content));
    function close() { back.remove(); }
    document.body.appendChild(back);
    return close;
  }

  // ---------- shell ----------
  function setRole(r) {
    state.role = r; store.set("role", r);
    document.querySelectorAll("#role-switch button").forEach((b) => b.classList.toggle("on", b.dataset.role === r));
    route();
  }
  document.querySelectorAll("#role-switch button").forEach((b) => b.addEventListener("click", () => setRole(b.dataset.role)));
  document.querySelectorAll("#role-switch button").forEach((b) => b.classList.toggle("on", b.dataset.role === state.role));

  async function refreshAppealCount() {
    try {
      const a = await api("/appeals?status=open");
      $("#appeal-count").textContent = a.length ? a.length : "";
    } catch (e) { /* ignore */ }
  }

  async function loadChecklists() {
    state.checklists = await api("/checklists");
    if (!state.checklists.find((c) => c.id === state.checklistId) && state.checklists.length) state.checklistId = state.checklists[0].id;
  }

  function checklistSelect(onchange) {
    return h("select", {
      onchange: (e) => { state.checklistId = e.target.value; store.set("checklist", state.checklistId); onchange && onchange(); },
    }, state.checklists.map((c) => h("option", { value: c.id, selected: c.id === state.checklistId }, `${c.name} · v${c.version}`)));
  }

  const views = {};
  async function route() {
    const hash = location.hash || (qs.get("conversation_id") ? "#/dialog/" + encodeURIComponent(qs.get("conversation_id")) : "#/dialogs");
    const [path, query] = hash.slice(2).split("?");
    const parts = path.split("/").map(decodeURIComponent);
    const params = new URLSearchParams(query || "");
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === (parts[0] === "dialog" ? "dialogs" : parts[0])));
    const app = $("#app");
    app.replaceChildren(h("div", { class: "empty" }, "Загрузка…"));
    try {
      if (!state.checklists.length) await loadChecklists();
      const v = { dialogs: views.dialogs, dialog: views.dialog, appeals: views.appeals, checklists: views.checklists, quality: views.quality }[parts[0]] || views.dialogs;
      await v(app, parts.slice(1), params);
    } catch (e) {
      app.replaceChildren(h("div", { class: "panel empty" }, "Ошибка: " + e.message));
    }
    refreshAppealCount();
  }
  window.addEventListener("hashchange", route);

  // ---------- jobs ----------
  async function runJob(ids, opts) {
    const body = Object.assign({
      conversation_ids: ids, checklist_id: state.checklistId,
      samples: state.samples, provider: state.provider,
    }, opts || {});
    const { job_id } = await api("/evaluations", { method: "POST", body });
    state.job = { id: job_id, total: ids.length, done: 0, status: "running" };
    renderJob();
    return new Promise((resolve) => {
      const tick = async () => {
        try {
          const j = await api("/jobs/" + job_id);
          state.job = j;
          renderJob();
          if (j.status === "running") return setTimeout(tick, 1500);
          if (j.failed && j.failed.length) toast(`Готово с ошибками: ${j.failed.length}. ${j.failed[0].error}`, true);
          else toast(`Оценено диалогов: ${j.done}`);
          resolve(j);
        } catch (e) { fail(e); resolve(null); }
      };
      setTimeout(tick, 800);
    });
  }

  function renderJob() {
    const box = $("#job-box");
    if (!box || !state.job) return;
    const j = state.job;
    if (j.status !== "running") { box.replaceChildren(); return; }
    const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
    box.replaceChildren(h("div", { class: "row" },
      h("span", { class: "badge b-acc" }, "Оценка идёт"),
      h("div", { class: "progress" }, h("div", { style: `width:${pct}%` })),
      h("span", { class: "small muted" }, `${j.done} / ${j.total}` + (j.current ? ` · сейчас: ${j.current}` : ""))));
  }

  // ---------- view: dialog list ----------
  views.dialogs = async function (app) {
    const groups = await api("/groups");
    if (state.group && !groups.includes(state.group)) state.group = "";
    let dialogs = await api("/dialogs?" + new URLSearchParams(Object.assign(
      { checklist_id: state.checklistId }, state.group ? { group_id: state.group } : {})));
    const filter = { q: "", channel: "", flagged: false };
    const selected = new Set();
    state.health = state.health || await api("/health");

    const tbody = h("tbody");
    const countEl = h("span", { class: "small muted" });
    const runSel = h("button", { class: "btn", onclick: () => run([...selected]) }, "Оценить выбранные");

    function visible() {
      return dialogs.filter((d) => {
        if (filter.q && !d.conversation_id.toLowerCase().includes(filter.q.toLowerCase())) return false;
        if (filter.channel && d.channel !== filter.channel) return false;
        if (filter.flagged) {
          const e = d.evaluation;
          if (!e || !(e.gross_violation || e.needs_review.length || e.low_confidence.length || e.open_appeals)) return false;
        }
        return true;
      });
    }

    function renderRows() {
      const rows = visible();
      countEl.textContent = `${rows.length} из ${dialogs.length}`;
      runSel.disabled = !selected.size;
      runSel.textContent = selected.size ? `Оценить выбранные (${selected.size})` : "Оценить выбранные";
      tbody.replaceChildren(...rows.map((d) => {
        const e = d.evaluation;
        const open = () => (location.hash = "#/dialog/" + encodeURIComponent(d.conversation_id));
        const cb = h("input", { type: "checkbox", checked: selected.has(d.conversation_id),
          onclick: (ev) => ev.stopPropagation(),
          onchange: (ev) => { ev.target.checked ? selected.add(d.conversation_id) : selected.delete(d.conversation_id); renderRows(); } });
        const flags = [];
        if (e) {
          if (e.gross_violation) flags.push(h("span", { class: "badge b-gross" }, "Грубая ошибка"));
          if (e.needs_review.length) flags.push(h("span", { class: "badge b-warn" }, "Нужна проверка: " + e.needs_review.join(", ")));
          if (e.low_confidence.length) flags.push(h("span", { class: "badge b-warn", title: "Прогоны модели разошлись" }, "Низкая уверенность: " + e.low_confidence.join(", ")));
          if (e.open_appeals) flags.push(h("span", { class: "badge b-score" }, "Апелляций: " + e.open_appeals));
          if (e.changed) flags.push(h("span", { class: "badge b-chan" }, "Изменено проверяющим"));
        }
        return h("tr", { class: "click", onclick: open },
          h("td", null, cb),
          h("td", null, h("b", null, d.conversation_id), h("div", { class: "small muted" }, d.group_id || "")),
          h("td", null, h("span", { class: "badge b-chan" }, CHANNEL[d.channel] || d.channel)),
          h("td", null, d.language || "—"),
          h("td", { class: "num" }, d.messages),
          h("td", { class: "num total" }, e ? (e.total == null ? "—" : fmt(e.total)) : h("span", { class: "muted" }, "не оценён")),
          h("td", { class: "num total" }, e && e.final_total !== e.total ? fmt(e.final_total) : ""),
          h("td", null, h("div", { class: "chips" }, flags)),
          h("td", { class: "small muted" }, e ? `v${e.checklist_version}` : ""));
      }));
      if (!rows.length) tbody.replaceChildren(h("tr", null, h("td", { colspan: 9, class: "empty" }, "Нет диалогов. Загрузите набор по group_id или добавьте диалог.")));
    }

    async function reload() {
      dialogs = await api("/dialogs?" + new URLSearchParams(Object.assign(
        { checklist_id: state.checklistId }, state.group ? { group_id: state.group } : {})));
      renderRows();
    }

    async function run(ids, opts) {
      if (!ids.length) return;
      try { await runJob(ids, opts); } catch (e) { fail(e); }
      await reload();
    }

    const groupInput = h("input", { placeholder: "group_id, например DEV", style: "width:180px" });
    const fetchBtn = h("button", { class: "btn", onclick: async () => {
      const g = groupInput.value.trim();
      if (!g) return toast("Введите group_id", true);
      fetchBtn.disabled = true;
      try {
        const r = await api("/datasets/fetch", { method: "POST", body: { group_id: g } });
        toast(`Загружено диалогов: ${r.imported}`);
        state.group = g; store.set("group", g);
        route();
      } catch (e) { fail(e); } finally { fetchBtn.disabled = false; }
    } }, "Загрузить набор");

    const exportBtn = h("button", { class: "btn", title: "Файл в формате организаторов (schema 1.0): последняя оценка каждого диалога набора по текущей версии чек-листа", onclick: async () => {
      if (!state.group) return toast("Выберите набор", true);
      exportBtn.disabled = true;
      try {
        const r = await api(`/export/results?group=${encodeURIComponent(state.group)}&checklist_id=${encodeURIComponent(state.checklistId)}`);
        const blob = new Blob([JSON.stringify(r.results, null, 2)], { type: "application/json" });
        const a = h("a", { href: URL.createObjectURL(blob), download: "results.json" });
        document.body.append(a); a.click(); a.remove();
        const okN = r.results.results.filter((x) => x.status === "ok").length;
        toast(r.problems.length ? `Выгружено с замечаниями (${r.problems.length}): ${r.problems.slice(0, 3).join("; ")}`
          : `results.json: ${r.total} диалогов, оценено ${okN}, чек-лист v${r.checklist_version}`, r.problems.length > 0);
      } catch (e) { fail(e); } finally { exportBtn.disabled = false; }
    } }, "Выгрузить results.json");

    const provOptions = [h("option", { value: "primary", selected: state.provider === "primary" },
      "Облако: " + ((state.health.providers.primary || {}).model || "не настроено"))];
    if (state.health.providers.local) provOptions.push(h("option", { value: "local", selected: state.provider === "local" }, "Локально: " + state.health.providers.local.model));

    app.replaceChildren(
      h("div", { class: "row", style: "margin-bottom:12px" },
        h("div", null, h("h1", null, "Диалоги"), h("div", { class: "sub" }, "Выберите диалоги и запустите оценку по чек-листу")),
        h("span", { class: "spacer" }),
        groupInput, fetchBtn,
        h("button", { class: "btn", onclick: uploadDialog }, "Добавить диалог"), exportBtn),
      h("div", { class: "panel", style: "margin-bottom:12px" },
        h("div", { class: "row" },
          h("label", { class: "field" }, "Набор",
            h("select", { onchange: (e) => { state.group = e.target.value; store.set("group", state.group); route(); } },
              h("option", { value: "" }, "Все"), groups.map((g) => h("option", { value: g, selected: g === state.group }, g)))),
          h("label", { class: "field" }, "Чек-лист", checklistSelect(reload)),
          h("label", { class: "field" }, "Модель", h("select", { onchange: (e) => { state.provider = e.target.value; store.set("provider", state.provider); } }, provOptions)),
          h("label", { class: "field", title: "Сколько раз модель оценивает каждый критерий; итог — голосование" }, "Прогонов на критерий",
            h("select", { onchange: (e) => { state.samples = +e.target.value; store.set("samples", state.samples); } },
              [1, 3, 5].map((n) => h("option", { value: n, selected: n === state.samples }, n)))),
          h("span", { class: "spacer" }),
          runSel,
          h("button", { class: "btn primary", onclick: () => run(visible().map((d) => d.conversation_id)) }, "Оценить все показанные")),
        h("div", { id: "job-box", style: "margin-top:10px" })),
      h("div", { class: "panel" },
        h("div", { class: "row", style: "margin-bottom:8px" },
          h("input", { placeholder: "Поиск по ID", oninput: (e) => { filter.q = e.target.value; renderRows(); } }),
          h("select", { onchange: (e) => { filter.channel = e.target.value; renderRows(); } },
            h("option", { value: "" }, "Все каналы"), h("option", { value: "agent chat" }, "Чаты"), h("option", { value: "voice call" }, "Звонки")),
          h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => { filter.flagged = e.target.checked; renderRows(); } }), "Только требующие внимания"),
          h("span", { class: "spacer" }), countEl),
        h("table", { class: "table" },
          h("thead", null, h("tr", null,
            h("th", null, h("input", { type: "checkbox", title: "Выбрать все показанные", onchange: (e) => {
              visible().forEach((d) => (e.target.checked ? selected.add(d.conversation_id) : selected.delete(d.conversation_id))); renderRows(); } })),
            h("th", null, "Диалог"), h("th", null, "Канал"), h("th", null, "Язык"), h("th", { class: "num" }, "Реплик"),
            h("th", { class: "num" }, "Оценка AI"), h("th", { class: "num" }, "Итог"), h("th", null, "Отметки"), h("th", null, "Чек-лист"))),
          tbody)));
    renderRows();
    renderJob();
  };

  function uploadDialog() {
    const ta = h("textarea", { rows: 12, placeholder: "JSON записи в формате API организаторов\nили текст построчно:\n00:01 Клиент: Здравствуйте, не проходит вывод\n00:40 Оператор: Здравствуйте, меня зовут Дана…" });
    const id = h("input", { placeholder: "ID диалога (необязательно)" });
    const channel = h("select", null, h("option", { value: "agent chat" }, "Чат"), h("option", { value: "voice call" }, "Звонок"));
    const lang = h("select", null, ["RU", "KZ", "EN"].map((l) => h("option", { value: l }, l)));
    const file = h("input", { type: "file", accept: ".json,.txt", onchange: async (e) => { const f = e.target.files[0]; if (f) ta.value = await f.text(); } });
    const audioFile = h("input", { type: "file", accept: ".mp3,audio/mpeg" });
    const audioLang = h("select", null, ["RU", "KZ", "EN", "AUTO"].map((l) => h("option", { value: l }, l)));
    const cloudReady = !!(state.health && state.health.stt && state.health.stt.cloud.available);
    const audioMode = h("select", null,
      h("option", { value: "local" }, "Локально"),
      h("option", { value: "cloud", disabled: !cloudReady }, "Alem STT" + (cloudReady ? "" : " (ожидает ключ)")));
    const progressLabel = h("span", { class: "small muted" });
    const progressFill = h("div", { style: "width:0%" });
    const progress = h("div", { class: "row" }, progressLabel, h("div", { class: "progress" }, progressFill));
    function stage(label, percent) { progressLabel.textContent = label; progressFill.style.width = percent + "%"; }
    async function uploadMp3() {
      const chosen = audioFile.files[0];
      if (!chosen) throw new Error("Выберите MP3-файл");
      const params = new URLSearchParams({ mode: audioMode.value, language: audioLang.value });
      if (id.value.trim()) params.set("conversation_id", id.value.trim());
      stage("Загрузка записи", 10);
      const response = await fetch(API + "/dialogs/upload-audio?" + params, {
        method: "POST", headers: { "Content-Type": "audio/mpeg" }, body: chosen,
      });
      const started = await response.json();
      if (!response.ok) throw new Error(started.detail || response.statusText);
      const labels = { queued: "В очереди", recognizing: "Распознавание речи", separating: "Разделение голосов",
        parsing: "Определение ролей", saving: "Сохранение диалога" };
      const widths = { queued: 15, recognizing: 35, separating: 65, parsing: 78, saving: 90 };
      let job;
      do {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        job = await api("/audio-jobs/" + started.job_id);
        stage(labels[job.stage] || "Обработка записи", widths[job.stage] || 20);
      } while (job.status === "running");
      if (job.status === "error") throw new Error(job.error || "Не удалось обработать запись");
      stage("Диалог добавлен", 100);
      return job.conversation_id;
    }
    const close = modal(h("div", { class: "stack" },
      h("h2", null, "Добавить диалог"),
      h("div", { class: "sub" }, "Можно вставить JSON (одна запись, массив или весь ответ API) или текст «роль: реплика»."),
      file, ta,
      h("div", { class: "sub" }, "Или загрузите MP3. Роли, определённые по фразам, потребуют проверки."),
      audioFile,
      h("div", { class: "row" }, h("label", { class: "field" }, "Распознавание", audioMode),
        h("label", { class: "field" }, "Язык записи", audioLang)),
      progress,
      h("div", { class: "row" }, id, channel, lang, h("span", { class: "spacer" }),
        h("button", { class: "btn", onclick: () => close() }, "Отмена"),
        h("button", { class: "btn primary", onclick: async (event) => {
          event.currentTarget.disabled = true;
          try {
            if (audioFile.files.length) {
              const cid = await uploadMp3();
              if (state.provider === "local" || confirm("Оценка отправит транскрипт внешнему API. Разрешить?")) {
                stage("Оценка по чек-листу", 95);
                try { await runJob([cid]); } catch (e) { fail(e); }
              }
              close();
              location.hash = "#/dialog/" + encodeURIComponent(cid);
              return;
            }
            const r = await api("/dialogs/upload", { method: "POST", body: {
              content: ta.value, conversation_id: id.value.trim() || null, channel: channel.value, language: lang.value } });
            close();
            toast("Добавлено: " + r.conversation_ids.join(", "));
            if (r.conversation_ids.length === 1) location.hash = "#/dialog/" + encodeURIComponent(r.conversation_ids[0]);
            else { state.group = "UPLOAD"; store.set("group", "UPLOAD"); route(); }
          } catch (e) { fail(e); }
          finally { event.currentTarget.disabled = false; }
        } }, "Добавить"))));
  }

  // ---------- view: dialog card ----------
  views.dialog = async function (app, parts, params) {
    const cid = parts[0];
    const data = await api("/dialogs/" + encodeURIComponent(cid) + "?" + new URLSearchParams({ checklist_id: state.checklistId }));
    const d = data.dialog;
    const facts = data.facts;
    const ev = data.evaluation;
    const isCall = d.channel === "voice call";
    const hasTranslation = isCall && d.messages.some((m) => m.translation_ru);
    let showTranslation = store.get("showTranslation", false);
    const holdIds = new Set(facts.on_hold_ids || []);
    const msgEls = {};
    let audio = null;
    let offsetCorrection = store.get("audioOffset." + cid, 0);

    // --- audio offsets: recording may start later than the first event and may be paused ---
    const recStart = ((facts.events || []).find((e) => e.type === "recording_started") || {}).t || 0;
    const pauses = [];
    let pauseStart = null;
    for (const e of facts.events || []) {
      if (e.type === "recording_paused") pauseStart = e.t;
      if (e.type === "recording_resumed" && pauseStart != null) { pauses.push([pauseStart, e.t]); pauseStart = null; }
    }
    function audioTime(t) {
      let x = t - recStart;
      for (const [a, b] of pauses) { if (t >= b) x -= b - a; else if (t > a) x -= t - a; }
      return Math.max(0, x + offsetCorrection);
    }

    function jump(fromId, toId, play) {
      document.querySelectorAll(".msg.ev").forEach((m) => m.classList.remove("ev"));
      const a = +fromId.slice(1), b = +(toId || fromId).slice(1);
      for (let i = a; i <= b; i++) { const el = msgEls["m" + i]; if (el) el.classList.add("ev"); }
      const first = msgEls[fromId];
      if (first) { first.scrollIntoView({ behavior: "smooth", block: "center" }); first.classList.remove("flash"); void first.offsetWidth; first.classList.add("flash"); }
      if (play && audio && first) {
        const m = d.messages[a - 1];
        if (m && m.t != null) { audio.currentTime = audioTime(m.t); audio.play().catch(() => {}); }
      }
    }

    // --- transcript ---
    const translationEls = [];
    const transcript = h("div", { class: "transcript" }, d.messages.map((m) => {
      const hold = holdIds.has(m.id);
      const translation = isCall && m.translation_ru
        ? h("div", { class: "translation-ru", hidden: !showTranslation }, m.translation_ru)
        : null;
      if (translation) translationEls.push(translation);
      const el = h("div", { class: `msg ${m.speaker}${hold ? " hold" : ""}`, id: "msg-" + m.id },
        h("div", { class: "meta" }, h("span", { class: "mid" }, m.id), fmtT(m.t),
          isCall && m.t != null ? h("div", null, h("button", { class: "play", title: "Воспроизвести с этого места", onclick: () => playAt(m) }, "▶")) : null),
        h("div", null,
          h("div", { class: "who" }, speakerName(m), hold ? " · на удержании (клиент не слышит)" : ""),
          h("div", { class: "text" }, m.text), translation));
      msgEls[m.id] = el;
      return el;
    }));
    const originalBtn = h("button", { class: "btn", onclick: () => toggleTranslation(false) }, "Оригинал");
    const translationBtn = h("button", { class: "btn", onclick: () => toggleTranslation(true) }, "Перевод");
    function toggleTranslation(value) {
      showTranslation = value;
      store.set("showTranslation", value);
      for (const el of translationEls) el.hidden = !value;
      originalBtn.className = value ? "btn" : "btn primary";
      translationBtn.className = value ? "btn primary" : "btn";
      originalBtn.setAttribute("aria-pressed", String(!value));
      translationBtn.setAttribute("aria-pressed", String(value));
    }
    toggleTranslation(showTranslation);
    const translationSwitch = hasTranslation
      ? h("div", { class: "translation-toggle", role: "group", "aria-label": "Отображение перевода" },
        originalBtn, translationBtn)
      : null;
    function fmtT(t) {
      if (t == null) return "";
      t = Math.round(t);
      const mm = Math.floor(t / 60), ss = String(t % 60).padStart(2, "0");
      return `${mm}:${ss}`;
    }
    function playAt(m) {
      if (!audio) return toast("Сначала загрузите запись", true);
      audio.currentTime = audioTime(m.t);
      audio.play().catch(() => {});
    }

    let audioBar = null;
    if (isCall && data.has_audio) {
      const holder = h("div", { class: "small muted" }, "Запись звонка");
      const offsetInput = h("input", { type: "number", step: "0.5", value: offsetCorrection, style: "width:80px",
        title: "Если звук не совпадает с репликой — сдвиньте (секунды)",
        onchange: (e) => { offsetCorrection = +e.target.value || 0; store.set("audioOffset." + cid, offsetCorrection); } });
      const loadBtn = h("button", { class: "btn sm", onclick: loadAudio }, "Загрузить запись");
      async function loadAudio() {
        loadBtn.disabled = true;
        try {
          const a = await api("/dialogs/" + encodeURIComponent(cid) + "/audio");
          audio = h("audio", { controls: true, preload: "metadata", src: a.src });
          holder.replaceChildren(audio);
          loadBtn.remove();
          audioBar.appendChild(h("div", { class: "row small muted", style: "margin-top:6px" },
            "Клик по доказательству или ▶ у реплики перематывает запись. Сдвиг, с:", offsetInput,
            h("a", { href: a.page, target: "_blank", rel: "noopener" }, "Открыть плеер организаторов")));
        } catch (e) { fail(e); loadBtn.disabled = false; }
      }
      audioBar = h("div", { class: "audio-bar" }, h("div", { class: "row" }, holder, h("span", { class: "spacer" }), loadBtn));
    }

    // --- evaluation panel ---
    const right = h("div", { class: "stack" });
    function renderEval(view) {
      if (!view) {
        right.replaceChildren(h("div", { class: "panel empty" }, "Диалог ещё не оценён по выбранному чек-листу.",
          h("div", { style: "margin-top:12px" }, h("button", { class: "btn primary", onclick: reevaluate }, "Оценить"))));
        return;
      }
      const cl = view.checklist;
      const critById = Object.fromEntries(cl.criteria.map((c) => [c.id, c]));
      const scoreById = Object.fromEntries(((view.final_score || view.score || {}).criteria || []).map((c) => [c.criterion_id, c]));
      const sections = Object.fromEntries((cl.sections || []).map((s) => [s.id, s.title]));
      const fs = view.final_score, os = view.score;
      const gross = (fs || os || {}).gross_violation;

      const scorePanel = h("div", { class: "panel" },
        h("div", { class: "scorebox" },
          h("div", null,
            h("div", { class: "small muted" }, view.changed ? "Итог после решения проверяющего" : "Итоговая оценка"),
            h("div", { class: "big" }, fs ? fmt(fs.total) : "—", h("small", null, " / 100"),
              view.changed && os ? h("span", { class: "was", style: "margin-left:10px", title: "Исходная оценка AI" }, fmt(os.total)) : null)),
          gross ? h("span", { class: "badge b-gross", style: "font-size:13px;padding:5px 10px" }, "Грубая ошибка") :
            h("span", { class: "badge b-yes" }, "Грубых ошибок нет"),
          h("span", { class: "spacer" }),
          h("dl", { class: "kv" },
            h("dt", null, "Чек-лист"), h("dd", null, `${view.checklist_name || cl.name} · v${view.checklist_version}`),
            h("dt", null, "Модель"), h("dd", null, (view.models || []).join(", ") || "—"),
            h("dt", null, "Прогонов"), h("dd", null, view.samples || "—"),
            h("dt", null, "Оценено"), h("dd", null, new Date(view.created_at).toLocaleString("ru-RU")),
            h("dt", null, "Токены / время"), h("dd", null, `${fmt(view.total_tokens, 0)} · ${fmt(view.elapsed_s, 1)} с` +
              (view.cached_criteria && view.cached_criteria.length ? ` · из кэша: ${view.cached_criteria.length}` : "")))),
        view.final_score_error ? h("div", { class: "decision changed" }, view.final_score_error) : null,
        fs && fs.sections.length ? h("div", { class: "sections" }, fs.sections.map((s) => h("div", { class: "section" },
          h("div", { class: "small muted" }, s.title),
          h("div", null, h("b", null, s.points == null ? "N/A" : fmt(s.points)), s.max_points != null ? h("span", { class: "muted" }, ` / ${fmt(s.max_points)}`) : null),
          h("div", { class: "bar" }, h("div", { style: `width:${s.percent || 0}%` }))))) : null,
        h("div", { class: "row", style: "margin-top:10px" },
          h("span", { class: "small muted" }, "Повторная оценка использует выбранный чек-лист и кэш по неизменённым критериям."),
          h("span", { class: "spacer" }),
          checklistSelect(() => route()),
          h("button", { class: "btn", onclick: reevaluate }, "Оценить заново")),
        h("div", { id: "job-box", style: "margin-top:8px" }));

      const cards = view.criteria.map((c) => criterionCard(c, critById[c.criterion_id] || {}, scoreById[c.criterion_id], sections, view));
      right.replaceChildren(scorePanel, ...cards);
      const focus = params.get("c");
      if (focus) {
        const el = document.getElementById("crit-" + focus);
        if (el) { el.classList.add("focus"); setTimeout(() => el.scrollIntoView({ block: "center" }), 100); }
      }
    }

    function criterionCard(c, crit, sc, sections, view) {
      const voice = isCall && crit.channel_overrides && crit.channel_overrides["voice call"];
      const title = (voice && voice.title) || crit.title || c.criterion_id;
      const changed = c.decision && c.decision.decision === "change";
      const openAppeal = (c.appeals || []).find((a) => a.status === "open");
      const card = h("div", { class: "crit", id: "crit-" + c.criterion_id });
      const pts = sc && sc.points != null ? h("span", { class: "pts" }, fmt(sc.points), h("small", null, ` / ${fmt(sc.effective_weight)}`)) :
        h("span", { class: "pts" }, h("small", null, crit.type === "flag" ? "без баллов" : (c.final_answer === "na" ? "вес перераспределён" : "")));
      card.append(
        h("div", { class: "crit-head" },
          h("span", { class: "crit-title" }, title, " ", h("span", { class: "crit-id" }, c.criterion_id + (crit.section ? " · " + (sections[crit.section] || "") : ""))),
          c.status !== "ok" ? h("span", { class: "badge b-warn" }, "нужна проверка") : null,
          c.confidence != null && c.confidence < 1 && c.status === "ok" ? h("span", { class: "badge b-warn", title: "Доля прогонов, совпавших с итогом" }, `уверенность ${Math.round(c.confidence * 100)}%`) : null,
          answerBadge(c.final_answer, c.final_value, crit.type), pts),
        h("div", { class: "small muted" }, `${TYPE_LABEL[crit.type] || ""} · вес ${gnum(crit.weight)}` + (crit.na_allowed ? " · N/A разрешён" : "")),
        h("div", { class: "expl" }, changed ? h("span", { class: "muted" }, "Обоснование AI: ") : (c.final_answer === "na" ? h("b", null, "N/A: ") : null), c.explanation),
        c.evidence && c.evidence.length ? h("div", { class: "chips" }, h("span", { class: "small muted" }, "Основание:"),
          c.evidence.map((e) => h("button", { class: "chip", title: isCall ? "Перейти и воспроизвести" : "Перейти к реплике",
            onmouseenter: () => highlight(e), onclick: () => jump(e.from_id, e.to_id, true) }, evLabel(e)))) : null);

      if (c.decision) {
        card.append(h("div", { class: "decision" + (changed ? " changed" : "") },
          h("b", null, changed ? "Оценка изменена проверяющим: " : "Оценка подтверждена проверяющим. "),
          changed ? h("span", null, "AI: ", answerBadge(c.answer, c.value, crit.type), " → ", answerBadge(c.final_answer, c.final_value, crit.type), ". ") : null,
          "Причина: ", c.decision.reason,
          c.decision.reviewer ? h("span", { class: "muted" }, ` (${c.decision.reviewer})`) : null));
      }
      for (const a of c.appeals || []) {
        card.append(h("div", { class: "appeal-box" },
          h("div", { class: "row" }, h("b", null, "Апелляция"), h("span", { class: "badge " + (a.status === "open" ? "b-score" : "b-chan") }, a.status === "open" ? "на рассмотрении" : "рассмотрена"),
            h("span", { class: "small muted" }, (a.author ? a.author + " · " : "") + new Date(a.created_at).toLocaleString("ru-RU"))),
          h("div", { style: "margin-top:4px" }, a.comment),
          a.status === "open" && state.role === "reviewer" ? decisionForm(a, crit, c, (v) => { renderEval(v); refreshAppealCount(); }) : null,
          a.status === "open" && state.role !== "reviewer" ? h("div", { class: "small muted", style: "margin-top:4px" }, "Переключитесь на роль «Проверяющий», чтобы принять решение.") : null));
      }
      if (!openAppeal && c.status === "ok" && state.role === "operator") {
        card.append(h("div", { class: "row", style: "margin-top:8px" }, h("span", { class: "spacer" }),
          h("button", { class: "btn sm", onclick: (e) => { e.target.replaceWith(appealForm(c, view)); } }, "Оспорить оценку")));
      }
      if (c.samples && c.samples.length > 1) {
        card.append(h("details", { class: "samples" }, h("summary", null, `Прогоны модели (${c.samples.length})`),
          c.samples.map((s, i) => h("div", { class: "s" }, h("b", null, `#${i + 1} `), answerBadge(s.answer, s.value, crit.type), " ", s.explanation))));
      }
      return card;
    }

    function highlight(e) {
      document.querySelectorAll(".msg.ev").forEach((m) => m.classList.remove("ev"));
      const a = +e.from_id.slice(1), b = +e.to_id.slice(1);
      for (let i = a; i <= b; i++) { const el = msgEls["m" + i]; if (el) el.classList.add("ev"); }
    }

    function appealForm(c, view) {
      const ta = h("textarea", { rows: 3, placeholder: "Почему вы не согласны с оценкой?" });
      const author = h("input", { placeholder: "Ваше имя (необязательно)", value: store.get("author", "") });
      return h("div", { class: "appeal-box", style: "width:100%" },
        h("b", null, "Апелляция по критерию"), ta,
        h("div", { class: "row", style: "margin-top:6px" }, author, h("span", { class: "spacer" }),
          h("button", { class: "btn primary sm", onclick: async () => {
            try {
              store.set("author", author.value);
              const r = await api(`/evaluations/${view.id}/appeals`, { method: "POST", body: { criterion_id: c.criterion_id, comment: ta.value, author: author.value || null } });
              toast("Апелляция отправлена проверяющему");
              renderEval(r.evaluation); refreshAppealCount();
            } catch (e) { fail(e); }
          } }, "Отправить")));
    }

    async function reevaluate() {
      try {
        await runJob([cid]);
        const fresh = await api("/dialogs/" + encodeURIComponent(cid) + "?" + new URLSearchParams({ checklist_id: state.checklistId }));
        renderEval(fresh.evaluation);
      } catch (e) { fail(e); }
    }

    app.replaceChildren(
      h("div", { class: "card-head" },
        h("a", { href: "#/dialogs", class: "btn ghost" }, "← К списку"),
        h("h1", null, d.conversation_id),
        h("span", { class: "badge b-chan" }, CHANNEL[d.channel] || d.channel),
        d.language ? h("span", { class: "badge b-chan" }, d.language) : null,
        h("span", { class: "small muted" }, `${d.messages.length} реплик` + (facts.duration_s ? ` · ${fmtT(facts.duration_s)}` : ""))),
      h("div", { class: "split" },
        h("div", { class: "panel sticky" }, audioBar, h("h2", null, "Транскрипт"), translationSwitch, transcript),
        right));
    renderEval(ev);
  };

  function decisionForm(appeal, crit, c, done) {
    const choice = { v: "confirm" };
    const options = [];
    if (crit.type === "scale") (crit.scale || []).slice().sort((a, b) => b.value - a.value).forEach((s) => options.push(["score:" + s.value, `${gnum(s.value)} — ${s.description}`]));
    else if (crit.type === "flag") options.push(["yes", "Выявлено"], ["no", "Нет"]);
    else options.push(["yes", "Да"], ["no", "Нет"]);
    if (crit.na_allowed) options.push(["na", "N/A"]);
    const sel = h("select", { disabled: true }, options.map(([v, l]) => h("option", { value: v }, l)));
    const reason = h("textarea", { rows: 2, placeholder: "Причина решения (обязательно)" });
    const reviewer = h("input", { placeholder: "Проверяющий", value: store.get("reviewer", "") });
    const radio = (v, label) => h("label", { class: "check" }, h("input", { type: "radio", name: "dec-" + appeal.id, checked: v === "confirm",
      onchange: () => { choice.v = v; sel.disabled = v !== "change"; } }), label);
    return h("div", { style: "margin-top:8px" },
      h("div", { class: "row" }, h("b", null, "Решение:"), radio("confirm", "Подтвердить оценку AI"), radio("change", "Изменить на"), sel),
      h("div", { style: "margin-top:6px" }, reason),
      h("div", { class: "row", style: "margin-top:6px" }, reviewer, h("span", { class: "spacer" }),
        h("button", { class: "btn primary sm", onclick: async () => {
          const body = { decision: choice.v, reason: reason.value, reviewer: reviewer.value || null };
          if (choice.v === "change") {
            const v = sel.value;
            if (v.startsWith("score:")) { body.answer = "score"; body.value = +v.slice(6); } else body.answer = v;
          }
          try {
            store.set("reviewer", reviewer.value);
            const view = await api(`/appeals/${appeal.id}/decision`, { method: "POST", body });
            toast("Решение сохранено, итог пересчитан");
            done(view);
          } catch (e) { fail(e); }
        } }, "Сохранить решение")));
  }

  // ---------- view: appeals ----------
  views.appeals = async function (app, parts, params) {
    const status = params.get("status") || "open";
    const list = await api("/appeals?status=" + status);
    const cache = {};
    app.replaceChildren(
      h("div", { class: "row", style: "margin-bottom:12px" },
        h("div", null, h("h1", null, "Апелляции"), h("div", { class: "sub" }, state.role === "reviewer" ? "Рассмотрите спорные оценки" : "Ваши обращения по оценкам")),
        h("span", { class: "spacer" }),
        h("div", { class: "tabs", style: "margin:0;border:0" },
          h("button", { class: status === "open" ? "on" : "", onclick: () => (location.hash = "#/appeals") }, "На рассмотрении"),
          h("button", { class: status === "all" ? "on" : "", onclick: () => (location.hash = "#/appeals?status=all") }, "Все"))),
      list.length ? h("div", { class: "stack" }, list.map((a) => appealCard(a))) : h("div", { class: "panel empty" }, "Апелляций нет."));

    function appealCard(a) {
      const frag = h("div", { class: "transcript", style: "margin-top:8px" });
      const card = h("div", { class: "panel" },
        h("div", { class: "row" },
          h("b", null, a.conversation_id), h("span", { class: "muted" }, "·"), h("span", null, a.criterion_id),
          h("span", { class: "badge " + (a.status === "open" ? "b-score" : "b-chan") }, a.status === "open" ? "на рассмотрении" : "рассмотрена"),
          h("span", { class: "spacer" }),
          h("a", { class: "btn sm", href: `#/dialog/${encodeURIComponent(a.conversation_id)}?c=${a.criterion_id}` }, "Открыть карточку")),
        h("div", { class: "grid g2", style: "margin-top:8px" },
          h("div", null, h("div", { class: "small muted" }, "Оценка AI"),
            h("div", null, answerBadge(a.ai_answer, a.ai_value), " ", a.ai_explanation)),
          h("div", null, h("div", { class: "small muted" }, "Комментарий" + (a.author ? " · " + a.author : "")), h("div", null, a.comment))),
        frag);
      loadFragment(a, frag, card);
      return card;
    }

    async function loadFragment(a, frag, card) {
      try {
        if (!cache[a.conversation_id]) cache[a.conversation_id] = await api("/dialogs/" + encodeURIComponent(a.conversation_id));
        const data = cache[a.conversation_id];
        const msgs = data.dialog.messages;
        const ids = new Set();
        for (const e of a.evidence || []) for (let i = +e.from_id.slice(1); i <= +e.to_id.slice(1); i++) ids.add(i);
        const around = new Set();
        ids.forEach((i) => { for (let k = i - 1; k <= i + 1; k++) if (k >= 1 && k <= msgs.length) around.add(k); });
        const show = [...around].sort((x, y) => x - y).slice(0, 12);
        frag.replaceChildren(h("div", { class: "small muted" }, "Фрагмент диалога (основание выделено)"),
          ...show.map((i) => { const m = msgs[i - 1]; return h("div", { class: `msg ${m.speaker}${ids.has(i) ? " ev" : ""}` },
            h("div", { class: "meta" }, h("span", { class: "mid" }, m.id)),
          h("div", null, h("div", { class: "who" }, speakerName(m)), h("div", { class: "text" }, m.text))); }));
        if (a.status === "open" && state.role === "reviewer" && data.evaluation) {
          const crit = data.evaluation.checklist.criteria.find((c) => c.id === a.criterion_id) || {};
          card.appendChild(decisionForm(a, crit, null, () => { route(); }));
        } else if (a.status === "open") {
          card.appendChild(h("div", { class: "small muted", style: "margin-top:8px" }, "Решение принимает проверяющий — переключите роль вверху."));
        }
      } catch (e) { frag.replaceChildren(h("div", { class: "small muted" }, "Не удалось загрузить фрагмент: " + e.message)); }
    }
  };

  // ---------- view: checklist editor ----------
  views.checklists = async function (app, parts, params) {
    const id = params.get("id") || state.checklistId;
    const loaded = await api("/checklists/" + encodeURIComponent(id) + (params.get("v") ? "?version=" + params.get("v") : ""));
    const versions = loaded.versions;
    delete loaded.versions;
    const cl = JSON.parse(JSON.stringify(loaded));
    let tab = "form";
    const body = h("div");
    const status = h("div", { class: "row" });

    function weightsInfo() {
      const active = cl.criteria.filter((c) => c.enabled !== false && c.type !== "flag");
      const sum = active.reduce((s, c) => s + (+c.weight || 0), 0);
      status.replaceChildren(h("span", { class: "badge " + (sum === 100 ? "b-yes" : "b-warn") }, `Сумма весов: ${gnum(sum)}`),
        h("span", { class: "small muted" }, sum !== 100 ? "Итог всё равно нормируется к 100, но исходные веса обычно дают 100." : ""));
    }

    function render() {
      weightsInfo();
      if (tab === "json") {
        const ta = h("textarea", { rows: 30, class: "mono", value: JSON.stringify(cl, null, 2) });
        body.replaceChildren(h("div", { class: "panel" }, ta, h("div", { class: "row", style: "margin-top:8px" }, h("span", { class: "spacer" }),
          h("button", { class: "btn", onclick: () => { try { Object.assign(cl, JSON.parse(ta.value)); tab = "form"; renderTabs(); render(); } catch (e) { fail(e); } } }, "Применить JSON"))));
        return;
      }
      body.replaceChildren(
        h("div", { class: "panel stack" },
          h("div", { class: "grid g2" },
            field("Название", input(cl, "name")),
            field("ID чек-листа (для нового чек-листа задайте свой)", input(cl, "id"))),
          field("Описание", textarea(cl, "description", 2)),
          field("Общие правила оценки (по одному на строку)", h("textarea", { rows: 6, value: (cl.general_rules || []).join("\n"),
            oninput: (e) => (cl.general_rules = e.target.value.split("\n").map((s) => s.trim()).filter(Boolean)) })),
          field("Справочник компании / продуктов (подставляется в промпт для проверки корректности консультации)", textarea(cl, "company_context", 4)),
          h("div", null, h("div", { class: "small muted" }, "Секции"),
            h("div", { class: "stack" }, (cl.sections || []).map((s, i) => h("div", { class: "row" },
              h("input", { value: s.id, style: "width:120px", oninput: (e) => (s.id = e.target.value) }),
              h("input", { value: s.title, style: "flex:1", oninput: (e) => (s.title = e.target.value) }),
              h("button", { class: "btn sm danger", onclick: () => { cl.sections.splice(i, 1); render(); } }, "✕")))),
            h("button", { class: "btn sm", style: "margin-top:6px", onclick: () => { (cl.sections = cl.sections || []).push({ id: "s" + (cl.sections.length + 1), title: "Новая секция" }); render(); } }, "+ секция"))),
        h("div", { class: "row", style: "margin:14px 0 8px" }, h("h2", { style: "margin:0" }, "Критерии"), h("span", { class: "spacer" }),
          h("button", { class: "btn", onclick: addCriterion }, "+ Добавить критерий")),
        h("div", { class: "stack" }, cl.criteria.map((c, i) => criterionEditor(c, i))));
    }

    const field = (label, ctrl) => h("label", { class: "field" }, label, ctrl);
    const input = (obj, key, attrs) => h("input", Object.assign({ value: obj[key] == null ? "" : obj[key], oninput: (e) => (obj[key] = e.target.value) }, attrs || {}));
    const textarea = (obj, key, rows) => h("textarea", { rows, value: obj[key] || "", oninput: (e) => (obj[key] = e.target.value || null) });

    function criterionEditor(c, i) {
      const sectionSel = h("select", { onchange: (e) => (c.section = e.target.value || null) },
        h("option", { value: "" }, "—"), (cl.sections || []).map((s) => h("option", { value: s.id, selected: s.id === c.section }, s.title)));
      const typeSel = h("select", { onchange: (e) => {
        c.type = e.target.value;
        if (c.type === "scale" && !(c.scale && c.scale.length)) c.scale = [{ value: 10, description: "Полностью" }, { value: 5, description: "Частично" }, { value: 0, description: "Нет" }];
        if (c.type !== "scale") delete c.scale;
        if (c.type === "flag") { c.weight = 0; c.na_allowed = false; }
        render();
      } }, Object.entries(TYPE_LABEL).map(([v, l]) => h("option", { value: v, selected: v === c.type }, l)));
      const voice = (c.channel_overrides && c.channel_overrides["voice call"]) || null;
      const el = h("div", { class: "editor-crit" + (c.enabled === false ? " disabled" : "") },
        h("div", { class: "row", style: "margin-bottom:8px" },
          h("b", null, `${i + 1}. ${c.title || "Без названия"}`), h("span", { class: "crit-id" }, c.id), h("span", { class: "spacer" }),
          h("label", { class: "check small" }, h("input", { type: "checkbox", checked: c.enabled !== false, onchange: (e) => { c.enabled = e.target.checked; render(); } }), "включён"),
          h("button", { class: "btn sm", disabled: i === 0, onclick: () => { cl.criteria.splice(i - 1, 0, cl.criteria.splice(i, 1)[0]); render(); } }, "↑"),
          h("button", { class: "btn sm", disabled: i === cl.criteria.length - 1, onclick: () => { cl.criteria.splice(i + 1, 0, cl.criteria.splice(i, 1)[0]); render(); } }, "↓"),
          h("button", { class: "btn sm danger", onclick: () => { if (confirm(`Удалить критерий «${c.title}»?`)) { cl.criteria.splice(i, 1); render(); } } }, "Удалить")),
        h("div", { class: "grid g5" },
          field("ID", input(c, "id")),
          field("Название", input(c, "title")),
          field("Секция", sectionSel),
          field("Тип ответа", typeSel),
          field("Вес", h("input", { type: "number", min: 0, value: c.weight, disabled: c.type === "flag", oninput: (e) => { c.weight = +e.target.value; weightsInfo(); } }))),
        h("div", { style: "margin-top:8px" }, field(c.type === "flag" ? "Что считается нарушением («Да»)" : "Что оценивается / когда ставится «Да»", textarea(c, "description", 2))),
        c.type !== "flag" ? h("div", { class: "row", style: "margin-top:8px;align-items:flex-start" },
          h("label", { class: "check", style: "margin-top:6px" }, h("input", { type: "checkbox", checked: !!c.na_allowed, onchange: (e) => { c.na_allowed = e.target.checked; render(); } }), "N/A разрешён"),
          c.na_allowed ? h("div", { style: "flex:1" }, field("Условие N/A", textarea(c, "na_condition", 1))) : null) : null,
        c.type === "scale" ? scaleEditor(c) : null,
        h("div", { style: "margin-top:8px" }, field("Уточнения для оценщика (необязательно)", textarea(c, "guidance", 2))),
        h("details", { style: "margin-top:8px", open: !!voice },
          h("summary", { class: "small", style: "cursor:pointer" }, "Формулировка для звонков" + (voice ? " (задана)" : " (как для чатов)")),
          voiceEditor(c)));
      return el;
    }

    function scaleEditor(c) {
      return h("div", { style: "margin-top:8px" }, h("div", { class: "small muted" }, "Ступени шкалы (верхняя ступень даёт полный вес)"),
        (c.scale || []).map((s, j) => h("div", { class: "scale-row" },
          h("input", { type: "number", value: s.value, oninput: (e) => (s.value = +e.target.value) }),
          h("input", { value: s.description, oninput: (e) => (s.description = e.target.value) }),
          h("button", { class: "btn sm danger", onclick: () => { c.scale.splice(j, 1); render(); } }, "✕"))),
        h("button", { class: "btn sm", style: "margin-top:6px", onclick: () => { c.scale.push({ value: 0, description: "" }); render(); } }, "+ ступень"));
    }

    function voiceEditor(c) {
      c.channel_overrides = c.channel_overrides || {};
      const ov = c.channel_overrides["voice call"] || {};
      const set = (k) => (e) => {
        const v = e.target.value.trim();
        if (v) ov[k] = e.target.value; else delete ov[k];
        if (Object.keys(ov).length) c.channel_overrides["voice call"] = ov; else delete c.channel_overrides["voice call"];
      };
      return h("div", { class: "grid", style: "margin-top:6px" },
        h("div", { class: "small muted" }, "Пустое поле — используется формулировка для чатов. Тип, вес и ступени шкалы общие для обоих каналов."),
        field("Название для звонков", h("input", { value: ov.title || "", oninput: set("title") })),
        field("Описание для звонков", h("textarea", { rows: 2, value: ov.description || "", oninput: set("description") })),
        c.na_allowed ? field("Условие N/A для звонков", h("textarea", { rows: 1, value: ov.na_condition || "", oninput: set("na_condition") })) : null,
        field("Уточнения для звонков", h("textarea", { rows: 2, value: ov.guidance || "", oninput: set("guidance") })),
        ov.scale ? h("div", { class: "small muted" }, "Описания ступеней для звонков заданы в JSON (вкладка «JSON»).") : null);
    }

    function addCriterion() {
      let n = cl.criteria.length + 1;
      while (cl.criteria.some((c) => c.id === "c" + String(n).padStart(2, "0"))) n++;
      cl.criteria.push({ id: "c" + String(n).padStart(2, "0"), section: (cl.sections && cl.sections[0] && cl.sections[0].id) || null,
        title: "Новый критерий", type: "binary", weight: 10, description: "Да — …", na_allowed: false, enabled: true, channel_overrides: {} });
      render();
      setTimeout(() => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }), 50);
    }

    function clean(obj) {
      const out = JSON.parse(JSON.stringify(obj));
      out.criteria.forEach((c) => {
        if (!c.na_allowed) c.na_condition = c.na_condition || null;
        ["guidance", "na_condition"].forEach((k) => { if (c[k] === "") c[k] = null; });
      });
      return out;
    }

    async function validate() {
      const r = await api("/checklists/validate", { method: "POST", body: clean(cl) });
      if (r.valid) toast("Чек-лист корректен"); else toast("Ошибки: " + r.errors.join("; "), true);
      return r.valid;
    }

    async function save() {
      try {
        if (!(await validate())) return;
        const saved = await api("/checklists", { method: "POST", body: clean(cl) });
        state.checklistId = saved.id; store.set("checklist", saved.id);
        await loadChecklists();
        toast(`Сохранено: ${saved.name} · версия ${saved.version}. Новые оценки будут по этой версии.`);
        location.hash = "#/checklists?id=" + encodeURIComponent(saved.id);
        route();
      } catch (e) { fail(e); }
    }

    const tabs = h("div", { class: "tabs" });
    function renderTabs() {
      tabs.replaceChildren(
        h("button", { class: tab === "form" ? "on" : "", onclick: () => { tab = "form"; renderTabs(); render(); } }, "Редактор"),
        h("button", { class: tab === "json" ? "on" : "", onclick: () => { tab = "json"; renderTabs(); render(); } }, "JSON"));
    }
    renderTabs();

    app.replaceChildren(
      h("div", { class: "row", style: "margin-bottom:12px" },
        h("div", null, h("h1", null, "Чек-листы"), h("div", { class: "sub" }, "Каждое сохранение создаёт новую версию; старые оценки остаются привязаны к своей версии")),
        h("span", { class: "spacer" }),
        h("select", { onchange: (e) => (location.hash = "#/checklists?id=" + encodeURIComponent(e.target.value)) },
          state.checklists.map((c) => h("option", { value: c.id, selected: c.id === id }, c.name))),
        h("select", { onchange: (e) => (location.hash = `#/checklists?id=${encodeURIComponent(id)}&v=${e.target.value}`) },
          versions.map((v) => h("option", { value: v.version, selected: v.version === loaded.version }, `v${v.version} · ${new Date(v.created_at).toLocaleString("ru-RU")}`))),
        h("button", { class: "btn", onclick: validate }, "Проверить"),
        h("button", { class: "btn primary", onclick: save }, "Сохранить новую версию")),
      h("div", { style: "margin-bottom:10px" }, status),
      tabs, body);
    render();
  };

  // ---------- view: quality ----------
  views.quality = async function (app) {
    const q = await api("/quality");
    const cl = await api("/checklists/" + encodeURIComponent(state.checklistId));
    const ids = cl.criteria.map((c) => c.id);
    Object.keys(q.mean_confidence).forEach((k) => { if (!ids.includes(k)) ids.push(k); });
    const title = Object.fromEntries(cl.criteria.map((c) => [c.id, c.title]));
    app.replaceChildren(
      h("div", { style: "margin-bottom:12px" }, h("h1", null, "Качество AI"),
        h("div", { class: "sub" }, "Решения проверяющих по апелляциям служат разметкой: видно, где AI ошибается и какие критерии нужно уточнить")),
      h("div", { class: "panel" },
        h("div", { class: "row", style: "margin-bottom:8px" }, h("span", { class: "badge b-acc" }, `Оценено диалогов: ${q.evaluated_dialogs}`)),
        h("table", { class: "table" },
          h("thead", null, h("tr", null, h("th", null, "Критерий"), h("th", null, "Распределение ответов AI"), h("th", { class: "num" }, "Средняя уверенность"),
            h("th", { class: "num" }, "Проверено"), h("th", { class: "num" }, "Изменено"), h("th", { class: "num" }, "Согласие с проверяющим"))),
          h("tbody", null, ids.map((id) => {
            const ra = q.reviewer_agreement[id] || {};
            const dist = q.answer_distribution[id] || {};
            return h("tr", null,
              h("td", null, h("b", null, id), " ", title[id] || ""),
              h("td", null, h("div", { class: "chips" }, Object.entries(dist).sort().map(([k, n]) => h("span", { class: "badge b-chan" }, `${LABEL[k] || k}: ${n}`)))),
              h("td", { class: "num" }, q.mean_confidence[id] != null ? Math.round(q.mean_confidence[id] * 100) + "%" : "—"),
              h("td", { class: "num" }, ra.reviewed || 0),
              h("td", { class: "num" }, ra.changed || 0),
              h("td", { class: "num total" }, ra.agreement != null ? Math.round(ra.agreement * 100) + "%" : "—"));
          })))));
  };

  route();
})();
