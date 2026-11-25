import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const clientSource = readFileSync(new URL("../app/static/app.js", import.meta.url), "utf8")
  .replace(/^import[^\n]+\n/m, "");

class Element {
  constructor(document, text = "") {
    this.document = document;
    this.textContent = text;
    this.children = [];
    this.listeners = new Map();
    this.style = {};
    this.dataset = {};
    this.classList = { add() {}, toggle() {} };
    this.value = "";
    this.scrollHeight = 44;
  }

  addEventListener(event, listener) { this.listeners.set(event, listener); }
  dispatch(event, data = {}) {
    if (event !== "click" || !this.disabled) return this.listeners.get(event)?.({ preventDefault() {}, ...data });
  }
  append(...children) {
    for (const child of children) child.parent = this;
    this.children.push(...children);
  }
  prepend(...children) {
    for (const child of children) child.parent = this;
    this.children.unshift(...children);
  }
  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }
  remove() { this.parent.children = this.parent.children.filter((node) => node !== this); }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  setAttribute() {}
  scrollIntoView() {}
  focus() { this.document.activeElement = this; }
  showModal() { this.open = true; }
  close() {
    this.open = false;
    this.dispatch("close");
  }
}

const historyKey = "proektoria.history.v1";

function browserStorage() {
  const values = new Map();
  const localStorage = {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  let queue = Promise.resolve();
  const locks = {
    request(_name, update) {
      const next = queue.then(update);
      queue = next.catch(() => {});
      return next;
    },
  };
  return { values, localStorage, locks };
}

function browser(fetch, { storage = browserStorage(), useLocks = true } = {}) {
  const nodes = new Map();
  const { values, localStorage, locks } = storage;
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Element(this));
      return nodes.get(id);
    },
    createElement(tag) {
      const node = new Element(this);
      node.tag = tag;
      return node;
    },
    createElementNS() { return new Element(this); },
    createDocumentFragment() { return new Element(this); },
    createTextNode(text) { return new Element(this, text); },
    querySelectorAll() { return []; },
    documentElement: { scrollHeight: 400 },
  };
  const window = new Element(document);
  window.scrollTo = () => {};
  const { streamReply, excerptWithEvidence } = runInNewContext(`${clientSource}\n({ streamReply, excerptWithEvidence });`, {
    document, window, fetch, localStorage, AbortController, TextDecoder, TextEncoder,
    navigator: useLocks ? { locks } : {},
    renderMarkdown: (text) => new Element(document, text),
  });
  const get = (id) => document.getElementById(id);
  function send(message) {
    get("question").value = message;
    return get("composer").dispatch("submit");
  }
  function openHistory() {
    get("open-history").dispatch("click");
    return get("panel-body");
  }
  function selectChat(title) {
    const list = openHistory().children[2];
    const entry = list.children.find((node) => node.children[0].textContent === title);
    assert.ok(entry, `Missing history entry: ${title}`);
    entry.children[0].dispatch("click");
  }
  return { document, get, send, streamReply, values, openHistory, selectChat, excerptWithEvidence, window };
}

function event(name, payload) {
  return `event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`;
}

function response(body) {
  return new Response(body, { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
}

function final(status, answer, extra = {}) {
  return event("final", { status, answer, sources: [], grounded: false, retrieval: { mode: "none" }, ...extra });
}

test("unavailable reply can be retried with its original history while preserving a new draft", async () => {
  const posts = [];
  const replies = [
    final("clarify", "Уточните тему."),
    final("unavailable", "Модель пока недоступна.", { code: "provider_rate_limit", retry_after_seconds: 5 }),
    final("answered", "Готово."),
  ];
  const tab = browser(async (_url, request) => {
    posts.push(JSON.parse(request.body));
    return response(replies.shift());
  });
  await tab.send("Первый вопрос");
  tab.get("send").focus();
  await tab.send("Второй вопрос");
  assert.equal(tab.document.activeElement, tab.get("question"));
  assert.notEqual(tab.get("announcement").textContent, "Ответ готов");
  const reply = tab.get("turns").children.at(-1).children.at(-1);
  assert.match(reply.children[0].children[1].textContent, /Повторите запрос через 5 секунд/);
  assert.equal(JSON.parse([...tab.values.values()][0])[0].turns.length, 1);

  tab.get("question").value = "Неотправленный черновик";
  await reply.children[1].dispatch("click");
  assert.deepEqual(posts[1], posts[2]);
  assert.equal(posts[2].history[1].content, "Уточните тему.");
  assert.equal(tab.get("turns").children.length, 2);
  assert.equal(tab.get("question").value, "Неотправленный черновик");
  assert.equal(JSON.parse([...tab.values.values()][0])[0].turns.length, 2);
});

test("stopping a request keeps the draft and focuses its recovery control", async () => {
  let pendingSignal;
  const posts = [];
  const tab = browser(async (_url, { signal, body }) => {
    posts.push(JSON.parse(body).message);
    if (posts.length === 2) return response(final("insufficient", "Сведений нет."));
    pendingSignal = signal;
    return new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    });
  });
  const pending = tab.send("Остановленный вопрос");
  tab.get("question").value = "Другой черновик";
  tab.get("stop").focus();
  tab.get("stop").dispatch("click");
  await pending;
  assert.equal(pendingSignal.aborted, true);
  assert.equal(tab.values.size, 0);
  assert.equal(tab.get("question").value, "Другой черновик");
  assert.equal(tab.get("announcement").textContent, "Получение ответа остановлено.");
  const reply = tab.get("turns").children[0].children[1];
  assert.equal(tab.document.activeElement, reply.children[1]);

  await reply.children[1].dispatch("click");
  assert.deepEqual(posts, ["Остановленный вопрос", "Остановленный вопрос"]);
  assert.equal(tab.get("question").value, "Другой черновик");
});

