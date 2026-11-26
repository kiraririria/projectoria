export const FEEDBACK_KEY = "projectoria.feedback.v1";
export const DAY = 24 * 60 * 60 * 1000;
export const PROMPT_EVERY = 10;
export const PROMPT_COOLDOWN = DAY;
export const SENT_COOLDOWN = 30 * DAY;
export const SEND_COOLDOWN = 60 * 1000;

export function readFeedbackState(storage) {
  let value;
  try { value = JSON.parse(storage.getItem(FEEDBACK_KEY)); } catch {}
  const state = {};
  for (const key of ["answers", "lastPromptAnswers", "lastPromptAt", "lastSentAt"]) {
    state[key] = Number.isSafeInteger(value?.[key]) && value[key] >= 0 ? value[key] : 0;
  }
  return state;
}

export function successfulAnswer(result) {
  return ["answered", "partial", "project_details"].includes(result?.status)
    && result.grounded === true && typeof result.answer === "string" && Boolean(result.answer.trim())
    && Array.isArray(result.sources) && result.sources.length > 0;
}

export function promptDue(state, now, prompted) {
  if (prompted || !state.answers || (state.lastSentAt && now - state.lastSentAt < SENT_COOLDOWN)) return false;
  if (!state.lastPromptAt) return true;
  return now - state.lastPromptAt >= PROMPT_COOLDOWN && state.answers - state.lastPromptAnswers >= PROMPT_EVERY;
}

export function initFeedback() {
  const get = (id) => document.getElementById(id);
  const dialog = get("feedback-dialog");
  const form = get("feedback-form");
  const comment = get("feedback-comment");
  const send = get("feedback-send");
  const status = get("feedback-status");
  const nudge = get("feedback-nudge");
  const radios = [...form.querySelectorAll('input[name="rating"]')];
  let local;
  try { local = globalThis.localStorage; } catch {}
  let state = readFeedbackState(local);
  let prompted = false;
  try { prompted = sessionStorage.getItem("projectoria.feedback.prompted") === "1"; } catch {}
  let sending = false;
  let blockedUntil = 0;
  let payloadKey;
  let payloadText;
  let revision = 0;
  const persist = () => { try { local?.setItem(FEEDBACK_KEY, JSON.stringify(state)); } catch {} };
  const message = (text, error = false) => {
    status.textContent = text;
    status.setAttribute("role", error ? "alert" : "status");
  };
  const ratingChanged = () => {
    const selected = Number(radios.find((radio) => radio.checked)?.value ?? 0);
    for (const radio of radios) radio.parentElement.classList.toggle("is-filled", Number(radio.value) <= selected);
    return selected;
  };
  for (const radio of radios) radio.addEventListener("change", ratingChanged);

  async function config() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/api/feedback/config", { headers: { Accept: "application/json" },
        cache: "no-store", signal: controller.signal });
      const data = await response.json();
      return response.ok && data?.enabled === true;
    } finally { clearTimeout(timer); }
  }

  async function open(rating) {
    const current = ++revision;
    if (rating) {
      for (const radio of radios) radio.checked = Number(radio.value) === rating;
      ratingChanged();
    }
    if (!dialog.open) dialog.showModal();
    get("feedback-title").focus();
    if (sending) return;
    message("Проверяю возможность отправки…");
    send.disabled = true;
    try {
      const enabled = await config();
      if (revision !== current) return;
      send.disabled = !enabled;
      message(enabled ? "" : "Отзывы временно недоступны. Попробуйте позже.", !enabled);
    } catch {
      if (revision === current) {
        send.disabled = false;
        message("Не удалось подключиться. Можно повторить отправку.", true);
      }
    }
  }

  get("open-feedback").addEventListener("click", () => { void open(); });
  get("feedback-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { revision += 1; });
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  get("feedback-nudge-close").addEventListener("click", () => { nudge.hidden = true; });
  get("feedback-nudge-open").addEventListener("click", () => { void open(); });
  for (const star of nudge.querySelectorAll("[data-rating]")) {
    star.addEventListener("click", () => { void open(Number(star.dataset.rating)); });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending) return;
    const rating = ratingChanged();
    if (!rating) { message("Выберите оценку от 1 до 5.", true); radios[0].focus(); return; }
    state = { ...state, ...readFeedbackState(local), answers: state.answers };
    const waitUntil = Math.max(blockedUntil, state.lastSentAt ? state.lastSentAt + SEND_COOLDOWN : 0);
    if (Date.now() < waitUntil) { message("Пожалуйста, подождите перед следующим отзывом.", true); return; }
    const body = JSON.stringify({ rating, comment: comment.value.trim() });
    if (body !== payloadText) {
      payloadText = body;
      payloadKey = crypto.randomUUID();
    }
    sending = true;
    send.disabled = true;
    message("Отправляю отзыв…");
    try {
      if (!await config()) throw new Error("Отзывы временно недоступны. Попробуйте позже.");
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 12000);
      let response;
      try {
        response = await fetch("/api/feedback", { method: "POST", headers: {
          "Content-Type": "application/json", "Idempotency-Key": payloadKey,
        }, body, signal: controller.signal });
      } finally { clearTimeout(timer); }
      const data = await response.json().catch(() => null);
      if (!response.ok || data?.status !== "sent") {
        const retry = Number(response.headers.get("Retry-After"));
        if (response.status === 429 && Number.isFinite(retry) && retry > 0) blockedUntil = Date.now() + retry * 1000;
        throw new Error(typeof data?.detail === "string" ? data.detail : "Не удалось подтвердить отправку. Попробуйте позже.");
      }
      state.lastSentAt = Date.now();
      persist();
      nudge.hidden = true;
      form.reset();
      ratingChanged();
      payloadKey = payloadText = undefined;
      message("Спасибо! Ваш отзыв поможет улучшить Проекторию.");
    } catch (error) {
      message(error.name === "AbortError" || error instanceof TypeError
        ? "Не удалось подтвердить отправку. Комментарий сохранён. Можно повторить попытку."
        : error.message, true);
    } finally {
      sending = false;
      send.disabled = false;
    }
  });

  return {
    async answerCompleted(result) {
      if (!successfulAnswer(result)) return;
      state = local ? readFeedbackState(local) : state;
      state.answers += 1;
      persist();
      if (!promptDue(state, Date.now(), prompted)) return;
      try { if (!await config()) return; } catch { return; }
      if (!promptDue(state, Date.now(), prompted)) return;
      prompted = true;
      state.lastPromptAt = Date.now();
      state.lastPromptAnswers = state.answers;
      persist();
      try { sessionStorage.setItem("projectoria.feedback.prompted", "1"); } catch {}
      nudge.hidden = false;
    },
  };
}
