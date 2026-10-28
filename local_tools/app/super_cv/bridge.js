// Claude bridge — talks to bridge_server.py (see that file's own header
// comment for the full design) to get a job ad's requirements turned into
// REAL Claude-authored multiple-choice questions instead of a heuristic.
// Nothing here calls an LLM directly: this just POSTs the job ad text to a
// session file on disk, polls until a human has told Claude Code (in a
// separate chat) to read that file and write real questions back into it,
// then hands the resulting {label, __qid} items to
// window.ChatAssistant.startFlow — the same per-question UX chat.js already
// had. Requires bridge_server.py to be running (see README.md); if it's
// not, every request below just fails and says so in the chat.
//
// Session shape (see bridge_server.py for the authoritative schema):
//   - rounds[]: { questions: [{id, text, parentId?}], answers, note? }
//     A question with `parentId` set is a follow-up sub-question Claude
//     Code drafted after a "not sure"/"other" answer on the question whose
//     id is `parentId` -- rendered exactly like any other question here,
//     since the standard option set already covers it (see chat.js).
//     `note`, if present, is Claude Code's own commentary (the requirement
//     explained, what it found in the CV) shown before that round's
//     questions -- the real version of what an old canned "explain & check
//     my CV" reply used to fake locally.
//   - resolutions[]: append-only. Each {id, level, cvText?} is Claude
//     Code's final call on a top-level question that came back "not
//     sure"/"other", made once it has enough follow-up answers to decide
//     -- applied here the same way a direct extensive/some/learning/no
//     answer would be (immediately, no extra confirmation step), since by
//     this point the user has already engaged with follow-up questions.

const BRIDGE_POLL_MS = 2500;

let session = null; // { id, roundsRendered, resolutionsApplied, questionLabels: Map, active }

async function bridgeApiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.error || `${path} failed (${res.status})`);
  }
  return res.json();
}

async function bridgeApiGet(path) {
  const res = await fetch(path);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.error || `${path} failed (${res.status})`);
  }
  return res.json();
}

function setJobAdZoneEnabled(enabled) {
  document.getElementById("jobAdInput").disabled = !enabled;
  document.getElementById("jobAdAnalyzeBtn").disabled = !enabled;
}

function setStopButtonVisible(visible) {
  document.getElementById("bridgeStopBtn").style.display = visible ? "" : "none";
}

async function submitJobAd(jobAdText) {
  setJobAdZoneEnabled(false);
  // Also disables the chat's reminder chips (reminders.js) for the
  // duration -- one bridge request of either kind in flight at a time,
  // whether this review started from a pasted job ad or from the
  // market-skill-scan reminder chip calling this same function directly.
  setRemindersEnabled(false);
  setStopButtonVisible(true);
  postBotText("Saving this job ad for Claude…");
  let id;
  try {
    const cvSnapshot = window.Boards.getRightTags();
    ({ id } = await bridgeApiPost("/api/sessions", { jobAdText, cvSnapshot }));
  } catch (err) {
    postBotText(
      `Couldn't reach the bridge server (${err.message}). Is bridge_server.py running? See README.md → "Claude bridge".`
    );
    endSession();
    return;
  }
  session = { id, roundsRendered: 0, resolutionsApplied: 0, questionLabels: new Map(), active: true };
  postBotText(
    'Saved. Tell Claude Code in your chat session to forward this job ad — the first round of questions will show up here automatically once it does.'
  );
  pollLoop();
}

async function pollLoop() {
  if (!session || !session.active) return;

  let data;
  try {
    data = await bridgeApiGet(`/api/sessions/${session.id}`);
  } catch (err) {
    console.error("bridge poll failed:", err);
    setTimeout(pollLoop, BRIDGE_POLL_MS);
    return;
  }
  if (!session || !session.active) return; // stopped while the request was in flight

  if (data.status === "cancelled") {
    postBotText("This job-ad review was stopped.");
    endSession();
    return;
  }

  // Apply any new resolutions regardless of what else this poll finds --
  // Claude Code may resolve a previously "not sure" item in the same
  // response that also introduces a fresh round, or on its own.
  applyNewResolutions(data);

  if (data.rounds.length > session.roundsRendered) {
    renderRound(data.rounds[session.roundsRendered]);
    return; // paused until that round's answers are posted back (see onRoundAnswered)
  }

  if (data.status === "done") {
    postBotText(data.conclusion && data.conclusion.summary ? data.conclusion.summary : "Done reviewing this job ad.");
    endSession();
    return;
  }

  // "pending" or "awaiting_claude" -- still waiting on a human to relay this
  // round to Claude Code. Keep polling quietly; no need to repeat the
  // "waiting" message every tick.
  setTimeout(pollLoop, BRIDGE_POLL_MS);
}

