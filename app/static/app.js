import { renderMarkdown } from "./markdown.js";
import { initFeedback } from "./feedback.js";

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
const historyLocks = globalThis.navigator?.locks;
const historyLimit = 20;
const dateFormat = new Intl.DateTimeFormat("ru", { dateStyle: "medium", timeStyle: "short" });
const replyStatuses = new Set(["answered", "partial", "project_details", "insufficient", "unrelated", "clarify", "unavailable"]);
const retrievalModes = new Set(["none", "lexical", "hybrid"]);
const incompleteReply = "Получен неполный ответ. Попробуйте ещё раз.";

const localChats = new Map();
const drafts = new Map();
const failures = new Map();
let newConversation = { turns: [] };
let conversation = newConversation;
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

function readStoredHistory() {
  const serialized = localStorage.getItem(storageKey);
  let stored;
  try {
    stored = JSON.parse(serialized ?? "[]");
  } catch {
    return [];
  }
  if (!Array.isArray(stored)) return [];
  const chats = new Map();
  for (const chat of stored) {
    if (typeof chat?.id !== "string" || !chat.id || typeof chat.title !== "string"
      || !Number.isFinite(chat.updated) || !Array.isArray(chat.turns) || chats.has(chat.id)) continue;
    const turns = chat.turns.filter((turn) => typeof turn?.question === "string" && turn.question.trim()
      && typeof turn.result?.answer === "string" && turn.result.answer.trim() && turn.result.status !== "unavailable");
    if (turns.length) chats.set(chat.id, { id: chat.id, title: chat.title, updated: chat.updated, turns, stored: true });
  }
  return [...chats.values()];
}

function changeStoredHistory(change) {
  return historyLocks.request(storageKey, () => {
    const next = change(readStoredHistory());
    if (!next) return false;
    if (next.length) {
      const data = next.map(({ id, title, updated, turns }) => ({ id, title, updated, turns }));
      localStorage.setItem(storageKey, JSON.stringify(data));
    } else {
      localStorage.removeItem(storageKey);
    }
    return true;
  });
}

function rememberLocalChat(chat) {
  localChats.delete(chat.id);
  localChats.set(chat.id, chat);
  if (localChats.size > historyLimit) localChats.delete(localChats.keys().next().value);
}

async function saveTurn(question, result) {
  const chat = conversation;
  if (!chat.id) {
    chat.id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    chat.title = question.replace(/\s+/g, " ").slice(0, 120);
    newConversation = { turns: [] };
    drafts.delete("");
  }
  chat.updated = Date.now();
  const turn = { question, result };
  chat.turns.push(turn);
  chat.pending ??= [];
  chat.pending.push(turn);
  if (!historyLocks) {
    rememberLocalChat(chat);
    return;
  }
  try {
    const saved = await changeStoredHistory((history) => {
      const latest = history.find(({ id }) => id === chat.id);
      if (chat.removed || (chat.stored && !latest)) return null;
      const next = { ...chat, turns: [...(latest?.turns ?? []), ...chat.pending] };
      return [next, ...history.filter(({ id }) => id !== chat.id)].slice(0, historyLimit);
    });
    persisted = true;
    localChats.delete(chat.id);
    if (saved) {
      chat.stored = true;
      chat.pending = [];
    } else {
      forgetConversation(chat.id);
    }
  } catch {
    persisted = false;
    rememberLocalChat(chat);
  }
}

function forgetConversation(id) {
  localChats.delete(id);
  drafts.delete(id);
  failures.delete(id);
  if (conversation.id === id) conversation.removed = true;
}

function readHistory() {
  let stored = [];
  try {
    stored = readStoredHistory();
  } catch {
    persisted = false;
  }
  return [...localChats.values(), ...stored.filter(({ id }) => !localChats.has(id))]
    .sort((first, second) => second.updated - first.updated)
    .slice(0, historyLimit);
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

function openConversation(chat = newConversation, closePanel = true) {
  if (activeRequest) return;
  if (!conversation.removed) drafts.set(conversation.id ?? "", input.value);
  conversation = chat;
  failedTurn = undefined;
  turns.replaceChildren();
  for (const turn of conversation.turns) {
    const exchange = renderQuestion(turn.question);
    exchange.reply.append(renderAnswer(turn.result));
  }
  const failure = failures.get(conversation.id ?? "");
  if (failure) {
    const exchange = renderQuestion(failure.question);
    failedTurn = exchange.turn;
    renderFailure(exchange, failure);
  }
  input.value = drafts.get(conversation.id ?? "") ?? "";
  showThread(conversation.turns.length > 0 || Boolean(failure));
  updateComposer();
  if (closePanel && panel.open) panel.close();
  if (!panel.open) {
    window.scrollTo(0, conversation.turns.length || failure ? document.documentElement.scrollHeight : 0);
    input.focus({ preventScroll: true });
  }
}

function resetRemovedConversation() {
  if (!conversation.removed || activeRequest) return;
  openConversation(newConversation, false);
  announcement.textContent = "Диалог удалён из истории.";
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
  if (result.grounded === true && sources.length) meta.append(element("span", "", "Цитаты сверены с базой"));
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
    return new Error(`Сервис получает слишком много запросов. ${retryAdvice(seconds) || "Попробуйте чуть позже."}`);
  }
  const data = await response.json().catch(() => null);
  if (typeof data?.detail === "string") return new Error(data.detail);
  if (response.status === 413 || response.status === 422) return new Error("Проверьте длину вопроса и попробуйте снова.");
  return new Error("Сервис временно недоступен. Попробуйте ещё раз.");
}

