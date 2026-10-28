// "CV next-step reminders" -- shortcut prompts pinned above the chat log
// (see index.html's #chatReminders), each grounded in a specific
// step/section of copilots/job_agent/skills/super-cv-assistant/SKILL.md.
// Clicking one is a shortcut for typing that whole request yourself.
//
// Two different mechanisms, picked per reminder:
//   - "market-skill-scan" (marketScanPrompt set) reuses the EXISTING job-ad
//     bridge session (bridge.js -> submitJobAd/sess-<id>.json) -- it needs
//     the exact same multiple-choice-per-requirement tab widget "Analyze
//     job ad" already has, just comparing against a market-researched
//     skill keyword instead of a pasted job ad's bullet, so there's nothing
//     to reinvent: the crafted text below stands in for jobAdText, and
//     Claude Code reads it as instructions (not literal ad copy) the same
//     way it reads a real one.
//   - Every other reminder (prompt set) POSTs to a new
//     .bridge/reminder-<id>.json file instead (see bridge_server.py) -- a
//     one-shot prompt-in/free-text-response-out task, since what Claude
//     Code needs to do for these (re-sort superCV.md, check the negative
//     list) doesn't produce a per-item Q&A round the way a skill/requirement
//     gap-check does.
// Either way, only one bridge request -- of either kind -- is ever in
// flight at once (see the `session`/`activeReminder` cross-checks below).

const REMINDERS = [
  {
    key: "market-skill-scan",
    label: "Check CV vs hot job-market skills",
    title:
      "Search the job market for in-demand skills missing from my CV's Tools & Skills tags, then ask me about each one, same as Analyze job ad (SKILL.md Steps 1-4)",
    // Stands in for a pasted job ad's text (see submitJobAd) -- Claude Code
    // reads this as instructions, does the actual market research with its
    // own tools, then drafts one requirement-style question per missing
    // skill into this session's next round, exactly like a real job-ad
    // review would.
    marketScanPrompt:
      "Using the super-cv-assistant skill (Steps 1-4), find skills that are frequently required in the job market right now but are NOT already in any of my Super CV's per-role \"Tools & Skills\" tags (see cvSnapshot below for my current CV, restricted to job-position roles). Research this the same way internal/customer-searcher/CONTACT_SEARCH_AGENT.md's \"Required sources checklist\" gathers job-market signal: (1) Platsbanken via Arbetsförmedlingen's JobTech API (WebFetch https://jobsearch.api.jobtechdev.se/search?q=<role/title>&limit=20 -- not the arbetsformedlingen.se frontend), (2) a general job-ad WebSearch sweep across the major job boards (Indeed, LinkedIn Jobs, Monster, StepStone, etc.), and (3) LinkedIn, via WebSearch only, never scraped. For each skill that comes up as frequently requested across these sources but is missing from my CV tags, draft one multiple-choice question about it -- same format and same 6 options as a job-ad requirement question (extensive experience / some experience / learning it / not applicable / not sure -- explain and check my CV / other) -- and write it into this session's next round exactly like a real job-ad review would (same schema this file already uses). Once I answer, continue through Steps 3-4 of the skill as normal.",
  },
  {
    key: "resort-skills",
    label: "Re-sort skill/tool lists by demand",
    title: "Re-sort the Programming/Tools/Other skills and each role's Tools & Skills lists by job-market demand (SKILL.md Step 6)",
    prompt:
      "Using the super-cv-assistant skill's Step 6, re-sort the skill/tool category lists in my Super CV (superCV.md) -- the About chapter's Programming/Tools/Other skills lists and each Experience role's Tools & Skills list -- by real-world job-market demand, most commonly requested first. Only reorder, don't delete/reword/merge anything, and don't touch Education.",
  },
  {
    key: "resort-bullets",
    label: "Re-sort experience bullets by impact",
    title: "Re-sort each role's accomplishment bullets by significance, biggest impact first (SKILL.md Step 6)",
    prompt:
      "Using the super-cv-assistant skill's Step 6, re-sort the narrative accomplishment bullets under each Experience role in my Super CV (superCV.md) by significance -- large/strategic/high-impact work first, routine/detail tasks last. Only reorder, don't delete/reword/merge anything.",
  },
  {
    key: "review-negative-list",
    label: 'Review "not applicable" skills',
    title: 'Check superCV_negative.md for skills to reconsider now that I may have gained them (SKILL.md "Negative list")',
    prompt:
      "Check superCV_negative.md (the negative list beside my Super CV) for skills I previously marked not applicable, and ask me with AskUserQuestion whether any should be reconsidered now -- e.g. because I've since gained that skill -- then remove/update the entries I confirm, per the super-cv-assistant skill's Negative list section.",
  },
];

