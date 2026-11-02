#!/usr/bin/env python3
"""Home RL Wiki: a tiny local MediaWiki-style server (stdlib only).

Serves index.html for /wiki/* paths and a JSON API under /api/.
Pages live as text files in content/pages/, every save is kept in
content/history/<page>/, and all edits are logged to content/changes.jsonl.

Run:  python3 server.py            (http://127.0.0.1:8765/wiki/Main_Page)
"""
import hashlib, html, json, os, re, sys, threading, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
CONTENT = os.path.join(ROOT, "content")
PAGES = os.path.join(CONTENT, "pages")
HIST = os.path.join(CONTENT, "history")
CACHE = os.path.join(CONTENT, "cache")
CHANGES = os.path.join(CONTENT, "changes.jsonl")
ANSWERS = os.path.join(CONTENT, "answers.json")
SEED = os.path.join(ROOT, "seed.wiki")
PORT = int(os.environ.get("WIKI_PORT", "8765"))
LOCK = threading.Lock()
SLUG_RE = re.compile(r"^(talk:)?[a-z0-9][a-z0-9.\-]{0,100}$")
UA = "Mozilla/5.0 (Macintosh; HomeRLWiki/1.0) AppleWebKit/537.36 (KHTML, like Gecko)"

for d in (PAGES, HIST, CACHE):
    os.makedirs(d, exist_ok=True)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fname(slug):
    return slug.replace(":", "__")


def parse_page(text):
    head, _, body = text.partition("\n---\n")
    meta = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    meta["cats"] = [c.strip() for c in meta.get("cats", "").split(",") if c.strip()]
    meta["src"] = body
    return meta


def dump_page(meta):
    head = [f"title: {meta.get('title','')}", f"cats: {', '.join(meta.get('cats', []))}",
            f"origin: {meta.get('origin','user')}", f"at: {meta.get('at','')}",
            f"author: {meta.get('author','')}", f"summary: {meta.get('summary','').replace(chr(10),' ')}"]
    if meta.get("seedhash"):
        head.append(f"seedhash: {meta['seedhash']}")
    return "\n".join(head) + "\n---\n" + meta.get("src", "")


def read_page(slug):
    p = os.path.join(PAGES, fname(slug) + ".wiki")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return parse_page(f.read())


def write_page(slug, meta, kind):
    """Write current version, keep a revision, log the change. Caller holds LOCK."""
    meta["at"] = meta.get("at") or now()
    with open(os.path.join(PAGES, fname(slug) + ".wiki"), "w", encoding="utf-8") as f:
        f.write(dump_page(meta))
    hd = os.path.join(HIST, fname(slug))
    os.makedirs(hd, exist_ok=True)
    rid = meta["at"].replace(":", "-")
    with open(os.path.join(hd, rid + ".wiki"), "w", encoding="utf-8") as f:
        f.write(dump_page(meta))
    with open(CHANGES, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": meta["at"], "slug": slug, "title": meta.get("title", slug),
                            "summary": meta.get("summary", ""), "author": meta.get("author", ""),
                            "kind": kind, "bytes": len(meta.get("src", "").encode())}) + "\n")


def load_seed():
    if not os.path.exists(SEED):
        return {}
    with open(SEED, encoding="utf-8") as f:
        text = f.read()
    out = {}
    for chunk in re.split(r"^@@@ ", text, flags=re.M)[1:]:
        slug, _, rest = chunk.partition("\n")
        m = parse_page(rest)
        out[slug.strip()] = {"title": m.get("title", slug), "cats": m["cats"], "src": m["src"].rstrip() + "\n"}
    return out


def content_hash(p):
    return hashlib.sha1(json.dumps([p.get("title", ""), p.get("cats", []), p.get("src", "").rstrip() + "\n"]).encode()).hexdigest()[:16]


