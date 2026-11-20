# Topic collection

`scripts/collect_topics.py` builds a local corpus for five PubMed topics. It
searches from newest to oldest and keeps a paper only when a PDF or XML file
is saved. Each saved file gets a JSON sidecar with the same stem.

## Run

From the repository root:

```console
uv run python scripts/collect_topics.py
```

The default limit is 1500 saved papers per topic. Restrict the run with
`--limit` and `--topics`:

```console
uv run python scripts/collect_topics.py --limit 100 --topics power_hydro crops_rice
```

Topic names are `power_hydro`, `power_thermal`, `crops_rice`, `crops_wheat`,
and `dairy`.

To write metadata without downloading files:

```console
uv run python scripts/collect_topics.py --metadata-only
uv run python scripts/collect_topics.py --metadata-only --skip-existing
```

`--skip-existing` keeps a non-empty topic JSONL instead of querying PubMed
again. It applies only together with `--metadata-only`.

## Output

The script writes to `D:/YYYYMMDD_paper/`, using the date the process starts.
Running it again on that same day resumes the folder. Starting it on a later
day creates a new folder and does not continue the previous one.

```text
D:/20260929_paper/
  collect.log
  power_hydro/
    pubmed.jsonl
    summary.json
    state.json
    progress.jsonl
    202609/15/10.1007_s00267-026-02598-8.pdf
    202609/15/10.1007_s00267-026-02598-8.json
```

| File | Contents |
| --- | --- |
| `pubmed.jsonl` | One saved paper per line, newest first |
| `summary.json` | Topic, query, requested count, saved count, and date span |
| `state.json` | Resume cursor, saved count, failed count, and whether the topic is done |
| `progress.jsonl` | Every DOI already considered, whether or not a file was saved |
| `collect.log` | Log for the whole run |
| `YYYYMM/DD/` | One folder per publication date |

The sidecar JSON contains `title`, `authors`, `date`, `doi`, `journal`,
`abstract`, and `source`. The filename is the DOI with characters that are
illegal in Windows paths replaced by `_`. A paper whose date cannot be parsed
is stored under `unknown/`. An XML-only fallback still gets a `.json` sidecar
beside that stem.

## Topics

Each topic is two keyword groups. Groups are combined with `AND`. Terms inside
a group are combined with `OR` and searched in the title and abstract. The
exact terms are `TOPICS` in `scripts/collect_topics.py`.

| Topic | Subject |
| --- | --- |
| `power_hydro` | Hydropower equipment, and efficiency, retrofit, or design |
| `power_thermal` | Thermal power plants, and efficiency, retrofit, or design |
| `crops_rice` | Rice (`Oryza sativa`) yield, productivity, or production cost |
| `crops_wheat` | Wheat (`Triticum aestivum`) yield, productivity, or production cost |
| `dairy` | Dairy processing, including pasteurization, fermentation, and drying |

PubMed is queried in windows of about 90 days, walking backward from today to
1980-01-01, or until the topic reaches its limit. A window that hits the
PubMed record cap is split into smaller ranges.

## What counts as saved

A paper is saved when `save_pdf` leaves a PDF or XML file on disk. The sidecar
is written after that file exists, including when the PDF comes from a
publisher `citation_pdf_url` tag. Papers without a DOI are skipped.

If the PDF or XML is already present, the script does not download it again. It
writes the sidecar if that file is missing. This covers an earlier run that
stored the PDF and recorded the download as failed.

A failed download deletes the empty date folder and any empty parent folders
inside the output root. Empty directories are also removed when a run starts.
Startup does not create a topic folder just to report that the topic has no
files yet.

## Resume

Stop the script and run the same command again on the same day.

- Papers already listed in `pubmed.jsonl` are left as they are.
- Papers recorded in `progress.jsonl` with no PDF or XML are not retried.
- Papers recorded as failed that do have a PDF or XML receive a sidecar and
  are counted as saved.
- The search cursor in `state.json` advances after each date window finishes.
  An interrupt in the middle of a window searches that window again, then
  skips DOIs already present in `progress.jsonl`.

`--metadata-only` does not use this per-paper resume. It writes the JSONL
after the topic query finishes.
