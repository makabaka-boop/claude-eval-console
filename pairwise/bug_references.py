"""Durable user-provided examples for Bug prompt design and wording."""

import hashlib
import math
import re


TABLE = "pairwise_bug_prompt_references"
MAX_REFERENCE_PROMPT = 6000
MAX_RELEVANT_REFERENCES = 3
REFERENCE_SUMMARY_CHARS = 320


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _redact_secrets(value):
    text = str(value or "")
    if re.search(r"cookie\s*为", text, re.I):
        text = re.sub(
            r"(?i)(?<![\w-])([a-z][a-z0-9_-]*)=([^;\s”\"]+)",
            lambda match: f"{match.group(1)}=<REDACTED>", text)
    return text


def initialize(h):
    with h.db_connection() as db:
        db.executescript(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
          id TEXT PRIMARY KEY,
          prompt TEXT NOT NULL,
          prompt_hash TEXT NOT NULL UNIQUE,
          source TEXT NOT NULL DEFAULT 'user',
          source_label TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pairwise_bug_prompt_reference_enabled
          ON {TABLE}(enabled,created_at,id);
        """)


def store_many(h, prompts, *, source="user", source_label=""):
    """Insert normalized prompts idempotently and return import statistics."""
    initialize(h)
    values = []
    skipped = 0
    for value in prompts:
        prompt = normalize(_redact_secrets(value))
        if len(prompt) < 40:
            skipped += 1
            continue
        if len(prompt) > MAX_REFERENCE_PROMPT:
            prompt = prompt[:MAX_REFERENCE_PROMPT].rstrip()
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        values.append((digest[:12], prompt, digest, source, source_label,
                       h.now_text(), h.now_text()))
    inserted = 0
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for row in values:
            inserted += db.execute(f"""INSERT OR IGNORE INTO {TABLE}
                (id,prompt,prompt_hash,source,source_label,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""", row).rowcount
    return {"received": len(prompts), "eligible": len(values), "inserted": inserted,
            "duplicates": len(values) - inserted, "skipped": skipped,
            "total": count(h)}


def count(h):
    initialize(h)
    with h.db_connection() as db:
        return int(db.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE enabled=1").fetchone()[0])


def _terms(value):
    text = normalize(value).casefold()
    result = set(re.findall(r"[a-z][a-z0-9_.+#/-]{1,}", text))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        for size in (2, 3):
            result.update(run[index:index + size]
                          for index in range(max(0, len(run) - size + 1)))
    return result


def _summary(value):
    """Return a short extractive style example without replaying a full prompt."""
    text = normalize(value)
    if len(text) <= REFERENCE_SUMMARY_CHARS:
        return text
    return text[:REFERENCE_SUMMARY_CHARS - 1].rstrip("，,；;：: ") + "…"


def relevant(h, context, limit=MAX_RELEVANT_REFERENCES):
    """Return at most three deterministic, stack/domain-biased summaries."""
    initialize(h)
    with h.db_connection() as db:
        rows = [dict(row) for row in db.execute(
            f"SELECT * FROM {TABLE} WHERE enabled=1 ORDER BY created_at,id")]
    if not rows or limit <= 0:
        return []
    query = " ".join(str(context.get(key) or "") for key in (
        "repo_name", "project_category", "language_framework",
        "original_or_current_prompt"))
    query_terms = _terms(query)
    documents = [_terms(row["prompt"]) for row in rows]
    frequencies = {}
    for terms in documents:
        for term in terms:
            frequencies[term] = frequencies.get(term, 0) + 1
    total = len(rows)
    scored = []
    for row, terms in zip(rows, documents):
        overlap = query_terms & terms
        score = sum(math.log((total + 1) / (frequencies[term] + 1)) + 1
                    for term in overlap)
        # Exact technology names are much more useful than generic Chinese bigrams.
        score += 4 * sum(1 for term in overlap
                         if re.fullmatch(r"[a-z][a-z0-9_.+#/-]{2,}", term))
        scored.append((-score, row["created_at"], row["id"], row))
    capped_limit = min(int(limit), MAX_RELEVANT_REFERENCES)
    selected = [item[-1] for item in sorted(scored)[:min(capped_limit, len(scored))]]
    return [{
        "reference_id": f"bugref:{row['id']}",
        "task_type": "Bug 修复参考",
        "summary": _summary(row["prompt"]),
        "source": row["source"],
        "source_label": row["source_label"],
    } for row in selected]
