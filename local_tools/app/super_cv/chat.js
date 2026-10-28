// Chat assistant for the CV Tag Matcher prototype.
//
// This file owns the per-round Q&A UX: when a batch of questions arrives
// (from bridge.js, which relays a real Claude-authored round through
// bridge_server.py -- see README.md's "Claude bridge" section), it renders
// them as a TAB WIDGET -- one tab per question, same shape as this VSCode
// plugin's own AskUserQuestion presentation: click any tab to jump to that
// question (including ones already answered, to change the answer),
// picking a plain option auto-advances to the next tab, and "Other" gets
// its own inline text box instead of a generic free-text escape hatch.
// Once every question in the round has an answer, it's submitted as one
// batch and the panel locks. This file does NOT decide what the questions
// ARE or resolve "not sure"/"other" answers itself -- that's bridge.js
// (relaying to/from Claude Code) and Claude Code's job respectively; this
// file just presents whatever it's given and reports back whatever the
// user picked, unresolved for "not sure"/"other".

const OPTIONS = [
  { key: "extensive", label: "Yes, extensively" },
  { key: "some", label: "Yes, some experience" },
  { key: "learning", label: "Not yet, learning it" },
  { key: "no", label: "No, not applicable" },
  { key: "unsure", label: "Not sure — explain & check my CV" },
  { key: "other", label: "Other — write your own answer" },
];

// The single active question panel, if any. Only one round is ever "in
// progress" at a time (bridge.js only starts a new one once the previous
// one's onBatchDone has fired), so this doesn't need to be a stack.
let PANEL = null; // { items, answers: Map, activeIndex, onBatchDone, submitted, els }

