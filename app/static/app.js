import { renderMarkdown } from "./markdown.js";

const byId = (id) => document.getElementById(id);
const workspace = byId("workspace");
const composer = byId("composer");
const input = byId("question");
const sendButton = byId("send");
const stopButton = byId("stop");
const turns = byId("turns");
const newButton = byId("new-chat");
const panel = byId("panel");
const panelBody = byId("panel-body");
const panelTitle = byId("panel-title");
const backButton = byId("panel-back");
const announcement = byId("announcement");
const storageKey = "proektoria.history.v1";
const dateFormat = new Intl.DateTimeFormat("ru", { dateStyle: "medium", timeStyle: "short" });

let conversations = readHistory();
let conversation = { turns: [] };
let activeRequest;
let failedTurn;
let documents;
let filter = "";
let panelView = "";
let panelRevision = 0;
let persisted = true;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function icon(name) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  const use = document.createElementNS(namespace, "use");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function button(className, label, action) {
  const control = element("button", className, label);
  control.type = "button";
  control.addEventListener("click", action);
  return control;
}

function readHistory() {
  try {
    const stored = JSON.parse(localStorage.getItem(storageKey) || "[]");
    return Array.isArray(stored) ? stored.filter((chat) =>
      typeof chat?.id === "string" && typeof chat.title === "string" && Number.isFinite(chat.updated)
      && Array.isArray(chat.turns) && chat.turns.every((turn) =>
        typeof turn?.question === "string" && typeof turn.result?.answer === "string")) : [];
  } catch {
    return [];
  }
}

