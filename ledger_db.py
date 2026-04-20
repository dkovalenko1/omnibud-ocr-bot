import os
import sqlite3

from config import LEDGER_DB_PATH


SCHEMA = """
create table if not exists chat_accounts (
  chat_id integer primary key,
  foreman_name text not null,
  opened_at text not null,
  is_active integer not null default 1
);

create table if not exists transactions (
  id integer primary key autoincrement,
  chat_id integer not null,
  source_key text not null unique,
  message_id integer,
  source_kind text not null check(source_kind in ('opening', 'receipt', 'topup', 'adjustment')),
  amount_kopecks integer not null,
  object_name text,
  description text,
  items_text text,
  created_by_user_id integer,
  created_by_name text,
  created_at text not null,
  foreign key (chat_id) references chat_accounts(chat_id)
);

create index if not exists idx_transactions_chat_created
  on transactions(chat_id, created_at);

create index if not exists idx_transactions_chat_kind
  on transactions(chat_id, source_kind);
"""


def _ensure_db_dir():
    db_dir = os.path.dirname(LEDGER_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    _ensure_db_dir()
    conn = sqlite3.connect(LEDGER_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)
