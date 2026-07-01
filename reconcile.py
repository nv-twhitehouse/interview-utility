#!/usr/bin/env python3
"""Build a deterministic, local reconciliation of Outlook and interview-sheet data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse


DEFAULT_DB = Path(__file__).with_name("outlook_objects.sqlite3")
DEFAULT_REPORT = Path(__file__).with_name("reconciliation_report.csv")
DEFAULT_REVIEW_QUEUE = Path(__file__).with_name("agent_review_queue.json")
RULE_VERSION = "1"
BOOKINGS_SENDER = "chataboutagentswithnvidia@nvidia.onmicrosoft.com"
BOOKINGS_SERVICE = "chat about agents with nvidia"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS outlook_message_bodies (
    source       TEXT NOT NULL,
    object_id    TEXT NOT NULL,
    body_text    TEXT NOT NULL,
    full_json    TEXT NOT NULL CHECK (json_valid(full_json)),
    hydrated_at  TEXT NOT NULL,
    PRIMARY KEY (source, object_id),
    FOREIGN KEY (source, object_id) REFERENCES objects(source, object_id)
);

CREATE TABLE IF NOT EXISTS outlook_engagements (
    engagement_id     INTEGER PRIMARY KEY,
    normalized_name   TEXT NOT NULL UNIQUE,
    candidate_name    TEXT NOT NULL,
    first_activity_at TEXT,
    last_activity_at  TEXT,
    event_count       INTEGER NOT NULL,
    inbox_count       INTEGER NOT NULL,
    sent_count        INTEGER NOT NULL,
    built_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS engagement_objects (
    engagement_id INTEGER NOT NULL,
    source        TEXT NOT NULL,
    object_id     TEXT NOT NULL,
    PRIMARY KEY (engagement_id, source, object_id),
    FOREIGN KEY (engagement_id) REFERENCES outlook_engagements(engagement_id),
    FOREIGN KEY (source, object_id) REFERENCES objects(source, object_id)
);

CREATE TABLE IF NOT EXISTS engagement_match_candidates (
    engagement_id INTEGER NOT NULL,
    linkedin_url  TEXT NOT NULL,
    score         REAL NOT NULL,
    method        TEXT NOT NULL,
    candidate_rank INTEGER NOT NULL,
    generated_at  TEXT NOT NULL,
    PRIMARY KEY (engagement_id, linkedin_url),
    FOREIGN KEY (engagement_id) REFERENCES outlook_engagements(engagement_id),
    FOREIGN KEY (linkedin_url) REFERENCES interviews(linkedin_url)
);

CREATE TABLE IF NOT EXISTS engagement_sheet_matches (
    engagement_id INTEGER PRIMARY KEY,
    linkedin_url  TEXT,
    score         REAL,
    method        TEXT,
    match_status  TEXT NOT NULL CHECK (
        match_status IN ('automatic', 'needs_review', 'confirmed', 'unmatched', 'rejected')
    ),
    matched_at    TEXT NOT NULL,
    FOREIGN KEY (engagement_id) REFERENCES outlook_engagements(engagement_id),
    FOREIGN KEY (linkedin_url) REFERENCES interviews(linkedin_url)
);

CREATE TABLE IF NOT EXISTS engagement_state_evidence (
    evidence_id     INTEGER PRIMARY KEY,
    engagement_id   INTEGER NOT NULL,
    area            TEXT NOT NULL CHECK (area IN ('scheduling', 'incentive')),
    inferred_state  TEXT NOT NULL,
    inferred_value  TEXT,
    occurred_at     TEXT,
    source          TEXT NOT NULL,
    object_id       TEXT NOT NULL,
    conversation_id TEXT,
    rule_id          TEXT NOT NULL,
    rule_version     TEXT NOT NULL,
    confidence       REAL NOT NULL,
    excerpt          TEXT,
    input_hash       TEXT NOT NULL,
    FOREIGN KEY (engagement_id) REFERENCES outlook_engagements(engagement_id),
    FOREIGN KEY (source, object_id) REFERENCES objects(source, object_id)
);

CREATE INDEX IF NOT EXISTS state_evidence_engagement_idx
    ON engagement_state_evidence(engagement_id, area, occurred_at);

CREATE TABLE IF NOT EXISTS engagement_current_state (
    engagement_id         INTEGER PRIMARY KEY,
    scheduling_state      TEXT NOT NULL,
    scheduling_confidence REAL,
    scheduling_evidence_id INTEGER,
    incentive_state       TEXT NOT NULL,
    incentive_confidence  REAL,
    incentive_evidence_id INTEGER,
    requested_incentive   TEXT,
    evaluated_at          TEXT NOT NULL,
    FOREIGN KEY (engagement_id) REFERENCES outlook_engagements(engagement_id),
    FOREIGN KEY (scheduling_evidence_id) REFERENCES engagement_state_evidence(evidence_id),
    FOREIGN KEY (incentive_evidence_id) REFERENCES engagement_state_evidence(evidence_id)
);

CREATE TABLE IF NOT EXISTS reconciliation_findings (
    finding_id      INTEGER PRIMARY KEY,
    engagement_id   INTEGER NOT NULL,
    linkedin_url    TEXT NOT NULL,
    field_name      TEXT NOT NULL,
    sheet_value     TEXT,
    observed_value  TEXT,
    finding_status  TEXT NOT NULL CHECK (
        finding_status IN ('match', 'sheet_missing', 'conflict', 'sheet_only', 'insufficient_evidence')
    ),
    confidence      REAL,
    evidence_id     INTEGER,
    detail          TEXT,
    generated_at    TEXT NOT NULL,
    UNIQUE (engagement_id, field_name),
    FOREIGN KEY (engagement_id) REFERENCES outlook_engagements(engagement_id),
    FOREIGN KEY (linkedin_url) REFERENCES interviews(linkedin_url),
    FOREIGN KEY (evidence_id) REFERENCES engagement_state_evidence(evidence_id)
);

CREATE TABLE IF NOT EXISTS agent_review_queue (
    review_id       INTEGER PRIMARY KEY,
    engagement_id   INTEGER,
    review_type     TEXT NOT NULL,
    input_hash      TEXT NOT NULL UNIQUE,
    payload_json    TEXT NOT NULL CHECK (json_valid(payload_json)),
    review_status   TEXT NOT NULL CHECK (
        review_status IN ('pending', 'resolved', 'dismissed', 'superseded')
    ),
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolution_json TEXT CHECK (resolution_json IS NULL OR json_valid(resolution_json)),
    FOREIGN KEY (engagement_id) REFERENCES outlook_engagements(engagement_id)
);

CREATE VIEW IF NOT EXISTS engagement_message_timeline AS
SELECT
    eo.engagement_id,
    o.conversation_id,
    o.source,
    o.object_id,
    coalesce(o.sent_at, o.received_at, o.event_or_sent_at) AS occurred_at,
    o.sender_name,
    o.sender_address,
    o.subject,
    b.body_text,
    b.hydrated_at
FROM engagement_objects AS eo
JOIN objects AS o ON o.source = eo.source AND o.object_id = eo.object_id
LEFT JOIN outlook_message_bodies AS b
  ON b.source = o.source AND b.object_id = o.object_id
WHERE o.object_type = 'message';

CREATE VIEW IF NOT EXISTS outlook_thread_summary AS
SELECT
    engagement_id,
    conversation_id,
    min(occurred_at) AS first_message_at,
    max(occurred_at) AS last_message_at,
    count(*) AS message_count,
    sum(source = 'inbox') AS inbox_count,
    sum(source = 'sent') AS sent_count
FROM engagement_message_timeline
GROUP BY engagement_id, conversation_id;
"""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    area: str
    state: str
    pattern: re.Pattern[str]
    confidence: float
    sources: tuple[str, ...] = ("inbox", "sent")
    incentive: str | None = None