function saveTurn(question, result) {
  conversation.id ??= `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  conversation.title ??= question.replace(/\s+/g, " ").slice(0, 120);
  conversation.updated = Date.now();
  conversation.turns.push({ question, result });
  conversations = [conversation, ...conversations.filter(({ id }) => id !== conversation.id)].slice(0, 20);
  try {
    localStorage.setItem(storageKey, JSON.stringify(conversations));
    persisted = true;
  } catch {
    persisted = false;
  }
}

function updateComposer() {
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
  sendButton.disabled = !input.value.trim();
  sendButton.hidden = Boolean(activeRequest);
  stopButton.hidden = !activeRequest;
  newButton.disabled = Boolean(activeRequest);
}

function showThread(visible) {
  byId("welcome").hidden = visible;
  byId("suggestions").hidden = visible;
  byId("thread").hidden = !visible;
  newButton.hidden = !visible;
  workspace.classList.toggle("in-chat", visible);
}

function openConversation(chat) {
  if (activeRequest) return;
  conversation = chat ?? { turns: [] };
  failedTurn = undefined;
  turns.replaceChildren();
  for (const turn of conversation.turns) {
    const exchange = renderQuestion(turn.question);
    exchange.reply.append(renderAnswer(turn.result));
  }
  input.value = "";
  showThread(conversation.turns.length > 0);
  updateComposer();
  if (panel.open) panel.close();
  window.scrollTo(0, conversation.turns.length ? document.documentElement.scrollHeight : 0);
  input.focus({ preventScroll: true });
}

function renderQuestion(question) {
  const turn = element("article", "turn");
  const bubble = element("p", "question", question);
  const reply = element("div", "reply");
  turn.append(bubble, reply);
  turns.append(turn);
  return { turn, reply };
}

function notice(message, isError = false) {
  const note = element("p", isError ? "notice notice-error" : "notice");
  note.append(icon("info"), element("span", "", message));
  if (isError) note.setAttribute("role", "alert");
  return note;
}

function renderAnswer(result) {
  const content = document.createDocumentFragment();
  const heading = element("h2", "reply-heading");
  const mark = element("span", "brand-mark");
  mark.append(icon("compass"));
  heading.append(mark, element("span", "", "Проектория"));
  const rich = element("div", "rich");
  const sources = Array.isArray(result.sources) ? result.sources.filter((source) =>
    typeof source?.id === "string" && /^[a-z0-9-]+$/.test(source.id)
    && typeof source.label === "string" && /^S\d+$/.test(source.label)
    && typeof source.title === "string" && typeof source.section === "string") : [];
  const citationButtons = new Map(sources.map((source) => [source.label, () => citation(source)]));
  rich.append(renderMarkdown(result.answer, citationButtons));
  content.append(heading, rich);

  if (sources.length) {
    const sourceList = element("section", "source-list");
    sourceList.append(element("h3", "", "Источники"));
    for (const source of sources) sourceList.append(sourceButton(source));
    content.append(sourceList);
  }

  const warnings = result.retrieval?.warnings;
  for (const warning of Array.isArray(warnings) ? warnings : []) {
    if (typeof warning === "string") content.append(notice(warning));
  }
  const meta = element("div", "reply-meta");
  if (result.retrieval?.mode === "hybrid") meta.append(element("span", "", "Гибридный поиск"));
  if (result.retrieval?.mode === "lexical") meta.append(element("span", "", "Лексический поиск"));
  if (result.grounded === true && sources.length) meta.append(element("span", "", "Подтверждено источниками"));
  if (result.status === "insufficient") meta.append(element("span", "", "В базе нет сведений"));
  const copy = button("text-button", "Копировать", async () => {
    try {
      await navigator.clipboard.writeText(result.answer);
      copy.replaceChildren(icon("check"), document.createTextNode("Скопировано"));
      announcement.textContent = "Ответ скопирован";
    } catch {
      copy.textContent = "Копирование недоступно";
      announcement.textContent = "Копирование недоступно. Можно выделить текст ответа и скопировать его вручную.";
    }
  });
  copy.prepend(icon("copy"));
  meta.append(copy);
  content.append(meta);
  return content;
}

function citation(source) {
  const control = button("citation", source.label?.slice(1), () => openSource(source));
  control.setAttribute("aria-label", `Источник ${source.label?.slice(1)}. ${source.title}. ${source.section}`);
  control.title = source.section || source.title;
  return control;
}

function sourceButton(source, fromLibrary = false) {
  const control = button("source-button", "", () => openSource(source, fromLibrary));
  control.setAttribute("aria-label", fromLibrary ? source.section : [source.title, source.section].filter(Boolean).join(". "));
  control.append(source.label ? element("span", "source-number", source.label.slice(1)) : icon("document"));
  const name = element("span", "source-name");
  if (fromLibrary) {
    control.dataset.sourceId = source.id;
    name.append(element("strong", "", source.section));
  } else {
    name.append(element("strong", "", source.title));
    if (source.section) name.append(element("span", "", ` · ${source.section}`));
  }
  control.append(name, icon("insert"));
  return control;
}

async function responseError(response) {
  if (response.status === 429) {
    const retryAfter = response.headers.get("Retry-After");
    const seconds = /^\d+$/.test(retryAfter ?? "") ? Number(retryAfter) : Math.ceil((Date.parse(retryAfter) - Date.now()) / 1000);
    let advice = "Попробуйте чуть позже.";
    if (Number.isSafeInteger(seconds) && seconds > 0) {
      const useMinutes = seconds >= 60;
      const delay = new Intl.NumberFormat("ru", { style: "unit", unit: useMinutes ? "minute" : "second", unitDisplay: "long" })
        .format(useMinutes ? Math.ceil(seconds / 60) : seconds);
      advice = `Повторите запрос через ${delay}.`;
    }
    return new Error(`Сервис получает слишком много запросов. ${advice}`);
  }
  const data = await response.json().catch(() => null);
  if (typeof data?.detail === "string") return new Error(data.detail);
  if (response.status === 413 || response.status === 422) return new Error("Проверьте длину вопроса и попробуйте снова.");
  return new Error("Сервис временно недоступен. Попробуйте ещё раз.");
}

function parseEvent(frame) {
  let event;
  const data = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  return event && data.length ? { event, payload: JSON.parse(data.join("\n")) } : null;
}

async function streamReply(message, history, signal, onStatus) {
  const encoder = new TextEncoder();
  let body = JSON.stringify({ message, history });
  while (history.length && encoder.encode(body).byteLength > 64 * 1024) {
    history = history.slice(2);
    body = JSON.stringify({ message, history });
  }
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body,
    signal,
  });
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new Error("Ответ не получен. Попробуйте ещё раз.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = done ? "" : frames.pop();
      for (const frame of frames) {
        const received = parseEvent(frame);
        if (!received) continue;
        const { event, payload } = received;
        if (event === "status" && typeof payload.message === "string") onStatus(payload.message);
        if (event === "error") throw new Error(payload.message || "Не удалось получить ответ. Попробуйте ещё раз.");
        if (event === "final" && typeof payload.answer === "string") return payload;
      }
      if (done) break;
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  throw new Error("Соединение прервалось до получения ответа. Попробуйте ещё раз.");
}

async function submitQuestion(event) {
  event.preventDefault();
  const question = input.value.trim();
  if (activeRequest || !question) return;
  failedTurn?.remove();
  failedTurn = undefined;
  announcement.textContent = "";

  const history = conversation.turns.slice(-4).flatMap((entry) => [
    { role: "user", content: entry.question },
    { role: "assistant", content: Array.from(entry.result.answer).slice(0, 3000).join("") },
  ]);
  activeRequest = new AbortController();
  showThread(true);
  const { turn, reply } = renderQuestion(question);
  const pending = element("div", "pending");
  pending.setAttribute("role", "status");
  const status = element("span", "", "Ищу материалы по вопросу…");
  pending.append(element("span", "pending-indicator"), status);
  reply.append(pending);
  input.value = "";
  updateComposer();
  turn.scrollIntoView({ block: "nearest" });

  try {
    const result = await streamReply(question, history, activeRequest.signal, (message) => { status.textContent = message; });
    reply.replaceChildren(renderAnswer(result));
    saveTurn(question, result);
    announcement.textContent = "Ответ готов";
  } catch (error) {
    let message = error.message;
    if (error.name === "AbortError") message = "Ответ остановлен. Вопрос сохранён в поле ввода.";
    if (error instanceof TypeError || error instanceof SyntaxError) message = "Не удалось получить ответ. Проверьте соединение и попробуйте ещё раз.";
    reply.replaceChildren(notice(message, error.name !== "AbortError"));
    failedTurn = turn;
    if (!input.value.trim()) input.value = question;
  } finally {
    const returnFocus = document.activeElement === stopButton;
    activeRequest = undefined;
    updateComposer();
    if (returnFocus && !panel.open) sendButton.focus({ preventScroll: true });
    if (panel.open && panelView === "history") renderHistory(false);
  }
}

function preparePanel(view, title, back = false) {
  ++panelRevision;
  panelView = view;
  panelTitle.textContent = title;
  backButton.hidden = !back;
  panelBody.replaceChildren();
  panelBody.scrollTop = 0;
  if (!panel.open) panel.showModal();
  panelTitle.focus({ preventScroll: true });
  return panelRevision;
}

function originalLink(url) {
  if (typeof url !== "string") return null;
  try {
    const href = new URL(url);
    if (href.protocol !== "https:" || href.username || href.password) return null;
    const link = element("a", "panel-link", "Открыть оригинал");
    link.href = href.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.append(icon("external"));
    return link;
  } catch {
    return null;
  }
}

async function fetchData(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw await responseError(response);
  return response.json();
}

function excerptWithEvidence(source) {
  const excerpt = element("div", "excerpt");
  const text = source.excerpt;
  const ranges = [];
  for (const quote of Array.isArray(source.evidence) ? source.evidence : []) {
    if (typeof quote !== "string" || !quote.trim()) continue;
    const pattern = quote.trim().split(/\s+/u)
      .map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/[её]/giu, "[её]"))
      .join("\\s+");
    const match = new RegExp(pattern, "iu").exec(text);
    if (match) ranges.push([match.index, match.index + match[0].length]);
  }
  const merged = [];
  for (const range of ranges.sort((a, b) => a[0] - b[0])) {
    const last = merged.at(-1);
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push(range);
  }
  let from = 0;
  for (const [start, end] of merged) {
    excerpt.append(document.createTextNode(text.slice(from, start)), element("mark", "", text.slice(start, end)));
    from = end;
  }
  excerpt.append(document.createTextNode(text.slice(from)));
  return excerpt;
}

async function openSource(source, fromLibrary = false) {
  const selected = { documentId: source.document_id, sourceId: source.id };
  const revision = preparePanel("source", source.title || "Фрагмент источника", fromLibrary);
  backButton.onclick = () => openLibrary(false, selected);

  if (typeof source.excerpt !== "string" || !source.excerpt) {
    panelBody.append(element("p", "panel-empty", "Загружаю фрагмент…"));
    try {
      source = await fetchData(`/api/sources/${encodeURIComponent(source.id)}`);
    } catch {
      if (panelRevision !== revision) return;
      panelBody.replaceChildren(notice("Фрагмент сейчас недоступен.", true));
      const link = originalLink(source.url);
      if (link) panelBody.append(link);
      return;
    }
  }
  if (panelRevision !== revision) return;
  panelTitle.textContent = source.title;
  const meta = [source.publisher, source.version].filter((value) => typeof value === "string" && value).join(" · ");
  panelBody.replaceChildren();
  if (meta) panelBody.append(element("p", "panel-meta", meta));
  if (source.section) panelBody.append(element("h3", "excerpt-title", source.section));
  panelBody.append(element("p", "panel-meta", "Фрагмент базы знаний"), excerptWithEvidence(source));
  const link = originalLink(source.url);
  if (link) panelBody.append(link);
}

function documentCard(doc) {
  const card = element("details", "doc-card");
  card.dataset.documentId = doc.id;
  const summary = element("summary");
  const name = element("span", "doc-summary");
  name.append(element("strong", "", doc.title));
  const meta = [doc.publisher, doc.version].filter(Boolean).join(" · ");
  if (meta) name.append(element("span", "", meta));
  const chevron = icon("chevron");
  chevron.classList.add("doc-chevron");
  summary.append(icon("book"), name, chevron);
  const body = element("div", "doc-body");
  if (doc.description) body.append(element("p", "", doc.description));
  if (doc.sections?.length) {
    const sections = element("div", "doc-sections");
    for (const section of doc.sections) sections.append(sourceButton({ ...doc, ...section, document_id: doc.id }, true));
    body.append(sections);
  }
  const link = originalLink(doc.url);
  if (link) body.append(link);
  card.append(summary, body);
  return card;
}

async function openLibrary(reset = true, selected = null) {
  const revision = preparePanel("library", "База знаний");
  if (reset) filter = "";
  panelBody.append(element("p", "panel-empty", "Загружаю список документов…"));
  try {
    if (!documents) ({ documents } = await fetchData("/api/documents"));
    if (!Array.isArray(documents)) throw new Error("Invalid library payload");
  } catch {
    if (panelRevision !== revision) return;
    documents = undefined;
    panelBody.replaceChildren(notice("Не удалось загрузить список документов.", true), button("text-button", "Повторить", () => openLibrary(false)));
    return;
  }
  if (panelRevision !== revision) return;
  const searchLabel = element("label", "panel-search");
  const search = element("input");
  search.type = "search";
  search.placeholder = "Найти документ или раздел";
  search.setAttribute("aria-label", search.placeholder);
  search.value = filter;
  searchLabel.append(icon("search"), search);
  const list = element("div", "panel-list");
  const render = () => {
    const query = filter.trim().toLocaleLowerCase("ru");
    const matches = documents.filter((doc) => !query || [doc.title, doc.publisher, doc.description,
      ...(doc.sections ?? []).map((section) => section.section)].join(" ").toLocaleLowerCase("ru").includes(query));
    const cards = matches.map(documentCard);
    list.replaceChildren(...(cards.length ? cards : [element("p", "panel-empty", "Документы не найдены.")]));
  };
  search.addEventListener("input", () => {
    filter = search.value;
    render();
  });
  panelBody.replaceChildren(element("p", "panel-meta", "Материалы о проектной работе, разработке и документации. Локальные регламенты УрФУ в базу пока не входят."), searchLabel, list);
  render();
  if (selected) {
    const card = Array.from(list.children).find((item) => item.dataset.documentId === selected.documentId);
    if (card) {
      card.open = true;
      const section = Array.from(card.querySelectorAll("[data-source-id]"))
        .find((item) => item.dataset.sourceId === selected.sourceId);
      section?.focus();
      return;
    }
    search.focus({ preventScroll: true });
  }
}

function renderHistory(open = true) {
  if (open) preparePanel("history", "История диалогов");
  const storage = element("p", "panel-meta", persisted ? "Диалоги хранятся в этом браузере." : "Браузер не сохранил последние изменения. Они доступны до закрытия страницы.");
  const add = button("text-button", "Новый диалог", () => openConversation());
  add.prepend(icon("plus"));
  add.disabled = Boolean(activeRequest);
  const list = element("div", "panel-list");
  for (const chat of conversations) {
    const row = button("history-row", chat.title, () => openConversation(chat));
    row.setAttribute("aria-label", chat.title);
    const updated = new Date(chat.updated);
    if (Number.isFinite(updated.getTime())) {
      const time = element("time", "", dateFormat.format(updated));
      time.dateTime = updated.toISOString();
      row.append(time);
    }
    row.disabled = Boolean(activeRequest);
    if (chat.id === conversation.id) row.setAttribute("aria-current", "true");
    list.append(row);
  }
  const hint = conversations.length ? list : element("p", "panel-empty", "Здесь появятся диалоги с полученными ответами.");
  panelBody.replaceChildren(storage, add, hint);
}

composer.addEventListener("submit", submitQuestion);
input.addEventListener("input", updateComposer);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !activeRequest && input.value.trim()) {
    event.preventDefault();
    composer.requestSubmit();
  }
});
for (const suggestion of document.querySelectorAll("[data-question]")) {
  suggestion.addEventListener("click", () => {
    input.value = suggestion.dataset.question;
    updateComposer();
    input.focus();
  });
}
stopButton.addEventListener("click", () => activeRequest?.abort());
newButton.addEventListener("click", () => openConversation());
byId("open-history").addEventListener("click", () => renderHistory());
byId("open-library").addEventListener("click", () => openLibrary());
byId("panel-close").addEventListener("click", () => panel.close());
panel.addEventListener("close", () => {
  ++panelRevision;
  panelView = "";
});
panel.addEventListener("click", (event) => {
  if (event.target === panel) panel.close();
});
updateComposer();
