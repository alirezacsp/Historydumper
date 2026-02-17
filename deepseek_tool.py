#!/usr/bin/env python3
"""
deepseek_tool.py
Multi-account exporter + live & offline regex search + multithreaded.
(Full version with clean console output)
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

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

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
            parts = [p.strip() for p in raw.split(":") if p.strip()]
            if len(parts) < 2:
                print(f"[WARN] invalid line #{idx}: {raw}")
                continue
            username = parts[-2]
            password = parts[-1]
            accounts.append((username, password))
    return accounts

def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class AccountWorker:
    def __init__(self, username, password, output_base, session_opts,
                 patterns=None, live_search=False, db_conn=None,
                 rate_delay=0.0, max_retries=2):

        self.username = username
        self.password = password
        self.output_base = output_base
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "deepseek-tool/1.0"
        })

        self.patterns = patterns or []
        self.live_search = live_search
        self.db_conn = db_conn
        self.rate_delay = rate_delay
        self.max_retries = max_retries
        self.session_opts = session_opts

    # ---------------- LOGIN ----------------
    def _try_login_payloads(self):
        return [
            {"email": self.username, "mobile": "", "password": self.password, "area_code": "", "device_id": "", "os": ""},
            {"email": "", "mobile": self.username, "password": self.password, "area_code": "", "device_id": "", "os": ""},
        ]

    def login(self):
        last_err = None
        for payload in self._try_login_payloads():
            for attempt in range(self.max_retries + 1):
                try:
                    r = self.session.post(
                        LOGIN_URL,
                        json=payload,
                        timeout=self.session_opts.get("timeout", 15),
                        proxies=self.session_opts.get("proxies"),
                        verify=self.session_opts.get("verify", True)
                    )

                    if r.status_code != 200:
                        last_err = f"HTTP {r.status_code}"
                        time.sleep(1 + attempt)
                        continue

                    data = r.json()
                    token = data.get("data", {}).get("biz_data", {}).get("user", {}).get("token")
                    if token:
                        return token, data

                    last_err = "No token in response"
                except Exception as e:
                    last_err = str(e)

                time.sleep(1 + attempt)

        raise RuntimeError(f"")

    # ---------------- FETCH ----------------
    def fetch_sessions(self, token):
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.get(
                    SESSIONS_URL,
                    headers=headers,
                    timeout=self.session_opts.get("timeout", 15),
                    proxies=self.session_opts.get("proxies"),
                    verify=self.session_opts.get("verify", True)
                )
                if r.status_code == 200:
                    data = r.json()
                    return data.get("data", {}).get("biz_data", {}).get("chat_sessions", [])
            except:
                pass
            time.sleep(1 + attempt)
        return []

    def fetch_history(self, token, chat_id):
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.get(
                    HISTORY_URL.format(chat_id),
                    headers=headers,
                    timeout=self.session_opts.get("timeout", 15),
                    proxies=self.session_opts.get("proxies"),
                    verify=self.session_opts.get("verify", True)
                )
                if r.status_code == 200:
                    data = r.json()
                    return data.get("data", {}).get("biz_data", {})
            except:
                pass
            time.sleep(1 + attempt)
        return {}

    # ---------------- RUN ----------------
    def run(self):

        ts = timestamp()
        folder = os.path.join(self.output_base, f"{safe_filename(self.username)}_{ts}")
        os.makedirs(folder, exist_ok=True)

        try:
            token, login_response = self.login()
        except Exception as e:
            console.print(f"")
            return {"username": self.username, "ok": False}

        with open(os.path.join(folder, "login_response.json"), "w", encoding="utf-8") as f:
            json.dump(login_response, f, indent=2, ensure_ascii=False)

        sessions = self.fetch_sessions(token)

        with open(os.path.join(folder, "chats_index.json"), "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2, ensure_ascii=False)

        compiled = [(p, re.compile(p, re.IGNORECASE)) for p in self.patterns]
        live_matches = []

        for chat in sessions:
            cid = chat.get("id")
            title = chat.get("title") or cid
            safe_title = safe_filename(title)

            history = self.fetch_history(token, cid)
            messages = history.get("chat_messages", [])

            chat_file = os.path.join(folder, f"{cid}-{safe_title}.txt")

            with open(chat_file, "w", encoding="utf-8") as fout:
                for m in messages:
                    role = m.get("role", "")
                    content = m.get("content", "") or ""
                    inserted_at = m.get("inserted_at", "")

                    fout.write(f"[{role}] inserted_at={inserted_at}\n{content}\n\n")

                    if self.db_conn:
                        try:
                            cur = self.db_conn.cursor()
                            cur.execute(
                                "INSERT INTO messages(account, chat_id, message_id, role, content, inserted_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (self.username, cid, m.get("message_id"), role, content, inserted_at)
                            )
                            self.db_conn.commit()
                        except:
                            pass

                    if self.live_search and compiled:
                        for pat, cre in compiled:
                            if cre.search(content):
                                excerpt = content[:200] + ("..." if len(content) > 200 else "")
                                live_matches.append((cid, pat, m.get("message_id"), excerpt))

                                out = {
                                    "username": self.username,
                                    "chat_id": cid,
                                    "pattern": pat,
                                    "message_id": m.get("message_id"),
                                    "match_excerpt": excerpt
                                }

                                with open(os.path.join(self.output_base, "matches.jsonl"), "a", encoding="utf-8") as mf:
                                    mf.write(json.dumps(out, ensure_ascii=False) + "\n")

            if self.rate_delay:
                time.sleep(self.rate_delay)


        if live_matches:
            console.print(f"\n[bold cyan]username:[/bold cyan] [bold yellow]{self.username}[/bold yellow]\n")

            table = Table(title="Live Search Results", box=box.ROUNDED, show_lines=True)
            table.add_column("Chat ID", style="magenta")
            table.add_column("Pattern", style="red")
            table.add_column("Message ID", style="green")
            table.add_column("Excerpt", style="white")

            for row in live_matches:
                table.add_row(str(row[0]), row[1], str(row[2]), row[3])

            console.print(table)

        return {"username": self.username, "ok": True}




def offline_search_files(root_dir: str, patterns: List[str], out_file: str = None):

    compiled = [(p, re.compile(p, re.IGNORECASE)) for p in patterns]
    grouped = {}

    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            if not fn.endswith((".txt", ".json", ".md")):
                continue

            path = os.path.join(dirpath, fn)

            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except:
                continue

            for pat, cre in compiled:
                for m in cre.finditer(text):
                    excerpt = text[max(0, m.start()-80):min(len(text), m.end()+80)]
                    excerpt = excerpt.replace("\n", " ")

                    if path not in grouped:
                        grouped[path] = []

                    grouped[path].append((pat, excerpt))

    if not grouped:
        return []

    for path, rows in grouped.items():
        console.print(f"\n[bold cyan]فایل:[/bold cyan] [bold yellow]{path}[/bold yellow]\n")

        table = Table(title="Offline Search Results", box=box.ROUNDED, show_lines=True)
        table.add_column("Pattern", style="red", width=20)
        table.add_column("Excerpt", style="white")

        for pat, excerpt in rows:
            table.add_row(pat, excerpt)

        console.print(table)

    if out_file:
        with open(out_file, "w", encoding="utf-8") as of:
            for path, rows in grouped.items():
                for pat, excerpt in rows:
                    entry = {"file": path, "pattern": pat, "match": excerpt}
                    of.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return grouped



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
    return conn



def main():
    parser = argparse.ArgumentParser(description="deepseek multi-account exporter + regex search power 1.0.0 - Developed by alirezacsp")
    parser.add_argument("--accounts", "-a")
    parser.add_argument("--threads", "-t", type=int, default=4)
    parser.add_argument("--patterns", "-p")
    parser.add_argument("--live-search", action="store_true")
    parser.add_argument("--offline-search", action="store_true")
    parser.add_argument("--save-db", action="store_true")
    parser.add_argument("--output", "-o", default="exports")
    parser.add_argument("--rate-delay", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--proxies", type=str, default=None)

    args = parser.parse_args()

    patterns = []
    if args.patterns:
        with open(args.patterns, "r", encoding="utf-8") as pf:
            patterns = [ln.strip() for ln in pf if ln.strip() and not ln.startswith("#")]

    if args.offline_search:
        offline_search_files(args.output, patterns)
        return

    accounts = parse_accounts(args.accounts)
    os.makedirs(args.output, exist_ok=True)

    db_conn = None
    if args.save_db:
        db_conn = init_db(os.path.join(args.output, "messages.db"))

    session_opts = {"timeout": args.timeout}
    if args.proxies:
        session_opts["proxies"] = {"http": args.proxies, "https": args.proxies}
    session_opts["verify"] = True

    matches_root = os.path.join(args.output, "matches.jsonl")
    open(matches_root, "w").close()

    with ThreadPoolExecutor(max_workers=args.threads) as exec:
        futures = []
        for username, password in accounts:
            worker = AccountWorker(
                username=username,
                password=password,
                output_base=args.output,
                session_opts=session_opts,
                patterns=patterns,
                live_search=args.live_search,
                db_conn=db_conn,
                rate_delay=args.rate_delay,
                max_retries=args.max_retries
            )
            futures.append(exec.submit(worker.run))

        for _ in tqdm(as_completed(futures), total=len(futures), desc="accounts"):
            pass

    if db_conn:
        db_conn.close()

    console.print("\n[green]Done.[/green]")


if __name__ == "__main__":
    main()