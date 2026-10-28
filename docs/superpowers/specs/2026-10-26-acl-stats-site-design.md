# ACL Anthology Statistics Site — Design

Date: 2026-10-26
Status: approved (in-chat), pending written review

## Goal

Turn the single-volume EMNLP 2026 author-stats page into a static, dashboard-style
site covering multiple ACL Anthology volumes, deployable anywhere (e.g. GitHub
Pages) with no server and no data-pipeline dependencies beyond Python 3 +
`defusedxml` (XML bomb protection for downloaded volume files).

## Data pipeline (`stats.py`)

- `VOLUMES` config in `stats.py`: dict of conference name → list of year/venue
  specs, e.g. `{"EMNLP": ["2026.emnlp", "2025.emnlp", ...], "ACL": [...]}`.
  Plain Python, no YAML dependency.
- For each volume ID `<year>.<venue>`:
  - XML at `xml/<year>.<venue>.xml`; downloaded from
    `https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/<id>.xml`
    if missing (cached, never re-downloaded).
  - Parse all `volume/paper` elements. A paper record:
    `title`, `url`, `authors: [full names]`.
  - Anthology URL: `https://aclanthology.org/{collection_id}-{volume_id}.{paper_id}`
    from XML attributes (`collection id="2026.emnlp"`, `volume id="main"`,
    `paper id="1"` → `2026.emnlp-main.1`). Verified against bibkey format.
  - Author name: `first` + `last`, stripped; empty names skipped.
- Per-volume output `data/<year>.<venue>.json`:
  ```json
  {
    "id": "2026.emnlp",
    "conference": "EMNLP",
    "year": 2026,
    "title": "EMNLP 2026 (main conference)",
    "papers": [{"title": "...", "url": "...", "authors": ["...", ...]}],
    "authors": [
      {"name": "...", "papers": [paper indices], "first_author": [paper indices],
       "coauthors": {"Name": count}}
    ],
    "stats": {
      "papers": N, "unique_authors": N, "author_slots": N,
      "avg_authors_per_paper": x.xx, "single_author_papers": N,
      "papers_per_author_dist": {"1": n, "2": n, ...},
      "authors_per_paper_dist": {"1": n, "2": n, ...}
    }
  }
  ```
  Papers stored once, referenced by index — keeps JSON small (no duplicated
  title/co-author strings per author).
- `data/index.json`: `[{id, conference, year, papers, unique_authors,
  avg_authors_per_paper}]` for all built volumes — powers sidebar and trends.
- CLI: `python stats.py [volume_id ...]`; no args = build all in `VOLUMES`
  (skips volumes whose JSON is newer than the XML unless `--force`).

## Frontend (`index.html`, single file, vanilla JS)

Dashboard layout, CSS grid: fixed sidebar + main area. Light/dark via
`prefers-color-scheme` and CSS custom properties; polished typography
(type scale, tabular numerals), sticky header, hover/focus states.

### Sidebar
- Lists conferences from `data/index.json`, grouped, years descending.
- Active volume highlighted; click loads `data/<id>.json`.
- URL hash `#<id>` selects the volume — deep-linkable; default = first entry.
- `history.hashchange` drives navigation (back/forward works).

### Main area — Authors tab (default)
- Header: conference title, year, source line.
- Stat cards: papers, unique authors, author–paper slots, avg authors/paper,
  single-author papers, max papers by one author.
- **Authors-per-paper histogram**: CSS bar chart (bar per team size, height ∝
  count, counts on hover/labels).
- Author table: rank, name (button), papers, co-author count; sortable columns;
  search filter + min-papers filter; renders in slices (2000 rows) as now.

### Author detail panel
- Author name in the table is a button; click opens a right slide-in panel:
  - Name, totals (papers, first-authored, distinct co-authors).
  - Publications: title links to anthology URL, marker when first author.
  - Top collaborators: name × shared-paper count, sorted desc.
- Close via ✕, Esc, or backdrop click. Focus returns to the table row.

### Trends tab
- Per-conference view across years (from `index.json` + per-volume stats):
  grouped CSS bar charts for papers, unique authors, avg team size per year.
- Conference selector (the conference of the current volume, switchable).

## Statistics included

| Stat | Where |
|---|---|
| Papers, unique authors, slots, avg team size, single-author | Cards |
| Authors-per-paper histogram | Authors tab |
| Papers-per-author distribution | Authors tab chips (kept) |
| Co-author network stats | Author table column + author panel top collaborators |
| Institutions | Institutions tab (papers/authors per affiliation string, self-reported; newer volumes only), affiliations shown in author panel |
| Year-over-year trends | Trends tab |

### Institutions (added 2026-10-27)
- Per-volume JSON gains `institutions: [{name, papers, authors}]` — papers counts
  distinct papers with ≥1 author declaring the affiliation; authors counts
  distinct declarers. Author records gain `affiliations: [str]`.
- Affiliation strings are raw/self-reported (newer volumes only, ~2/3 of slots);
  no normalization in v1. Caveat shown in the tab.

## Error handling

- Missing/failed download of a volume → build error mentioning the volume ID;
  that volume is skipped, others continue.
- Fetch failure in browser → visible error message in main area, sidebar stays
  usable.
- Empty filter result → "no authors match" row.

## Testing

- Build: run `python stats.py 2026.emnlp 2025.emnlp`; sanity-check printed
  counts and spot-check one generated URL format
  (`https://aclanthology.org/2026.emnlp-main.1/`).
- Frontend: serve locally, open in browser; click through sidebar, tabs,
  author panel, sorting, filters, deep link `#2025.emnlp`; verify dark mode.
- No unit-test framework (project has none); smoke verification per above.

## Non-goals

- No client-side XML fetching, no server, no framework/build step.
- Not building the full Anthology (1000+ volumes) — curated list only.
- No citation/bib data beyond title + URL (abstracts excluded from JSON to
  keep size down).
