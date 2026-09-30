import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from cryptography.fernet import Fernet


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root):
        self.lock = threading.RLock()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        key_file = self.root / 'secret.key'
        if not key_file.exists():
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        self.crypto = Fernet(key_file.read_bytes())
        self.db = sqlite3.connect(self.root / 'assistant.db', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS students (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
          secret BLOB NOT NULL, config TEXT NOT NULL, enabled INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, student_id TEXT NOT NULL, status TEXT NOT NULL,
          detail TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY, student_id TEXT NOT NULL, source_key TEXT NOT NULL,
          payload BLOB NOT NULL, created TEXT NOT NULL, extracted INTEGER NOT NULL DEFAULT 0,
          UNIQUE(student_id, source_key));
        CREATE TABLE IF NOT EXISTS items (
          id TEXT PRIMARY KEY, message_id TEXT NOT NULL, student_id TEXT NOT NULL,
          payload TEXT NOT NULL, draft TEXT NOT NULL, status TEXT NOT NULL,
          created TEXT NOT NULL, updated TEXT NOT NULL, sent_at TEXT);
        CREATE TABLE IF NOT EXISTS audits (
          id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
          entity_id TEXT NOT NULL, created TEXT NOT NULL);
        ''')
        if 'extracted' not in [r[1] for r in self.db.execute('PRAGMA table_info(messages)')]:
            self.db.execute('ALTER TABLE messages ADD COLUMN extracted INTEGER NOT NULL DEFAULT 0')
        self.db.execute("UPDATE jobs SET status='interrupted', detail='服务重启，会话已结束，请重新采集', updated=? WHERE status IN ('queued','logging_in','waiting_verification','collecting','diagnosing')", (now(),))
        self.db.commit()

    def seal(self, value):
        return self.crypto.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def unseal(self, value):
        return json.loads(self.crypto.decrypt(value))

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.lock:
            cursor = self.db.execute(sql, args)
            self.db.commit()
            return cursor.rowcount

    def student(self, sid):
        rows = self.rows('SELECT * FROM students WHERE id=?', (sid,))
        if not rows:
            raise KeyError(sid)
        row = rows[0]
        row['password'] = self.unseal(row.pop('secret'))
        row['config'] = json.loads(row['config'])
        return row

    def audit(self, action, entity):
        self.execute('INSERT INTO audits(action,entity_id,created) VALUES(?,?,?)', (action, entity, now()))
