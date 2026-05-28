#!/usr/bin/env python3 import os import json import re import smtplib import sqlite3 import hashlib from email.mime.text import MIMEText from email.utils import formataddr from datetime import datetime, timezone from dateutil import parser as dtparser import pytz import requests from bs4 import BeautifulSoup

---------------- User-configurable bits ----------------
WATCHLIST = [ "Olivia Rodrigo", "Yung Kai", "Taylor Swift", "Gracie Abrams", "John Legend", "Ariana Grande", "Bruno Major", "Sabrina Carpenter" ] CITIES = ["Hong Kong", "Shanghai"] TIMEZONE_NAME = "Europe/Berlin" # local time zone for send hour SEND_HOUR_LOCAL = 6 # send at 06:00 local (Europe/Berlin) DATA_DIR = "data" DB_PATH = f"{DATA_DIR}/events.sqlite" JSON_PATH = f"{DATA_DIR}/events.json" USER_AGENT = "Mozilla/5.0 (concert-tracker/0.1; +private use)" SOURCES = ["ticketflap", "damai", "smartshanghai"]

--------------------------------------------------------
def ensure_dirs(): os.makedirs(DATA_DIR, exist_ok=True)

def now_local(tzname=TIMEZONE_NAME): return datetime.now(pytz.timezone(tzname))

def should_send_now(): if os.getenv("FORCE_SEND_NOW"): return True local = now_local() return local.hour == SEND_HOUR_LOCAL

def slugify(s): s = re.sub(r"\s+", " ", (s or "")).strip().lower() s = re.sub(r"[^\w\s-]+", "", s) return re.sub(r"\s+", "-", s)

def english_score(text): if not text: return 0.0 letters = sum(c.isalpha() for c in text) latin = sum((ord(c) < 128 and c.isalpha()) for c in text) if letters == 0: return 0.0 return latin / letters

def normalize_city(name): n = (name or "").lower() if "hong kong" in n or n == "hk": return "Hong Kong" if "shanghai" in n or "上海" in n: return "Shanghai" return name or ""

def event_hash(artists, date_iso, venue, city): key = "|".join([ slugify(", ".join(sorted([a.strip() for a in artists]))), date_iso or "", slugify(venue or ""), slugify(city or "") ]) return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

def create_db(conn): cur = conn.cursor() cur.execute(""" CREATE TABLE IF NOT EXISTS events ( id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT UNIQUE, title TEXT, artists TEXT, venue TEXT, city TEXT, date_local TEXT, timezone TEXT, status TEXT, source TEXT, url TEXT, first_seen_at TEXT, last_seen_at TEXT ); """) cur.execute("CREATE INDEX IF NOT EXISTS idx_hash ON events(hash);") conn.commit()

def open_db(): ensure_dirs() conn = sqlite3.connect(DB_PATH) create_db(conn) return conn

def get_existing_by_hash(conn, h): cur = conn.cursor() cur.execute( "SELECT id, status, title, artists, venue, city, date_local, url FROM events WHERE hash=?", (h,) ) row = cur.fetchone() if not row: return None return { "id": row[0], "status": row[1], "title": row[2], "artists": row[3], "venue": row[4], "city": row[5], "date_local": row[6], "url": row[7] }

def upsert_event(conn, ev): h = ev["hash"] existing = get_existing_by_hash(conn, h) now_iso = datetime.now(timezone.utc).isoformat() cur = conn.cursor() if not existing: cur.execute(""" INSERT INTO events(hash,title,artists,venue,city,date_local,timezone,status,source,url,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) """, ( h, ev["title"], ", ".join(ev["artists"]), ev["venue"], ev["city"], ev["date_local"], ev["timezone"], ev["status"], ev["source"], ev["url"], now_iso, now_iso )) conn.commit() return "new" else: changed = False if ev["status"] and ev["status"] != existing["status"]: changed = True if ev["date_local"] != existing["date_local"] or ev["venue"] != existing["venue"]: changed = True if changed: cur.execute(""" UPDATE events SET title=?, artists=?, venue=?, city=?, date_local=?, timezone=?, status=?, source=?, url=?, last_seen_at=? WHERE hash=? """, ( ev["title"], ", ".join(ev["artists"]), ev["venue"], ev["city"], ev["date_local"], ev["timezone"], ev["status"], ev["source"], ev["url"], now_iso, h )) conn.commit() return "updated" else: cur.execute("UPDATE events SET last_seen_at=? WHERE hash=?", (now_iso, h)) conn.commit() return "seen"

def classify_include(ev): for a in ev["artists"]: for w in WATCHLIST: if slugify(w) in slugify(a): return True, "watchlist" text = " ".join([ev.get("title", ""), " ".join(ev.get("artists", []))]) score = english_score(text) englishish = score >= 0.35 source_bonus = ev["source"] in ("ticketflap", "smartshanghai") include = englishish or source_bonus reason = "english" if englishish else ("source" if source_bonus else "other") return include, reason

def parse_date(s, tzname): if not s: return None try: dt = dtparser.parse(s) if not dt.tzinfo: dt = pytz.timezone(tzname).localize(dt) return dt except Exception: return None

------------- Source: Ticketflap (HK) -------------
def fetch_ticketflap(): url = "https://www.ticketflap.com/events" out = [] try: r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25) r.raise_for_status() soup = BeautifulSoup(r.text, "html.parser") cards = soup.select("a[href*='/event'], a[href*='/events/']") seen = set() for a in cards: href = a.get("href") or "" if not href: continue if not href.startswith("http"): href = "https://www.ticketflap.com" + href title = (a.get_text(" ", strip=True) or "").strip() if not title or href in seen: continue seen.add(href) card = a.find_parent(["div", "li", "article"]) or a meta = card.get_text(" ", strip=True) date_text = "" m = re.search(r"\b(\d{1,2}\s+\w+\s+\d{4})\b", meta) if m: date_text = m.group(1) dt = parse_date(date_text, "Asia/Hong_Kong") date_local = dt.strftime("%Y-%m-%d %H:%M") if dt else "" status = "on_sale" if re.search(r"\bbuy\b|\btickets?\b", meta.lower()) else "announced" out.append({ "source": "ticketflap", "url": href, "title": title, "artists": [title], "venue": "", "city": "Hong Kong", "date_local": date_local, "timezone": "Asia/Hong_Kong", "status": status }) except Exception as e: print("[ticketflap] error:", e) return out

------------- Source: Damai (Shanghai) -------------
def fetch_damai(): url = "https://search.damai.cn/search.html?keyword=&destCity=%E4%B8%8A%E6%B5%B7&projectType=1" out = [] try: r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30) r.raise_for_status() soup = BeautifulSoup(r.text, "html.parser") items = soup.select("a[href*='detail'], a[href*='item']") seen = set() for a in items: href = a.get("href") or "" if not href: continue if href.startswith("//"): href = "https:" + href if href and not href.startswith("http"): href = "https://search.damai.cn/" + href.lstrip("/") title = (a.get_text(" ", strip=True) or "").strip() if not title or href in seen: continue seen.add(href) card = a.find
