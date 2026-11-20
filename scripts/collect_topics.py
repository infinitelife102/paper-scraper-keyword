"""Collect newest papers for each topic, then download PDF and sidecar JSON.

Metadata is one JSONL file per topic, newest paper first.
Each PDF is stored by publication date, for example crops_rice/202609/27/, beside a JSON file with the same stem.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import arxiv
import pandas as pd

from paperscraper.arxiv import get_arxiv_papers_api
from paperscraper.pdf import save_pdf
from paperscraper.pubmed import get_pubmed_papers

ROOT = Path(__file__).resolve().parents[1]
RESULTS = Path(f"D:/{date.today():%Y%m%d}_paper")
LIMIT_DEFAULT = 1500
PUBMED_MAX = 9998
DATE_FLOOR = date(1980, 1, 1)

KeywordGroup = Union[str, Sequence[str]]
TOPIC_LABELS = {
    "power_hydro": "Hydropower",
    "power_thermal": "Thermal power",
    "crops_rice": "Rice",
    "crops_wheat": "Wheat",
    "dairy": "Dairy processing",
}

TOPICS: List[Dict[str, Any]] = [
    {
        "name": "power_hydro",
        "source": "pubmed",
        "groups": [
            [
                "hydropower",
                "hydroelectric",
                "hydroelectricity",
                "hydraulic turbine",
                "Francis turbine",
                "Kaplan turbine",
                "Pelton turbine",
                "draft tube",
                "turbine runner",
                "pumped storage hydropower",
            ],
            [
                "efficiency",
                "power output",
                "retrofit",
                "rehabilitation",
                "upgrade",
                "modernization",
                "design",
                "performance",
            ],
        ],
    },
    {
        "name": "power_thermal",
        "source": "pubmed",
        "groups": [
            [
                "thermal power plant",
                "coal-fired power plant",
                "combined cycle power plant",
                "steam turbine",
                "heat rate",
                "supercritical boiler",
                "ultra-supercritical",
                "Rankine cycle",
            ],
            [
                "efficiency",
                "power output",
                "retrofit",
                "upgrade",
                "repowering",
                "design",
                "performance",
            ],
        ],
    },
    {
        "name": "crops_rice",
        "source": "pubmed",
        "groups": [
            ["rice", "Oryza sativa"],
            [
                "yield",
                "grain yield",
                "productivity",
                "production cost",
                "input cost",
                "profitability",
                "cost reduction",
            ],
        ],
    },
    {
        "name": "crops_wheat",
        "source": "pubmed",
        "groups": [
            ["wheat", "Triticum aestivum"],
            [
                "yield",
                "grain yield",
                "productivity",
                "production cost",
                "input cost",
                "profitability",
                "cost reduction",
            ],
        ],
    },
    {
        "name": "dairy",
        "source": "pubmed",
        "groups": [
            ["dairy", "milk", "cheese", "yogurt", "yoghurt", "whey", "butter"],
            [
                "processing",
                "pasteurization",
                "pasteurisation",
                "fermentation",
                "spray drying",
                "membrane filtration",
                "ultrafiltration",
                "homogenization",
                "UHT",
                "cheese making",
            ],
        ],
    },
]


def setup_logging() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(RESULTS / "collect.log", encoding="utf-8"),
        ],
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("arxiv").setLevel(logging.WARNING)


def quote_term(term: str) -> str:
    return '"' + term.replace('"', "") + '"'


def arxiv_query(groups: Sequence[KeywordGroup]) -> str:
    parts = []
    for group in groups:
        if isinstance(group, str):
            parts.append(f"all:{quote_term(group)}")
        else:
            ors = " OR ".join(f"all:{quote_term(term)}" for term in group)
            parts.append(f"({ors})")
    return " AND ".join(parts)


def pubmed_clause(groups: Sequence[KeywordGroup]) -> str:
    parts = []
    for group in groups:
        if isinstance(group, str):
            parts.append(f"{quote_term(group)}[Title/Abstract]")
        else:
            ors = " OR ".join(f"{quote_term(term)}[Title/Abstract]" for term in group)
            parts.append(f"({ors})")
    return " AND ".join(parts)


def pubmed_query(groups: Sequence[KeywordGroup], start: date, end: date) -> str:
    clause = pubmed_clause(groups)
    start_s = start.strftime("%Y/%m/%d")
    end_s = end.strftime("%Y/%m/%d")
    return (
        f"{clause} AND "
        f'("{start_s}"[Date - Publication] : "{end_s}"[Date - Publication])'
    )


def norm_date(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    match = re.search(r"(\d{4})", text)
    if match:
        return f"{match.group(1)}-01-01"
    return ""


def json_ready(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (datetime, date)):
        return norm_date(value)
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def record_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    record = {key: json_ready(value) for key, value in row.items()}
    record["date"] = norm_date(record.get("date"))
    return record


def frame_to_records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    records = [record_from_row(row) for row in frame.to_dict(orient="records")]
    records.sort(key=lambda paper: paper.get("date") or "", reverse=True)
    return records


def dedupe_key(paper: Dict[str, Any]) -> str:
    doi = paper.get("doi")
    if isinstance(doi, str) and doi.strip():
        return "doi:" + doi.strip().lower()
    title = paper.get("title")
    if isinstance(title, str) and title.strip():
        return "title:" + re.sub(r"\s+", " ", title.strip().lower())
    return "row:" + json.dumps(paper, ensure_ascii=False, sort_keys=True)


def take_newest(papers: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    chosen: List[Dict[str, Any]] = []
    seen = set()
    ordered = sorted(papers, key=lambda paper: paper.get("date") or "", reverse=True)
    for paper in ordered:
        key = dedupe_key(paper)
        if key in seen:
            continue
        seen.add(key)
        chosen.append(paper)
        if len(chosen) >= limit:
            break
    return chosen


def write_jsonl(path: Path, papers: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for paper in papers:
            handle.write(json.dumps(paper, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    papers = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                papers.append(json.loads(line))
    return papers


def collect_arxiv(groups: Sequence[KeywordGroup], limit: int) -> List[Dict[str, Any]]:
    query = arxiv_query(groups)
    logging.info("arXiv query: %s", query)
    frame = get_arxiv_papers_api(
        query,
        max_results=limit,
        client_options={"num_retries": 5, "delay_seconds": 3, "page_size": 200},
        search_options={
            "sort_by": arxiv.SortCriterion.SubmittedDate,
            "sort_order": arxiv.SortOrder.Descending,
        },
    )
    return take_newest(frame_to_records(frame), limit)


def collect_pubmed_range(
    groups: Sequence[KeywordGroup], start: date, end: date, limit: int
) -> List[Dict[str, Any]]:
    query = pubmed_query(groups, start, end)
    logging.info("PubMed %s .. %s", start.isoformat(), end.isoformat())
    frame = get_pubmed_papers(query, max_results=PUBMED_MAX)
    time.sleep(0.4)
    if len(frame) >= PUBMED_MAX and (end - start).days > 7:
        mid = start + timedelta(days=(end - start).days // 2)
        newer = collect_pubmed_range(groups, mid + timedelta(days=1), end, limit)
        if len(newer) >= limit:
            return newer[:limit]
        older = collect_pubmed_range(groups, start, mid, limit - len(newer))
        return take_newest(newer + older, limit)
    if len(frame) >= PUBMED_MAX:
        logging.warning(
            "PubMed window %s..%s hit the %s-record cap, so some papers in that window may be missing.",
            start.isoformat(),
            end.isoformat(),
            PUBMED_MAX,
        )
    return frame_to_records(frame)


def collect_pubmed(groups: Sequence[KeywordGroup], limit: int) -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []
    cursor_end = date.today()
    window_days = 90
    while len(collected) < limit and cursor_end >= DATE_FLOOR:
        cursor_start = max(DATE_FLOOR, cursor_end - timedelta(days=window_days - 1))
        batch = collect_pubmed_range(
            groups, cursor_start, cursor_end, limit - len(collected)
        )
        collected.extend(batch)
        collected = take_newest(collected, limit)
        if len(collected) >= limit:
            break
        cursor_end = cursor_start - timedelta(days=1)
    return collected


def collect_topic(topic: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    if topic["source"] == "arxiv":
        return collect_arxiv(topic["groups"], limit)
    if topic["source"] == "pubmed":
        return collect_pubmed(topic["groups"], limit)
    raise ValueError(f"Unknown source: {topic['source']}")


def write_summary(
    topic: Dict[str, Any], papers: Sequence[Dict[str, Any]], limit: int, query: str
) -> None:
    dates = [paper.get("date") or "" for paper in papers if paper.get("date")]
    summary = {
        "topic": topic["name"],
        "source": topic["source"],
        "requested": limit,
        "saved": len(papers),
        "newest": dates[0] if dates else "",
        "oldest": dates[-1] if dates else "",
        "query": query,
    }
    path = RESULTS / topic["name"] / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(
        "%s: saved %s/%s, newest %s, oldest %s",
        topic["name"],
        summary["saved"],
        limit,
        summary["newest"] or "-",
        summary["oldest"] or "-",
    )


def topic_dir(name: str) -> Path:
    return RESULTS / name


def prune_empty_dirs(root: Path) -> None:
    """Remove directories that contain nothing, leaving files in place."""
    if not root.exists():
        return
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in directories:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def remove_empty_parents(path: Path, stop: Path) -> None:
    """Delete `path` and empty parents, stopping before `stop` itself."""
    current = path
    stop_resolved = stop.resolve()
    while current.exists() and current.resolve() != stop_resolved:
        if not current.is_dir() or any(current.iterdir()):
            return
        parent = current.parent
        current.rmdir()
        current = parent


def metadata_path(topic: Dict[str, Any]) -> Path:
    filename = "arxiv.jsonl" if topic["source"] == "arxiv" else "pubmed.jsonl"
    return topic_dir(topic["name"]) / filename


def state_path(name: str) -> Path:
    return topic_dir(name) / "state.json"


def progress_path(name: str) -> Path:
    return topic_dir(name) / "progress.jsonl"


def load_state(name: str) -> Dict[str, Any]:
    path = state_path(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning(
            "Could not read %s; starting that topic's cursor from today.", path
        )
        return {}


def save_state(name: str, state: Dict[str, Any]) -> None:
    path = state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def append_json_line(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_progress(name: str) -> set:
    seen = set()
    path = progress_path(name)
    if not path.exists():
        return seen
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            seen.add(json.loads(line)["key"])
        except (json.JSONDecodeError, KeyError):
            continue
    return seen


def saved_papers(topic: Dict[str, Any]) -> List[Dict[str, Any]]:
    path = metadata_path(topic)
    if not path.exists():
        return []
    papers = []
    seen = set()
    for paper in load_jsonl(path):
        key = dedupe_key(paper)
        if key in seen:
            continue
        seen.add(key)
        papers.append(paper)
    return papers


def print_counts(topics: Sequence[Dict[str, Any]], limit: int, heading: str) -> int:
    print(heading)
    total = 0
    for topic in topics:
        count = len(saved_papers(topic))
        total += count
        label = TOPIC_LABELS.get(topic["name"], topic["name"])
        print(f"  {label} ({topic['name']}): {count}/{limit}")
    print(f"  Total: {total}")
    return total


def safe_stem(doi: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*]+', "_", doi).strip(" .")
    return stem[:180] or "paper"


def download_paths(
    topic_name: str, paper: Dict[str, Any]
) -> Optional[tuple[Path, Path]]:
    doi = paper.get("doi")
    if not isinstance(doi, str) or not doi.strip():
        return None
    day_dir = paper_dir(topic_name, paper)
    stem = safe_stem(doi)
    return day_dir / f"{stem}.pdf", day_dir / f"{stem}.xml"


def download_exists(topic_name: str, paper: Dict[str, Any]) -> bool:
    paths = download_paths(topic_name, paper)
    if paths is None:
        return False
    pdf_file, xml_file = paths
    return pdf_file.exists() or xml_file.exists()


SIDECAR_FIELDS = ("title", "authors", "date", "doi", "journal", "abstract")


def paper_dir(topic_name: str, paper: Dict[str, Any]) -> Path:
    """Return topic/YYYYMM/DD from the paper date, for example crops_rice/202609/27."""
    text = norm_date(paper.get("date"))
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})$", text)
    if not match:
        return RESULTS / topic_name / "unknown"
    year, month, day = match.groups()
    return RESULTS / topic_name / f"{year}{month}" / day


def write_sidecar(paper: Dict[str, Any], pdf_file: Path, source: str) -> None:
    """Write the full collected record next to the PDF."""
    sidecar = {field: paper.get(field) for field in SIDECAR_FIELDS}
    sidecar["source"] = source
    pdf_file.with_suffix(".json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def store_pdf(topic: Dict[str, Any], paper: Dict[str, Any]) -> bool:
    """Save one PDF and its JSON. Return whether a file is now on disk."""
    doi = paper.get("doi")
    if not isinstance(doi, str) or not doi.strip():
        logging.warning("%s skipped, no DOI: %s", topic["name"], paper.get("title"))
        return False
    paths = download_paths(topic["name"], paper)
    if paths is None:
        return False
    pdf_file, xml_file = paths
    if pdf_file.exists() or xml_file.exists():
        write_sidecar(paper, pdf_file, topic["source"])
        return True
    day_dir = pdf_file.parent
    day_dir.mkdir(parents=True, exist_ok=True)
    try:
        save_pdf(paper, str(pdf_file), save_metadata=False)
    except Exception:
        logging.exception("%s failed for %s", topic["name"], doi)
    if pdf_file.exists() or xml_file.exists():
        write_sidecar(paper, pdf_file, topic["source"])
        return True
    remove_empty_parents(day_dir, RESULTS)
    return False


def iter_arxiv_newest(groups: Sequence[KeywordGroup], offset: int = 0):
    """Yield arXiv records from newest to oldest, starting after `offset` results."""
    query = arxiv_query(groups)
    logging.info("arXiv query: %s (offset %s)", query, offset)
    client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=5)
    search = arxiv.Search(
        query=query,
        max_results=100000,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )
    for result in client.results(search, offset=offset):
        arxiv_id = result.entry_id.split("/")[-1].split("v")[0]
        yield {
            "title": result.title,
            "authors": ", ".join(author.name for author in result.authors),
            "date": result.published.strftime("%Y-%m-%d"),
            "abstract": result.summary,
            "journal": result.journal_ref or "",
            "doi": f"10.48550/arXiv.{arxiv_id}",
        }


def fill_topic(topic: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """Download until `limit` PDFs exist, resuming from the saved cursor."""
    name = topic["name"]
    label = TOPIC_LABELS.get(name, name)
    kept = saved_papers(topic)
    saved_keys = {dedupe_key(paper) for paper in kept}
    seen = load_progress(name)
    state = load_state(name)
    failed = sum(1 for key in seen if key not in saved_keys)

    if len(kept) >= limit or state.get("done"):
        print(f"{label} ({name}): {len(kept)} files already saved; skipping.")
        state["done"] = True
        state["saved"] = len(kept)
        save_state(name, state)
        return kept

    cursor_text = state.get("cursor_end") or date.today().isoformat()
    print(
        f"{label} ({name}): resuming at {len(kept)}/{limit}. "
        f"Next search window ends on {cursor_text}."
    )

    def persist(
        cursor_end: Optional[str] = None, arxiv_offset: Optional[int] = None
    ) -> None:
        if cursor_end is not None:
            state["cursor_end"] = cursor_end
        if arxiv_offset is not None:
            state["arxiv_offset"] = arxiv_offset
        state["saved"] = len(kept)
        state["failed"] = failed
        state["done"] = len(kept) >= limit
        save_state(name, state)

    def consider(paper: Dict[str, Any]) -> None:
        nonlocal failed
        key = dedupe_key(paper)
        if key in saved_keys:
            return
        already_seen = key in seen
        if already_seen and not download_exists(name, paper):
            return
        ok = store_pdf(topic, paper)
        if ok and key not in saved_keys:
            kept.append(paper)
            saved_keys.add(key)
            append_json_line(metadata_path(topic), paper)
            if already_seen:
                failed = max(failed - 1, 0)
        elif not ok and not already_seen:
            failed += 1
        if not already_seen:
            append_json_line(progress_path(name), {"key": key, "ok": ok})
            seen.add(key)
        persist()
        if ok:
            print(f"  {label}: {len(kept)}/{limit} (failed {failed})")

    if topic["source"] == "pubmed":
        cursor_end = date.fromisoformat(
            state.get("cursor_end") or date.today().isoformat()
        )
        while len(kept) < limit and cursor_end >= DATE_FLOOR:
            cursor_start = max(DATE_FLOOR, cursor_end - timedelta(days=89))
            batch = collect_pubmed_range(
                topic["groups"], cursor_start, cursor_end, PUBMED_MAX
            )
            for paper in batch:
                consider(paper)
                if len(kept) >= limit:
                    break
            cursor_end = cursor_start - timedelta(days=1)
            persist(cursor_end=cursor_end.isoformat())
        if cursor_end < DATE_FLOOR:
            state["done"] = True
            save_state(name, state)
    else:
        offset = int(state.get("arxiv_offset") or 0)
        for paper in iter_arxiv_newest(topic["groups"], offset=offset):
            offset += 1
            consider(paper)
            persist(arxiv_offset=offset)
            if len(kept) >= limit:
                break
        else:
            state["done"] = True
            save_state(name, state)

    print(f"{label} ({name}): saved {len(kept)}/{limit}, failed {failed}")
    return kept


def selected_topics(names: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    if not names:
        return TOPICS
    wanted = set(names)
    topics = [topic for topic in TOPICS if topic["name"] in wanted]
    missing = wanted - {topic["name"] for topic in topics}
    if missing:
        raise SystemExit(f"Unknown topic(s): {', '.join(sorted(missing))}")
    return topics


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect newest papers by topic.")
    parser.add_argument("--limit", type=int, default=LIMIT_DEFAULT)
    parser.add_argument("--topics", nargs="*", default=None)
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--pdf-only", action="store_true")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Keep an existing JSONL instead of querying again.",
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    setup_logging()
    prune_empty_dirs(RESULTS)
    topics = selected_topics(args.topics)
    print_counts(topics, args.limit, "Starting status")

    for topic in topics:
        jsonl_path = metadata_path(topic)
        query = (
            arxiv_query(topic["groups"])
            if topic["source"] == "arxiv"
            else pubmed_clause(topic["groups"])
        )
        if args.metadata_only:
            if args.skip_existing and jsonl_path.exists() and jsonl_path.stat().st_size:
                papers = load_jsonl(jsonl_path)
                logging.info(
                    "%s metadata already exists (%s papers)", topic["name"], len(papers)
                )
            else:
                try:
                    papers = collect_topic(topic, args.limit)
                except Exception:
                    logging.exception("%s metadata collection failed", topic["name"])
                    continue
                write_jsonl(jsonl_path, papers)
                write_summary(topic, papers, args.limit, query)
            continue
        try:
            papers = fill_topic(topic, args.limit)
        except Exception:
            logging.exception("%s download failed", topic["name"])
            print_counts(
                topics, args.limit, "Stopped. Run the same command again to resume."
            )
            raise
        write_jsonl(jsonl_path, papers)
        write_summary(topic, papers, args.limit, query)
    print_counts(topics, args.limit, "Downloads by topic")


if __name__ == "__main__":
    main()