const REMINDER_POLL_MS = 2500;

let activeReminder = null; // { id } -- only one in flight at a time

async function remindersApiPost(path, body) {
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

async function remindersApiGet(path) {
  const res = await fetch(path);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.error || `${path} failed (${res.status})`);
  }
  return res.json();
}

function setRemindersEnabled(enabled) {
  document.querySelectorAll(".reminder-chip").forEach((btn) => {
    btn.disabled = !enabled;
  });
}

// `session` and `setJobAdZoneEnabled` are bridge.js's own -- classic
// <script> tags share one global scope (the same reasoning skillroles.js
// used to rely on for ROLE_META, and app.js's editHistory note calls out
// directly), so they're reachable here by bare name without any export.
// Only one bridge request of EITHER kind (a plain reminder, or a job-ad-
// style review -- whether started from the left column or from the
// market-skill-scan reminder below) is ever allowed in flight at once, so a
// plain reminder disables the left job-ad zone too, not just the other
// chips -- otherwise pasting a job ad while a reminder is pending would
// silently start a second, independent bridge request in parallel.
function setReminderUiEnabled(enabled) {
  setRemindersEnabled(enabled);
  setJobAdZoneEnabled(enabled);
}

async function triggerReminder(reminder) {
  if (activeReminder || session) return;
  postUserText(reminder.label);
  setReminderUiEnabled(false);

  let id;
  try {
    const cvSnapshot = window.Boards.getRightTags().map(({ role, text }) => ({ role, text }));
    ({ id } = await remindersApiPost("/api/reminders", {
      reminderKey: reminder.key,
      label: reminder.label,
      prompt: reminder.prompt,
      cvSnapshot,
    }));
  } catch (err) {
    postBotText(`Couldn't reach the bridge server (${err.message}). Is bridge_server.py running? See README.md → "Claude bridge".`);
    setReminderUiEnabled(true);
    return;
  }

  activeReminder = { id };
  postBotText("Saved. Tell Claude Code in your chat session to forward it — the answer will show up here once it does.");
  pollReminder();
}

async function pollReminder() {
  if (!activeReminder) return;
  const { id } = activeReminder;

  let data;
  try {
    data = await remindersApiGet(`/api/reminders/${id}`);
  } catch (err) {
    console.error("reminder poll failed:", err);
    setTimeout(pollReminder, REMINDER_POLL_MS);
    return;
  }
  if (!activeReminder || activeReminder.id !== id) return; // superseded/cleared while in flight

  if (data.status === "done") {
    postBotText(data.response || "Done — no summary was written back.");
    activeReminder = null;
    setReminderUiEnabled(true);
    return;
  }
  if (data.status === "cancelled") {
    postBotText("This request was cancelled.");
    activeReminder = null;
    setReminderUiEnabled(true);
    return;
  }

  setTimeout(pollReminder, REMINDER_POLL_MS);
}

// The market-skill-scan reminder doesn't create a reminder-<id>.json session
// at all -- it hands its crafted instructions to bridge.js's own
// submitJobAd (same function "Analyze job ad" calls), which runs it through
// the existing job-ad rounds/tab-widget UI. submitJobAd itself disables the
// reminder chips for the duration (see bridge.js), so there's nothing left
// to do here once it's kicked off.
function triggerMarketScan(reminder) {
  if (activeReminder || session) return;
  postUserText(reminder.label);
  submitJobAd(reminder.marketScanPrompt);
}

document.addEventListener("DOMContentLoaded", () => {
  const wrap = document.getElementById("chatReminders");
  if (!wrap) return;
  REMINDERS.forEach((reminder) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "reminder-chip";
    btn.textContent = reminder.label;
    btn.title = reminder.title;
    btn.addEventListener("click", () => {
      if (reminder.marketScanPrompt) triggerMarketScan(reminder);
      else triggerReminder(reminder);
    });
    wrap.appendChild(btn);
  });
});
