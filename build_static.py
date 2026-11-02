#!/usr/bin/env python3
"""Build a static, read-only copy of the wiki into dist/ (for GitHub Pages).

Pages come from content/pages/. Revision history and recent changes come from
git (every commit that touched a page is one revision). arXiv abstracts and link
previews are resolved at build time through server.py's cache.

    WIKI_BASE=/home-rl-wiki WIKI_REPO=0xSero/home-rl-wiki python3 build_static.py
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
import server as S  # noqa: E402

BASE = os.environ.get("WIKI_BASE", "/home-rl-wiki").rstrip("/")
REPO = os.environ.get("WIKI_REPO", "0xSero/home-rl-wiki")
BRANCH = os.environ.get("WIKI_BRANCH", "main")
OUT = os.path.join(S.ROOT, "dist")


def git(*args):
    try:
        r = subprocess.run(["git", *args], cwd=S.ROOT, capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def git_history(fn):
    """Revisions of content/pages/<fn> from git, newest first, with full source."""
    path = "content/pages/" + fn
    revs = []
    for line in git("log", "--format=%H%x1f%aI%x1f%an%x1f%s", "--", path).splitlines():
        sha, at, author, subject = line.split("\x1f", 3)
        text = git("show", f"{sha}:{path}")
        if not text:
            continue
        m = S.parse_page(text)
        revs.append({"id": sha[:12], "at": at, "author": author, "summary": subject, "bytes": len(m["src"].encode()),
                     "src": m["src"], "title": m.get("title", ""), "cats": m["cats"]})
    return revs


def local_history(slug):
    revs = []
    for r in S.history(slug):
        with open(os.path.join(S.HIST, S.fname(slug), r["id"] + ".wiki"), encoding="utf-8") as f:
            m = S.parse_page(f.read())
        revs.append({**r, "src": m["src"], "title": m.get("title", ""), "cats": m["cats"]})
    return revs


def write_json(rel, data):
    p = os.path.join(OUT, "api", rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    pages = S.all_pages()
    raw = {slug: S.read_page(slug) for slug in pages}
    changes = []
    for slug, p in pages.items():
        fn = S.fname(slug) + ".wiki"
        revs = git_history(fn) or local_history(slug)
        if revs:
            p["at"], p["author"] = revs[0]["at"], revs[0]["author"]
            for i, r in enumerate(revs):
                changes.append({"at": r["at"], "slug": slug, "title": r["title"] or p["title"], "summary": r["summary"],
                                "author": r["author"], "bytes": r["bytes"], "kind": "new page" if i == len(revs) - 1 else "edit"})
        # A seed page whose text no longer matches its seed hash was edited by hand: treat it as user content.
        m = raw[slug]
        if p.get("origin") == "seed" and m.get("seedhash") and S.content_hash(m) != m["seedhash"]:
            p["origin"] = "user"
        write_json(f"history/{S.fname(slug)}.json", revs)
    changes.sort(key=lambda c: c["at"], reverse=True)
    write_json("all.json", {"pages": pages})
    write_json("changes.json", changes[:500])

    # Embeds
    papers, cards = {}, {}
    alltext = re.sub(r"<nowiki>[\s\S]*?</nowiki>", "", "\n".join(p["src"] for p in pages.values()))
    for aid in sorted(set(re.findall(r"\{\{paper\s+(\d{4}\.\d{4,5}(?:v\d+)?)", alltext))):
        papers[aid] = S.arxiv(aid)
    urls = re.findall(r"\{\{card\s+(https?://\S+?)\s*\}\}", alltext)
    for block in re.findall(r"\{\{cards\s+([\s\S]*?)\}\}", alltext):
        urls += [u for u in block.split() if u.startswith("http")]
    for u in sorted(set(urls)):
        cards[u] = S.card(u)
    write_json("arxiv.json", papers)
    write_json("cards.json", cards)

    # App shell: one HTML file per article so links and refreshes return 200; 404.html catches the rest.
    with open(os.path.join(S.ROOT, "index.html"), encoding="utf-8") as f:
        app = f.read()
    cfg = json.dumps({"base": BASE, "static": True, "repo": REPO, "branch": BRANCH})
    app = app.replace("<script>\n(function(){", f"<script>window.WIKI_CFG={cfg};</script>\n<script>\n(function(){{", 1)
    assert "WIKI_CFG" in app

    def shell(title, desc):
        head = f"<title>{html.escape(title)} - Home RL Wiki</title>\n" \
               f'<meta name="description" content="{html.escape(desc)}">\n' \
               f'<meta property="og:title" content="{html.escape(title)}">\n' \
               f'<meta property="og:description" content="{html.escape(desc)}">\n' \
               '<meta property="og:site_name" content="Home RL Wiki">'
        return app.replace("<title>Home RL Wiki</title>", head, 1)

    def lead(src):
        t = re.sub(r"<ref>[\s\S]*?</ref>|\{\{[\s\S]*?\}\}|<[^>]+>", " ", src)
        t = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", t)
        t = re.sub(r"\[\[([^\]]+)\]\]", r"\1", t)
        t = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", t).replace("'''", "").replace("''", "")
        for para in re.split(r"\n\s*\n", t):
            para = " ".join(para.split())
            if para and not para.startswith(("=", "*", "#", "|")):
                return para[:280]
        return ""

    site_desc = "A wiki on RL post-training with LoRA on home hardware: concepts, algorithms, hardware, models and run times."
    for name in ("index.html", "404.html"):
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
            f.write(shell("Main Page", site_desc))
    os.makedirs(os.path.join(OUT, "wiki"))
    n = 0
    for slug, p in pages.items():
        if ":" in slug:
            continue
        name = "Main_Page" if slug == "main" else slug
        with open(os.path.join(OUT, "wiki", name + ".html"), "w", encoding="utf-8") as f:
            f.write(shell(p["title"] or slug, lead(p["src"]) or site_desc))
        n += 1
    if "main" not in pages:
        shutil.copy(os.path.join(OUT, "index.html"), os.path.join(OUT, "wiki", "Main_Page.html"))
    open(os.path.join(OUT, ".nojekyll"), "w").close()
    bad = [k for k, v in {**papers, **cards}.items() if v.get("error")]
    print(f"[build] {len(pages)} pages ({n} article shells), {len(changes)} revisions, "
          f"{len(papers)} papers, {len(cards)} cards, {len(bad)} embed errors {bad[:5]} -> {OUT}")


if __name__ == "__main__":
    main()