test("unframed EOF and malformed final payloads cannot complete a chat", async () => {
  const replies = [
    response(final("answered", "Готово.").slice(0, -2)),
    response(event("final", { answer: "Готово." })),
    response("event: final\ndata: {\n\n"),
    new Response("{}", { headers: { "Content-Type": "application/json" } }),
  ];
  for (const reply of replies) {
    const tab = browser(async () => reply);
    await tab.send("Вопрос");
    assert.equal(tab.values.size, 0);
    assert.notEqual(tab.get("announcement").textContent, "Ответ готов");
    assert.equal(tab.get("turns").children[0].children[1].children[1].textContent, "Повторить вопрос");
  }
});

test("complete SSE frames survive UTF-8 characters split across chunks", async () => {
  const progress = [];
  const bytes = new TextEncoder().encode(event("status", { message: "Ищу…" }) + final("answered", "Ответ."));
  const body = new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      controller.close();
    },
  });
  const tab = browser(async () => response(body));
  const result = await tab.streamReply("Вопрос", [], new AbortController().signal, (message) => progress.push(message));
  assert.equal(result.answer, "Ответ.");
  assert.deepEqual(progress, ["Ищу…"]);
});

test("an SSE error and an HTTP rate limit keep retry advice actionable", async () => {
  const replies = [
    response(event("error", { message: "Ограничение модели.", retry_after_seconds: 7 })),
    new Response("Too many requests", { status: 429, headers: { "Retry-After": "9" } }),
    new Response('{"detail":[]}', { status: 422, headers: { "Content-Type": "application/json" } }),
  ];
  const tab = browser(async () => replies.shift());
  const request = () => tab.streamReply("Вопрос", [], new AbortController().signal, () => {});
  await assert.rejects(request, /Ограничение модели. Повторите запрос через 7 секунд/);
  await assert.rejects(request, /Повторите запрос через 9 секунд/);
  await assert.rejects(request, /Проверьте длину вопроса/);
});

test("simultaneous tabs save their own conversations and both turns on the same conversation", async () => {
  const storage = browserStorage();
  const fetch = async () => response(final("answered", "Ответ."));
  const a = browser(fetch, { storage });
  const b = browser(fetch, { storage });
  const chats = () => JSON.parse(storage.localStorage.getItem(historyKey));

  await Promise.all([a.send("Диалог A"), b.send("Диалог B")]);
  assert.deepEqual(new Set(chats().map(({ title }) => title)), new Set(["Диалог A", "Диалог B"]));

  a.selectChat("Диалог A");
  b.selectChat("Диалог A");
  await Promise.all([a.send("Продолжение A"), b.send("Продолжение B")]);
  const questions = chats().find(({ title }) => title === "Диалог A").turns.map(({ question }) => question);
  assert.deepEqual(new Set(questions), new Set(["Диалог A", "Продолжение A", "Продолжение B"]));
  assert.equal(questions.length, 3);
});

for (const action of ["delete", "clear"]) {
  test(`a dormant tab cannot restore a conversation after ${action} during an in-flight reply`, async () => {
    const storage = browserStorage();
    const primary = browser(async () => response(final("answered", "Ответ.")), { storage });
    await primary.send("Удаляемый диалог");
    await primary.send("Продолжение");

    let finishReply;
    const stale = browser(() => new Promise((resolve) => { finishReply = resolve; }), { storage });
    stale.selectChat("Удаляемый диалог");
    const pending = stale.send("Вопрос из старой вкладки");

    const history = primary.openHistory();
    const control = action === "clear" ? history.children[1].children[1] : history.children[2].children[0].children[1];
    control.dispatch("click");
    assert.equal(primary.get("panel-title").textContent, action === "clear" ? "Очистить историю?" : "Удалить диалог?");
    await history.children[1].children[1].dispatch("click");
    assert.equal(storage.values.has(historyKey), false);

    finishReply(response(final("answered", "Поздний ответ.")));
    await pending;
    assert.equal(storage.values.has(historyKey), false);
    assert.equal(stale.get("turns").children.length, 0);
  });
}

