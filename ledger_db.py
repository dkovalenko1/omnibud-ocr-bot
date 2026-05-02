import os
import sqlite3

from config import LEDGER_DB_PATH


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


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
  voided_at text,
  voided_by_user_id integer,
  voided_by_name text,
  foreign key (chat_id) references chat_accounts(chat_id)
);

create table if not exists saved_receipts (
  id integer primary key autoincrement,
  chat_id integer not null,
  source_message_id integer not null,
  receipt_index integer not null,
  sheet_tab text not null,
  sheet_row integer not null,
  ledger_transaction_id integer not null,
  status text not null default 'saved' check(status in ('saved', 'undone')),
  created_at text not null,
  undone_at text,
  undone_by_user_id integer,
  undone_by_name text,
  unique(chat_id, source_message_id, receipt_index),
  foreign key (chat_id) references chat_accounts(chat_id),
  foreign key (ledger_transaction_id) references transactions(id)
);

create index if not exists idx_transactions_chat_created
  on transactions(chat_id, created_at);

create index if not exists idx_transactions_chat_kind
  on transactions(chat_id, source_kind);

create index if not exists idx_saved_receipts_chat_message
  on saved_receipts(chat_id, source_message_id);
"""


def _ensure_db_dir():
    db_dir = os.path.dirname(LEDGER_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    _ensure_db_dir()
    conn = sqlite3.connect(LEDGER_DB_PATH, timeout=10, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
        }
        migrations = {
            "voided_at": "alter table transactions add column voided_at text",
            "voided_by_user_id": "alter table transactions add column voided_by_user_id integer",
            "voided_by_name": "alter table transactions add column voided_by_name text",
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)
