from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS notices (
  record_id TEXT PRIMARY KEY,
  docket_number TEXT NOT NULL,
  subject TEXT NOT NULL,
  issue_date TEXT,
  publish_date TEXT,
  make TEXT,
  model TEXT,
  product_type TEXT,
  product_subtype TEXT,
  ad_number TEXT,
  supersedes_ad TEXT,
  mentions_serial_numbers TEXT,
  citation TEXT,
  pages TEXT,
  source_url TEXT,
  summary TEXT,
  fetch_status TEXT NOT NULL DEFAULT 'pending',
  fetch_error TEXT,
  official_document_number TEXT,
  official_action TEXT,
  raw_path TEXT,
  text_hash TEXT
);

CREATE TABLE IF NOT EXISTS revision_chain (
  docket_number TEXT NOT NULL,
  version INTEGER NOT NULL,
  versions_total INTEGER NOT NULL,
  corpus_status TEXT NOT NULL,
  record_id TEXT NOT NULL REFERENCES notices(record_id),
  publish_date TEXT,
  subject TEXT,
  PRIMARY KEY (docket_number, record_id)
);

CREATE TABLE IF NOT EXISTS data_notes (
  topic TEXT PRIMARY KEY,
  detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL REFERENCES notices(record_id),
  source_kind TEXT NOT NULL,
  page TEXT,
  section TEXT,
  paragraph TEXT,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  searchable_text TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  source_url TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  searchable_text,
  content='chunks',
  content_rowid='rowid',
  tokenize='unicode61 tokenchars ''-_/'''
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, chunk_id, searchable_text)
  VALUES (new.rowid, new.chunk_id, new.searchable_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, chunk_id, searchable_text)
  VALUES ('delete', old.rowid, old.chunk_id, old.searchable_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, chunk_id, searchable_text)
  VALUES ('delete', old.rowid, old.chunk_id, old.searchable_text);
  INSERT INTO chunks_fts(rowid, chunk_id, searchable_text)
  VALUES (new.rowid, new.chunk_id, new.searchable_text);
END;

CREATE INDEX IF NOT EXISTS idx_notices_docket ON notices(docket_number);
CREATE INDEX IF NOT EXISTS idx_notices_model ON notices(model);
CREATE INDEX IF NOT EXISTS idx_chunks_record ON chunks(record_id);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con

