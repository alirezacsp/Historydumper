#!/usr/bin/env python3
"""
deepseek_tool.py
Multi-account exporter + live & offline regex search + multithreaded.

Depends:
    pip install requests tqdm

Usage examples:
    python deepseek_tool.py --accounts accounts.txt --threads 8 --patterns patterns.txt --live-search --save-db --output exports
    python deepseek_tool.py --offline-search --patterns patterns.txt --output exports
"""

import argparse
import requests
import os
import re
import json
import time
import sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from typing import List, Tuple

# -----------------------
# Config / Endpoints
# -----------------------
BASE = "https://chat.deepseek.com"
LOGIN_URL = BASE + "/api/v0/users/login"
SESSIONS_URL = BASE + "/api/v0/chat_session/fetch_page?lte_cursor.pinned=false"
HISTORY_URL = BASE + "/api/v0/chat/history_messages?chat_session_id={}"

# -----------------------
# Helpers
# -----------------------
def safe_filename(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", text)[:120]

def parse_accounts(file_path: str) -> List[Tuple[str,str]]:
    accounts = []
    with open(file_path, "r", encoding="utf-8") as f:
        for idx, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            # split by ":" and trim spaces; keep parts that are not empty
            parts = [p.strip() for p in raw.split(":") if p.strip()]
            if len(parts) < 2:
                print(f"[WARN] invalid line #{idx}: {raw}")
                continue
            # username = second-last, password = last
            username = parts[-2]
            password = parts[-1]
            accounts.append((username, password))
    return accounts

def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

# -----------------------
# Networking / login / fetch
# -----------------------
class AccountWorker:
    def __init__(self, username, password, output_base, session_opts, patterns=None, live_search=False, db_conn=None, rate_delay=0.0, max_retries=2):
        self.username = username
        self.password = password
        self.output_base = output_base
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "User-Agent": "deepseek-tool/1.0"})
        self.patterns = patterns or []
        self.live_search = live_search
        self.db_conn = db_conn
        self.rate_delay = rate_delay
        self.max_retries = max_retries
        self.session_opts = session_opts  # dict: timeout, proxies, verify

    def _try_login_payloads(self):
        # Try a couple payloads: first treat username as email, then as mobile
        payloads = [
            {"email": self.username, "mobile": "", "password": self.password, "area_code": "", "device_id": "", "os": ""},
            {"email": "", "mobile": self.username, "password": self.password, "area_code": "", "device_id": "", "os": ""},
        ]
        return payloads

    def login(self):
        last_err = None
        for p in self._try_login_payloads():
            for attempt in range(self.max_retries + 1):
                try:
                    r = self.session.post(LOGIN_URL, json=p, timeout=self.session_opts.get("timeout", 15), proxies=self.session_opts.get("proxies"), verify=self.session_opts.get("verify", True))
                    # Accept 200 with JSON
                    if r.status_code != 200:
                        last_err = f"HTTP {r.status_code}"
                        time.sleep(1 + attempt)
                        continue
                    data = r.json()
                    # try extract token path safely
                    token = None
                    try:
                        token = data.get("data", {}).get("biz_data", {}).get("user", {}).get("token")
                    except Exception:
                        token = None
                    if token:
                        return token, data
                    else:
                        last_err = f"No token in response: {r.text[:200]}"
                        time.sleep(1 + attempt)
                        continue
                except Exception as e:
                    last_err = str(e)
                    time.sleep(1 + attempt)
                    continue
        raise RuntimeError(f"Login failed for {self.username}: {last_err}")

    def fetch_sessions(self, token):
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.get(SESSIONS_URL, headers=headers, timeout=self.session_opts.get("timeout", 15), proxies=self.session_opts.get("proxies"), verify=self.session_opts.get("verify", True))
                if r.status_code != 200:
                    time.sleep(1 + attempt)
                    continue
                data = r.json()
                sessions = data.get("data", {}).get("biz_data", {}).get("chat_sessions", [])
                return sessions
            except Exception:
                time.sleep(1 + attempt)
        return []

    def fetch_history(self, token, chat_id):
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.get(HISTORY_URL.format(chat_id), headers=headers, timeout=self.session_opts.get("timeout", 15), proxies=self.session_opts.get("proxies"), verify=self.session_opts.get("verify", True))
                if r.status_code != 200:
                    time.sleep(1 + attempt)
                    continue
                data = r.json()
                return data.get("data", {}).get("biz_data", {})
            except Exception:
                time.sleep(1 + attempt)
        return {}

    def run(self):
        # create output folder
        ts = timestamp()
        folder = os.path.join(self.output_base, f"{safe_filename(self.username)}_{ts}")
        os.makedirs(folder, exist_ok=True)
        result_index = {"username": self.username, "folder": folder, "timestamp": ts, "sessions": []}

        try:
            token, login_response = self.login()
        except Exception as e:
            print(f"[ERROR] Login failed for {self.username}: {e}")
            return {"username": self.username, "ok": False, "error": str(e)}

        # store login raw
        with open(os.path.join(folder, "login_response.json"), "w", encoding="utf-8") as f:
            json.dump(login_response, f, indent=2, ensure_ascii=False)

        # fetch sessions
        sessions = self.fetch_sessions(token)
        result_index["sessions"] = sessions
        with open(os.path.join(folder, "chats_index.json"), "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2, ensure_ascii=False)

        # for live search: compile patterns
        compiled = [(p, re.compile(p, re.IGNORECASE)) for p in self.patterns]

        # iterate sessions
        for chat in sessions:
            cid = chat.get("id")
            title = chat.get("title") or cid
            safe_title = safe_filename(title)
            history = self.fetch_history(token, cid)
            messages = history.get("chat_messages", [])
            # write chat file
            chat_file = os.path.join(folder, f"{cid}-{safe_title}.txt")
            with open(chat_file, "w", encoding="utf-8") as fout:
                for m in messages:
                    role = m.get("role", "")
                    content = m.get("content", "") or ""
                    inserted_at = m.get("inserted_at", "")
                    fout.write(f"[{role}] inserted_at={inserted_at}\n{content}\n\n")

                    # insert to DB if requested
                    if self.db_conn:
                        try:
                            cur = self.db_conn.cursor()
                            cur.execute(
                                "INSERT INTO messages(account, chat_id, message_id, role, content, inserted_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (self.username, cid, m.get("message_id"), role, content, inserted_at)
                            )
                            self.db_conn.commit()
                        except Exception:
                            pass

                    # live regex search
                    if self.live_search and compiled:
                        for pat, cre in compiled:
                            if cre.search(content):
                                out = {"username": self.username, "chat_id": cid, "pattern": pat, "message_id": m.get("message_id"), "match_excerpt": (content[:200] + ("..." if len(content) > 200 else ""))}
                                # print and append to matches.jsonl in root output
                                print(f"[MATCH][{self.username}] pattern={pat} chat={cid} msg={m.get('message_id')}")
                                with open(os.path.join(self.output_base, "matches.jsonl"), "a", encoding="utf-8") as mf:
                                    mf.write(json.dumps(out, ensure_ascii=False) + "\n")

            # rate limit between chats
            if self.rate_delay:
                time.sleep(self.rate_delay)

        return {"username": self.username, "ok": True, "folder": folder}

# -----------------------
# SQLite helpers
# -----------------------
def init_db(db_path: str):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT,
        chat_id TEXT,
        message_id INTEGER,
        role TEXT,
        content TEXT,
        inserted_at TEXT
    )
    """)
    conn.commit()
    # add regexp function to sqlite
    def _regexp(pattern, item):
        try:
            return 1 if re.search(pattern, item or "", re.IGNORECASE) else 0
        except re.error:
            return 0
    conn.create_function("REGEXP", 2, _regexp)
    return conn

def sqlite_search(conn: sqlite3.Connection, pattern: str):
    cur = conn.cursor()
    cur.execute("SELECT account, chat_id, message_id, role, content FROM messages WHERE REGEXP(?, content) = 1", (pattern,))
    for row in cur.fetchall():
        yield {"account": row[0], "chat_id": row[1], "message_id": row[2], "role": row[3], "content_excerpt": (row[4] or "")[:300]}

# -----------------------
# Offline file search (raw files)
# -----------------------
def offline_search_files(root_dir: str, patterns: List[str], out_file: str = None):
    compiled = [(p, re.compile(p, re.IGNORECASE)) for p in patterns]
    results = []
    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            if not fn.endswith(".txt") and not fn.endswith(".json") and not fn.endswith(".md"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except Exception:
                continue
            for pat, cre in compiled:
                for m in cre.finditer(text):
                    excerpt = text[max(0, m.start()-80):min(len(text), m.end()+80)].replace("\n", " ")
                    entry = {"file": path, "pattern": pat, "match": excerpt}
                    results.append(entry)
                    print(f"[OFFLINE MATCH] {path} pattern={pat} -> ...{excerpt}...")
    if out_file:
        with open(out_file, "w", encoding="utf-8") as of:
            for r in results:
                of.write(json.dumps(r, ensure_ascii=False) + "\n")
    return results

# -----------------------
# CLI / Orchestration
# -----------------------
def main():
    parser = argparse.ArgumentParser(description="deepseek multi-account exporter + regex search")
    parser.add_argument("--accounts", "-a", help="path to accounts.txt (one entry per line)", required=False)
    parser.add_argument("--threads", "-t", help="number of threads (default 4)", type=int, default=4)
    parser.add_argument("--patterns", "-p", help="path to patterns.txt (one regex per line)", required=False)
    parser.add_argument("--live-search", action="store_true", help="perform live regex search while fetching")
    parser.add_argument("--offline-search", action="store_true", help="run offline search on existing output dir using patterns")
    parser.add_argument("--save-db", action="store_true", help="store messages in sqlite db (messages.db under output dir)")
    parser.add_argument("--output", "-o", help="output base dir (default exports)", default="exports")
    parser.add_argument("--rate-delay", type=float, default=0.0, help="delay (s) between requests to reduce rate (default 0.0)")
    parser.add_argument("--timeout", type=int, default=15, help="HTTP timeout seconds")
    parser.add_argument("--max-retries", type=int, default=2, help="max retries on request errors")
    parser.add_argument("--proxies", type=str, default=None, help="optional proxies in requests format (e.g. http://127.0.0.1:8080)")
    parser.add_argument("--quiet", action="store_true", help="less console output")
    parser.add_argument("--matches-out", help="file to store matches.jsonl (default: <output>/matches.jsonl)", default=None)

    args = parser.parse_args()

    # load patterns
    patterns = []
    if args.patterns:
        with open(args.patterns, "r", encoding="utf-8") as pf:
            for ln in pf:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                patterns.append(ln)

    # if offline search only:
    if args.offline_search:
        if not patterns:
            print("[ERROR] offline-search needs --patterns")
            return
        out_file = args.matches_out or os.path.join(args.output, "offline_matches.jsonl")
        print("[*] Running offline search...")
        offline_search_files(args.output, patterns, out_file)
        print("[*] Offline search done.")
        return

    # run main pipeline
    if not args.accounts:
        print("[ERROR] --accounts is required for fetching")
        return

    accounts = parse_accounts(args.accounts)
    if not accounts:
        print("[ERROR] No valid accounts parsed")
        return

    os.makedirs(args.output, exist_ok=True)

    # prepare DB if requested
    db_conn = None
    if args.save_db:
        db_path = os.path.join(args.output, "messages.db")
        db_conn = init_db(db_path)
        if not args.quiet:
            print(f"[+] SQLite DB active: {db_path}")

    # session options
    session_opts = {"timeout": args.timeout}
    if args.proxies:
        session_opts["proxies"] = {"http": args.proxies, "https": args.proxies}
    session_opts["verify"] = True

    # matches root file
    matches_root = args.matches_out or os.path.join(args.output, "matches.jsonl")
    # ensure empty or exists
    open(matches_root, "w", encoding="utf-8").close()

    # worker pool for accounts
    results = []
    with ThreadPoolExecutor(max_workers=args.threads) as exec:
        futures = []
        for username, password in accounts:
            w = AccountWorker(
                username=username, password=password,
                output_base=args.output, session_opts=session_opts,
                patterns=patterns, live_search=args.live_search,
                db_conn=db_conn, rate_delay=args.rate_delay, max_retries=args.max_retries
            )
            futures.append(exec.submit(w.run))
        # progress with tqdm
        for fut in tqdm(as_completed(futures), total=len(futures), desc="accounts"):
            try:
                res = fut.result()
                results.append(res)
            except Exception as e:
                results.append({"ok": False, "error": str(e)})

    # summary
    succ = sum(1 for r in results if r.get("ok"))
    fail = len(results) - succ
    print(f"[*] Done. success={succ}, failed={fail}. matches written to {matches_root}")

    if db_conn:
        db_conn.close()

if __name__ == "__main__":
    main()

