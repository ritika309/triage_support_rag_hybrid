"""Corpus loader, chunker, and product_area taxonomy.

Walks data/{hackerrank,claude,visa}/**/*.md, strips YAML frontmatter,
chunks larger files by H2 sections, and tags each chunk with metadata.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from pathlib import Path

from .config import DATA_DIR

CHUNK_SPLIT_THRESHOLD = 2500   # chars; below this, keep file as one chunk
MAX_CHUNK_CHARS = 3500         # target per-chunk size after splitting

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_H1_RE = re.compile(r"^# +(.+?)\s*$", re.MULTILINE)
_H2_SPLIT_RE = re.compile(r"^(## +.+?)$", re.MULTILINE)
_TITLE_FRONTMATTER_RE = re.compile(r'^title:\s*"?(.+?)"?\s*$', re.MULTILINE)
_SOURCE_URL_RE = re.compile(r'^source_url:\s*"?(.+?)"?\s*$', re.MULTILINE)


@dataclass
class Chunk:
    chunk_id: str
    company: str           # HackerRank | Claude | Visa
    category: str          # top-level folder under company (e.g. "screen")
    subcategory: str       # second-level folder, or "" if none
    product_area: str      # canonicalized (underscores, lowercased) -- best label
    doc_path: str          # relative path from repo root
    title: str
    source_url: str
    text: str


_COMPANY_DIRS = {
    "hackerrank": "HackerRank",
    "claude": "Claude",
    "visa": "Visa",
}


def _strip_frontmatter(raw: str) -> tuple[str, dict]:
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return raw, {}
    fm_block = m.group(1)
    body = raw[m.end():]
    meta: dict = {}
    t = _TITLE_FRONTMATTER_RE.search(fm_block)
    if t:
        meta["title"] = t.group(1).strip().strip('"')
    s = _SOURCE_URL_RE.search(fm_block)
    if s:
        meta["source_url"] = s.group(1).strip().strip('"')
    return body, meta


def _extract_title(body: str, fm_title: str) -> str:
    if fm_title:
        return fm_title
    h1 = _H1_RE.search(body)
    return h1.group(1).strip() if h1 else ""


def _split_by_h2(body: str) -> list[str]:
    """Split markdown by H2 headers; preserve the header in each chunk."""
    parts = _H2_SPLIT_RE.split(body)
    if len(parts) <= 1:
        return [body.strip()]
    head = parts[0].strip()
    sections: list[str] = [head] if head else []
    for i in range(1, len(parts), 2):
        header = parts[i].strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        sections.append(f"{header}\n\n{content}".strip())
    merged: list[str] = []
    buf = ""
    for sec in sections:
        if not sec:
            continue
        if len(buf) + len(sec) + 2 <= MAX_CHUNK_CHARS:
            buf = f"{buf}\n\n{sec}".strip() if buf else sec
        else:
            if buf:
                merged.append(buf)
            buf = sec
    if buf:
        merged.append(buf)
    return merged


def _canonical_product_area(
    category: str, subcategory: str, company: str, doc_path: str
) -> str:
    """Best-guess product area label, lowercased + underscored.

    Empirical mapping derived from sample_support_tickets.csv labels:
      HackerRank -> top-level folder (e.g. screen, hackerrank_community -> community)
      Claude     -> top-level folder (e.g. privacy_and_legal); responder LLM
                    refines to topic tags (privacy, conversation_management) at stage 5
      Visa       -> deepest meaningful folder + filename heuristic for travel_support
    """
    def norm(s: str) -> str:
        return s.replace("-", "_").replace(" ", "_").strip("_").lower()

    path_lower = doc_path.lower()

    if company == "Visa":
        # filename / path heuristics — sample labels are topic-tag style
        if "traveler" in path_lower or "travel" in path_lower or "cheque" in path_lower:
            return "travel_support"
        if "merchant" in path_lower:
            return "merchant"
        if "small-business" in path_lower or "small_business" in path_lower:
            return "small_business"
        if subcategory:
            return norm(subcategory)
        if category:
            return norm(category)
        return "general_support"

    # HackerRank / Claude: top-level folder under the company root.
    # Claude has a literal "claude/" subfolder under data/claude/ that's not
    # a meaningful product area -> drop down one level.
    if company == "Claude" and category == "claude" and subcategory:
        return norm(subcategory)
    cat = norm(category) if category else norm(subcategory)
    # known shortenings the sample CSV uses
    aliases = {
        "hackerrank_community": "community",
        "privacy_and_legal": "privacy",
    }
    return aliases.get(cat, cat)


def _hash_id(*parts: str) -> str:
    h = hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()
    return h[:12]


def _load_one(md_path: Path, repo_data_dir: Path) -> list[Chunk]:
    raw = md_path.read_text(encoding="utf-8", errors="replace")
    body, meta = _strip_frontmatter(raw)
    rel = md_path.relative_to(repo_data_dir)
    parts = rel.parts  # e.g. ("hackerrank", "screen", "best-practice-guides", "foo.md")
    company_key = parts[0]
    company = _COMPANY_DIRS.get(company_key, company_key)
    middle = parts[1:-1]
    if company == "Visa" and middle and middle[0] == "support":
        middle = middle[1:]
    category = middle[0] if middle else ""
    subcategory = middle[1] if len(middle) > 1 else ""
    doc_rel_for_pa = str(rel).replace("\\", "/")
    product_area = _canonical_product_area(category, subcategory, company, doc_rel_for_pa)
    title = _extract_title(body, meta.get("title", ""))
    source_url = meta.get("source_url", "")
    body = body.strip()
    pieces = [body] if len(body) <= CHUNK_SPLIT_THRESHOLD else _split_by_h2(body)
    out: list[Chunk] = []
    doc_rel = str(md_path.relative_to(md_path.parents[len(parts)]))  # repo-relative
    for i, piece in enumerate(pieces):
        text = piece if title and title in piece else (f"# {title}\n\n{piece}" if title else piece)
        chunk_id = _hash_id(doc_rel, str(i))
        out.append(
            Chunk(
                chunk_id=chunk_id,
                company=company,
                category=category,
                subcategory=subcategory,
                product_area=product_area,
                doc_path=doc_rel,
                title=title,
                source_url=source_url,
                text=text,
            )
        )
    return out


def load_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for company_key in _COMPANY_DIRS:
        company_dir = DATA_DIR / company_key
        if not company_dir.is_dir():
            continue
        for md in sorted(company_dir.rglob("*.md")):
            if md.name == "index.md":
                continue
            chunks.extend(_load_one(md, DATA_DIR))
    return chunks


def product_area_enum(chunks: list[Chunk]) -> dict[str, list[str]]:
    """Return {company: sorted unique product_area values}."""
    out: dict[str, set[str]] = {}
    for c in chunks:
        out.setdefault(c.company, set()).add(c.product_area)
    return {k: sorted(v) for k, v in out.items()}


def stats(chunks: list[Chunk]) -> dict:
    by_company: dict[str, int] = {}
    for c in chunks:
        by_company[c.company] = by_company.get(c.company, 0) + 1
    return {"total_chunks": len(chunks), "by_company": by_company}


if __name__ == "__main__":
    import json
    cs = load_chunks()
    print(json.dumps(stats(cs), indent=2))
    print("product_area enum:")
    print(json.dumps(product_area_enum(cs), indent=2))
