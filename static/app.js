const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const app = {
  state: null,
  comments: [],
  filters: { page_id: "", status: "all", search: "" },
  poller: null,
};

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("json") ? await response.json() : { error: await response.text() };
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function toast(title, message = "", kind = "success") {
  const item = document.createElement("div");
  item.className = `toast ${kind}`;
  item.innerHTML = `<b>${escapeHtml(title)}</b>${escapeHtml(message)}`;
  $("#toastStack").append(item);
  setTimeout(() => item.remove(), 4600);
}

function loading(show, text = "Зв’язуємось із Meta...") {
  $("#loadingText").textContent = text;
  $("#loadingOverlay").classList.toggle("show", show);
}

function initials(name = "Page") {
  return name.split(/\s+/).slice(0, 2).map(word => word[0]).join("").toUpperCase();
}

function formatNumber(value) {
  return new Intl.NumberFormat("uk-UA", { notation: value > 9999 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value || 0);
}

function formatDate(value) {
  if (!value) return { day: "—", time: "" };
  const date = new Date(value);
  return {
    day: new Intl.DateTimeFormat("uk-UA", { day: "2-digit", month: "short" }).format(date),
    time: new Intl.DateTimeFormat("uk-UA", { hour: "2-digit", minute: "2-digit" }).format(date),
  };
}

function renderState() {
  const state = app.state;
  if (!state) return;
  $("#apiVersion").textContent = state.graph_version;
  $("#commentsNavCount").textContent = state.stats.total;
  $("#autoHideToggle").checked = state.auto_hide;
  $("#autoHideState").textContent = state.auto_hide ? "Увімкнено" : "Вимкнено";
  $("#autoHideState").style.color = state.auto_hide ? "var(--lime)" : "";
  $("#skipOwnToggle").checked = state.skip_page_comments;
  $("#connectedStat").textContent = state.pages.filter(page => page.subscribed).length;
  $("#hiddenStat").textContent = formatNumber(state.stats.hidden);
  $("#todayStat").textContent = formatNumber(state.stats.today);
  $("#errorsStat").textContent = formatNumber(state.stats.errors);
  $("#pagesCount").textContent = state.pages.length;
  renderPages();
  renderPageFilter();
}

function renderPages() {
  const grid = $("#pagesGrid");
  if (!app.state.pages.length) {
    grid.innerHTML = `<div class="empty-pages"><span>◉</span><h3>Фанпейджів ще немає</h3><p>Натисни «Синхронізувати з Meta», щоб підтягнути доступні сторінки.</p></div>`;
    return;
  }
  grid.innerHTML = app.state.pages.map(page => `
    <article class="page-card ${page.subscribed ? "on" : ""}">
      <div class="page-top">
        ${page.picture_url
          ? `<img class="page-avatar" src="${escapeHtml(page.picture_url)}" alt="">`
          : `<span class="page-avatar">${escapeHtml(initials(page.name))}</span>`}
        <div class="page-info"><b>${escapeHtml(page.name)}</b><small>${escapeHtml(page.category || `ID ${page.id}`)}</small></div>
        <label class="switch" title="${page.subscribed ? "Відключити webhook" : "Підключити webhook"}">
          <input class="page-toggle" type="checkbox" data-page-id="${escapeHtml(page.id)}" ${page.subscribed ? "checked" : ""}>
          <span class="slider"><i></i></span>
        </label>
      </div>
      <div class="page-meta">
        <span><b>${formatNumber(page.followers_count)}</b> читачів</span>
        <span><b>${formatNumber(page.comment_count)}</b> коментарів</span>
        <span class="page-status">● ${page.subscribed ? "WEBHOOK ON" : "OFFLINE"}</span>
      </div>
      ${page.last_error ? `<p class="page-error">${escapeHtml(page.last_error)}</p>` : ""}
    </article>`).join("");
  $$(".page-toggle", grid).forEach(toggle => toggle.addEventListener("change", changePageSubscription));
}

function renderPageFilter() {
  const select = $("#pageFilter");
  const selected = select.value;
  select.innerHTML = `<option value="">Усі сторінки</option>` + app.state.pages.map(page =>
    `<option value="${escapeHtml(page.id)}">${escapeHtml(page.name)}</option>`).join("");
  select.value = selected;
}

function statusPresentation(comment) {
  if (comment.status === "error") return ["Помилка", "error"];
  if (comment.status === "skipped") return ["Своя відповідь", "skipped"];
  if (["queued", "hiding"].includes(comment.status)) return ["Обробляється", "hidden"];
  if (comment.is_hidden) return ["Приховано", "hidden"];
  return ["Видимий", "visible"];
}

function renderComments() {
  const body = $("#commentsTable");
  const empty = $("#commentsEmpty");
  empty.style.display = app.comments.length ? "none" : "block";
  body.innerHTML = app.comments.map(comment => {
    const date = formatDate(comment.received_at);
    const [label, kind] = statusPresentation(comment);
    const canToggle = !["queued", "hiding", "unhiding"].includes(comment.status);
    return `<tr>
      <td class="time-cell">${date.day}<small>${date.time}</small></td>
      <td><div class="table-page">
        ${comment.picture_url ? `<img src="${escapeHtml(comment.picture_url)}" alt="">` : `<span class="avatar-fallback">${escapeHtml(initials(comment.page_name))}</span>`}
        <span>${escapeHtml(comment.page_name)}</span>
      </div></td>
      <td><div class="comment-copy"><b>${escapeHtml(comment.author_name || "Невідомий автор")}</b><p title="${escapeHtml(comment.message)}">${escapeHtml(comment.message || "Без тексту")}</p>${comment.error ? `<p class="comment-error">${escapeHtml(comment.error)}</p>` : ""}</div></td>
      <td><span class="status-badge status-${kind}">${label}</span></td>
      <td>${canToggle ? `<button class="row-action" data-comment-id="${escapeHtml(comment.id)}" data-hidden="${comment.is_hidden ? "true" : "false"}">${comment.is_hidden ? "Показати" : "Сховати"}</button>` : ""}</td>
    </tr>`;
  }).join("");
  $$(".row-action", body).forEach(button => button.addEventListener("click", changeCommentVisibility));
}

async function loadState({ silent = false } = {}) {
  try {
    app.state = await api("/api/state");
    renderState();
    if (!app.state.token_configured && !silent) toast("Токен не знайдено", "Додай SYSTEM_USER_TOKEN у .env", "error");
  } catch (error) {
    if (!silent) toast("Не вдалося завантажити панель", error.message, "error");
  }
}

async function loadComments({ silent = false } = {}) {
  const params = new URLSearchParams({ limit: "200" });
  Object.entries(app.filters).forEach(([key, value]) => value && value !== "all" && params.set(key, value));
  try {
    const result = await api(`/api/comments?${params}`);
    app.comments = result.comments;
    renderComments();
  } catch (error) {
    if (!silent) toast("Журнал недоступний", error.message, "error");
  }
}

async function syncPages() {
  loading(true, "Забираємо фанпейджі з Meta...");
  try {
    const result = await api("/api/pages/sync", { method: "POST", body: "{}" });
    app.state = result.state;
    renderState();
    toast("Синхронізація завершена", `Знайдено сторінок: ${result.count}`);
    if (result.warnings?.length) toast("Meta повернула попередження", result.warnings[0], "error");
  } catch (error) {
    toast("Meta не віддала сторінки", error.message, "error");
  } finally { loading(false); }
}

async function changePageSubscription(event) {
  const toggle = event.currentTarget;
  const enabled = toggle.checked;
  toggle.disabled = true;
  try {
    const result = await api(`/api/pages/${encodeURIComponent(toggle.dataset.pageId)}/subscription`, {
      method: "POST", body: JSON.stringify({ enabled }),
    });
    app.state = result.state;
    renderState();
    toast(enabled ? "Webhook підключено" : "Webhook відключено", enabled ? "Нові коментарі з цієї сторінки вже слухаємо." : "Події з цієї сторінки більше не надходитимуть.");
  } catch (error) {
    toggle.checked = !enabled;
    toggle.disabled = false;
    await loadState({ silent: true });
    toast("Не вдалося змінити підписку", error.message, "error");
  }
}

async function saveSetting(key, value) {
  try {
    const result = await api("/api/settings", { method: "POST", body: JSON.stringify({ [key]: value }) });
    app.state = result.state;
    renderState();
    toast(value ? "Monster mode активовано" : "Налаштування оновлено", key === "auto_hide" ? (value ? "Нові коментарі будуть приховуватись автоматично." : "Нові коментарі залишатимуться видимими.") : "Правило для відповідей сторінки збережено.");
  } catch (error) {
    toast("Не вдалося зберегти", error.message, "error");
    await loadState({ silent: true });
  }
}

async function changeCommentVisibility(event) {
  const button = event.currentTarget;
  const currentlyHidden = button.dataset.hidden === "true";
  button.disabled = true;
  button.textContent = "...";
  try {
    await api(`/api/comments/${encodeURIComponent(button.dataset.commentId)}/visibility`, {
      method: "POST", body: JSON.stringify({ hidden: !currentlyHidden }),
    });
    await Promise.all([loadComments({ silent: true }), loadState({ silent: true })]);
    toast(currentlyHidden ? "Коментар знову видимий" : "Коментар приховано");
  } catch (error) {
    button.disabled = false;
    button.textContent = currentlyHidden ? "Показати" : "Сховати";
    toast("Meta відхилила дію", error.message, "error");
    await loadComments({ silent: true });
  }
}

function changeView(view) {
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === view));
  $$(".view").forEach(item => item.classList.remove("active"));
  $(`#${view}View`).classList.add("active");
  $("#pageTitle").textContent = ({ pages: "Фанпейджі", comments: "Коментарі", insights: "Аналітика" })[view];
  if (view === "comments") loadComments();
}

