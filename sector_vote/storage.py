"""SQLite persistence for analyzed videos and sector calls."""

import sqlite3
from datetime import datetime, timezone
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

    def list_video_ids(self) -> set[str]:
        with self._con() as con:
            return {row[0] for row in con.execute("SELECT video_id FROM videos").fetchall()}

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

    def status(self) -> dict:
        with self._con() as con:
            videos = con.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            calls = con.execute("SELECT COUNT(*) FROM sector_calls").fetchone()[0]
            latest = con.execute("SELECT MAX(analyzed_at) FROM videos").fetchone()[0]
        return {"videos": videos, "calls": calls, "latest_analysis": latest}