function retryAdvice(seconds) {
  if (!Number.isSafeInteger(seconds) || seconds <= 0) return "";
  const useMinutes = seconds >= 60;
  const delay = new Intl.NumberFormat("ru", { style: "unit", unit: useMinutes ? "minute" : "second", unitDisplay: "long" })
    .format(useMinutes ? Math.ceil(seconds / 60) : seconds);
  return `Повторите запрос через ${delay}.`;
}

function chatFailure(message, seconds) {
  return new Error([message, retryAdvice(seconds)].filter(Boolean).join(" "));
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

const followUp = /^\s*(?:а\s+)?(?:если|тогда|в таком случае|покажи пример|что насч[её]т|можешь уточнить|как (?:это|именно)|какие из них|кто за это|продолжи)(?![\p{L}\p{N}_])|^\s*а\s+(?:кто|когда|где|почему|зачем|сколько)(?![\p{L}\p{N}_])/iu;
const reaction = /^(?:[\s.!?…]+|(?:что|ч[её]|шо)\s+(?:это\s+)?за\s+(?:х[у*]йн[яю]|фигн[яю]|ерунд[ау]|бред|дичь|хрень|хрен[ья])[\s.!?…]*|(?:не понял[аи]?|не понимаю|непонятно|что это такое|что это было)[\s.!?…]*)$/iu;
const scrumConfirmation = /^да(?:\s*,?\s*(?:scrum|скрам))?[.!?…]*$/iu;
const scrumSpelling = "Вы имели в виду Scrum?";

const replyReference = /^\s*(?:а\s+)?(?:почему ты (?:сказал[аи]?|написал[аи]?|ответил[аи]?)|что ты (?:имел[аи]? в виду|подразумевал[аи]?)|в (?:тво[её]м|прошлом|предыдущем) (?:ответе|сообщении))(?![\p{L}\p{N}_])/iu;
const continuesTopic = (question) => followUp.test(question) || replyReference.test(question);

function historyExcerpt(text) {
  return Array.from(text).slice(0, 700).join("").trimEnd();
}

function dialogHistory(question, turns) {
  const latest = turns.at(-1);
  if (!latest) return [];

  if (scrumConfirmation.test(question) && latest.result.answer === scrumSpelling) {
    return [
      { role: "user", content: latest.question },
      { role: "assistant", content: scrumSpelling },
    ];
  }

  if (reaction.test(question)) return [{ role: "user", content: historyExcerpt(latest.question) }];
  if (!continuesTopic(question)) return [];

  const recent = [];
  let anchor;
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index];
    if (reaction.test(turn.question)) continue;
    if (!continuesTopic(turn.question)) {
      anchor = turn;
      break;
    }
    if (recent.length < 2) recent.unshift(turn);
  }

  const relevant = anchor ? [anchor, ...recent] : recent;
  const history = relevant.map(({ question }) => {
    const excerpt = historyExcerpt(question);
    // A leading reaction or follow-up prefix can change classification when cut mid-word.
    const content = reaction.test(excerpt) || continuesTopic(question) !== continuesTopic(excerpt) ? question : excerpt;
    return { role: "user", content };
  });
  const previousReply = relevant.at(-1)?.result.answer;
  if (replyReference.test(question) && previousReply?.trim()) {
    history.push({ role: "assistant", content: historyExcerpt(previousReply.trim()) });
  }
  return history;
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
  const mediaType = response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase();
  if (!response.body || mediaType !== "text/event-stream") throw new Error(incompleteReply);
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop();
      for (const frame of frames) {
        const received = parseEvent(frame);
        if (!received) continue;
        const { event, payload } = received;
        if (event === "status" && typeof payload?.message === "string") onStatus(payload.message);
        if (event === "error") {
          const message = typeof payload?.message === "string" && payload.message.trim()
            ? payload.message : "Не удалось получить ответ. Попробуйте ещё раз.";
          throw chatFailure(message, payload?.retry_after_seconds);
        }
        if (event === "final") {
          if (typeof payload?.answer !== "string" || !payload.answer.trim() || !replyStatuses.has(payload.status)
            || !Array.isArray(payload.sources) || typeof payload.grounded !== "boolean"
            || !retrievalModes.has(payload.retrieval?.mode)) throw new Error(incompleteReply);
          return payload;
        }
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
  failures.delete(conversation.id ?? "");

  const history = dialogHistory(question, conversation.turns);
  showThread(true);
  await requestReply(question, history, renderQuestion(question), true);
}