function chatEl(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

function scrollChatToBottom() {
  const m = document.getElementById("chatMessages");
  m.scrollTop = m.scrollHeight;
}

function postBotText(text) {
  const m = document.getElementById("chatMessages");
  m.appendChild(chatEl("div", "msg bot", text));
  scrollChatToBottom();
}

function postUserText(text) {
  const m = document.getElementById("chatMessages");
  m.appendChild(chatEl("div", "msg user", text));
  scrollChatToBottom();
}

function setChatStatus(text) {
  document.getElementById("chatStatus").textContent = text;
}

function setInputMode(mode, placeholder) {
  const input = document.getElementById("chatInput");
  input.disabled = mode === "disabled";
  input.placeholder =
    placeholder || (mode === "disabled" ? "Answer the questions above ↑" : "Type a message…");
  if (mode !== "disabled") input.focus();
}

// ---------- the tabbed question panel ----------

// items: [{ label, __qid }]. onBatchDone(resolved), if given, fires once
// every item in THIS batch has an answer -- resolved is
// [{ __qid, level, otherText? }], in the items' original order (not answer
// order, since tabs can be answered out of order). "unsure"/"other" entries
// are NOT resolved to a final extensive/some/learning/no level here; that's
// Claude Code's call, made in a later round (see bridge.js).
function startFlow(items, dropIndex, onBatchDone) {
  if (!items || !items.length) return;
  items.forEach((item) => (item.__dropIndex = dropIndex));

  const els = buildPanelDom(items.length);
  document.getElementById("chatMessages").appendChild(els.root);

  PANEL = {
    items,
    answers: new Map(), // __qid -> { level, otherText? }
    activeIndex: 0,
    onBatchDone: onBatchDone || null,
    submitted: false,
    els,
  };

  renderTabs();
  renderActiveQuestion();
  setInputMode("disabled");
  scrollChatToBottom();
}

function buildPanelDom(count) {
  const root = chatEl("div", "qpanel");
  const tabs = chatEl("div", "qpanel-tabs");
  for (let i = 0; i < count; i++) {
    const tab = chatEl("button", "qpanel-tab", String(i + 1));
    tab.type = "button";
    tab.addEventListener("click", () => {
      if (!PANEL || PANEL.submitted) return;
      PANEL.activeIndex = i;
      renderTabs();
      renderActiveQuestion();
    });
    tabs.appendChild(tab);
  }
  root.appendChild(tabs);

  const body = chatEl("div", "qpanel-body");
  root.appendChild(body);

  return { root, tabs, body };
}

function renderTabs() {
  const { items, answers, activeIndex, els } = PANEL;
  [...els.tabs.children].forEach((tab, i) => {
    tab.classList.toggle("active", i === activeIndex);
    tab.classList.toggle("answered", answers.has(items[i].__qid));
  });
}

function renderActiveQuestion() {
  const { items, answers, activeIndex, els, submitted } = PANEL;
  const item = items[activeIndex];
  const existing = answers.get(item.__qid);
  els.body.innerHTML = "";

  els.body.appendChild(chatEl("div", "qpanel-progress", `Question ${activeIndex + 1} of ${items.length}`));
  // item.requirement is the job ad's own bullet (quoted/closely paraphrased)
  // this question is checking -- shown up front so it's always clear which
  // line is being asked about, not folded away into prose. item.label is
  // Claude Code's own explanation + the actual ask, grounded in both the
  // job ad and this CV -- rendered verbatim below it, not wrapped in a
  // generic "do you have this experience?" template (see bridge_server.py's
  // schema docs for what's expected in each).
  if (item.requirement) {
    els.body.appendChild(chatEl("div", "qpanel-requirement", `Requirement: "${item.requirement}"`));
  }
  els.body.appendChild(chatEl("div", "qpanel-question", item.label));

  const optList = chatEl("div", "qpanel-options");
  OPTIONS.forEach((opt) => {
    const btn = chatEl("button", "qpanel-opt", opt.label);
    btn.type = "button";
    if (existing && existing.level === opt.key) btn.classList.add("chosen");
    btn.disabled = submitted;
    btn.addEventListener("click", () => {
      // Mark the clicked option chosen immediately, not just once an answer
      // is finalized -- extensive/some/other don't finalize until the
      // inline text box below is submitted (or skipped), so without this
      // the button gave no feedback at all for the whole time it was
      // waiting on that input, which read as "my click did nothing."
      [...optList.children].forEach((b) => b.classList.remove("chosen"));
      btn.classList.add("chosen");
      chooseOption(item, opt.key, extraRow);
    });
    optList.appendChild(btn);
  });
  els.body.appendChild(optList);

  const extraRow = chatEl("div", "qpanel-extra");
  els.body.appendChild(extraRow);

  // Reopen whichever inline text box applies if this question was already
  // answered as extensive/some/other, pre-filled with what was typed --
  // lets the user see and revise it, not just the button choice.
  if (existing && (existing.level === "extensive" || existing.level === "some")) {
    showDetailInput(item, existing.level, extraRow, existing.otherText || "");
  } else if (existing && existing.level === "other") {
    showOtherInput(item, extraRow, existing.otherText || "");
  }
}

function chooseOption(item, key, extraRow) {
  if (PANEL.submitted) return;
  extraRow.innerHTML = "";
  if (key === "extensive" || key === "some") {
    showDetailInput(item, key, extraRow, "");
  } else if (key === "other") {
    showOtherInput(item, extraRow, "");
  } else {
    // learning / no / unsure -- clear-cut, nothing more to collect.
    finalizeAnswer(item, { level: key });
  }
}

function showDetailInput(item, level, extraRow, prefill) {
  extraRow.innerHTML = "";
  extraRow.appendChild(chatEl("div", "qpanel-hint", "Quick detail (optional) — what project or example shows this?"));
  const input = document.createElement("input");
  input.type = "text";
  input.className = "qpanel-input";
  input.placeholder = "Type a detail, or press Enter to skip…";
  input.value = prefill;
  input.disabled = PANEL.submitted;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      finalizeAnswer(item, { level, otherText: input.value.trim() || undefined });
    }
  });
  extraRow.appendChild(input);
  if (!PANEL.submitted) input.focus();
}

function showOtherInput(item, extraRow, prefill) {
  extraRow.innerHTML = "";
  extraRow.appendChild(chatEl("div", "qpanel-hint", "Write your own answer:"));
  const input = document.createElement("input");
  input.type = "text";
  input.className = "qpanel-input";
  input.placeholder = "Type your answer, then press Enter…";
  input.value = prefill;
  input.disabled = PANEL.submitted;
  const errorEl = chatEl("div", "qpanel-error");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) {
        errorEl.textContent = "Write at least a few words, or pick a different option above.";
        return;
      }
      finalizeAnswer(item, { level: "other", otherText: text });
    }
  });
  extraRow.appendChild(input);
  extraRow.appendChild(errorEl);
  if (!PANEL.submitted) input.focus();
}

