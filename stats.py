"""Build per-volume ACL Anthology statistics JSONs for the stats site.

Usage:
    python stats.py [--force] [volume_id ...]

With no volume IDs, builds every volume in VOLUMES. Each main-conference volume
also pulls the Findings and workshop volumes listed in its <event><colocated>.
XML is downloaded on demand from the acl-anthology repo into xml/ and cached.
Output per event: data/<volume_id>.json (main conference) plus
data/<volume_id>+findings.json, +workshops.json and +all.json when non-empty;
data/index.json lists every dataset; data/people/<letter>.json holds each
person's history across events; data/inst_trends.json holds institution
papers per year. Authors are resolved via the Anthology people registry and
affiliations via ROR (see refs.py; both cached in ref/). Tutorials, keynotes,
invited talks and panels are excluded.
"""
from __future__ import annotations

import http.client
import json
import re
import sys
from collections import Counter
from pathlib import Path
from xml.etree.ElementTree import Element  # defusedxml returns stdlib Elements

import defusedxml.ElementTree as ET

from refs import PEOPLE_PATH, ROR_PATH, People, Ror, ensure_refs, load_refs

XML_PATH = "acl-org/acl-anthology/master/data/xml"

# Curated volume set: conference -> volume IDs (most recent first).
VOLUMES = {
    "EMNLP": ["2026.emnlp", "2025.emnlp", "2024.emnlp", "2023.emnlp",
              "2022.emnlp", "2021.emnlp", "2020.emnlp"],
    "ACL": ["2026.acl", "2025.acl", "2024.acl", "2023.acl", "2022.acl", "2021.acl", "2020.acl"],
    "NAACL": ["2025.naacl", "2024.naacl", "2022.naacl", "2021.naacl"],
    "COLING": ["2025.coling", "2022.coling", "2020.coling"],
    "AACL": ["2026.aacl", "2025.ijcnlp", "2023.ijcnlp", "2022.aacl", "2020.aacl"],
}

XML_DIR = Path("xml")
OUT_DIR = Path("data")
# Bump when the per-volume JSON layout changes so cached outputs are rebuilt.
SCHEMA = 8

# Research-topic lexicon: share of papers whose title or abstract matches.
TOPICS = {
    "LLMs": r"\bllms?\b|large language models?",
    "Multimodal / vision": r"multi-?modal|vision[- ]language|\bimages?\b|\bvisual\b",
    "Multilingual / low-resource": r"multi-?lingual|cross-?lingual|low-?resource",
    "Machine translation": r"machine translation|\bnmt\b",
    "Benchmarks / evaluation": r"\bbenchmarks?\b|\bevaluation\b",
    "Agents / tool use": r"\bagents?\b|agentic|tool[- ]use",
    "RAG / retrieval": r"retrieval|\brag\b",
    "Reasoning": r"\breasoning\b|chain[- ]of[- ]thought",
    "Hallucination / factuality": r"hallucinat|factual",
    "Safety / bias": r"\bsafety\b|jailbreak|\bbias(es)?\b|toxic|fairness",
    "Speech": r"\bspeech\b|\basr\b|spoken",
    "BERT-era encoders": r"\bbert\b|roberta|pre-?trained (language )?models?",
}
TOPIC_RES = {k: re.compile(v) for k, v in TOPICS.items()}
TOPIC_NAMES = list(TOPICS)

STOPWORDS = set("""
large
a an and are as at be by for from in into is it its of on or over the to via with
without towards toward through using based how what when where which who why do does
can we our your their this that these those not no new more less than vs versus
about across between beyond under all any each both one two three study approach
approaches method methods model models task tasks learning language languages neural
data dataset datasets analysis towards improving improve better case
""".split())
WORD_RE = re.compile(r"[a-z][a-z0-9\-]+")


def _median(xs: list[int]) -> float:
    s = sorted(xs)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _components(n_nodes: int, edges) -> list[int]:
    """Connected-component sizes (desc) of the co-authorship graph."""
    parent = list(range(n_nodes))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    return sorted(Counter(find(i) for i in range(n_nodes)).values(), reverse=True)


# Joint IJCNLP-AACL editions are published under the ijcnlp venue but belong
# to the AACL series; map them onto the AACL conference.
CONF_OVERRIDE = {"2023.ijcnlp": "AACL", "2025.ijcnlp": "AACL"}

