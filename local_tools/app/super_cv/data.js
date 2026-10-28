// Seed data for the CV Tag Matcher prototype.
//
// Everything is inlined here (instead of fetched from a file) so index.html
// works when opened directly via file:// with no local server needed.
//
// Intentionally holds only TEMPLATE placeholder examples (one Experience
// bullet + its Tools & Skills tag, one Language, one Interest), not any
// real person's CV content -- this file previously held one specific
// person's real data, which doesn't belong committed alongside the tool's
// own frontend/backend code. Replace the placeholders below (anything in
// `<angle brackets>`) with real data to use the tool for yourself; delete
// an example entirely if you don't want it. Everything else (app.js,
// chat.js, bridge.js, reminders.js, bridge_server.py, resize.js,
// style.css, index.html) works unchanged regardless of what's seeded here.

// SUPER_CV_TAGS: one tag per line/item of a CV -- the full thing, not a
// cherry-picked subset. `role` is the group a tag belongs to -- app.js
// renders one shared header per contiguous run of the same role in the
// right board, rather than repeating it on every card.
//
// Shape per item: { role, text, level?, place?, period?, section? }
//   - `level`: "strong" | "neutral" -- only Interests tags carry this (see
//     app.js -> tagEl's hasLevel branch).
//   - `place`/`period`: only Education tags carry their own (per-entry,
//     since several degrees can share the "Education" role/header -- see
//     app.js -> eduRowEl). A job position's place/period instead lives
//     role-wide in ROLE_META below.
//   - `section: "tools"`: marks a job position's own "Tools & Skills"
//     subsection tags (see app.js -> appendGroup) -- kept as part of the
//     SAME contiguous run as that role's bullets, not a separate role.
const SUPER_CV_TAGS = [
  // --- Example experience (placeholder -- add more bullets/roles, or
  // delete this one, the same way you'd edit any other tag/group) ---
  { role: "<Company> · <Role>", text: "<Accomplishment — what you did and the impact it had>" },
  { role: "<Company> · <Role>", section: "tools", text: "<tool>" },

  // --- Example language ---
  { role: "Languages", text: "<Language>: <proficiency, e.g. native / fluent / conversational>" },

  // --- Example interest ---
  { role: "Interests", text: "<Interest>", level: "neutral" },
];

// ROLE_META: place + time period shown as small non-draggable tags on the
// right side of a group header's own line (app.js -> groupHeaderEl), for
// job positions only -- every other role (Interests, Languages, Education,
// Patents, ...) has no entry here and just renders its plain header,
// unchanged. Education's place/period live per-item instead (see the
// Education tags above and app.js -> eduRowEl) since its several degrees
// all share one "Education" role/header, unlike a job position. A role's
// presence here is also what app.js uses to decide it's a job position at
// all -- e.g. whether it gets the "Tools & Skills" subsection (see
// app.js -> appendGroup).
const ROLE_META = {
  "<Company> · <Role>": { place: "<City, Country>", period: "<yyyy-mm> – <yyyy-mm>" },
};
