"""Tests for topic PDF collection bookkeeping."""

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "collect_topics.py"


def load_collect_topics():
    spec = importlib.util.spec_from_file_location("collect_topics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def collect(tmp_path, monkeypatch):
    module = load_collect_topics()
    monkeypatch.setattr(module, "RESULTS", tmp_path)
    return module


def test_saved_papers_does_not_create_topic_directory(collect, tmp_path):
    topic = {"name": "crops_rice", "source": "pubmed"}
    assert collect.saved_papers(topic) == []
    assert not (tmp_path / "crops_rice").exists()


def test_failed_download_leaves_no_empty_day_directory(collect, tmp_path):
    topic = {"name": "power_hydro", "source": "pubmed"}
    paper = {
        "doi": "10.1111/risa.70347",
        "date": "2026-09-03",
        "title": "Risk",
        "authors": "A",
        "journal": "J",
        "abstract": "Z",
    }

    def fail_download(paper_metadata, filepath, save_metadata=False):
        return False

    collect.save_pdf = fail_download
    assert collect.store_pdf(topic, paper) is False
    assert not (tmp_path / "power_hydro").exists()


def test_pdf_on_disk_gets_sidecar_even_if_save_pdf_returns_false(collect, tmp_path):
    topic = {"name": "power_hydro", "source": "pubmed"}
    paper = {
        "doi": "10.1371/journal.pone.0355967",
        "date": "2026-09-08",
        "title": "Rice water",
        "authors": "A",
        "journal": "PLOS",
        "abstract": "Z",
    }

    def write_without_success(paper_metadata, filepath, save_metadata=False):
        Path(filepath).write_bytes(b"%PDF-1.4")
        return False

    collect.save_pdf = write_without_success
    assert collect.store_pdf(topic, paper) is True
    pdf = (
        tmp_path / "power_hydro" / "202609" / "08" / "10.1371_journal.pone.0355967.pdf"
    )
    sidecar = json.loads(pdf.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["title"] == "Rice water"
    assert sidecar["doi"] == paper["doi"]


def test_existing_pdf_marked_failed_is_recovered(collect, tmp_path, monkeypatch):
    topic = {"name": "power_hydro", "source": "pubmed", "groups": [["hydropower"]]}
    today = date.today()
    paper = {
        "doi": "10.1007/s00267-026-02598-8",
        "date": today.isoformat(),
        "title": "Hydropower",
        "authors": "A",
        "journal": "Springer",
        "abstract": "Z",
    }
    day = tmp_path / "power_hydro" / f"{today:%Y%m}" / f"{today:%d}"
    day.mkdir(parents=True)
    (day / "10.1007_s00267-026-02598-8.pdf").write_bytes(b"%PDF-1.4")
    progress = tmp_path / "power_hydro" / "progress.jsonl"
    progress.write_text(
        json.dumps({"key": "doi:10.1007/s00267-026-02598-8", "ok": False}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(collect, "DATE_FLOOR", today - timedelta(days=1))
    monkeypatch.setattr(
        collect, "collect_pubmed_range", lambda *args, **kwargs: [paper]
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("existing PDF should not be downloaded again")

    collect.save_pdf = fail_if_called
    saved = collect.fill_topic(topic, limit=1)
    assert [item["doi"] for item in saved] == [paper["doi"]]
    assert (day / "10.1007_s00267-026-02598-8.json").exists()


def test_prune_empty_dirs_keeps_files(collect, tmp_path):
    empty = tmp_path / "crops_rice" / "202609" / "03"
    empty.mkdir(parents=True)
    kept = tmp_path / "power_hydro" / "202609" / "15"
    kept.mkdir(parents=True)
    (kept / "a.pdf").write_bytes(b"%PDF")
    collect.prune_empty_dirs(tmp_path)
    assert not (tmp_path / "crops_rice").exists()
    assert (kept / "a.pdf").exists()
