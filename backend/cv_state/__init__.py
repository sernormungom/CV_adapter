"""The CV editor's two storage identities (V2 Slice 3, `docs/DECISIONS.md` P-009).

- `service` — the **base** CV artifact: the consultant's own working CV, stored
  as `data/profiles/<username>/cv_state.json`, now carrying its own revision and
  content hash.
- `routes` — `/api/cv/base/{username}`, the base half of the editor's explicit
  mode routing. The Application half is `backend/tailored_cv/`.

The shared vocabulary (`CVArtifact`, `canonical_json`, `content_hash`) lives in
`service` and is used by both halves, so a base artifact and a tailored artifact
are the same shape to a client and only their address differs.
"""