# Pre-2020 events live in old-style collections (P19, D19, N19, C18, I17, ...): output id -> collection.
# The ID letter gives the series; IJCNLP editions belong to the AACL series like the 2023/2025 ones.
_LEGACY_YEARS = {
    ("acl", "P"): range(1979, 2020),
    ("emnlp", "D"): range(2007, 2020),
    ("naacl", "N"): [2001, 2003, 2004, 2006, 2007, 2009, 2010, 2012, 2013, 2015, 2016, 2018, 2019],
    ("coling", "C"): [1965, 1967, 1969, 1973, 1980, 1982, 1986, 1988, 1990, 1992, 1994, 1996,
                      1998, 2000, 2002, 2004, 2008, 2010, 2012, 2014, 2016, 2018],
    ("ijcnlp", "I"): [2005, 2008, 2011, 2013, 2017],
}
LEGACY_SRC = {f"{y}.{slug}": f"{letter}{y % 100:02d}"
              for (slug, letter), years in _LEGACY_YEARS.items() for y in years}
CONF_OVERRIDE.update({f"{y}.ijcnlp": "AACL" for y in _LEGACY_YEARS[("ijcnlp", "I")]})
for _vid in sorted(LEGACY_SRC, reverse=True):
    VOLUMES[CONF_OVERRIDE.get(_vid) or _vid.split(".")[1].upper()].append(_vid)


def vol_meta(vol_id: str) -> tuple[str, int]:
    m = re.fullmatch(r"(\d{4})\.(.+)", vol_id)
    if not m:
        raise ValueError(f"bad volume id: {vol_id!r}")
    try:
        return CONF_OVERRIDE.get(vol_id, m.group(2).upper()), int(m.group(1))
    except ValueError:
        raise ValueError(f"bad year in volume id: {vol_id!r}") from None


def load_xml(vol_id: str, xml_dir: Path) -> Element | None:
    """Anthology collection XML; `vol_id` is the collection id (2020.acl, P19, W18, ...)."""
    path = xml_dir / f"{vol_id}.xml"
    if not path.exists():
        # HTTPSConnection is HTTPS-only by construction: no `file:` or
        # custom-scheme ambiguity, host pinned to the acl-anthology mirror.
        print(f"downloading https://raw.githubusercontent.com/{XML_PATH}/{vol_id}.xml ...")
        try:
            conn = http.client.HTTPSConnection("raw.githubusercontent.com", timeout=60)
            conn.request("GET", f"/{XML_PATH}/{vol_id}.xml")
            resp = conn.getresponse()
            if resp.status != 200:
                raise OSError(f"HTTP {resp.status}")
            path.write_bytes(resp.read())
            conn.close()
        except (http.client.HTTPException, OSError) as exc:
            print(f"warning: skipping {vol_id}: {exc}", file=sys.stderr)
            return None
    # XML comes from the official acl-anthology repo over HTTPS. Stdlib expat
    # resolves no external entities but is expansion-bomb-able, so reject DTDs.
    raw = path.read_bytes()
    if b"<!ENTITY" in raw or b"<!DOCTYPE" in raw:
        print(f"warning: {vol_id}: XML declares a DTD, skipped", file=sys.stderr)
        path.unlink(missing_ok=True)
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # Delete so a bad cached page (e.g. rate-limit HTML) can't poison runs.
        print(f"warning: {vol_id}: malformed XML, cache cleared", file=sys.stderr)
        path.unlink(missing_ok=True)
        return None
    if root.tag != "collection":  # well-formed but not anthology XML
        print(f"warning: {vol_id}: unexpected root <{root.tag}>, skipped", file=sys.stderr)
        path.unlink(missing_ok=True)
        return None
    return root


SCOPES = ("main", "findings", "workshops", "all")


def scoped_id(vol_id: str, scope: str) -> str:
    return vol_id if scope == "main" else f"{vol_id}+{scope}"


NEW_COLL = re.compile(r"\d{4}\.[^-]+")
OLD_COLL = re.compile(r"[A-Z]\d{2}")
GENERIC_VENUES = {"ws", "acl", "emnlp", "naacl", "coling", "ijcnlp", "hlt", "conll", "semeval"}


