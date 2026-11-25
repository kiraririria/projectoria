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
    this.classList = { add() {}, toggle() {} };
    this.value = "";
    this.scrollHeight = 44;
  }

  addEventListener(event, listener) { this.listeners.set(event, listener); }
  dispatch(event) { return this.listeners.get(event)?.({ preventDefault() {} }); }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children = children; }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  setAttribute() {}
  scrollIntoView() {}
  focus() { this.document.activeElement = this; }
}

function browser(fetch) {
  const nodes = new Map();
  const values = new Map();
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Element(this));
      return nodes.get(id);
    },
    createElement() { return new Element(this); },
    createElementNS() { return new Element(this); },
    createDocumentFragment() { return new Element(this); },
    createTextNode(text) { return new Element(this, text); },
    querySelectorAll() { return []; },
  };
  const localStorage = {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, value); },
  };
  const streamReply = runInNewContext(`${clientSource}\nstreamReply;`, {
    document, fetch, localStorage, AbortController, TextDecoder, TextEncoder,
    renderMarkdown: (text) => new Element(document, text),
  });
  const get = (id) => document.getElementById(id);
  function send(message) {
    get("question").value = message;
    return get("composer").dispatch("submit");
  }
  return { document, get, send, streamReply, values };
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