RULES = (
    Rule("schedule_no_show", "scheduling", "no_show", re.compile(r"\b(no[ -]?show|did not (show|join|attend)|didn['’]t (show|join|attend)|miss(?:ed|ing) the (meeting|call|touch base))\b", re.I), 0.98),
    Rule("schedule_cancelled", "scheduling", "cancelled", re.compile(r"\b(cancel|cancelled|canceled|cancellation|declined)\b", re.I), 0.96),
    Rule("schedule_reschedule_requested", "scheduling", "reschedule_requested", re.compile(r"\b(reschedul(?:e|ed|ing)|different time|another time|move (?:this|the) meeting)\b", re.I), 0.9),
    Rule("schedule_completed", "scheduling", "completed", re.compile(r"\b(interview (?:is )?complete[sd]?|completed the interview|thanks for (?:speaking|chatting|your time)|thank you (?:very much )?for (?:talking|chatting|taking (?:the )?time to (?:chat|talk)|your time)|enjoyed (?:our |the )?(?:conversation|chat))\b", re.I), 0.93),
    Rule("schedule_booked", "scheduling", "scheduled", re.compile(r"\b(new booking|booking confirmed|meeting (?:is )?scheduled|accepted:)\b", re.I), 0.94),
    Rule("incentive_problem", "incentive", "problem", re.compile(r"\b(expired|invalid|not working|doesn['’]t work|issue with (?:the )?code|problem with (?:the )?code)\b", re.I), 0.98),
    Rule("incentive_received", "incentive", "received", re.compile(r"\b(i (?:have )?received|received (?:it|the (?:gift|shirt|code))|redeemed|ordered successfully|order(?:ed)? complete|got (?:it|the gift)|code worked)\b", re.I), 0.93, ("inbox",)),
    Rule("incentive_sent", "incentive", "sent", re.compile(r"\b(code (?:was |has been )?sent|sent (?:you |the )?(?:a )?code|here(?:'s| is) (?:your|the) code|code (?:is )?below|enter\s+this\s+code)\b", re.I), 0.95, ("sent",)),
    Rule("incentive_choice", "incentive", "requested", re.compile(r"\b(i(?:'d|d| would) (?:like|love|prefer|choose|take|be interested)|i(?:'ll| will|'m going to| am going to) (?:go (?:for|with)|take|choose)|my (?:choice|preference)|i choose)\b", re.I), 0.94, ("inbox",)),
    Rule("incentive_offered", "incentive", "options_offered", re.compile(r"\b(which (?:option|gift)|choose (?:between|from)|would you (?:like|prefer)|gift options?)\b", re.I), 0.9, ("sent",)),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--review-queue", type=Path, default=DEFAULT_REVIEW_QUEUE)
    parser.add_argument(
        "--hydrate",
        action="store_true",
        help="retrieve full bodies for messages not yet hydrated",
    )
    parser.add_argument(
        "--hydrate-limit",
        type=int,
        default=0,
        help="maximum messages to hydrate; 0 means all",
    )
    return parser.parse_args()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(value: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(character for character in decomposed.casefold() if character.isalnum())


def name_tokens(value: str | None) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", value or "")
    return re.findall(r"[a-z0-9]+", decomposed.casefold())


def linkedin_slug(url: str) -> str:
    path = unquote(urlparse(url).path).strip("/")
    slug = path.split("/")[-1] if path else ""
    slug = re.sub(r"(?:-|_)?[0-9a-f]{6,}$", "", slug, flags=re.I)
    return slug


def match_score(outlook_name: str, sheet_name: str | None, linkedin_url: str) -> tuple[float, str]:
    outlook_compact = normalize_name(outlook_name)
    sheet_compact = normalize_name(sheet_name)
    slug = linkedin_slug(linkedin_url)
    slug_compact = normalize_name(slug)

    candidates: list[tuple[float, str]] = []
    if sheet_compact:
        if outlook_compact == sheet_compact:
            candidates.append((1.0, "sheet_name_exact"))
        else:
            candidates.append(
                (SequenceMatcher(None, outlook_compact, sheet_compact).ratio() * 0.97, "sheet_name_fuzzy")
            )
    if slug_compact:
        ratio = SequenceMatcher(None, outlook_compact, slug_compact).ratio()
        candidates.append((ratio * 0.96, "linkedin_slug_fuzzy"))
        tokens = name_tokens(outlook_name)
        if len(tokens) >= 2 and slug_compact.startswith(tokens[0]) and tokens[-1] in slug_compact:
            candidates.append((0.97, "linkedin_first_last"))
    return max(candidates, default=(0.0, "no_signal"), key=lambda value: value[0])


def open_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection


def unwrap_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise RuntimeError("outlook-cli returned an error or invalid response")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("outlook-cli response has no message object")
    return data


def hydrate_messages(connection: sqlite3.Connection, limit: int) -> tuple[int, list[str]]:
    sql = """
        SELECT o.source, o.object_id
        FROM objects AS o
        LEFT JOIN outlook_message_bodies AS b
          ON b.source = o.source AND b.object_id = o.object_id
        WHERE o.object_type = 'message' AND b.object_id IS NULL
        ORDER BY o.event_or_sent_at, o.source, o.object_id
    """
    rows = connection.execute(sql).fetchall()
    if limit > 0:
        rows = rows[:limit]
    errors: list[str] = []
    hydrated = 0
    for index, row in enumerate(rows, start=1):
        command = [
            "outlook-cli",
            "message",
            "read",
            row["object_id"],
            "--fields",
            "id,body,bodyPreview,conversationId,subject,sentDateTime,receivedDateTime",
            "--json",
            "--utc",
        ]
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{row['source']}:{row['object_id']}: timed out after 60s")
            continue
        if result.returncode:
            errors.append(f"{row['source']}:{row['object_id']}: exit {result.returncode}")
            continue
        try:
            data = unwrap_data(json.loads(result.stdout))
            body = data.get("body", {})
            body_text = body.get("content", "") if isinstance(body, dict) else str(body or "")
            connection.execute(
                """
                INSERT OR REPLACE INTO outlook_message_bodies
                    (source, object_id, body_text, full_json, hydrated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["source"],
                    row["object_id"],
                    body_text,
                    json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    now(),
                ),
            )
            connection.commit()
            hydrated += 1
            print(f"Hydrated message {index}/{len(rows)}", flush=True)
        except (json.JSONDecodeError, RuntimeError) as error:
            errors.append(f"{row['source']}:{row['object_id']}: {error}")
    return hydrated, errors


def build_engagements(connection: sqlite3.Connection) -> None:
    built_at = now()
    rows = connection.execute(
        """
        SELECT source, object_id, candidate_name, event_or_sent_at
        FROM objects
        WHERE candidate_name IS NOT NULL AND trim(candidate_name) <> ''
        ORDER BY event_or_sent_at, source, object_id
        """
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    display_names: dict[str, str] = {}
    for row in rows:
        normalized = normalize_name(row["candidate_name"])
        grouped.setdefault(normalized, []).append(row)
        display_names.setdefault(normalized, row["candidate_name"].strip())

    connection.execute("DELETE FROM reconciliation_findings")
    connection.execute("DELETE FROM engagement_current_state")
    connection.execute("DELETE FROM engagement_state_evidence")
    connection.execute("DELETE FROM engagement_match_candidates")
    connection.execute("DELETE FROM engagement_objects")

    active_ids: set[int] = set()
    for normalized, objects in grouped.items():
        timestamps = [row["event_or_sent_at"] for row in objects if row["event_or_sent_at"]]
        counts = {source: sum(row["source"] == source for row in objects) for source in ("calendar", "inbox", "sent")}
        connection.execute(
            """
            INSERT INTO outlook_engagements (
                normalized_name, candidate_name, first_activity_at, last_activity_at,
                event_count, inbox_count, sent_count, built_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_name) DO UPDATE SET
                candidate_name = excluded.candidate_name,
                first_activity_at = excluded.first_activity_at,
                last_activity_at = excluded.last_activity_at,
                event_count = excluded.event_count,
                inbox_count = excluded.inbox_count,
                sent_count = excluded.sent_count,
                built_at = excluded.built_at
            """,
            (
                normalized,
                display_names[normalized],
                min(timestamps) if timestamps else None,
                max(timestamps) if timestamps else None,
                counts["calendar"],
                counts["inbox"],
                counts["sent"],
                built_at,
            ),
        )
        engagement_id = int(
            connection.execute(
                "SELECT engagement_id FROM outlook_engagements WHERE normalized_name = ?",
                (normalized,),
            ).fetchone()[0]
        )
        active_ids.add(engagement_id)
        connection.executemany(
            "INSERT INTO engagement_objects (engagement_id, source, object_id) VALUES (?, ?, ?)",
            [(engagement_id, row["source"], row["object_id"]) for row in objects],
        )

    stale_ids = [
        row[0]
        for row in connection.execute("SELECT engagement_id FROM outlook_engagements")
        if row[0] not in active_ids
    ]
    for engagement_id in stale_ids:
        connection.execute(
            "UPDATE agent_review_queue SET engagement_id = NULL WHERE engagement_id = ?",
            (engagement_id,),
        )
        connection.execute(
            "DELETE FROM engagement_sheet_matches WHERE engagement_id = ?",
            (engagement_id,),
        )
        connection.execute(
            "DELETE FROM outlook_engagements WHERE engagement_id = ?",
            (engagement_id,),
        )


def build_matches(connection: sqlite3.Connection) -> None:
    generated_at = now()
    engagements = connection.execute(
        "SELECT engagement_id, candidate_name FROM outlook_engagements ORDER BY engagement_id"
    ).fetchall()
    interviews = connection.execute(
        "SELECT linkedin_url, candidate_name FROM interviews ORDER BY linkedin_url"
    ).fetchall()

    # Confirmed matches survive deterministic recomputation when their engagement name still exists.
    confirmed_by_name = {
        row["normalized_name"]: dict(row)
        for row in connection.execute(
            """
            SELECT e.normalized_name, m.linkedin_url, m.score, m.method, m.match_status
            FROM engagement_sheet_matches AS m
            JOIN outlook_engagements AS e USING (engagement_id)
            WHERE m.match_status = 'confirmed'
            """
        )
    }
    connection.execute("DELETE FROM engagement_sheet_matches WHERE match_status <> 'confirmed'")

    for engagement in engagements:
        scored = []
        for interview in interviews:
            score, method = match_score(
                engagement["candidate_name"],
                interview["candidate_name"],
                interview["linkedin_url"],
            )
            scored.append((score, method, interview["linkedin_url"]))
        scored.sort(key=lambda item: (-item[0], item[2]))
        for rank, (score, method, linkedin_url) in enumerate(scored[:5], start=1):
            connection.execute(
                """
                INSERT INTO engagement_match_candidates
                    (engagement_id, linkedin_url, score, method, candidate_rank, generated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (engagement["engagement_id"], linkedin_url, score, method, rank, generated_at),
            )

        top_score, top_method, top_url = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        normalized = normalize_name(engagement["candidate_name"])
        if normalized in confirmed_by_name:
            continue
        if top_score >= 0.88 and top_score - runner_up >= 0.05:
            status = "automatic"
            selected_url = top_url
        elif top_score >= 0.60 and top_score - runner_up >= 0.02:
            status = "needs_review"
            selected_url = top_url
        else:
            status = "unmatched"
            selected_url = None
        connection.execute(
            """
            INSERT OR REPLACE INTO engagement_sheet_matches
                (engagement_id, linkedin_url, score, method, match_status, matched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                engagement["engagement_id"],
                selected_url,
                top_score,
                top_method,
                status,
                generated_at,
            ),
        )


def newest_message_text(body: str) -> str:
    split_patterns = (
        r"(?im)^-{3,}\s*original message\s*-{3,}\s*$",
        r"(?im)^\s*(?:>\s*)?(?:\*\*)?from:(?:\*\*)?\s+.+$",
        r"(?im)^\s*(?:>\s*)?on .+ wrote:\s*$",
    )
    end = len(body)
    for pattern in split_patterns:
        match = re.search(pattern, body)
        if match:
            end = min(end, match.start())
    return body[:end].strip()


def excerpt_for(text: str, match: re.Match[str], radius: int = 100) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def detect_incentive(text: str) -> str | None:
    options = (
        ("T-Shirt", re.compile(r"\b(t[ -]?shirt|shirt)\b", re.I)),
        ("DLI", re.compile(r"\bDLI\b|deep learning institute|\b(?:free )?courses?\b", re.I)),
        ("Brev", re.compile(r"\bbrev\b", re.I)),
    )
    matches = [name for name, pattern in options if pattern.search(text)]
    return matches[0] if len(matches) == 1 else None


def parse_booking_notification(
    subject: str | None, sender_address: str | None, body: str | None
) -> dict[str, str] | None:
    """Extract stable fields from outlook-cli's Markdown rendering of Bookings mail."""
    if not subject or not subject.casefold().startswith("new booking:"):
        return None
    if (sender_address or "").casefold() != BOOKINGS_SENDER:
        return None

    text = (body or "").replace("\u00a0", " ")
    if "Powered by Microsoft Bookings" not in text:
        return None

    result: dict[str, str] = {}
    interviewer = re.search(
        rf"{re.escape(BOOKINGS_SERVICE)}\s+with\s*\n+\s*([^\n]+)",
        text,
        flags=re.IGNORECASE,
    )
    if interviewer:
        result["interviewer"] = interviewer.group(1).strip()

    scheduled = re.search(
        r"(?m)^\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
        r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s*$",
        text,
    )
    if scheduled:
        try:
            parsed = datetime.strptime(scheduled.group(1), "%B %d, %Y")
            result["date_scheduled"] = f"{parsed.month}/{parsed.day}/{parsed.year}"
        except ValueError:
            pass

    teams = re.search(
        r"\[Join your appointment\]\((https://teams\.microsoft\.com/[^)]+)\)",
        text,
        flags=re.IGNORECASE,
    )
    if teams:
        result["teams_link"] = teams.group(1).strip()

    return result or None


def add_evidence(
    connection: sqlite3.Connection,
    engagement_id: int,
    area: str,
    state: str,
    value: str | None,
    occurred_at: str | None,
    source: str,
    object_id: str,
    conversation_id: str | None,
    rule_id: str,
    confidence: float,
    excerpt: str,
) -> int:
    digest = hashlib.sha256(
        json.dumps(
            [engagement_id, area, state, value, source, object_id, rule_id, excerpt],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    cursor = connection.execute(
        """
        INSERT INTO engagement_state_evidence (
            engagement_id, area, inferred_state, inferred_value, occurred_at,
            source, object_id, conversation_id, rule_id, rule_version,
            confidence, excerpt, input_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            engagement_id,
            area,
            state,
            value,
            occurred_at,
            source,
            object_id,
            conversation_id,
            rule_id,
            RULE_VERSION,
            confidence,
            excerpt,
            digest,
        ),
    )
    return int(cursor.lastrowid)


def infer_states(connection: sqlite3.Connection) -> None:
    objects = connection.execute(
        """
        SELECT eo.engagement_id, o.*, b.body_text
        FROM engagement_objects AS eo
        JOIN objects AS o ON o.source = eo.source AND o.object_id = eo.object_id
        LEFT JOIN outlook_message_bodies AS b
          ON b.source = o.source AND b.object_id = o.object_id
        ORDER BY eo.engagement_id, o.event_or_sent_at, o.source, o.object_id
        """
    ).fetchall()
    by_engagement: dict[int, list[sqlite3.Row]] = {}
    for row in objects:
        by_engagement.setdefault(row["engagement_id"], []).append(row)

    evaluated_at = now()
    for engagement_id, rows in by_engagement.items():
        evidence_ids: list[int] = []
        for row in rows:
            raw = json.loads(row["raw_json"])
            conversation_id = raw.get("conversationId")
            if row["source"] == "calendar":
                state = "cancelled" if raw.get("isCancelled") else "scheduled"
                confidence = 0.99 if state == "cancelled" else 0.98
                evidence_ids.append(
                    add_evidence(
                        connection,
                        engagement_id,
                        "scheduling",
                        state,
                        None,
                        row["event_or_sent_at"],
                        row["source"],
                        row["object_id"],
                        None,
                        f"calendar_{state}",
                        confidence,
                        row["subject"] or "",
                    )
                )
                continue

            text = newest_message_text(row["body_text"] or raw.get("bodyPreview") or "")
            searchable = re.sub(r"\s+", " ", f"{row['subject'] or ''}\n{text}").strip()
            sender_address = (row["sender_address"] or "").casefold()
            direction = (
                "sent"
                if sender_address.endswith("@nvidia.com")
                or sender_address.endswith("@nvidia.onmicrosoft.com")
                else "inbox"
            )
            for rule in RULES:
                if direction not in rule.sources:
                    continue
                match = rule.pattern.search(searchable)
                if not match:
                    continue
                incentive = rule.incentive
                if rule.area == "incentive" and rule.state in {"requested", "sent", "received"}:
                    incentive = incentive or detect_incentive(searchable)
                evidence_ids.append(
                    add_evidence(
                        connection,
                        engagement_id,
                        rule.area,
                        rule.state,
                        incentive,
                        row["event_or_sent_at"],
                        row["source"],
                        row["object_id"],
                        conversation_id,
                        rule.rule_id,
                        rule.confidence,
                        excerpt_for(searchable, match),
                    )
                )

        current: dict[str, sqlite3.Row | None] = {"scheduling": None, "incentive": None}
        for area in current:
            current[area] = connection.execute(
                """
                SELECT * FROM engagement_state_evidence
                WHERE engagement_id = ? AND area = ?
                ORDER BY coalesce(occurred_at, '') DESC, confidence DESC, evidence_id DESC
                LIMIT 1
                """,
                (engagement_id, area),
            ).fetchone()
        schedule = current["scheduling"]
        incentive = current["incentive"]
        requested = connection.execute(
            """
            SELECT inferred_value FROM engagement_state_evidence
            WHERE engagement_id = ? AND area = 'incentive' AND inferred_value IS NOT NULL
            ORDER BY coalesce(occurred_at, '') DESC, confidence DESC, evidence_id DESC
            LIMIT 1
            """,
            (engagement_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO engagement_current_state (
                engagement_id, scheduling_state, scheduling_confidence,
                scheduling_evidence_id, incentive_state, incentive_confidence,
                incentive_evidence_id, requested_incentive, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                engagement_id,
                schedule["inferred_state"] if schedule else "unknown",
                schedule["confidence"] if schedule else None,
                schedule["evidence_id"] if schedule else None,
                incentive["inferred_state"] if incentive else "unknown",
                incentive["confidence"] if incentive else None,
                incentive["evidence_id"] if incentive else None,
                requested["inferred_value"] if requested else None,
                evaluated_at,
            ),
        )


def graph_value(raw: dict[str, Any], *path: str) -> str | None:
    value: Any = raw
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, str) and value.strip() else None


def format_sheet_date(value: str | None) -> str | None:
    if not value:
        return None
    iso_date = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:T|$)", value)
    if iso_date:
        year, month, day = (int(part) for part in iso_date.groups())
        return f"{month}/{day}/{year}"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return f"{parsed.month}/{parsed.day}/{parsed.year}"
    except ValueError:
        return value


def normalized_comparison(field: str, value: str | None) -> str:
    if not value:
        return ""
    compact = re.sub(r"\s+", " ", value.strip()).casefold()
    if field.startswith("date_"):
        for pattern in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(compact, pattern).date().isoformat()
            except ValueError:
                pass
    return compact.rstrip("/")


def finding_status(field: str, sheet: str | None, observed: str | None) -> str:
    if observed and sheet:
        return "match" if normalized_comparison(field, sheet) == normalized_comparison(field, observed) else "conflict"
    if observed:
        return "sheet_missing"
    if sheet:
        return "sheet_only"
    return "insufficient_evidence"


def event_observations(connection: sqlite3.Connection, engagement_id: int) -> dict[str, tuple[str | None, float, int | None]]:
    events = connection.execute(
        """
        SELECT o.raw_json, o.object_id, o.event_or_sent_at, s.evidence_id
        FROM engagement_objects AS eo
        JOIN objects AS o ON o.source = eo.source AND o.object_id = eo.object_id
        LEFT JOIN engagement_state_evidence AS s
          ON s.engagement_id = eo.engagement_id AND s.source = o.source
         AND s.object_id = o.object_id AND s.area = 'scheduling'
        WHERE eo.engagement_id = ? AND o.source = 'calendar'
        ORDER BY o.event_or_sent_at DESC
        """,
        (engagement_id,),
    ).fetchall()
    active = []
    for event in events:
        raw = json.loads(event["raw_json"])
        if not raw.get("isCancelled"):
            active.append((event, raw))
    observations: dict[str, tuple[str | None, float, int | None]] = {}
    if active:
        event, raw = active[0]
        start = graph_value(raw, "start", "dateTime") or event["event_or_sent_at"]
        teams = (
            graph_value(raw, "onlineMeeting", "joinUrl")
            or graph_value(raw, "onlineMeetingUrl")
            or graph_value(raw, "location", "locationUri")
        )
        observations.update(
            {
                "date_scheduled": (
                    format_sheet_date(start),
                    0.98,
                    event["evidence_id"],
                ),
                "teams_link": (teams, 0.98 if teams else 0.0, event["evidence_id"]),
            }
        )

    bookings = connection.execute(
        """
        SELECT o.subject, o.sender_address, o.event_or_sent_at, b.body_text,
               s.evidence_id
        FROM engagement_objects AS eo
        JOIN objects AS o ON o.source = eo.source AND o.object_id = eo.object_id
        LEFT JOIN outlook_message_bodies AS b
          ON b.source = o.source AND b.object_id = o.object_id
        LEFT JOIN engagement_state_evidence AS s
          ON s.engagement_id = eo.engagement_id AND s.source = o.source
         AND s.object_id = o.object_id AND s.rule_id = 'schedule_booked'
        WHERE eo.engagement_id = ? AND o.source = 'inbox'
          AND lower(o.sender_address) = ?
          AND lower(o.subject) LIKE 'new booking:%'
        ORDER BY o.event_or_sent_at DESC, o.object_id DESC
        """,
        (engagement_id, BOOKINGS_SENDER),
    ).fetchall()
    for booking in bookings:
        parsed = parse_booking_notification(
            booking["subject"], booking["sender_address"], booking["body_text"]
        )
        if not parsed:
            continue
        for field, value in parsed.items():
            observations.setdefault(field, (value, 0.97, booking["evidence_id"]))
        break
    return observations


def build_findings(connection: sqlite3.Connection) -> None:
    generated_at = now()
    matches = connection.execute(
        """
        SELECT m.engagement_id, m.linkedin_url, m.match_status,
               i.date_scheduled, i.teams_link, i.interviewer, i.date_completed,
               i.incentive_status, i.requested_incentive,
               s.scheduling_state, s.scheduling_confidence, s.scheduling_evidence_id,
               s.incentive_state, s.incentive_confidence, s.incentive_evidence_id,
               s.requested_incentive AS observed_requested_incentive
        FROM engagement_sheet_matches AS m
        JOIN interviews AS i ON i.linkedin_url = m.linkedin_url
        JOIN engagement_current_state AS s ON s.engagement_id = m.engagement_id
        WHERE m.match_status IN ('automatic', 'confirmed')
        """
    ).fetchall()
    incentive_labels = {
        "options_offered": "Asked",
        "requested": "Replied",
        "sent": "Sent",
        "received": "Received",
        "problem": "Problem",
    }
    for row in matches:
        observations = event_observations(connection, row["engagement_id"])
        if row["scheduling_state"] == "completed":
            evidence = connection.execute(
                "SELECT occurred_at FROM engagement_state_evidence WHERE evidence_id = ?",
                (row["scheduling_evidence_id"],),
            ).fetchone()
            observations["date_completed"] = (
                format_sheet_date(evidence["occurred_at"]) if evidence else None,
                row["scheduling_confidence"],
                row["scheduling_evidence_id"],
            )
        observations["incentive_status"] = (
            incentive_labels.get(row["incentive_state"]),
            row["incentive_confidence"] or 0.0,
            row["incentive_evidence_id"],
        )
        observations["requested_incentive"] = (
            row["observed_requested_incentive"],
            row["incentive_confidence"] or 0.0,
            row["incentive_evidence_id"],
        )
        for field, (observed, confidence, evidence_id) in observations.items():
            sheet = row[field]
            status = finding_status(field, sheet, observed)
            connection.execute(
                """
                INSERT INTO reconciliation_findings (
                    engagement_id, linkedin_url, field_name, sheet_value, observed_value,
                    finding_status, confidence, evidence_id, detail, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["engagement_id"],
                    row["linkedin_url"],
                    field,
                    sheet,
                    observed,
                    status,
                    confidence or None,
                    evidence_id,
                    "Outlook-derived observation; sheet values are never overwritten.",
                    generated_at,
                ),
            )


def queue_review(connection: sqlite3.Connection, engagement_id: int | None, review_type: str, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{review_type}:{serialized}".encode()).hexdigest()
    connection.execute(
        """
        INSERT INTO agent_review_queue (
            engagement_id, review_type, input_hash, payload_json, review_status, created_at
        ) VALUES (?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(input_hash) DO UPDATE SET
            engagement_id = excluded.engagement_id,
            payload_json = excluded.payload_json,
            review_status = CASE
                WHEN agent_review_queue.review_status = 'resolved' THEN 'resolved'
                ELSE 'pending'
            END
        """,
        (engagement_id, review_type, digest, serialized, now()),
    )


def thread_context(connection: sqlite3.Connection, evidence_id: int | None) -> list[dict[str, Any]]:
    if evidence_id is None:
        return []
    evidence = connection.execute(
        "SELECT engagement_id, conversation_id FROM engagement_state_evidence WHERE evidence_id = ?",
        (evidence_id,),
    ).fetchone()
    if not evidence or not evidence["conversation_id"]:
        return []
    rows = connection.execute(
        """
        SELECT source, object_id, occurred_at, sender_name, sender_address, subject, body_text
        FROM engagement_message_timeline
        WHERE engagement_id = ? AND conversation_id = ?
        ORDER BY occurred_at, source, object_id
        """,
        (evidence["engagement_id"], evidence["conversation_id"]),
    ).fetchall()
    return [
        {
            "source": row["source"],
            "object_id": row["object_id"],
            "occurred_at": row["occurred_at"],
            "sender_name": row["sender_name"],
            "sender_address": row["sender_address"],
            "subject": row["subject"],
            "latest_content": newest_message_text(row["body_text"] or "")[:1500],
        }
        for row in rows
    ]


def build_review_queue(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE agent_review_queue SET review_status = 'superseded' WHERE review_status = 'pending'")
    weak_matches = connection.execute(
        """
        SELECT e.engagement_id, e.candidate_name, m.match_status,
               c.linkedin_url, c.score, c.method, c.candidate_rank,
               i.candidate_name AS sheet_name
        FROM outlook_engagements AS e
        JOIN engagement_sheet_matches AS m USING (engagement_id)
        JOIN engagement_match_candidates AS c USING (engagement_id)
        JOIN interviews AS i ON i.linkedin_url = c.linkedin_url
        WHERE m.match_status IN ('needs_review', 'unmatched') AND c.candidate_rank <= 3
        ORDER BY e.engagement_id, c.candidate_rank
        """
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    names: dict[int, str] = {}
    for row in weak_matches:
        names[row["engagement_id"]] = row["candidate_name"]
        grouped.setdefault(row["engagement_id"], []).append(
            {
                "linkedin_url": row["linkedin_url"],
                "sheet_name": row["sheet_name"],
                "score": round(row["score"], 4),
                "method": row["method"],
                "rank": row["candidate_rank"],
            }
        )
    for engagement_id, candidates in grouped.items():
        queue_review(
            connection,
            engagement_id,
            "identity_match",
            {
                "outlook_candidate_name": names[engagement_id],
                "allowed_decisions": ["confirm_candidate", "no_match", "needs_human"],
                "candidates": candidates,
            },
        )

    conflicts = connection.execute(
        """
        SELECT f.*, e.candidate_name, s.excerpt, s.rule_id
        FROM reconciliation_findings AS f
        JOIN outlook_engagements AS e USING (engagement_id)
        LEFT JOIN engagement_state_evidence AS s USING (evidence_id)
        WHERE f.finding_status = 'conflict'
        """
    ).fetchall()
    for row in conflicts:
        queue_review(
            connection,
            row["engagement_id"],
            "reconciliation_conflict",
            {
                "candidate_name": row["candidate_name"],
                "linkedin_url": row["linkedin_url"],
                "field": row["field_name"],
                "sheet_value": row["sheet_value"],
                "outlook_value": row["observed_value"],
                "confidence": row["confidence"],
                "rule_id": row["rule_id"],
                "evidence_excerpt": row["excerpt"],
                "thread_messages": thread_context(connection, row["evidence_id"]),
                "allowed_decisions": ["keep_sheet", "accept_outlook", "needs_human"],
            },
        )


def export_report(connection: sqlite3.Connection, path: Path) -> None:
    rows = connection.execute(
        """
        SELECT e.candidate_name AS outlook_candidate_name,
               i.candidate_name AS sheet_candidate_name,
               f.linkedin_url, f.field_name, f.sheet_value, f.observed_value,
               f.finding_status, f.confidence, f.detail
        FROM reconciliation_findings AS f
        JOIN outlook_engagements AS e USING (engagement_id)
        JOIN interviews AS i USING (linkedin_url)
        ORDER BY e.candidate_name, f.field_name
        """
    ).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(rows[0].keys() if rows else (
            "outlook_candidate_name", "sheet_candidate_name", "linkedin_url",
            "field_name", "sheet_value", "observed_value", "finding_status",
            "confidence", "detail",
        ))
        writer.writerows(tuple(row) for row in rows)


def export_review_queue(connection: sqlite3.Connection, path: Path) -> None:
    rows = connection.execute(
        """
        SELECT review_id, engagement_id, review_type, input_hash, payload_json, created_at
        FROM agent_review_queue
        WHERE review_status = 'pending'
        ORDER BY review_type, review_id
        """
    ).fetchall()
    output = [
        {
            "review_id": row["review_id"],
            "engagement_id": row["engagement_id"],
            "review_type": row["review_type"],
            "input_hash": row["input_hash"],
            "created_at": row["created_at"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"generated_at": now(), "reviews": output}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def summary(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        "engagements": connection.execute("SELECT COUNT(*) FROM outlook_engagements").fetchone()[0],
        "matches": dict(connection.execute("SELECT match_status, COUNT(*) FROM engagement_sheet_matches GROUP BY match_status").fetchall()),
        "hydrated_messages": connection.execute("SELECT COUNT(*) FROM outlook_message_bodies").fetchone()[0],
        "evidence": connection.execute("SELECT COUNT(*) FROM engagement_state_evidence").fetchone()[0],
        "findings": dict(connection.execute("SELECT finding_status, COUNT(*) FROM reconciliation_findings GROUP BY finding_status").fetchall()),
        "pending_reviews": connection.execute("SELECT COUNT(*) FROM agent_review_queue WHERE review_status = 'pending'").fetchone()[0],
    }


def main() -> int:
    args = parse_args()
    if not args.db.exists():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 1
    try:
        with open_db(args.db) as connection:
            if args.hydrate:
                hydrated, errors = hydrate_messages(connection, args.hydrate_limit)
                print(f"Hydrated {hydrated} messages; {len(errors)} errors.")
                for error in errors:
                    print(f"warning: {error}", file=sys.stderr)
            build_engagements(connection)
            build_matches(connection)
            infer_states(connection)
            build_findings(connection)
            build_review_queue(connection)
            export_report(connection, args.report)
            export_review_queue(connection, args.review_queue)
            result = summary(connection)
    except (OSError, sqlite3.Error, RuntimeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Database: {args.db.resolve()}")
    print(f"Report: {args.report.resolve()}")
    print(f"Agent review queue: {args.review_queue.resolve()}")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