function renderRound(round) {
  session.roundsRendered += 1;
  // Prefer the short job-ad requirement (if given) for the label used in
  // resolution messages later -- Claude's own `text` is a full explanation
  // + question, too long to read well inline in a one-line chat message.
  round.questions.forEach((q) => session.questionLabels.set(q.id, q.requirement || q.text));
  if (round.note) postBotText(round.note);
  const items = round.questions.map((q) => ({ label: q.text, requirement: q.requirement, __qid: q.id }));
  window.ChatAssistant.startFlow(items, undefined, onRoundAnswered);
}

// resolutions[] is append-only, so anything past what's already been
// applied is new. A resolution's `id` always refers to a top-level
// requirement question (never a follow-up sub-question) -- see the
// file-level note.
function applyNewResolutions(data) {
  const resolutions = data.resolutions || [];
  for (let i = session.resolutionsApplied; i < resolutions.length; i++) {
    const r = resolutions[i];
    const label = session.questionLabels.get(r.id) || r.id;
    if (r.level === "extensive" || r.level === "some") {
      const bulletText = r.cvText || label;
      const newTag = {
        id: `cv-${Math.random().toString(36).slice(2, 9)}`,
        kind: "cv",
        role: r.level === "extensive" ? "Added from job ad — extensive" : "Added from job ad — some experience",
        text: bulletText,
        selected: false,
      };
      window.Boards.insertRightTag(newTag, window.Boards.getRightTags().length);
      postBotText(`Claude resolved "${label}" — added to your Super CV: "${bulletText}"`);
    } else {
      postBotText(`Claude resolved "${label}" — ${r.level === "learning" ? "training candidate, not added" : "not applicable, nothing added"}.`);
    }
  }
  session.resolutionsApplied = resolutions.length;
}

async function onRoundAnswered(resolved) {
  const answers = resolved.map((r) => {
    const a = { id: r.__qid, level: r.level };
    if (r.otherText != null) a.otherText = r.otherText;
    return a;
  });
  try {
    await bridgeApiPost(`/api/sessions/${session.id}/answers`, { answers });
  } catch (err) {
    postBotText(`Couldn't save your answers to the bridge (${err.message}). Is bridge_server.py still running?`);
    return;
  }
  postBotText("Saved your answers. Tell Claude Code in your chat session to forward them, and I'll continue.");
  pollLoop();
}

async function stopSession() {
  if (!session) return;
  const id = session.id;
  endSession(); // stop polling immediately, don't wait on the network
  try {
    await bridgeApiPost(`/api/sessions/${id}/cancel`, {});
  } catch (err) {
    console.error("bridge cancel failed:", err);
  }
  postBotText("Stopped.");
}

function endSession() {
  session = null;
  setJobAdZoneEnabled(true);
  setRemindersEnabled(true);
  setStopButtonVisible(false);
}

document.addEventListener("DOMContentLoaded", () => {
  const jobAdInput = document.getElementById("jobAdInput");
  const jobAdAnalyzeBtn = document.getElementById("jobAdAnalyzeBtn");
  const stopBtn = document.getElementById("bridgeStopBtn");

  function runAnalysis() {
    // One review at a time -- including a reminder chip's request, either
    // kind (see reminders.js).
    if (session || activeReminder) return;
    const text = jobAdInput.value.trim();
    if (!text) return;
    jobAdInput.value = "";
    submitJobAd(text);
  }

  jobAdAnalyzeBtn.addEventListener("click", runAnalysis);
  // A native external-text drop (e.g. a selection dragged in from another
  // tab) is inserted into the textarea by the browser's own default drop
  // action, which runs AFTER this listener finishes -- reading .value
  // synchronously here would miss the text that was just dropped, so defer
  // to the next tick.
  jobAdInput.addEventListener("drop", () => setTimeout(runAnalysis, 0));

  stopBtn.addEventListener("click", stopSession);
  setStopButtonVisible(false);
});
