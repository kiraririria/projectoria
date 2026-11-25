import assert from "node:assert/strict";
import { test } from "node:test";
import { initFeedback, readFeedbackState, successfulAnswer, promptDue, FEEDBACK_KEY, DAY } from "../app/static/feedback.js";

const good = { status: "answered", grounded: true, sources: [{}], answer: "Проверенный ответ." };
function storage() {
  const values = new Map();
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), values };
}

class Element {
  constructor() { this.handlers = {}; this.value = ""; this.hidden = true; this.classList = { toggle() {} }; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  dispatch(name) { return this.handlers[name]?.({ preventDefault() {} }); }
  setAttribute(name, value) { this[name] = value; }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; this.dispatch("close"); }
}

function setup(responder, existing = storage(), session = storage()) {
  const nodes = new Map();
  const get = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const radios = [1, 2, 3, 4, 5].map((n) => {
    const radio = new Element(); radio.value = String(n); radio.parentElement = new Element(); return radio;
  });
  get("feedback-form").querySelectorAll = () => radios;
  get("feedback-nudge").querySelectorAll = () => [];
  get("feedback-form").reset = () => { radios.forEach((radio) => { radio.checked = false; }); get("feedback-comment").value = ""; };
  globalThis.document = { getElementById: get };
  globalThis.localStorage = existing;
  globalThis.sessionStorage = session;
  globalThis.fetch = responder ?? (async () => Response.json({ enabled: true }));
  const controller = initFeedback();
  const send = (rating = 3, comment = "") => {
    radios.forEach((radio) => { radio.checked = Number(radio.value) === rating; });
    get("feedback-comment").value = comment;
    return get("feedback-form").dispatch("submit");
  };
  return { get, radios, controller, send, storage: existing, session };
}

test("only completed grounded answers count; cancellation/error statuses never qualify", () => {
  assert.ok(successfulAnswer(good));
  for (const result of [null, { ...good, status: "unavailable" }, { ...good, status: "clarify" },
    { ...good, grounded: false }, { ...good, sources: [] }, { ...good, answer: " " }]) {
    assert.equal(successfulAnswer(result), false);
  }
});

test("automatic prompts require ten more answers and a day; sent feedback pauses thirty days", () => {
  const now = 100 * DAY;
  const state = { answers: 1, lastPromptAt: 0, lastSentAt: 0, lastPromptAnswers: 0 };
  assert.equal(promptDue(state, now, false), true);
  assert.equal(promptDue(state, now, true), false);
  assert.equal(promptDue({ ...state, answers: 11, lastPromptAt: now - DAY + 1, lastPromptAnswers: 1 }, now, false), false);
  assert.equal(promptDue({ ...state, answers: 10, lastPromptAt: now - DAY, lastPromptAnswers: 1 }, now, false), false);
  assert.equal(promptDue({ ...state, answers: 11, lastPromptAt: now - DAY, lastPromptAnswers: 1 }, now, false), true);
  assert.equal(promptDue({ ...state, lastSentAt: now - 29 * DAY }, now, false), false);
});

test("localStorage corruption does not affect history or crash feedback", () => {
  const store = storage();
  store.setItem(FEEDBACK_KEY, "not json");
  store.setItem("proektoria.history.v1", "keep");
  assert.equal(readFeedbackState(store).answers, 0);
  const broken = { getItem() { throw new Error("blocked"); } };
  assert.equal(readFeedbackState(broken).lastSentAt, 0);
  assert.equal(store.getItem("proektoria.history.v1"), "keep");
});

test("first success offers a nonmodal card once per session and persists dismissal", async () => {
  const tab = setup();
  await tab.controller.answerCompleted({ ...good, status: "unavailable" });
  assert.equal(tab.get("feedback-nudge").hidden, true);
  await tab.controller.answerCompleted(good);
  assert.equal(tab.get("feedback-nudge").hidden, false);
  assert.notEqual(tab.get("feedback-dialog").open, true);
  tab.get("feedback-nudge-close").dispatch("click");
  await tab.controller.answerCompleted(good);
  assert.equal(tab.get("feedback-nudge").hidden, true);
  const reload = setup(undefined, tab.storage, tab.session);
  await reload.controller.answerCompleted(good);
  assert.equal(reload.get("feedback-nudge").hidden, true);
});

test("rating 1, 3 and 5, with and without comment; success cooldown persists", async () => {
  for (const rating of [1, 3, 5]) for (const comment of ["", "Нужны примеры <b>ТЗ</b>"]) {
    const posts = [];
    const tab = setup(async (url, options) => {
      if (url.endsWith("/config")) return Response.json({ enabled: true });
      posts.push(options); return Response.json({ status: "sent" }, { status: 201 });
    });
    await tab.send(rating, comment);
    assert.deepEqual(JSON.parse(posts[0].body), { rating, comment });
    assert.ok(posts[0].headers["Idempotency-Key"]);
    assert.match(tab.get("feedback-status").textContent, /Спасибо/);
    assert.equal(tab.get("feedback-comment").value, "");
    await tab.send(rating, comment);
    assert.equal(posts.length, 1);
    assert.ok(readFeedbackState(tab.storage).lastSentAt);
  }
});

test("network failure preserves comment and reuses idempotency key for retry", async () => {
  const posts = [];
  const tab = setup(async (url, options) => {
    if (url.endsWith("/config")) return Response.json({ enabled: true });
    posts.push(options);
    if (posts.length === 1) throw new TypeError("network");
    return Response.json({ status: "sent" });
  });
  await tab.send(3, "Сохранить комментарий");
  assert.equal(tab.get("feedback-comment").value, "Сохранить комментарий");
  await tab.send(3, "Сохранить комментарий");
  assert.equal(posts[0].headers["Idempotency-Key"], posts[1].headers["Idempotency-Key"]);
});

test("pending submit blocks double clicks and a 429 never displays success", async () => {
  let release;
  let count = 0;
  const tab = setup(async (url) => {
    if (url.endsWith("/config")) return Response.json({ enabled: true });
    count += 1;
    return new Promise((resolve) => { release = resolve; });
  });
  const first = tab.send();
  await new Promise(setImmediate);
  await tab.send();
  assert.equal(count, 1);
  assert.equal(tab.get("feedback-send").disabled, true);
  release(Response.json({ detail: "Лимит отзывов" }, { status: 429, headers: { "Retry-After": "60" } }));
  await first;
  assert.match(tab.get("feedback-status").textContent, /Лимит/);
  assert.equal(readFeedbackState(tab.storage).lastSentAt, 0);
  await tab.send();
  assert.equal(count, 1);
});