function finalizeAnswer(item, answer) {
  const wasAnswered = PANEL.answers.has(item.__qid);
  PANEL.answers.set(item.__qid, answer);

  // A CV bullet is drafted the moment extensive/some is finalized, exactly
  // like the old sequential flow -- if the user comes back and changes
  // their answer for this question, remove the previous bullet (if any)
  // first so revisiting a tab doesn't pile up duplicates.
  if (item.__cvTagId) {
    window.Boards.removeRightTag(item.__cvTagId);
    item.__cvTagId = null;
  }
  if (answer.level === "extensive" || answer.level === "some") {
    // A CV line should read as a concise statement, not Claude Code's full
    // explanation paragraph -- prefer the short job-ad requirement when
    // there is one, only falling back to the full question text if a round
    // didn't include one (e.g. an older session).
    const base = item.requirement || item.label;
    const bulletText = answer.otherText ? `${base}: ${answer.otherText}` : base;
    const newTag = {
      id: `cv-${Math.random().toString(36).slice(2, 9)}`,
      kind: "cv",
      role: answer.level === "extensive" ? "Added from job ad — extensive" : "Added from job ad — some experience",
      text: bulletText,
      selected: false,
    };
    item.__cvTagId = newTag.id;
    const insertAt = item.__dropIndex != null ? item.__dropIndex : window.Boards.getRightTags().length;
    window.Boards.insertRightTag(newTag, insertAt);
  }

  renderTabs();
  setChatStatus(`${PANEL.answers.size}/${PANEL.items.length} answered`);

  const allAnswered = PANEL.items.every((it) => PANEL.answers.has(it.__qid));
  if (allAnswered) {
    submitPanel();
    return;
  }

  // Auto-advance: go to the next tab if there is one, otherwise (we just
  // finished answering the last tab out of order) jump to the first one
  // still unanswered.
  const { activeIndex, items } = PANEL;
  if (!wasAnswered || activeIndex === PANEL.activeIndex) {
    if (activeIndex + 1 < items.length) {
      PANEL.activeIndex = activeIndex + 1;
    } else {
      PANEL.activeIndex = items.findIndex((it) => !PANEL.answers.has(it.__qid));
    }
  }
  renderTabs();
  renderActiveQuestion();
}

function submitPanel() {
  PANEL.submitted = true;
  PANEL.els.root.classList.add("submitted");
  renderTabs();
  renderActiveQuestion(); // re-render to disable inputs/buttons on the visible tab
  setChatStatus("");
  setInputMode("idle");

  const resolved = PANEL.items.map((item) => {
    const a = PANEL.answers.get(item.__qid);
    return { __qid: item.__qid, level: a.level, otherText: a.otherText };
  });
  const onBatchDone = PANEL.onBatchDone;
  if (onBatchDone) onBatchDone(resolved);
}

// ---------- chat input (general chat only -- answering happens in the panel above) ----------

function handleChatSubmit() {
  const input = document.getElementById("chatInput");
  if (input.disabled) return;
  const text = input.value.trim();
  input.value = "";
  if (!text) return;
  postUserText(text);
  handleGeneralChat(text);
}

// General free chat when no question panel is active. Templated, not a
// real model call — see bridge.js/README.md for how real questions
// actually get authored.
function handleGeneralChat(text) {
  const lower = text.toLowerCase();
  let reply;
  if (/help|how.*work/.test(lower)) {
    reply =
      'Paste or drag a job ad\'s text onto the left panel, then hit "Analyze job ad" (dropping it in starts automatically). That saves it for Claude — tell me in our chat session to forward it, and the questions will show up here as tabs once you do.';
  } else {
    reply =
      'I only actively ask about a job ad\'s requirements once you drop or paste one on the left and it\'s been forwarded to Claude. Try that, or type "help".';
  }
  postBotText(reply);
}

document.addEventListener("DOMContentLoaded", () => {
  setInputMode("idle");
  document.getElementById("chatSendBtn").addEventListener("click", handleChatSubmit);
  document.getElementById("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleChatSubmit();
    }
  });

  postBotText(
    "Hi, I'm your CV Assistant. Paste or drag a job ad's text into the box on the left to get started."
  );
});

window.ChatAssistant = { startFlow };
