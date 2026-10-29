"""
Government recruitment watcher.

Opens each careers page in gov_sites.json (in a hidden Chrome), collects the
links that look like recruitment notices, and compares them with what it saw
last time (gov_seen.json). New notices are printed and saved to
gov_new_notices.csv.

Usage:
  python gov_watch.py            -> check every site
  python gov_watch.py --notify   -> also show a popup when something new appears
"""
import csv
import ctypes
import json
import re
import sys
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(errors="replace")

BOT = Path(__file__).resolve().parent
SITES = json.loads((BOT / "gov_sites.json").read_text(encoding="utf-8"))
SEEN_FILE = BOT / "gov_seen.json"
NEW_FILE = BOT / "gov_new_notices.csv"

NOTICE = re.compile(r"recruit|advt|advertisement|notification|vacanc|walk[- ]?in|engineer|scientist|"
                    r"trainee|apprentice|officer|project|post|career|corrigendum|result|shortlist", re.I)
NOISE = re.compile(r"^(home|about|contact|login|sitemap|faq|feedback|tender|rti|screen reader|skip)", re.I)

HREF = re.compile(r"recruit|career|advt|advertis|vacanc|notification|walk.?in|\.pdf$", re.I)

# Links plus visible text lines (some sites build the page with scripts and have no plain links).
COLLECT_JS = r"""() => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const links = Array.from(document.querySelectorAll('a'))
    .map(a => [t(a.innerText || a.title), a.href])
    .filter(x => x[1] && !x[1].startsWith('javascript'));
  const lines = (document.body ? document.body.innerText : '').split('\n').map(t)
    .filter(s => s.length >= 15 && s.length <= 200);
  return {links, lines};
}"""


def notices_in(page):
    found = set()
    for frame in page.frames:
        try:
            data = frame.evaluate(COLLECT_JS)
        except Exception:
            continue
        for text, href in data["links"]:
            if (NOTICE.search(text) or HREF.search(href)) and not NOISE.search(text):
                found.add(f"{(text or href.rsplit('/', 1)[-1])[:150]} | {href}")
        for line in data["lines"]:
            if NOTICE.search(line) and not NOISE.search(line):
                found.add(f"{line[:150]} | {page.url}")
    return found


def main():
    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.is_file() else {}
    first_run = not seen
    new_rows, failed = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(locale="en-US", extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
        for site in SITES:
            page = ctx.new_page()  # a fresh tab per site, so one hung site can't break the rest
            try:
                page.goto(site["url"], wait_until="domcontentloaded", timeout=site.get("timeout", 60) * 1000)
                page.wait_for_timeout(8000)
                notices = notices_in(page)
            except Exception as e:
                failed.append(f"{site['name']} ({str(e).splitlines()[0][:70]})")
                continue
            finally:
                page.close()
            old = set(seen.get(site["name"], []))
            fresh = sorted(notices - old) if old else []
            for item in fresh:
                text, _, href = item.rpartition(" | ")
                new_rows.append({"date": date.today().isoformat(), "site": site["name"],
                                 "notice": text, "link": href})
            seen[site["name"]] = sorted(notices | old)
            print(f"  {site['name']:<34} {len(notices):>3} notice links" +
                  (f"   {len(fresh)} NEW" if fresh else ""))
        browser.close()

    SEEN_FILE.write_text(json.dumps(seen, indent=1, ensure_ascii=False), encoding="utf-8")
    if new_rows:
        new = not NEW_FILE.is_file()
        with open(NEW_FILE, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "site", "notice", "link"])
            if new:
                w.writeheader()
            w.writerows(new_rows)
        print(f"\n{len(new_rows)} new notice(s), saved to gov_new_notices.csv:")
        for r in new_rows:
            print(f"  [{r['site']}] {r['notice'][:90]}\n      {r['link']}")
    elif first_run:
        print("\nFirst run: saved what every site shows today. From the next run, only new notices are shown.")
    else:
        print("\nNo new notices since the last check.")
    if failed:
        print("\nCouldn't open (site slow or down, will retry next run):", "; ".join(failed))
    if new_rows and "--notify" in sys.argv:
        names = sorted({r["site"] for r in new_rows})
        ctypes.windll.user32.MessageBoxW(0, f"{len(new_rows)} new government notice(s):\n" + "\n".join(names) +
                                         "\n\nSee gov_new_notices.csv in JobApplyBot.", "Job alert", 0x40)


if __name__ == "__main__":
    main()