def colocated(root: Element, legacy: bool = False) -> dict[str, dict[str, str]]:
    """Co-located volumes from <event><colocated>: collection -> {volume: track label}.

    Legacy (pre-2020) events also reference old-style collections (W18-51, I05-3); their
    labels are filled in later from the volume metadata (see workshop_label).
    """
    out: dict[str, dict[str, str]] = {}
    for el in root.findall("event/colocated/volume-id"):
        text = (el.text or "").strip()
        m = re.fullmatch(r"(\d{4}\.[^-]+)-(.+)", text)
        if not m and legacy:
            m = re.fullmatch(r"([A-Z]\d{2})-(.+)", text)
        if not m:
            continue
        coll, vol = m.groups()
        if OLD_COLL.fullmatch(coll):
            out.setdefault(coll, {})[vol] = ""
            continue
        name = coll.split(".", 1)[1]
        label = "findings" if name == "findings" else (
            name if vol in ("1", "main") else f"{name}-{vol}")
        out.setdefault(coll, {})[vol] = label
    return out


def legacy_type(volume: Element) -> str:
    """Paper type of a pre-2020 main-conference volume, from its booktitle."""
    bt = volume.find("meta/booktitle")
    t = "".join(bt.itertext()).lower() if bt is not None else ""
    for key, label in (("tutorial", "tutorial"), ("demo", "demo"), ("student", "srw"),
                       ("industry", "industry"), ("shared task", "shared-task")):
        if key in t:
            return label
    if "short" in t and "long" not in t:
        return "short"
    if "long" in t and "short" not in t:
        return "long"
    return "main"


def workshop_label(coll_root: Element, vol: str) -> str:
    """Workshop label for an old-style volume: its first non-generic <venue>, else ws-<vol>."""
    for v in coll_root.findall("volume"):
        if v.attrib.get("id") == vol:
            for ve in v.findall("meta/venue"):
                name = (ve.text or "").strip().lower()
                if name and name not in GENERIC_VENUES:
                    return name
    return f"ws-{vol}"


def parse_papers(root: Element, tracks: dict[str, str] | None = None) -> list[dict]:
    """Paper records of a collection; `tracks` restricts volumes and names their track."""
    coll_id = root.attrib.get("id", "")
    out = []
    for volume in root.findall("volume"):
        vol_short = volume.attrib.get("id", "")
        if tracks is not None and vol_short not in tracks:
            continue
        track = vol_short if tracks is None else tracks[vol_short]
        for p in volume.findall("paper"):
            title_el = p.find("title")
            abstract_el = p.find("abstract")
            # Raw mentions (name, affiliation|None, anthology id|None); person
            # resolution and dedup happen in aggregate() against the registry.
            authors = []
            for a in p.findall("author"):
                name = f"{(a.findtext('first') or '').strip()} {(a.findtext('last') or '').strip()}".strip()
                if name:
                    authors.append((name, (a.findtext("affiliation") or "").strip() or None,
                                    a.attrib.get("id") or None))
            if not authors:
                continue
            out.append({
                "title": "".join(title_el.itertext()).strip() if title_el is not None else "",
                "abstract": "".join(abstract_el.itertext()) if abstract_el is not None else "",
                "url": f"https://aclanthology.org/{coll_id}-{vol_short}.{p.attrib.get('id', '')}",
                "track": track,
                "award": [" ".join(a.itertext()).strip() for a in p.findall("award")],
                "authors": authors,
            })
    return out


NON_RESEARCH_TITLE = re.compile(r"^\W*(invited talk|keynote|panel( discussion)?)\b", re.I)


def is_research(r: dict) -> bool:
    """False for tutorials, keynotes, invited talks and panels (not research papers)."""
    return "tutorial" not in r["track"] and not NON_RESEARCH_TITLE.match(r["title"])


_REFS: tuple[People, Ror] | None = None


def refs() -> tuple[People, Ror]:
    """People registry and ROR matcher, loaded once and only when a rebuild needs them."""
    global _REFS
    if _REFS is None:
        _REFS = load_refs()
    return _REFS


def _fresh(out_path: Path, sources: list[Path]) -> dict | None:
    """Cached output if it matches SCHEMA and is newer than every source file."""
    if not out_path.exists():
        return None
    mtime = out_path.stat().st_mtime
    if any(s.exists() and s.stat().st_mtime > mtime for s in sources):
        return None
    try:
        d = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return d if d.get("schema") == SCHEMA else None