function renderFailure(exchange, failure) {
  const { question, history, message, stopped, restoredDraft } = failure;
  const retry = button("text-button", "Повторить вопрос", () => {
    const clearRestoredDraft = restoredDraft && input.value === question;
    return requestReply(question, history, exchange, clearRestoredDraft);
  });
  exchange.reply.replaceChildren(notice(message, !stopped), retry);
  return retry;
}

async function requestReply(question, history, exchange, clearInput = false) {
  if (activeRequest) return;
  const { turn, reply } = exchange;
  const chatKey = conversation.id ?? "";
  const focused = document.activeElement;
  const focusInput = focused === sendButton || reply.contains(focused);
  const controller = new AbortController();
  activeRequest = controller;
  announcement.textContent = "";
  const pending = element("div", "pending");
  pending.setAttribute("role", "status");
  const status = element("span", "", "Ищу материалы по вопросу…");
  pending.append(element("span", "pending-indicator"), status);
  reply.replaceChildren(pending);
  if (clearInput) input.value = "";
  updateComposer();
  if (focusInput && !panel.open) input.focus({ preventScroll: true });
  turn.scrollIntoView({ block: "nearest" });

  let retry;
  try {
    const result = await streamReply(question, history, controller.signal, (message) => { status.textContent = message; });
    controller.signal.throwIfAborted();
    if (result.status === "unavailable") throw chatFailure(result.answer, result.retry_after_seconds);
    const focusAfterReceive = document.activeElement === stopButton;
    stopButton.disabled = true;
    if (focusAfterReceive && !panel.open) input.focus({ preventScroll: true });
    reply.replaceChildren(renderAnswer(result));
    await saveTurn(question, result);
    failures.delete(chatKey);
    failedTurn = undefined;
    announcement.textContent = "Ответ готов";
    void feedback.answerCompleted(result);
  } catch (error) {
    const stopped = controller.signal.aborted;
    const restoredDraft = !input.value;
    if (restoredDraft) input.value = question;
    let message = stopped ? "Получение ответа остановлено." : error.message;
    if (!stopped && (error instanceof TypeError || error instanceof SyntaxError)) {
      message = "Не удалось получить ответ. Проверьте соединение и попробуйте ещё раз.";
    }
    const failure = { question, history, message, stopped, restoredDraft };
    if (!conversation.removed) failures.set(chatKey, failure);
    retry = renderFailure(exchange, failure);
    if (stopped) announcement.textContent = message;
    failedTurn = turn;
  } finally {
    const returnFocus = document.activeElement === stopButton;
    activeRequest = undefined;
    stopButton.disabled = false;
    updateComposer();
    if (conversation.removed) resetRemovedConversation();
    else if (returnFocus && !panel.open) (retry ?? input).focus();
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

function evidenceRanges(source) {
  const { excerpt: text, evidence_spans: spans } = source;
  const quotes = new Set(Array.isArray(source.evidence) ? source.evidence.filter((quote) =>
    typeof quote === "string" && quote.trim() && quote.length <= text.length) : []);
  if (Array.isArray(spans)) {
    return spans.filter((range) => Number.isInteger(range?.start) && Number.isInteger(range.end)
      && range.start >= 0 && range.end > range.start && range.end <= text.length
      && quotes.has(text.slice(range.start, range.end)))
      .map(({ start, end }) => [start, end]);
  }

  const ranges = [];
  const wordCharacter = "[\\p{L}\\p{N}_]";
  for (const quote of quotes) {
    const trimmed = quote.trim();
    const begin = /^[\p{L}\p{N}]/u.test(trimmed) ? `(?<!${wordCharacter})` : "";
    const end = /[\p{L}\p{N}]$/u.test(trimmed) ? `(?!${wordCharacter})` : "";
    const pattern = trimmed.split(/\s+/u)
      .map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/[её]/giu, "[её]"))
      .join("\\s+");
    const match = new RegExp(`${begin}${pattern}${end}`, "iu").exec(text);
    if (match) ranges.push([match.index, match.index + match[0].length]);
  }
  return ranges;
}

