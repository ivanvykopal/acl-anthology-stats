# ACL Anthology Statistics Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the single-volume EMNLP 2026 stats page into a static multi-volume dashboard with author publication panels and per-conference trends.

**Architecture:** `stats.py` precomputes one JSON per Anthology volume plus an `index.json`; a single-file vanilla-JS frontend with sidebar + tabs renders it. No framework, no build step, Python 3 stdlib only.

**Tech Stack:** Python 3 (stdlib), single-file HTML/CSS/JS.

**Spec:** `docs/superpowers/specs/2026-10-26-acl-stats-site-design.md`

## Global Constraints

- Python stdlib only (no PyYAML/requests); XML from `xml.etree.ElementTree`.
- Frontend: no framework, no build step, no external assets/CDNs.
- Anthology URL format: `https://aclanthology.org/{collection_id}-{volume_id}.{paper_id}`.
- Author name = `first` + `last`, stripped; skip empty names.
- Light/dark via `prefers-color-scheme` and CSS custom properties.
- No git repo exists in the project — skip all commit steps (note in delivery).

## Review Focus

1. Author with `first` missing (org-only names like "Adobe Research") → name must not get a leading space; expect `last` only.
2. Same person spelled slightly differently across papers → treated as distinct authors (accepted limitation; must not crash).
3. Empty-name author elements (missing first AND last) → skipped, never "" in output.
4. Volume with 0 papers → `stats.py` must not divide by zero (`avg_authors_per_paper`); skip with warning.
5. Author appearing in current filter with 10k+ rows → table render must stay sliced at 2000.

---

### Task 1: Multi-volume build in `stats.py`

**Files:**
- Modify: `stats.py` (full rewrite)

**Interfaces:**
- Consumes: `xml/<id>.xml` (downloaded on demand from acl-anthology raw GitHub).
- Produces:
  - `VOLUMES: dict[str, list[str]]` — conference → volume IDs, e.g. `{"EMNLP": ["2026.emnlp", "2025.emnlp", "2024.emnlp", "2023.emnlp", "2022.emnlp", "2021.emnlp", "2020.emnlp"], "ACL": ["2025.acl", "2024.acl", "2023.acl", "2022.acl", "2021.acl", "2020.acl"], "NAACL": ["2025.naacl", "2024.naacl", "2022.naacl", "2021.naacl"], "COLING": ["2025.coling", "2022.coling", "2020.coling"]}`.
  - `build_volume(vol_id: str, xml_dir: Path, out_dir: Path, force: bool) -> dict | None` — returns the index entry `{id, conference, year, papers, unique_authors, avg_authors_per_paper}` or `None` on skip/failure.
  - `main()` — CLI: `python stats.py [--force] [volume_id ...]`; no volume args = all of VOLUMES; writes `data/index.json` last, sorted by (conference, year desc).
- Per-volume JSON schema exactly as in spec section "Data pipeline": keys `id, conference, year, title, papers, authors, stats`; `papers[i] = {title, url, authors}`; `authors[j] = {name, papers: [int], first_author: [int], coauthors: {str: int}}`; `stats` keys `papers, unique_authors, author_slots, avg_authors_per_paper, single_author_papers, papers_per_author_dist, authors_per_paper_dist` (dist keys are strings).
- `title`: derive from volume `<title>` element text if present, else `f"{conference} {year}"`.
- `conference`/`year`: parsed from volume ID (`"2026.emnlp"` → `("EMNLP", 2026)`, uppercased venue part).

- [ ] **Step 1: Implement the rewrite**

Details beyond spec:
- Download cached at `xml/<id>.xml` from `https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/<id>.xml`; on `URLError` print warning, return `None`.
- Rebuild skip: if `data/<id>.json` exists and is newer than the XML and not `force`, read its stats header for the index entry and skip parsing.
- `avg_authors_per_paper = 0` when 0 papers; print warning and return `None` if no papers at all.
- Keep old `2026.emnlp.xml` at repo root working: on startup, if `2026.emnlp.xml` exists at root and `xml/2026.emnlp.xml` does not, move it into `xml/`.
- Old `authors.json` at root: delete after successful build of 2026.emnlp (obsolete path — the frontend moves to `data/`).

- [ ] **Step 2: Run and verify**

Run: `python stats.py 2026.emnlp 2025.emnlp`
Expected: downloads missing XML, prints per-volume `N authors, M papers -> data/<id>.json`, writes `data/index.json` with 2 entries.

- [ ] **Step 3: Sanity-check output**

Run:
```python
import json
d = json.load(open("data/2026.emnlp.json", encoding="utf-8"))
assert d["stats"]["papers"] == len(d["papers"])
assert all(a["name"].strip() == a["name"] and a["name"] for a in d["authors"])
p = d["papers"][0]
assert p["url"].startswith("https://aclanthology.org/2026.emnlp-")
top = max(d["authors"], key=lambda a: len(a["papers"]))
print(d["stats"], top["name"], len(top["papers"]))
idx = json.load(open("data/index.json", encoding="utf-8"))
assert {e["id"] for e in idx} == {"2026.emnlp", "2025.emnlp"}
```
Expected: no assertion errors; printed URL prefix correct; counts plausible (EMNLP 2026 main ≈ thousands of papers).