def build_event(vol_id: str, xml_dir: Path, out_dir: Path,
                force: bool = False) -> list[dict]:
    """Build main, Findings, workshops and combined datasets for one conference event."""
    conference, year = vol_meta(vol_id)
    src = LEGACY_SRC.get(vol_id, vol_id)
    root = load_xml(src, xml_dir)
    if root is None:
        return []
    coloc = colocated(root, src in LEGACY_SRC.values())
    ensure_refs()
    sources = ([xml_dir / f"{src}.xml"] + [xml_dir / f"{c}.xml" for c in coloc if c != src]
               + [PEOPLE_PATH, ROR_PATH])
    if not force and all(s.exists() for s in sources):
        # The main dataset records which scopes the event produced (COLING has no Findings).
        main = _fresh(out_dir / f"{vol_id}.json", sources)
        cached = [main] + [_fresh(out_dir / f"{scoped_id(vol_id, s)}.json", sources)
                           for s in (main or {}).get("scopes", [])[1:]]
        if all(d is not None for d in cached):
            print(f"{vol_id}: up to date, skipped")
            return [_index_entry(d) for d in cached if d is not None]

    legacy = vol_id in LEGACY_SRC
    main_tracks = None
    if legacy:
        # Old collections hold the main proceedings, its companion volumes (demos, SRW, ...)
        # and, for some events, workshops as further volumes of the same collection.
        ws_vols = set(coloc.get(src, {}))
        main_tracks = {v.attrib["id"]: legacy_type(v) for v in root.findall("volume")
                       if v.attrib.get("id") not in ws_vols}
    records = {"main": parse_papers(root, main_tracks), "findings": [], "workshops": []}
    for coll, tracks in coloc.items():
        if coll == src and not legacy:  # e.g. 2026.aacl lists its own demo volume as colocated
            continue
        croot = root if coll == src else load_xml(coll, xml_dir)
        if croot is None:
            continue
        if not NEW_COLL.fullmatch(coll):
            tracks = {v: workshop_label(croot, v) for v in tracks}
        scope = "findings" if coll.endswith(".findings") else "workshops"
        records[scope] += parse_papers(croot, tracks)
    # "Whole event" only differs from main when Findings or workshops exist.
    records["all"] = (records["main"] + records["findings"] + records["workshops"]
                      if records["findings"] or records["workshops"] else [])
    excluded = {s: sum(1 for r in recs if not is_research(r)) for s, recs in records.items()}
    records = {s: [r for r in recs if is_research(r)] for s, recs in records.items()}

    title_el = root.find(".//volume/meta/booktitle")
    main_title = (" ".join("".join(title_el.itertext()).split())
                  if title_el is not None else f"{conference} {year}")
    if legacy:  # "... (Volume 1: Long Papers)" names one volume of the proceedings
        main_title = re.sub(r"[\s:,(]*\(?\s*volume[- ].*$", "", main_title, flags=re.I) or main_title
    titles = {
        "main": main_title,
        "findings": f"Findings of {conference} {year}",
        "workshops": f"Workshops co-located with {conference} {year}",
        "all": f"{conference} {year}: main conference, Findings and workshops",
    }
    datasets = [aggregate(scoped_id(vol_id, s), conference, year, s, titles[s], records[s],
                          excluded[s])
                for s in SCOPES if records[s]]
    if not datasets or datasets[0]["scope"] != "main":
        print(f"warning: {vol_id} has 0 main-conference papers, skipped", file=sys.stderr)
        return []
    datasets[0]["scopes"] = [d["scope"] for d in datasets]
    out_dir.mkdir(exist_ok=True)
    for data in datasets:
        out_path = out_dir / f"{data['id']}.json"
        out_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                            encoding="utf-8")
        print(f"{data['id']}: {data['stats']['unique_authors']} authors, "
              f"{data['stats']['papers']} papers -> {out_path}")
    return [_index_entry(d) for d in datasets]