function excerptWithEvidence(source) {
  const excerpt = element("div", "excerpt");
  const text = source.excerpt;
  const merged = [];
  for (const range of evidenceRanges(source).sort((a, b) => a[0] - b[0])) {
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
      const data = await fetchData(`/api/sources/${encodeURIComponent(source.id)}`);
      if (typeof data?.excerpt !== "string" || typeof data.title !== "string") throw new Error("Invalid source response");
      source = data;
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
  const focused = document.activeElement?.dataset?.historyAction;
  const conversations = readHistory();
  let storageText = "Диалоги хранятся в этом браузере.";
  if (!historyLocks) {
    storageText = "Браузер не поддерживает совместную запись истории между вкладками. Новые ответы доступны до закрытия страницы.";
  } else if (!persisted) {
    storageText = "Браузер не сохранил последние изменения. Они доступны до закрытия страницы.";
  }
  const storage = element("p", "panel-meta", storageText);
  const actions = element("div", "history-actions");
  const controls = new Map();
  const register = (control, action) => {
    control.dataset.historyAction = action;
    controls.set(action, control);
    return control;
  };
  const add = button("text-button", "Новый диалог", () => openConversation());
  add.prepend(icon("plus"));
  add.disabled = Boolean(activeRequest);
  const clear = button("text-button history-remove", "Очистить историю", () => confirmHistoryRemoval());
  clear.disabled = Boolean(activeRequest) || !conversations.length;
  actions.append(register(add, "new"), register(clear, "clear"));
  const list = element("div", "panel-list");
  for (const chat of conversations) {
    const entry = element("div", "history-entry");
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
    const remove = button("text-button history-remove", "Удалить", () => confirmHistoryRemoval(chat));
    remove.setAttribute("aria-label", `Удалить диалог «${chat.title}»`);
    remove.disabled = Boolean(activeRequest) || (!historyLocks && Boolean(chat.stored));
    entry.append(register(row, `open-${chat.id}`), register(remove, `remove-${chat.id}`));
    list.append(entry);
  }
  const hint = conversations.length ? list : element("p", "panel-empty", "Здесь появятся диалоги с полученными ответами.");
  panelBody.replaceChildren(storage, actions, hint);
  if (!open && focused) {
    const control = controls.get(focused);
    (control && !control.disabled ? control : panelTitle).focus({ preventScroll: true });
  }
}

function confirmHistoryRemoval(chat) {
  const title = chat ? "Удалить диалог?" : "Очистить историю?";
  const revision = preparePanel("history-remove", title);
  const summary = element("p", "panel-meta", chat ? `Диалог «${chat.title}» будет удалён.` : "Все диалоги в истории браузера будут удалены.");
  const actions = element("div", "history-actions");
  const cancel = button("text-button", "Отмена", () => renderHistory());
  const remove = button("text-button history-remove", chat ? "Удалить" : "Очистить", async () => {
    remove.disabled = true;
    cancel.disabled = true;
    try {
      if (historyLocks && (!chat || chat.stored)) {
        await changeStoredHistory((stored) => chat ? stored.filter(({ id }) => id !== chat.id) : []);
      } else if (!chat) {
        localStorage.removeItem(storageKey);
      }
    } catch {
      if (panelRevision === revision) {
        panelBody.append(notice("Браузер не смог удалить историю. Попробуйте ещё раз.", true));
        remove.disabled = false;
        cancel.disabled = false;
        remove.focus();
      }
      return;
    }
    if (chat) {
      forgetConversation(chat.id);
    } else {
      const ids = new Set([...localChats.keys(), ...drafts.keys(), ...failures.keys(), conversation.id]);
      for (const id of ids) if (id) forgetConversation(id);
    }
    resetRemovedConversation();
    if (panelRevision === revision) renderHistory();
  });
  actions.append(cancel, remove);
  panelBody.append(summary, actions);
  cancel.focus();
}

function syncHistory(event) {
  if (event.storageArea !== localStorage || (event.key !== storageKey && event.key !== null)) return;
  let saved;
  try {
    saved = new Set(readStoredHistory().map(({ id }) => id));
  } catch {
    return;
  }
  for (const chat of localChats.values()) {
    if (chat.stored && !saved.has(chat.id)) forgetConversation(chat.id);
  }
  for (const id of new Set([...drafts.keys(), ...failures.keys()])) {
    if (id && !saved.has(id) && !localChats.has(id)) forgetConversation(id);
  }
  if (conversation.stored && !saved.has(conversation.id)) forgetConversation(conversation.id);
  if (conversation.removed) activeRequest?.abort();
  resetRemovedConversation();
  if (panel.open && panelView === "history") renderHistory(false);
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
window.addEventListener("storage", syncHistory);
const feedback = initFeedback();
updateComposer();
