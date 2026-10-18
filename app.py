import os
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse
import feedparser
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-for-production")
DB_PATH = os.environ.get("DB_PATH", "news.db")
FEEDS_FILE = "feeds.txt"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL UNIQUE,
            summary TEXT,
            published TEXT,
            fetched_at TEXT NOT NULL
        )
    """)
    return conn

def load_feeds():
    feeds = []
    if os.path.exists(FEEDS_FILE):
        with open(FEEDS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|", 1)
                if len(parts) == 2 and parts[1].startswith(("http://", "https://")):
                    feeds.append((parts[0].strip(), parts[1].strip()))
    return feeds

def refresh_feeds():
    conn = get_db()
    added = 0
    errors = []
    for source_name, feed_url in load_feeds():
        try:
            parsed = feedparser.parse(feed_url)
            if getattr(parsed, "bozo", False) and not parsed.entries:
                errors.append(source_name)
                continue
            for entry in parsed.entries[:40]:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                if not title or not link or not link.startswith(("http://", "https://")):
                    continue
                summary = entry.get("summary", "") or entry.get("description", "") or ""
                summary = summary.replace("<", " ").replace(">", " ")[:1200]
                published = entry.get("published", "") or entry.get("updated", "")
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO news(source,title,link,summary,published,fetched_at) VALUES(?,?,?,?,?,?)",
                        (source_name, title, link, summary, published, datetime.now(timezone.utc).isoformat())
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        added += 1
                except sqlite3.Error:
                    pass
        except Exception:
            errors.append(source_name)
    conn.commit()
    conn.close()
    return added, errors

@app.route("/")
def index():
    # Refresh on visits; free hosting may sleep, so this is not guaranteed real-time polling.
    added, errors = refresh_feeds()
    conn = get_db()
    q = request.args.get("q", "").strip()
    source = request.args.get("source", "").strip()
    sql = "SELECT * FROM news WHERE 1=1"
    args = []
    if q:
        sql += " AND (title LIKE ? OR summary LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if source:
        sql += " AND source = ?"
        args.append(source)
    sql += " ORDER BY id DESC LIMIT 150"
    items = conn.execute(sql, args).fetchall()
    sources = [r["source"] for r in conn.execute("SELECT DISTINCT source FROM news ORDER BY source").fetchall()]
    conn.close()
    return render_template("index.html", items=items, sources=sources, q=q, selected_source=source, added=added)

@app.route("/refresh", methods=["POST"])
def refresh():
    added, errors = refresh_feeds()
    if errors:
        flash(f"به‌روزرسانی انجام شد؛ بعضی منابع پاسخ ندادند: {', '.join(errors)}")
    else:
        flash(f"به‌روزرسانی تمام شد. {added} خبر تازه دریافت شد.")
    return redirect(url_for("index"))

@app.route("/sources")
def sources():
    return render_template("sources.html", feeds=load_feeds())

@app.route("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