- [ ] **Step 4: Verify zero-paper guard [Review Focus #4]**

Run: `python stats.py 1952.earlymt`
Expected: either builds fine with small numbers or warns and skips — no `ZeroDivisionError`, no traceback.

---

### Task 2: Dashboard frontend in `index.html`

**Files:**
- Modify: `index.html` (full rewrite)

**Interfaces:**
- Consumes: `data/index.json` and `data/<id>.json` per Task 1's schema.
- Produces: none (terminal task).

- [ ] **Step 1: Implement the page**

Layout per spec sections "Frontend"/"Sidebar"/"Main area"/"Author detail panel"/"Trends tab". Key decisions the spec leaves open, pinned here:
- State: `let INDEX = [], DATA = null (current volume), currentId, activeTab ("authors"|"trends"), selAuthor (name|null)`.
- Routing: `#<volumeId>` on load and `hashchange`; unknown hash → first index entry. `location.hash = id` when sidebar item clicked.
- Sidebar: group `INDEX` by `conference` (order = first appearance in index.json), years descending within group; `<button>` per volume; `.active` class on current.
- Authors tab: header (title, year, "source: ACL Anthology XML, <id>"), 6 stat cards from `stats`, authors-per-paper histogram (`stats.authors_per_paper_dist`, CSS flex bars, height ∝ value/max, axis labels = team size, bar tooltip/count label above), papers-per-author chips (kept from current page), controls (search `#q`, min papers `#min`, count), table columns: `#`, `Name`, `Papers`, `Co-authors` (= `Object.keys(a.coauthors).length`); all sortable incl. co-authors; slice 2000.
- Author panel: `<aside class="panel">` fixed right, slide-in via CSS transform; header + ✕ button; stats line (papers / first-authored / distinct co-authors); "Publications" list: each item = `<a href=url>title</a>`, badge "1st author" when index in `a.first_author`; "Top collaborators" list: `name — n papers`, sorted desc, top 20; open on author-name button click; close on ✕, Esc, or clicking outside panel; `document.activeElement` focus return. Selecting a different volume closes the panel.
- Trends tab: conference of current volume (from `DATA.conference`), `<select>` of all conferences; three bar charts (Papers, Unique authors, Avg authors/paper) with years ascending x-axis built from `INDEX.filter(e => e.conference === conf)`; values from index entries (they carry `papers, unique_authors, avg_authors_per_paper`).
- Errors: failed fetch of index.json → main area shows "Could not load data/index.json — run `python stats.py` first."; failed volume fetch → inline error, sidebar intact; empty filter → single row "No authors match."
- Styling: CSS custom properties (`--fg, --muted, --border, --surface, --accent`) with dark-scheme overrides; grid `grid-template-columns: 220px 1fr`; sticky header; tab bar with underline active state; cards, chips, table styled like current page but polished (spacing scale, border-radius 8px, tabular-nums, focus-visible outlines). Max content width ~1200px.
- Keep full UTF-8 author names (use `encoding="utf-8"` writes are Task 1; HTML already `<meta charset>`).

- [ ] **Step 2: Browser smoke test**

Serve: `python -m http.server 8000` in project root; open `http://localhost:8000/`.
Verify by clicking through:
- Sidebar shows EMNLP/ACL groups from Task 1's build (2 volumes); clicking switches content; URL hash updates; reload keeps selection.
- Cards, histogram bars, chips render with real numbers matching Task 1 Step 3 output.
- Search "the" and min-papers 2 both filter; count text updates; sort by Papers desc/asc works.
- Click an author name → panel slides in with their publications (links to aclanthology.org) and collaborators; ✕ and Esc close it.
- Trends tab shows the conference's per-year bars (2 years).
- Unknown hash `#bogus` → loads first index volume, no JS error.
- Empty-filter message [Review Focus #5 path] shows for a nonsense query.

- [ ] **Step 3: Dark-mode check**

Toggle OS dark mode (or `prefers-color-scheme` emulation in devtools); verify readable contrast, no hard-coded light colors leaking.

- [ ] **Step 4: Cleanup**

Delete `authors.json` at root (Task 1 handled) and any stale root `2026.emnlp.xml` (moved by Task 1). README not required. Update spec/plan checkboxes as complete.

## Self-Review Notes

- Spec coverage: pipeline (Task 1), sidebar/routing/layout/panel/trends/stats/errors (Task 2). All spec sections mapped.
- Type consistency: index entry fields (`papers, unique_authors, avg_authors_per_paper`) produced by `build_volume` are exactly what the Trends tab reads; author record fields (`papers, first_author, coauthors`) match panel/table usage.
- Review Focus #1–#3 pinned in Task 1 Steps 1+3; #4 in Task 1 Step 4; #5 in Task 2.
