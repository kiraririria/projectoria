const alternateQuestions = [
  ["Как определить границы проекта?", "Как зафиксировать границы проекта и критерии готовности?"],
  ["Зачем нужна цель спринта?", "Как цель спринта связана с работой Scrum Team?"],
  ["Какие требования описать в ТЗ?", "Что входит в требования к программе по ГОСТ 19.201-78?"],
  ["Что проверяет GitHub Actions?", "Как запустить проверки pull request через GitHub Actions?"],
];
const hints = ["про план проекта", "про командные роли", "про техническое задание", "про Scrum", "про Git и CI/CD"];
const timeFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" });

export function initInterface({ readHistory, openConversation, submitExample }) {
  const get = (id) => document.getElementById(id);
  const menu = get("menu");
  const nav = get("navButton");
  const flyout = get("flyout");
  const home = get("heroMark");
  const input = get("question");
  const starters = get("suggestions");
  const shuffle = get("shuffle");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const narrowHero = matchMedia("(max-width: 639px)");
  const recentLayout = matchMedia("(min-width: 900px)");
  const artwork = window.Paragraf;
  const sets = [[...starters.querySelectorAll("[data-question]")].map((button) => [button.textContent.trim(), button.dataset.question]), alternateQuestions];
  let set = 0;
  let busy = false;
  let mascot;
  const covers = new Map();

  function mountMascot() {
    mascot?.stop();
    home.replaceChildren();
    mascot = artwork?.auraMark(home, narrowHero.matches ? 132 : 168, { palette: "soft" });
    if (busy) mascot?.mood("thinking");
  }

  mountMascot();
  narrowHero.addEventListener("change", mountMascot);
  if (matchMedia("(hover: hover) and (pointer: fine)").matches) input.focus({ preventScroll: true });
  if (artwork) {
    const dots = document.createElement("canvas");
    dots.className = "mark";
    dots.setAttribute("aria-hidden", "true");
    get("navMark").append(dots);
    artwork.mark(dots, { size: 26, eyes: false });
    artwork.aura(get("navGlow"), { palette: "psb", round: true, bands: 0, fps: 12, seed: 3 });
  }

  const hint = get("hintCycle");
  let hintIndex = 0;
  let hintChange = 0;
  setInterval(() => {
    if (document.hidden || input.value || reduced.matches || busy) return;
    hint.classList.add("out");
    hintChange = setTimeout(() => {
      hintIndex = (hintIndex + 1) % hints.length;
      hint.textContent = hints[hintIndex];
      hint.classList.remove("out");
      hintChange = 0;
    }, 360);
  }, 3200);
  reduced.addEventListener("change", () => {
    clearTimeout(hintChange);
    hint.classList.remove("out");
  });

  function swapExamples() {
    set = (set + 1) % sets.length;
    const buttons = sets[set].map(([label, question]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chip suggestion";
      button.textContent = label;
      button.dataset.question = question;
      return button;
    });
    starters.replaceChildren(...buttons);
    starters.classList.remove("out");
    shuffle.removeAttribute("aria-disabled");
  }

  starters.addEventListener("click", (event) => {
    const starter = event.target.closest("[data-question]");
    if (!starter || busy) return;
    submitExample(starter.dataset.question);
  });
  shuffle.addEventListener("click", () => {
    if (shuffle.hasAttribute("aria-disabled")) return;
    if (reduced.matches) {
      swapExamples();
      return;
    }
    shuffle.setAttribute("aria-disabled", "true");
    starters.classList.add("out");
    setTimeout(swapExamples, 300);
  });

  function chatCover(id) {
    if (!artwork || covers.has(id)) return covers.get(id);
    let seed = 0;
    for (const char of id) seed = Math.imul(seed, 31) + char.charCodeAt(0) | 0;
    seed = Math.abs(seed);
    const url = artwork.auraImage(["psb", "soft", "night"][seed % 3], seed % 17, true);
    if (covers.size >= 20) covers.delete(covers.keys().next().value);
    covers.set(id, url);
    return url;
  }

  function renderRecent() {
    const chats = !menu.hidden && recentLayout.matches ? readHistory().slice(0, 4) : [];
    flyout.hidden = !chats.length;
    flyout.replaceChildren();
    if (!chats.length) return;
    const heading = document.createElement("p");
    heading.className = "caption flyout__head";
    heading.textContent = "Недавние";
    flyout.append(heading);
    for (const chat of chats) {
      const button = document.createElement("button");
      button.className = "flyout__item";
      button.type = "button";
      button.disabled = busy;
      const cover = document.createElement("span");
      cover.className = "flyout__cover";
      const url = chatCover(chat.id);
      if (url) cover.style.backgroundImage = `url("${url}")`;
      const text = document.createElement("span");
      text.className = "flyout__text";
      const title = document.createElement("span");
      title.className = "flyout__title";
      title.textContent = chat.title;
      text.append(title);
      const updated = new Date(chat.updated);
      if (Number.isFinite(updated.getTime())) {
        const date = document.createElement("time");
        date.className = "caption";
        date.dateTime = updated.toISOString();
        date.textContent = timeFormat.format(updated);
        text.append(date);
      }
      button.append(cover, text);
      button.addEventListener("click", () => {
        setMenu(false, true);
        openConversation(chat);
      });
      flyout.append(button);
    }
  }

  function setMenu(open, focus = false) {
    menu.hidden = !open;
    nav.setAttribute("aria-expanded", String(open));
    renderRecent();
    if (focus) (open ? menu.querySelector(":enabled") : nav)?.focus({ preventScroll: true });
  }

  nav.addEventListener("click", (event) => setMenu(menu.hidden, event.detail === 0));
  menu.addEventListener("click", (event) => {
    if (event.target.closest(".menu__item")) setMenu(false, true);
  }, true);
  menu.addEventListener("keydown", (event) => {
    const items = [...menu.querySelectorAll(".menu__item:enabled")];
    const index = items.indexOf(document.activeElement);
    const next = { ArrowDown: (index + 1) % items.length, ArrowUp: (index - 1 + items.length) % items.length,
      Home: 0, End: items.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    items[next]?.focus();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!menu.hidden && !menu.contains(event.target) && !nav.contains(event.target) && !flyout.contains(event.target)) setMenu(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) setMenu(false, true);
  });
  recentLayout.addEventListener("change", renderRecent);

  return {
    refreshRecent() {
      if (!menu.hidden) renderRecent();
    },
    setBusy(active) {
      if (busy === active) return;
      busy = active;
      document.body.classList.toggle("busy", active);
      mascot?.mood(active ? "thinking" : "idle");
      if (!menu.hidden) renderRecent();
    },
    revealAnswer(rich) {
      if (reduced.matches) return;
      const walker = document.createTreeWalker(rich, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          return node.nodeValue.trim() && !node.parentElement.closest("button, pre, code")
            ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
        },
      });
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      let word = 0;
      for (const node of nodes) {
        const part = document.createDocumentFragment();
        for (const token of node.nodeValue.match(/\S+|\s+/g) || []) {
          if (!token.trim()) {
            part.append(document.createTextNode(token));
            continue;
          }
          const span = document.createElement("span");
          span.className = "w";
          span.textContent = token;
          span.style.animationDelay = Math.min(word++ * .022, 1.6) + "s";
          part.append(span);
        }
        node.replaceWith(part);
      }
    },
  };
}
