"""SQLite persistence for analyzed videos and sector calls."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class SectorStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _con(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self):
        with self._con() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS videos(
                video_id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                published_at TEXT NOT NULL,
                analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS sector_calls(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                sector TEXT NOT NULL,
                direction TEXT NOT NULL,
                horizon TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                quote TEXT NOT NULL,
                FOREIGN KEY(video_id) REFERENCES videos(video_id)
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS video_attempts(
                video_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                next_retry_at TEXT,
                updated_at TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            )""")

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def claim_video(self, video_id: str, now: datetime, lease_minutes: int = 120) -> bool:
        """Atomically lease a video before any paid provider call."""
        current = self._utc(now)
        lease_until = (current + timedelta(minutes=lease_minutes)).isoformat()
        with self._con() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,)).fetchone():
                return False
            attempt = con.execute(
                "SELECT status,next_retry_at FROM video_attempts WHERE video_id=?",
                (video_id,),
            ).fetchone()
            if attempt:
                if attempt["status"] == "unavailable":
                    return False
                if attempt["next_retry_at"] and attempt["next_retry_at"] > current.isoformat():
                    return False
            con.execute(
                """INSERT INTO video_attempts(video_id,status,next_retry_at,updated_at,error)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(video_id) DO UPDATE SET
                   status=excluded.status,next_retry_at=excluded.next_retry_at,
                   updated_at=excluded.updated_at,error=excluded.error""",
                (video_id, "processing", lease_until, current.isoformat(), ""),
            )
        return True

    def mark_video_attempt(
        self,
        video_id: str,
        *,
        status: str,
        now: datetime,
        retry_minutes: int | None = None,
        error: str = "",
    ) -> None:
        if status not in {"failed", "unavailable"}:
            raise ValueError("invalid video attempt status")
        current = self._utc(now)
        next_retry = None
        if status == "failed":
            retry_minutes = 360 if retry_minutes is None else retry_minutes
            next_retry = (current + timedelta(minutes=retry_minutes)).isoformat()
        with self._con() as con:
            con.execute(
                """INSERT INTO video_attempts(video_id,status,next_retry_at,updated_at,error)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(video_id) DO UPDATE SET
                   status=excluded.status,next_retry_at=excluded.next_retry_at,
                   updated_at=excluded.updated_at,error=excluded.error""",
                (video_id, status, next_retry, current.isoformat(), error[:180]),
            )

    def save_video_calls(
        self,
        *,
        video_id: str,
        channel: str,
        title: str,
        url: str,
        published_at: str,
        calls: list[dict],
    ) -> None:
        normalized_published_at = datetime.fromisoformat(
            published_at.replace("Z", "+00:00")
        ).astimezone(timezone.utc).isoformat()
        with self._con() as con:
            con.execute(
                """INSERT INTO videos(video_id,channel,title,url,published_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(video_id) DO UPDATE SET
                   channel=excluded.channel,title=excluded.title,url=excluded.url,
                   published_at=excluded.published_at,analyzed_at=CURRENT_TIMESTAMP""",
                (video_id, channel, title, url, normalized_published_at),
            )
            con.execute("DELETE FROM sector_calls WHERE video_id=?", (video_id,))
            con.executemany(
                """INSERT INTO sector_calls(
                   video_id,sector,direction,horizon,confidence,reason,quote
                   ) VALUES(?,?,?,?,?,?,?)""",
                [(
                    video_id,
                    row["sector"],
                    row["direction"],
                    row["horizon"],
                    float(row.get("confidence", 0)),
                    row.get("reason", "")[:160],
                    row.get("quote", "")[:120],
                ) for row in calls],
            )
            con.execute("DELETE FROM video_attempts WHERE video_id=?", (video_id,))

    def list_video_ids(self) -> set[str]:
        with self._con() as con:
            rows = con.execute("""SELECT video_id FROM videos
                UNION SELECT video_id FROM video_attempts WHERE status='unavailable'""").fetchall()
            return {row[0] for row in rows}

    def has_video(self, video_id: str) -> bool:
        with self._con() as con:
            return con.execute(
                "SELECT 1 FROM videos WHERE video_id=?", (video_id,)
            ).fetchone() is not None

    def list_calls(self, since: str | None = None) -> list[dict]:
        sql = """SELECT c.sector,c.direction,c.horizon,c.confidence,c.reason,c.quote,
                 v.video_id,v.channel,v.title,v.url,v.published_at
                 FROM sector_calls c JOIN videos v ON v.video_id=c.video_id"""
        args = ()
        if since:
            sql += " WHERE v.published_at>=?"
            args = (since,)
        sql += " ORDER BY v.published_at DESC,c.id ASC"
        with self._con() as con:
            return [dict(row) for row in con.execute(sql, args).fetchall()]

    def prune_before(self, cutoff: str) -> int:
        """Delete stored metadata and short evidence older than the retention window."""
        with self._con() as con:
            removed = con.execute(
                "SELECT COUNT(*) FROM videos WHERE published_at<?", (cutoff,)
            ).fetchone()[0]
            con.execute(
                """DELETE FROM sector_calls WHERE video_id IN (
                    SELECT video_id FROM videos WHERE published_at<?
                )""",
                (cutoff,),
            )
            con.execute("DELETE FROM videos WHERE published_at<?", (cutoff,))
            con.execute("DELETE FROM video_attempts WHERE updated_at<?", (cutoff,))
        return removed

    def status(self) -> dict:
        with self._con() as con:
            videos = con.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            calls = con.execute("SELECT COUNT(*) FROM sector_calls").fetchone()[0]
            latest = con.execute("SELECT MAX(analyzed_at) FROM videos").fetchone()[0]
        return {"videos": videos, "calls": calls, "latest_analysis": latest}
