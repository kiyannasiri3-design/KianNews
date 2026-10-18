
import os
import re
import sqlite3
from datetime import datetime, timezone
from html import unescape
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

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(news)").fetchall()
    }
    if "image_url" not in columns:
        conn.execute("ALTER TABLE news ADD COLUMN image_url TEXT")
    if "video_url" not in columns:
        conn.execute("ALTER TABLE news ADD COLUMN video_url TEXT")

    return conn


def valid_url(value):
    if not value:
        return ""
    value = str(value).strip()
    if value.startswith(("https://", "http://")):
        return value
    return ""


def extract_media(entry):
    image_url = ""
    video_url = ""

    # عکس‌های معرفی‌شده در RSS
    for media in entry.get("media_thumbnail", []):
        image_url = valid_url(media.get("url"))
        if image_url:
            break

    for media in entry.get("media_content", []):
        media_url = valid_url(media.get("url"))
        media_type = str(media.get("type", "")).lower()
        medium = str(media.get("medium", "")).lower()

        if media_url and (
            medium == "video" or media_type.startswith("video/")
        ):
            video_url = media_url
        elif media_url and (
            medium == "image" or media_type.startswith("image/")
        ):
            if not image_url:
                image_url = media_url

    # فایل‌های پیوست خبر
    for link in entry.get("links", []):
        media_url = valid_url(link.get("href"))
        media_type = str(link.get("type", "")).lower()
        relation = str(link.get("rel", "")).lower()

        if not media_url:
            continue

        if media_type.startswith("video/"):
            video_url = video_url or media_url
        elif media_type.startswith("image/"):
            image_url = image_url or media_url
        elif relation == "enclosure":
            path = urlparse(media_url).path.lower()
            if path.endswith((".mp4", ".webm", ".ogg", ".m4v")):
                video_url = video_url or media_url
            elif path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                image_url = image_url or media_url

    # گاهی عکس فقط داخل متن HTML خبر است
    if not image_url:
        content = entry.get("summary", "") or entry.get("description", "")
        match = re.search(
            r'<img[^>]+src=["\']([^"\']+)["\']',
            str(content),
            re.IGNORECASE
        )
        if match:
            image_url = valid_url(unescape(match.group(1)))

    return image_url, video_url


def load_feeds():
    feeds = []
    if os.path.exists(FEEDS_FILE):
        with open(FEEDS_FILE, "r", encoding="utf-8") as file:
            for line in file:
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
                link = valid_url(entry.get("link"))

                if not title or not link:
                    continue

                summary = (
                    entry.get("summary", "")
                    or entry.get("description", "")
                    or ""
                )[:12000]
                published = (
                    entry.get("published", "")
                    or entry.get("updated", "")
                )
                image_url, video_url = extract_media(entry)

                try:
                    cursor = conn.execute("""
                        INSERT OR IGNORE INTO news
                        (source, title, link, summary, published,
                         fetched_at, image_url, video_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        source_name, title, link, summary, published,
                        datetime.now(timezone.utc).isoformat(),
                        image_url, video_url
                    ))

                    if cursor.rowcount > 0:
                        added += 1
                    else:
                        # اطلاعات رسانه‌ای خبرهای قبلی را نیز تکمیل می‌کند
                        conn.execute("""
                            UPDATE news
                            SET image_url = COALESCE(NULLIF(image_url, ''), ?),
                                video_url = COALESCE(NULLIF(video_url, ''), ?)
                            WHERE link = ?
                        """, (image_url, video_url, link))

                except sqlite3.Error:
                    pass

        except Exception:
            errors.append(source_name)

    conn.commit()
    conn.close()
    return added, errors


@app.route("/")
def index():
    added, errors = refresh_feeds()
    conn = get_db()

    q = request.args.get("q", "").strip()
    source = request.args.get("source", "").strip()
    language = request.args.get("lang", "all").strip()

    sql = "SELECT * FROM news WHERE 1=1"
    args = []

    if q:
        sql += " AND (title LIKE ? OR summary LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]

    if source:
        sql += " AND source = ?"
        args.append(source)

    # تشخیص تقریبی فارسی بودن عنوان
    if language == "fa":
        sql += " AND title GLOB '*[ء-ی]*'"
    elif language == "en":
        sql += " AND title NOT GLOB '*[ء-ی]*'"

    # در حالت نمایش همه، خبرهای فارسی اولویت دارند
    sql += """
        ORDER BY
        CASE WHEN title GLOB '*[ء-ی]*' THEN 0 ELSE 1 END,
        id DESC
        LIMIT 150
    """

    items = conn.execute(sql, args).fetchall()
    sources = [
        row["source"]
        for row in conn.execute(
            "SELECT DISTINCT source FROM news ORDER BY source"
        ).fetchall()
    ]
    conn.close()

    return render_template(
        "index.html",
        items=items,
        sources=sources,
        q=q,
        selected_source=source,
        selected_language=language,
        added=added
    )


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
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
