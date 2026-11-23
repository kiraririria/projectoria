const headingPattern = /^\s*(#{1,6})\s+(.+)$/;
const listPattern = /^\s*(?:([-+*])|(\d{1,5})[.)])\s+(.+)$/;
const fencePattern = /^\s*(`{3,}|~{3,})/;
const thematicBreak = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;

// Every string from the model is inserted as text. Citation controls come from the API allowlist.
function appendInline(parent, text, citations, allowEmphasis = true) {
  const tokens = /(?<code>`[^`\n]+`)|(?<citation>\[S\d+\])|(?<link>\[[^\]\n]+\]\([^\)\n]+\))|(?<strong>\*\*[^*\n]+\*\*|__[^_\n]+__)|(?<emphasis>\*[^*\n]+\*)/g;
  let offset = 0;
  for (const match of text.matchAll(tokens)) {
    const { code, citation, link, strong, emphasis } = match.groups;
    let prefix = text.slice(offset, match.index);
    const citationButton = citation && citations.get(citation.slice(1, -1));
    // Keep small footnotes next to the preceding word on narrow screens.
    const anchor = citationButton && prefix.match(/(?:^|\s)(\S{1,22}\s*)$/);
    if (anchor) prefix = prefix.slice(0, -anchor[1].length);
    parent.append(document.createTextNode(prefix));
    if (anchor) {
      const group = document.createElement("span");
      group.className = "citation-anchor";
      group.append(document.createTextNode(anchor[1]), citationButton());
      parent.append(group);
    } else if (citationButton) {
      parent.append(citationButton());
    } else if (code) {
      const node = document.createElement("code");
      node.textContent = code.slice(1, -1);
      parent.append(node);
    } else if (link) {
      parent.append(document.createTextNode(link.slice(1, link.indexOf("]("))));
    } else if (allowEmphasis && (strong || emphasis)) {
      const trim = strong ? 2 : 1;
      const node = document.createElement(strong ? "strong" : "em");
      appendInline(node, match[0].slice(trim, -trim), citations, false);
      parent.append(node);
    } else {
      parent.append(document.createTextNode(match[0]));
    }
    offset = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}

function addBlock(parent, tag, text, citations) {
  const node = document.createElement(tag);
  appendInline(node, text, citations);
  parent.append(node);
  return node;
}

function splitCells(line) {
  return line.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
}

function tableStartsAt(lines, index) {
  if (!lines[index]?.includes("|") || !lines[index + 1]?.includes("|")) return false;
  const divider = splitCells(lines[index + 1]);
  return divider.length === splitCells(lines[index]).length && divider.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function appendTable(parent, lines, index, citations) {
  const wrapper = document.createElement("div");
  wrapper.className = "table-wrap";
  const table = document.createElement("table");
  const header = table.createTHead().insertRow();
  const columns = splitCells(lines[index]);
  for (const cell of columns) {
    const th = addBlock(header, "th", cell, citations);
    th.scope = "col";
  }
  const body = table.createTBody();
  index += 2;
  while (lines[index]?.includes("|") && splitCells(lines[index]).length === columns.length) {
    const row = body.insertRow();
    for (const cell of splitCells(lines[index++])) addBlock(row, "td", cell, citations);
  }
  wrapper.append(table);
  parent.append(wrapper);
  return index;
}

function isParagraphContinuation(lines, index) {
  const line = lines[index];
  return line?.trim() && !headingPattern.test(line) && !listPattern.test(line) && !fencePattern.test(line)
    && !thematicBreak.test(line) && !/^\s*>/.test(line) && !tableStartsAt(lines, index);
}

export function renderMarkdown(text, citations = new Map()) {
  const result = document.createDocumentFragment();
  const lines = text.split(/\r?\n/);
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const heading = line.match(headingPattern);
    const listItem = line.match(listPattern);
    const fence = line.match(fencePattern)?.[1];

    if (!line.trim()) {
      ++index;
    } else if (fence) {
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      const closing = new RegExp(`^\\s*${fence[0]}{${fence.length},}\\s*$`);
      const content = [];
      while (++index < lines.length && !closing.test(lines[index])) content.push(lines[index]);
      if (index < lines.length) ++index;
      code.textContent = content.join("\n");
      pre.append(code);
      result.append(pre);
    } else if (heading) {
      const level = Math.max(2, Math.min(4, heading[1].length));
      addBlock(result, `h${level}`, heading[2].replace(/\s+#+\s*$/, ""), citations);
      ++index;
    } else if (thematicBreak.test(line)) {
      result.append(document.createElement("hr"));
      ++index;
    } else if (tableStartsAt(lines, index)) {
      index = appendTable(result, lines, index, citations);
    } else if (listItem) {
      const ordered = Boolean(listItem[2]);
      const list = document.createElement(ordered ? "ol" : "ul");
      if (ordered) list.start = Number(listItem[2]);
      let item = listItem;
      while (item && Boolean(item[2]) === ordered) {
        addBlock(list, "li", item[3], citations);
        item = lines[++index]?.match(listPattern);
      }
      result.append(list);
    } else if (/^\s*>/.test(line)) {
      const quote = [];
      while (/^\s*>/.test(lines[index] ?? "")) quote.push(lines[index++].replace(/^\s*>\s?/, ""));
      addBlock(result, "blockquote", quote.join(" "), citations);
    } else {
      const paragraph = [line.trim()];
      while (isParagraphContinuation(lines, ++index)) paragraph.push(lines[index].trim());
      addBlock(result, "p", paragraph.join(" "), citations);
    }
  }
  return result;
}