function debounce(fn, wait = 300) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
}

function bindEvents() {
  $$(".nav-item").forEach(item => item.addEventListener("click", () => changeView(item.dataset.view)));
  $("#syncPagesButton").addEventListener("click", syncPages);
  $("#refreshButton").addEventListener("click", async () => {
    await Promise.all([loadState({ silent: true }), loadComments({ silent: true })]);
    toast("Оновлено", "Дані панелі актуальні.");
  });
  $("#autoHideToggle").addEventListener("change", event => saveSetting("auto_hide", event.target.checked));
  $("#skipOwnToggle").addEventListener("change", event => saveSetting("skip_page_comments", event.target.checked));
  $("#pageFilter").addEventListener("change", event => { app.filters.page_id = event.target.value; loadComments(); });
  $("#commentSearch").addEventListener("input", debounce(event => { app.filters.search = event.target.value; loadComments({ silent: true }); }));
  $$("#statusFilter button").forEach(button => button.addEventListener("click", () => {
    $$("#statusFilter button").forEach(item => item.classList.remove("active"));
    button.classList.add("active"); app.filters.status = button.dataset.status; loadComments();
  }));
}

async function start() {
  bindEvents();
  await Promise.all([loadState(), loadComments({ silent: true })]);
  app.poller = setInterval(async () => {
    if (!document.hidden) {
      await loadState({ silent: true });
      if ($("#commentsView").classList.contains("active")) await loadComments({ silent: true });
    }
  }, 12000);
}

start();