def aggregate(out_id: str, conference: str, year: int, scope: str, title: str,
              records: list[dict], excluded: int = 0) -> dict:
    people, ror = refs()
    papers = []
    per_author = {}  # key -> {name, ambiguous, papers, first_author, coauthors, affiliations}
    inst = {}  # institution -> {"papers": set, "first": set, "authors": set, "country"}
    author_slots = 0
    aff_slots = 0
    id_slots = 0
    cc_slots = 0
    country_papers = Counter()
    with_country = 0
    international = 0
    single_author = 0
    team_sizes = Counter()
    tracks = Counter()
    topic_hits = Counter()
    term_df = Counter()  # title-term document frequency
    awards = 0
    for r in records:
        ptitle = r["title"]
        # Resolve mentions to persons; dedupe by person (first mention wins).
        entries, seen = [], set()
        for name, aff, pid in r["authors"]:
            key, display, ambiguous = people.resolve(name, pid)
            if key not in seen:
                seen.add(key)
                entries.append((key, display, aff, pid, ambiguous))
        keys = [e[0] for e in entries]
        idx = len(papers)
        paper = {"title": ptitle, "url": r["url"], "authors": keys, "track": r["track"]}
        if r["award"]:
            paper["award"] = r["award"]
            awards += 1
        papers.append(paper)
        tracks[r["track"]] += 1
        text = f"{ptitle} {r['abstract']}".lower()
        hits = [j for j, rx in enumerate(TOPIC_RES.values()) if rx.search(text)]
        topic_hits.update(TOPIC_NAMES[j] for j in hits)
        if hits:
            paper["topics"] = hits  # indices into TOPIC_NAMES (= stats["topics"] key order)
        term_df.update({w for w in WORD_RE.findall(ptitle.lower())
                        if w not in STOPWORDS and len(w) > 2})
        author_slots += len(keys)
        team_sizes[len(keys)] += 1
        if len(keys) == 1:
            single_author += 1
        ccs = set()
        paper_insts: list[str] = []  # matched institutions, author order, deduped
        for i, (key, name, aff, pid, ambiguous) in enumerate(entries):
            rec = per_author.setdefault(
                key, {"name": name, "ambiguous": ambiguous, "papers": [], "first_author": [],
                      "coauthors": Counter(), "affiliations": Counter()})
            rec["papers"].append(idx)
            if i == 0:
                rec["first_author"].append(idx)
            rec["coauthors"].update(k for k in keys if k != key)
            if pid:
                id_slots += 1
            hit = ror.match(aff) if aff else None
            if hit:
                inst_name, cc = hit
                aff_slots += 1
                rec["affiliations"][inst_name] += 1
                slot = inst.setdefault(inst_name, {"papers": set(), "first": set(),
                                                   "authors": set(), "country": cc})
                slot["papers"].add(idx)
                slot["authors"].add(key)
                if i == 0:
                    slot["first"].add(idx)
                    paper["first_inst"] = inst_name
                elif i == len(entries) - 1:
                    paper["last_inst"] = inst_name
                if inst_name not in paper_insts:
                    paper_insts.append(inst_name)
                if cc:
                    cc_slots += 1
                    ccs.add(cc)
        country_papers.update(ccs)
        if paper_insts:
            paper["inst"] = paper_insts
        if ccs:
            with_country += 1
            international += len(ccs) > 1

    n_papers = len(papers)
    sizes = Counter(len(rec["papers"]) for rec in per_author.values())
    node = {key: i for i, key in enumerate(per_author)}
    comps = _components(len(node), (
        (node[a], node[b]) for a, rec in per_author.items() for b in rec["coauthors"]))
    # Concentration: share of author-paper slots held by the top 1% of authors.
    counts = sorted((len(r["papers"]) for r in per_author.values()), reverse=True)
    top1 = max(1, len(counts) // 100)
    inst_sorted = sorted(inst.items(), key=lambda kv: (-len(kv[1]["papers"]), kv[0]))
    # Papers reference institutions by index into the "institutions" list.
    inst_idx = {name: i for i, (name, _) in enumerate(inst_sorted)}
    for p in papers:
        if "inst" in p:
            p["inst"] = [inst_idx[n] for n in p["inst"]]
        for f in ("first_inst", "last_inst"):
            if f in p:
                p[f] = inst_idx[p[f]]
    # Same name under several person records (IDs and/or one un-ID'd bucket).
    keys_per_name = Counter(rec["name"] for rec in per_author.values())
    return {
        "schema": SCHEMA,
        "id": out_id,
        "event": out_id.split("+")[0],
        "scope": scope,
        "conference": conference,
        "year": year,
        "title": title,
        "papers": papers,
        "authors": [
            {
                "key": key,
                "name": rec["name"],
                # The Anthology person ID (key without "~", absent for ambiguous
                # buckets) and co-author counts are derived client-side.
                "ambiguous": rec["ambiguous"],
                "namesakes": keys_per_name[rec["name"]] - 1,
                "papers": rec["papers"],
                "first_author": rec["first_author"],
                "affiliations": [a for a, _ in rec["affiliations"].most_common()],
            }
            for key, rec in sorted(
                per_author.items(), key=lambda kv: (-len(kv[1]["papers"]), kv[0]))
        ],
        "institutions": [
            {"name": name, "country": slot["country"], "papers": len(slot["papers"]),
             "first_author_papers": len(slot["first"]),
             "authors": len(slot["authors"])}
            for name, slot in inst_sorted
        ],
        "stats": {
            "papers": n_papers,
            "unique_authors": len(per_author),
            "author_slots": author_slots,
            "avg_authors_per_paper": round(author_slots / n_papers, 2),
            "median_authors_per_paper": _median([len(p["authors"]) for p in papers]),
            "single_author_papers": single_author,
            "award_papers": awards,
            "excluded_items": excluded,
            "affiliation_coverage": round(aff_slots / author_slots, 4),
            "anthology_id_coverage": round(id_slots / author_slots, 4),
            "country_coverage": round(cc_slots / author_slots, 4),
            "papers_with_country": with_country,
            "international_share": round(international / with_country, 4) if with_country else None,
            "country_dist": dict(country_papers.most_common()),
            "shared_name_records": sum(1 for r in per_author.values()
                                       if keys_per_name[r["name"]] > 1),
            "top1pct_author_slot_share": round(sum(counts[:top1]) / author_slots, 4),
            "one_paper_author_share": round(sizes[1] / len(per_author), 4),
            "collab_components": len(comps),
            "largest_component_share": round(comps[0] / len(per_author), 4),
            "papers_per_author_dist": {str(k): sizes[k] for k in sorted(sizes)},
            "authors_per_paper_dist": {str(k): team_sizes[k] for k in sorted(team_sizes)},
            "track_dist": dict(tracks.most_common()),
            "topics": {k: round(topic_hits[k] / n_papers, 4) for k in TOPICS},
            "top_terms": term_df.most_common(40),
        },
    }


def _index_entry(data: dict) -> dict:
    s = data["stats"]
    return {
        "id": data["id"],
        "event": data["event"],
        "scope": data["scope"],
        "conference": data["conference"],
        "year": data["year"],
        "papers": s["papers"],
        "unique_authors": s["unique_authors"],
        "avg_authors_per_paper": s["avg_authors_per_paper"],
        "median_authors_per_paper": s["median_authors_per_paper"],
        "single_author_share": round(s["single_author_papers"] / s["papers"], 4),
        "affiliation_coverage": s["affiliation_coverage"],
        "country_coverage": s["country_coverage"],
        "international_share": s["international_share"],
        "countries": {cc: round(n / s["papers_with_country"], 4)
                      for cc, n in list(s["country_dist"].items())[:25]},
        "largest_component_share": s["largest_component_share"],
        "top1pct_author_slot_share": s["top1pct_author_slot_share"],
        "topics": s["topics"],
        # Workshop volumes have one track per workshop; keep the index small.
        **({"track_dist": s["track_dist"]} if data["scope"] != "workshops" else {}),
    }


def _shard(key: str) -> str:
    c = key[:1].lower()
    return c if "a" <= c <= "z" else "_"


def build_cross(entries: list[dict], out_dir: Path) -> None:
    """Cross-volume outputs, from the per-dataset JSONs.

    - newcomer_share on each index entry: share of its people absent from every
      earlier-year dataset of the same scope (earliest year: None).
    - data/people/<shard>.json: per-person history across tracked events, from
      the main/Findings/workshops datasets (positions: first, last, other).
    - data/inst_trends.json: per scope and conference, papers per year for the
      40 institutions and the 40 institution pairs with the most papers overall.
    """
    keys: dict[str, set[str]] = {}
    people: dict[str, dict] = {}
    inst_years: dict[tuple[str, str], dict[str, dict]] = {}
    pair_years: dict[tuple[str, str], dict[tuple[str, str], Counter]] = {}
    for e in entries:
        try:
            d = json.loads((out_dir / f"{e['id']}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"dataset {e['id']} not built or unreadable: {exc}") from exc
        keys[e["id"]] = {a["key"] for a in d["authors"]}
        bucket = inst_years.setdefault((e["scope"], e["conference"]), {})
        for x in d["institutions"]:
            rec = bucket.setdefault(x["name"], {"country": x.get("country"), "papers": {}})
            rec["papers"][str(e["year"])] = x["papers"]
        names = [x["name"] for x in d["institutions"]]
        pbucket = pair_years.setdefault((e["scope"], e["conference"]), {})
        for p in d["papers"]:
            ins = sorted(names[i] for i in p.get("inst", ()))
            for i, a in enumerate(ins):
                for b in ins[i + 1:]:
                    pbucket.setdefault((a, b), Counter())[str(e["year"])] += 1
        if e["scope"] == "all":
            continue
        last = Counter(p["authors"][-1] for p in d["papers"] if len(p["authors"]) > 1)
        for a in d["authors"]:
            p = people.setdefault(a["key"], {"name": a["name"], "ambiguous": a["ambiguous"],
                                             "aff": [], "affYear": 0, "venues": []})
            n, first, last_n = len(a["papers"]), len(a["first_author"]), last[a["key"]]
            p["venues"].append([e["event"], e["year"], e["conference"], e["scope"], n, first, last_n])
            if a["affiliations"] and e["year"] >= p["affYear"]:
                p["aff"], p["affYear"] = a["affiliations"][:3], e["year"]

    for scope in SCOPES:
        group = [e for e in entries if e["scope"] == scope]
        first_year = min((e["year"] for e in group), default=0)
        for e in group:
            if e["year"] == first_year:
                e["newcomer_share"] = None
                continue
            seen = set().union(*(keys[o["id"]] for o in group if o["year"] < e["year"]))
            cur = keys[e["id"]]
            e["newcomer_share"] = round(len(cur - seen) / len(cur), 4)

    pdir = out_dir / "people"
    pdir.mkdir(exist_ok=True)
    for old in pdir.glob("*.json"):
        old.unlink()
    shards: dict[str, dict] = {}
    for key, p in people.items():
        p.pop("affYear")
        p["venues"].sort(key=lambda v: (v[1], v[2], SCOPES.index(v[3])))
        shards.setdefault(_shard(key), {})[key] = p
    for s, data in shards.items():
        (pdir / f"{s}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"people: {len(people)} persons in {len(shards)} shards")

    totals = {(e["scope"], e["conference"], str(e["year"])): e["papers"] for e in entries}
    trends: dict[str, dict] = {}
    for (scope, conf), insts in inst_years.items():
        years = sorted({y for x in insts.values() for y in x["papers"]})
        top = sorted(insts.items(), key=lambda kv: -sum(kv[1]["papers"].values()))[:40]
        trends.setdefault(scope, {})[conf] = {
            "years": years,
            "papers": {y: totals[(scope, conf, y)] for y in years},
            "institutions": [{"name": name, "country": x["country"],
                              "counts": [x["papers"].get(y, 0) for y in years]}
                             for name, x in top],
            "pairs": [{"a": a, "b": b, "counts": [c.get(y, 0) for y in years]}
                      for (a, b), c in sorted(pair_years[(scope, conf)].items(),
                                              key=lambda kv: (-sum(kv[1].values()), kv[0]))[:40]],
        }
    (out_dir / "inst_trends.json").write_text(json.dumps(trends, ensure_ascii=False),
                                              encoding="utf-8")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    vol_ids = args or [v for vols in VOLUMES.values() for v in vols]

    XML_DIR.mkdir(exist_ok=True)
    # Migrate the original single-volume file into xml/.
    legacy = Path("2026.emnlp.xml")
    if legacy.exists() and not (XML_DIR / legacy.name).exists():
        legacy.rename(XML_DIR / legacy.name)

    entries = [e for v in vol_ids for e in build_event(v, XML_DIR, OUT_DIR, force)]
    build_cross(entries, OUT_DIR)
    entries.sort(key=lambda e: (e["conference"], -e["year"], SCOPES.index(e["scope"])))
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "index.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    print(f"index.json: {len(entries)} datasets")


if __name__ == "__main__":
    main()