test("dialog navigation keeps drafts and interrupted replies in their own conversation", async () => {
  const replies = [
    final("answered", "Начало."),
    final("unavailable", "Повторите запрос."),
    final("answered", "Продолжение."),
  ];
  const tab = browser(async () => response(replies.shift()));
  await tab.send("Первый диалог");
  await tab.send("Что дальше?");
  tab.get("question").value = "Черновик первого";

  tab.get("new-chat").dispatch("click");
  tab.get("question").value = "Черновик нового";
  tab.selectChat("Первый диалог");
  assert.equal(tab.get("question").value, "Черновик первого");
  const reply = tab.get("turns").children.at(-1).children[1];
  await reply.children[1].dispatch("click");
  assert.equal(tab.get("question").value, "Черновик первого");
  tab.get("new-chat").dispatch("click");
  assert.equal(tab.get("question").value, "Черновик нового");
});

test("legacy history keeps valid turns and an unsupported browser uses memory for new replies", async () => {
  const storage = browserStorage();
  const legacy = JSON.stringify([
    null,
    {
      id: "legacy", title: "Старый диалог", updated: 1000,
      turns: [
        { question: "Старый вопрос", result: { answer: "Старый ответ" } },
        { question: "Сбой", result: { status: "unavailable", answer: "Сбой" } },
        { question: 42, result: { answer: "Некорректная запись" } },
      ],
    },
  ]);
  storage.localStorage.setItem(historyKey, legacy);
  const tab = browser(async () => response(final("answered", "Ответ в памяти.")), { storage, useLocks: false });
  tab.selectChat("Старый диалог");
  assert.equal(tab.get("turns").children.length, 1);
  await tab.send("Новый вопрос");
  assert.equal(storage.localStorage.getItem(historyKey), legacy);
  assert.match(tab.openHistory().children[0].textContent, /до закрытия страницы/);
  tab.selectChat("Старый диалог");
  assert.equal(tab.get("turns").children.length, 2);
});

test("a terminal reply cannot be stopped while its history write waits for a lock", async () => {
  const storage = browserStorage();
  let release;
  storage.locks.request = (_name, update) => new Promise((resolve) => { release = () => resolve(update()); });
  const tab = browser(async () => response(final("answered", "Получен ответ.")), { storage });
  const pending = tab.send("Вопрос");
  tab.get("stop").focus();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(tab.get("stop").disabled, true);
  assert.equal(tab.document.activeElement, tab.get("question"));
  tab.get("stop").dispatch("click");
  release();
  await pending;
  assert.equal(tab.get("announcement").textContent, "Ответ готов");
});

test("a tab-only dialog can be removed when the browser rejects persistent writes", async () => {
  const storage = browserStorage();
  storage.localStorage.setItem = () => { throw new Error("Storage is full"); };
  const tab = browser(async () => response(final("answered", "Ответ в памяти.")), { storage });
  await tab.send("Временный диалог");
  const history = tab.openHistory();
  assert.match(history.children[0].textContent, /до закрытия страницы/);
  history.children[2].children[0].children[1].dispatch("click");
  await history.children[1].children[1].dispatch("click");
  assert.match(history.children[2].textContent, /Здесь появятся диалоги/);
  assert.equal(tab.get("turns").children.length, 0);
});

test("citation marks use verified UTF-16 offsets and preserve word boundaries in old results", () => {
  const tab = browser(async () => {});
  const excerpt = "📘 pull requests; потом pull request и pull request.";
  const quote = "pull request";
  const last = excerpt.lastIndexOf(quote);
  const marked = tab.excerptWithEvidence({
    excerpt, evidence: [quote],
    evidence_spans: [{ start: last, end: last + quote.length }, { start: 0, end: 1 }],
  });
  assert.equal(marked.children[0].textContent.length, last);
  assert.equal(marked.children[1].tag, "mark");
  assert.equal(marked.children[1].textContent, quote);

  const legacy = tab.excerptWithEvidence({ excerpt, evidence: [quote] });
  assert.equal(legacy.children[0].textContent, "📘 pull requests; потом ");
  assert.equal(legacy.children[1].textContent, quote);
});
