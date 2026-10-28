"""Reference data for the stats build: Anthology people and ROR institutions.

- ref/people.json: the Anthology's person registry (IDs, name variants). Used to
  map author mentions to one stable person key across volumes.
- ref/ror.zip: the Research Organization Registry data dump (Zenodo). Used to
  map free-text affiliations to a canonical institution and a country.

Both are downloaded on first use; delete the files to refresh them.
"""
from __future__ import annotations

import http.client
import json
import re
import sys
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

REF_DIR = Path("ref")
PEOPLE_PATH = REF_DIR / "people.json"
ROR_PATH = REF_DIR / "ror.zip"
PEOPLE_SRC = ("raw.githubusercontent.com",
              "/acl-org/acl-anthology/master/data/json/people.json")
ROR_INDEX = ("zenodo.org", "/api/communities/ror-data/records?sort=newest&size=1")
ALLOWED_HOSTS = {"raw.githubusercontent.com", "zenodo.org"}


def _get(host: str, path: str, redirects: int = 5) -> bytes:
    """HTTPS GET pinned to known hosts; follows HTTPS redirects within them."""
    for _ in range(redirects):
        conn = http.client.HTTPSConnection(host, timeout=120)
        try:
            conn.request("GET", path, headers={"User-Agent": "acl-stats"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = urlsplit(resp.getheader("Location", ""))
                if loc.scheme not in ("", "https") or (loc.netloc and loc.netloc not in ALLOWED_HOSTS):
                    raise OSError(f"refusing redirect to {loc.geturl()}")
                host = loc.netloc or host
                path = loc.path + (f"?{loc.query}" if loc.query else "")
                continue
            if resp.status != 200:
                raise OSError(f"HTTP {resp.status} for https://{host}{path}")
            return resp.read()
        finally:
            conn.close()
    raise OSError(f"too many redirects for https://{host}{path}")


def ensure_refs() -> None:
    REF_DIR.mkdir(exist_ok=True)
    if not PEOPLE_PATH.exists():
        print("downloading Anthology people registry ...")
        PEOPLE_PATH.write_bytes(_get(*PEOPLE_SRC))
    if not ROR_PATH.exists():
        print("downloading ROR data dump ...")
        rec = json.loads(_get(*ROR_INDEX))["hits"]["hits"][0]
        zf = next(f for f in rec["files"] if f["key"].endswith(".zip"))
        url = urlsplit(zf["links"]["self"])
        if url.netloc not in ALLOWED_HOSTS:
            raise OSError(f"unexpected ROR host {url.netloc}")
        ROR_PATH.write_bytes(_get(url.netloc, url.path))


def norm(s: str) -> str:
    """Accent-, case- and punctuation-insensitive form used for matching."""
    s = re.sub(r"[’'`]", "", s)  # "King’s" == "Kings"
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


LOOSE_DROP = {"the", "at", "of", "and", "inc", "ltd", "co", "corp", "corporation",
              "llc", "gmbh", "limited", "group", "company", "plc", "sa", "ag"}


def loose(n: str) -> str:
    """Looser key for an already-normalised name: drops legal suffixes and fillers."""
    return " ".join(t for t in n.split() if t not in LOOSE_DROP)


class People:
    """Resolve an author mention to a stable person key.

    Order: explicit Anthology ID on the mention; else the single registry person
    with that name (the Anthology attributes unmarked mentions of unambiguous
    names to that person); else, if several registry people share the name, an
    ambiguous bucket ("?Name") that may merge people; else the name slug, which
    is how the Anthology identifies people absent from its registry.
    """

    def __init__(self, path: Path = PEOPLE_PATH):
        reg = json.loads(path.read_text(encoding="utf-8"))
        self.display: dict[str, str] = {}
        self.by_name: dict[str, set[str]] = defaultdict(set)
        for pid, rec in reg.items():
            names = [f"{n.get('first', '')} {n.get('last', '')}".strip() for n in rec["names"]]
            self.display[pid] = names[0]
            for n in names:
                self.by_name[norm(n)].add(pid)

    def resolve(self, name: str, pid: str | None) -> tuple[str, str, bool]:
        """(key, display name, ambiguous)."""
        if pid:
            return pid, self.display.get(pid, name), False
        n = norm(name)
        ids = self.by_name.get(n, ())
        if len(ids) == 1:
            pid = next(iter(ids))
            return pid, self.display[pid], False
        if len(ids) > 1:
            return f"?{name}", name, True
        slug = n.replace(" ", "-")
        return (slug if slug not in self.display else f"~{slug}"), name, False


# Country spellings seen in affiliations that ROR's English names don't cover.
COUNTRY_ALIASES = {
    "usa": "US", "u s a": "US", "us": "US", "united states of america": "US",
    "uk": "GB", "u k": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "korea": "KR", "south korea": "KR", "republic of korea": "KR",
    "prc": "CN", "p r china": "CN", "pr china": "CN", "people s republic of china": "CN",
    "hong kong": "HK", "hong kong sar": "HK", "taiwan": "TW", "macau": "MO", "macao": "MO",
    "russia": "RU", "viet nam": "VN", "vietnam": "VN", "the netherlands": "NL",
    "czechia": "CZ", "czech republic": "CZ", "uae": "AE", "turkiye": "TR", "turkey": "TR",
    "iran": "IR",
}


# Affiliation strings that carry no institution.
NON_AFFILIATIONS = {"na", "n a", "none", "independent", "independent researcher", "unaffiliated"}


class Ror:
    """Match a free-text affiliation to (institution name, ISO country code)."""

    def __init__(self, path: Path = ROR_PATH):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.endswith(".json"))
            recs = json.load(z.open(name))
        # id -> (name, country code, has_parent, n_children)
        self.orgs: dict[str, tuple[str, str | None, bool, int]] = {}
        exact: dict[str, set[str]] = defaultdict(set)
        lax: dict[str, set[str]] = defaultdict(set)
        acronyms: dict[str, set[str]] = defaultdict(set)
        countries: dict[str, str] = {}
        for r in recs:
            if r.get("status") != "active":
                continue
            disp = next((n["value"] for n in r["names"] if "ror_display" in n["types"]),
                        r["names"][0]["value"])
            loc = r["locations"][0]["geonames_details"] if r.get("locations") else {}
            cc = loc.get("country_code")
            if cc and loc.get("country_name"):
                countries[norm(loc["country_name"])] = cc
            rels = [x["type"] for x in r.get("relationships", [])]
            self.orgs[r["id"]] = (disp, cc, "parent" in rels, rels.count("child"))
            for n in r["names"]:
                v = n["value"]
                if "acronym" in n["types"]:
                    acronyms[norm(v)].add(r["id"])
                    continue
                exact[norm(v)].add(r["id"])
                # ROR qualifies companies by country: "Microsoft (United States)".
                bare = norm(re.sub(r"\s*\([^)]*\)\s*$", "", v))
                lax[loose(bare)].add(r["id"])
        for a, ids in acronyms.items():
            if len(ids) == 1 and len(a) >= 3 and a not in exact:
                exact[a] = ids
        self.exact, self.lax = dict(exact), dict(lax)
        countries.update(COUNTRY_ALIASES)
        self.country_re = re.compile(
            r"\b(" + "|".join(sorted(map(re.escape, countries), key=len, reverse=True)) + r")\b")
        self.countries = countries
        self.cache: dict[str, tuple[str, str | None] | None] = {}

    def _pick(self, ids: set[str]) -> tuple[str, str | None] | None:
        """One org for a name, or None when the name is genuinely ambiguous.

        Top-level orgs (no parent) win over their subsidiaries; among several
        top-level orgs the one with most children (a corporate HQ) wins, unless
        they are unrelated same-name orgs in different countries.
        """
        top = [i for i in ids if not self.orgs[i][2]] or list(ids)
        if len({self.orgs[i][1] for i in top}) > 1:
            ranked = sorted(top, key=lambda i: -self.orgs[i][3])
            if self.orgs[ranked[0]][3] == 0 or (
                    len(ranked) > 1 and self.orgs[ranked[0]][3] == self.orgs[ranked[1]][3]):
                return None
            top = ranked[:1]
        disp, cc, _, _ = self.orgs[top[0]]
        # Drop ROR's country qualifier: "Amazon (United States)" -> "Amazon".
        return re.sub(r"\s*\([^)]*\)\s*$", "", disp), cc

    def _lookup(self, cand: str) -> tuple[str, str | None] | None:
        n = norm(cand)
        for ids in (self.exact.get(n), self.lax.get(loose(n))):
            if ids and (hit := self._pick(ids)):
                return hit
        return None

    def match(self, aff: str) -> tuple[str, str | None] | None:
        """(institution, country code|None), or None for non-affiliations like "NA"."""
        if aff in self.cache:
            return self.cache[aff]
        result: tuple[str, str | None] | None
        if norm(aff) in NON_AFFILIATIONS:
            result = None
        else:
            segs = [s.strip() for s in re.split(r"[,;/()]|\s[-–]\s", aff) if s.strip()]
            # "X, X" duplicates are common in the XML; collapse them for display.
            clean = ", ".join(dict.fromkeys(segs)) or aff.strip()
            parts = segs + [p.strip() for s in segs for p in s.split(" and ") if " and " in s]
            result = next((h for c in [aff] + parts if (h := self._lookup(c))), None)
            if result is None:
                m = self.country_re.search(norm(aff))
                result = (clean, self.countries[m.group(1)] if m else None)
        self.cache[aff] = result
        return result


def load_refs() -> tuple[People, Ror]:
    ensure_refs()
    print("loading reference data (people registry, ROR) ...", file=sys.stderr)
    return People(), Ror()
