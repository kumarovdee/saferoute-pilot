"""Local SafeRoute workflow with a synthetic payment register."""

import json
import sqlite3
import time
from contextlib import contextmanager


class DecisionError(ValueError):
    pass


class Pilot:
    def __init__(self, database, hold_seconds=90, clock=time.time):
        if type(hold_seconds) is not int or hold_seconds < 1:
            raise ValueError("hold_seconds must be a positive integer")
        self.database = database
        self.hold_seconds = hold_seconds
        self.clock = clock
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    mode TEXT NOT NULL CHECK (mode IN ('active', 'shadow'))
                );
                INSERT OR IGNORE INTO settings VALUES (1, 'active');
                CREATE TABLE IF NOT EXISTS transfers (
                    id TEXT PRIMARY KEY,
                    request TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    deadline INTEGER
                );
                CREATE TABLE IF NOT EXISTS payments (
                    transfer_id TEXT PRIMARY KEY REFERENCES transfers(id),
                    amount INTEGER NOT NULL,
                    sent INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    transfer_id TEXT REFERENCES transfers(id),
                    kind TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created INTEGER NOT NULL
                );
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, transfer_id, kind, actor, note, now):
        db.execute(
            "INSERT INTO events (transfer_id, kind, actor, note, created) "
            "VALUES (?, ?, ?, ?, ?)", (transfer_id, kind, actor, note, now)
        )

    @staticmethod
    def mode(db):
        return db.execute("SELECT mode FROM settings WHERE id = 1").fetchone()[0]

    def expire_due(self, db, now):
        due = db.execute(
            "SELECT id FROM transfers WHERE state = 'held' AND deadline <= ?",
            (now,),
        ).fetchall()
        for row in due:
            db.execute("UPDATE transfers SET state = 'expired' WHERE id = ?", (row["id"],))
            self.event(db, row["id"], "expired", "system", "deadline", now)

    @staticmethod
    def record_payment(db, transfer_id, amount, now):
        db.execute("INSERT INTO payments VALUES (?, ?, ?)", (transfer_id, amount, now))

    def submit(self, transfer_id, amount, new_payee, unusual_amount, urgent, blocked):
        if not isinstance(transfer_id, str) or not (1 <= len(transfer_id) <= 64):
            raise DecisionError("Некорректный идентификатор поручения.")
        if type(amount) is not int or not (1 <= amount <= 1_000_000_000):
            raise DecisionError("Введите целую сумму от 1 до 1 000 000 000 ₸.")
        flags = (new_payee, unusual_amount, urgent, blocked)
        if any(type(flag) is not bool for flag in flags):
            raise DecisionError("Некорректные сигналы риска.")
        request = json.dumps((amount, *flags))
        with self.transaction() as db:
            now = int(self.clock())
            self.expire_due(db, now)
            existing = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
            if existing:
                if existing["request"] != request:
                    raise DecisionError("Идентификатор уже связан с другим поручением.")
                return existing["state"]
            mode = self.mode(db)
            needs_review = new_payee and (unusual_amount or urgent)
            signal = "baseline_block" if blocked else ("risk_review" if needs_review else "none")
            state = "sent"
            if blocked:
                state = "declined"
            elif needs_review and mode == "active":
                state = "held"
            deadline = now + self.hold_seconds if state == "held" else None
            db.execute(
                "INSERT INTO transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (transfer_id, request, amount, mode, signal, state, now, deadline),
            )
            self.event(db, transfer_id, state, "system", signal, now)
            if state == "sent":
                self.record_payment(db, transfer_id, amount, now)
                if needs_review:
                    self.event(db, transfer_id, "would_hold", "system", "shadow", now)
            return state

    def resolve(self, transfer_id, action, note=""):
        if action not in {"cancel", "approve", "reject", "reconfirm", "contact"}:
            raise DecisionError("Неизвестное действие.")
        if not isinstance(note, str) or len(note) > 300:
            raise DecisionError("Комментарий должен быть не длиннее 300 символов.")
        note = note.strip()
        if action in {"approve", "reject"} and not note:
            raise DecisionError("Укажите основание решения сотрудника.")
        with self.transaction() as db:
            now = int(self.clock())
            self.expire_due(db, now)
            row = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
            if row is None:
                raise DecisionError("Поручение не найдено.")
            if row["state"] != "held":
                return row["state"]
            if action in {"reconfirm", "contact"}:
                self.event(db, transfer_id, action, "customer", "", now)
                return "held"
            if action in {"approve", "reject"}:
                contacted = db.execute(
                    "SELECT 1 FROM events WHERE transfer_id = ? AND kind = 'contact' LIMIT 1",
                    (transfer_id,),
                ).fetchone()
                if not contacted:
                    raise DecisionError("Сначала откройте обращение клиента в банк.")
            state, actor = {
                "cancel": ("cancelled", "customer"),
                "approve": ("sent", "operator"),
                "reject": ("declined", "operator"),
            }[action]
            if state == "sent":
                self.record_payment(db, transfer_id, row["amount"], now)
            db.execute("UPDATE transfers SET state = ? WHERE id = ?", (state, transfer_id))
            self.event(db, transfer_id, state, actor, note, now)
            return state

    def set_mode(self, mode):
        if mode not in {"active", "shadow"}:
            raise DecisionError("Неизвестный режим.")
        with self.transaction() as db:
            if self.mode(db) != mode:
                db.execute("UPDATE settings SET mode = ? WHERE id = 1", (mode,))
                self.event(db, None, "mode_changed", "supervisor", mode, int(self.clock()))

    def snapshot(self):
        with self.transaction() as db:
            now = int(self.clock())
            self.expire_due(db, now)
            return {
                "mode": self.mode(db),
                "now": now,
                "transfers": [dict(row) for row in db.execute(
                    "SELECT id, amount, mode, signal, state, deadline "
                    "FROM transfers ORDER BY rowid DESC"
                )],
                "events": [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC")],
                "payments": [dict(row) for row in db.execute(
                    "SELECT * FROM payments ORDER BY rowid DESC"
                )],
            }