def sync_seed():
    """Create missing pages from seed.wiki; refresh pages nobody has edited.

    A page is refreshed only while its text still matches the seed version it was
    written from (seedhash). Pages changed by hand, here or on GitHub, are left alone.
    """
    seed = load_seed()
    with LOCK:
        for slug, s in seed.items():
            h = content_hash(s)
            cur = read_page(slug)
            if cur is None:
                write_page(slug, {**s, "origin": "seed", "author": "Claude", "summary": "Original version", "at": now(), "seedhash": h}, "new page")
            elif cur.get("origin") == "seed":
                if not cur.get("seedhash") and content_hash(cur) == h:
                    with open(os.path.join(PAGES, fname(slug) + ".wiki"), "w", encoding="utf-8") as f:
                        f.write(dump_page({**cur, "seedhash": h}))  # backfill, not a new revision
                    continue
                stored = cur.get("seedhash") or content_hash(cur)
                if content_hash(cur) != stored:
                    continue  # edited outside the wiki; keep it
                if h != stored:
                    write_page(slug, {**s, "origin": "seed", "author": "Claude", "summary": "Updated original version", "at": now(), "seedhash": h}, "edit")
    return len(seed)


def all_pages():
    out = {}
    for fn in os.listdir(PAGES):
        if fn.endswith(".wiki"):
            slug = fn[:-5].replace("__", ":")
            m = read_page(slug)
            out[slug] = {k: m.get(k, "") for k in ("title", "cats", "src", "at", "author", "origin")}
    return out


def history(slug):
    hd = os.path.join(HIST, fname(slug))
    if not os.path.isdir(hd):
        return []
    revs = []
    for fn in sorted(os.listdir(hd), reverse=True):
        if fn.endswith(".wiki"):
            with open(os.path.join(hd, fn), encoding="utf-8") as f:
                m = parse_page(f.read())
            revs.append({"id": fn[:-5], "at": m.get("at"), "summary": m.get("summary", ""), "author": m.get("author", ""),
                         "bytes": len(m["src"].encode())})
    return revs


