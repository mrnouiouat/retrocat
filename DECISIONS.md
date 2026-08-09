# DECISIONS.md — design calls made while building retrocat

Running log of design decisions and deviations from `RETROCAT-HANDOFF.md`
(which lives in the source repo, not here). Newest entries at the bottom of
each phase section. Kept per the handoff's Authority section: this is the
record the owner reads instead of re-deriving the reasoning.

## Phase 1 — skeleton (2026-08-08)

- **Cloned the existing GitHub repo** rather than `git init`, per the amended
  handoff. Branch `main`; the placeholder 35-byte `README.md` stays until
  Phase 8 overwrites it.
- **LICENSE copyright holder is the GitHub handle `thefirstsamurai`**, not a
  legal name — no confirmed real name was available to the build session, and
  guessing one from an email address seemed worse than the handle. Swap in a
  real name whenever; one-line change.
- `.gitignore` carried over from the source repo verbatim (the handoff calls
  it correct). `output/` and `.cache/` stay ignored for the same reasons as
  the source repo: regenerable vs. precious-but-local.
- `pyproject.toml` includes a `[tool.setuptools.package-data]` entry for
  `retrocat/data/*.toml` ahead of Phase 5, where the class-fallback subject
  mapping becomes a shipped, overridable data file.
- Version starts at `0.1.0` (the source repo says `1.0.0`, but that number
  described the *internal* tool's maturity; the generalized package has not
  yet earned it).