def fetch(url, limit=600_000, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode(r.headers.get_content_charset() or "utf-8", "replace")


def cached(kind, key, fn):
    d = os.path.join(CACHE, kind)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, hashlib.sha1(key.encode()).hexdigest() + ".json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    data = fn()
    if data and not data.get("error"):
        with open(p, "w") as f:
            json.dump(data, f)
    return data


def arxiv(aid):
    def go():
        try:
            x = fetch("https://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(aid), timeout=10)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            e = ET.fromstring(x).find("a:entry", ns)
            if e is None or e.find("a:title", ns) is None:
                return {"error": "not found"}
            t = lambda tag: re.sub(r"\s+", " ", (e.find(tag, ns).text or "")).strip()
            return {"id": aid, "title": t("a:title"), "abstract": t("a:summary"), "published": t("a:published")[:10],
                    "authors": [re.sub(r"\s+", " ", a.find("a:name", ns).text or "") for a in e.findall("a:author", ns)],
                    "url": "https://arxiv.org/abs/" + aid}
        except Exception as ex:  # noqa: BLE001
            return {"error": str(ex)[:200]}
    return cached("arxiv", aid, go)


def card(url):
    def go():
        try:
            h = fetch(url)
            def meta(*names):
                for n in names:
                    m = re.search(r'<meta[^>]+(?:property|name)=["\']' + re.escape(n) + r'["\'][^>]*content=["\']([^"\']*)', h, re.I) \
                        or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']' + re.escape(n) + r'["\']', h, re.I)
                    if m and m.group(1).strip():
                        return html.unescape(m.group(1).strip())
                return ""
            title = meta("og:title", "twitter:title")
            if not title:
                m = re.search(r"<title[^>]*>([\s\S]*?)</title>", h, re.I)
                title = html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else url
            img = meta("og:image", "twitter:image")
            if img:
                img = urllib.parse.urljoin(url, img)
            return {"url": url, "title": title, "description": meta("og:description", "description", "twitter:description")[:600],
                    "image": img, "site": meta("og:site_name") or urllib.parse.urlparse(url).netloc}
        except Exception as ex:  # noqa: BLE001
            return {"url": url, "error": str(ex)[:200]}
    return cached("cards", url, go)


class H(BaseHTTPRequestHandler):
    server_version = "HomeRLWiki/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[wiki] " + fmt % args + "\n")

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = urllib.parse.unquote(u.path)
        if p == "/" or p == "/wiki" or p == "/wiki/":
            self.send_response(302); self.send_header("Location", "/wiki/Main_Page"); self.end_headers(); return
        if p.startswith("/api/"):
            parts = p[5:].split("/")
            if parts[0] == "all":
                return self.send(200, {"pages": all_pages()})
            if parts[0] == "history" and len(parts) == 2 and SLUG_RE.match(parts[1]):
                return self.send(200, history(parts[1]))
            if parts[0] == "rev" and len(parts) == 3 and SLUG_RE.match(parts[1]) and re.match(r"^[\w.\-]+$", parts[2]):
                fp = os.path.join(HIST, fname(parts[1]), parts[2] + ".wiki")
                if os.path.exists(fp):
                    with open(fp, encoding="utf-8") as f:
                        return self.send(200, parse_page(f.read()))
                return self.send(404, {"error": "no such revision"})
            if parts[0] == "changes":
                rows = []
                if os.path.exists(CHANGES):
                    with open(CHANGES, encoding="utf-8") as f:
                        rows = [json.loads(l) for l in f if l.strip()]
                return self.send(200, rows[::-1][:500])
            if parts[0] == "answers":
                if os.path.exists(ANSWERS):
                    with open(ANSWERS, encoding="utf-8") as f:
                        return self.send(200, json.load(f))
                return self.send(200, {})
            if parts[0] == "arxiv" and len(parts) == 2 and re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", parts[1]):
                return self.send(200, arxiv(parts[1]))
            if parts[0] == "card":
                url = (q.get("url") or [""])[0]
                if not re.match(r"^https?://", url):
                    return self.send(400, {"error": "bad url"})
                return self.send(200, card(url))
            return self.send(404, {"error": "unknown api"})
        if p == "/favicon.ico":
            return self.send(204, b"", "image/x-icon")
        # every other path is the single-page app
        with open(os.path.join(ROOT, "index.html"), "rb") as f:
            return self.send(200, f.read(), "text/html; charset=utf-8")

    def do_POST(self):
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"error": "bad origin"})
        try:
            b = self.body()
        except Exception:  # noqa: BLE001
            return self.send(400, {"error": "bad json"})
        parts = p[5:].split("/") if p.startswith("/api/") else []
        if len(parts) == 2 and parts[0] == "page" and SLUG_RE.match(parts[1]):
            slug = parts[1]
            src = str(b.get("src", ""))
            if len(src) > 400_000:
                return self.send(413, {"error": "page too large"})
            with LOCK:
                cur = read_page(slug)
                kind = "new page" if cur is None else ("restore" if b.get("restore") else "edit")
                meta = {"title": str(b.get("title") or (cur or {}).get("title") or slug)[:200],
                        "cats": [str(c)[:60] for c in (b.get("cats") if b.get("cats") is not None else (cur or {}).get("cats", []))][:20],
                        "src": src, "origin": "user", "author": str(b.get("author") or "Sero")[:60],
                        "summary": str(b.get("summary") or "")[:300], "at": now()}
                write_page(slug, meta, kind)
            return self.send(200, {"ok": True, "page": {k: meta[k] for k in ("title", "cats", "src", "at", "author", "origin")}})
        if parts == ["answers"]:
            b["updatedAt"] = now()
            with LOCK, open(ANSWERS, "w", encoding="utf-8") as f:
                json.dump(b, f, indent=2)
            return self.send(200, {"ok": True, "updatedAt": b["updatedAt"]})
        return self.send(404, {"error": "unknown api"})


if __name__ == "__main__":
    n = sync_seed()
    print(f"[wiki] {n} seed articles synced; serving http://127.0.0.1:{PORT}/wiki/Main_Page", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
