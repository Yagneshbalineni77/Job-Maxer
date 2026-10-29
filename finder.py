"""
Fresher job finder.

Reads the public job feeds of the companies in companies.json (Greenhouse,
Lever and Ashby all publish open JSON feeds), keeps entry-level
engineering roles in your locations (settings.json), and writes:
  found_jobs.csv   (open in Excel)
  found_jobs.json  (autofill.py reads this: press c in its menu)

Usage:
  python finder.py            -> search every company
  python finder.py --check    -> just report which company names work
"""
import csv
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

BOT = Path(__file__).resolve().parent
COMPANIES = json.loads((BOT / "companies.json").read_text(encoding="utf-8"))

SETTINGS_FILE = BOT / "settings.json"
SETTINGS = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.is_file() else {}
# Which job locations to keep: "locations" in settings.json (a regex), India's tech cities by default.
INDIA = re.compile(SETTINGS.get("locations") or
                   r"india|bengaluru|bangalore|hyderabad|chennai|pune|mumbai|gurgaon|gurugram|"
                   r"noida|delhi|ncr|kolkata|ahmedabad|kochi|coimbatore|trivandrum|jaipur", re.I)
FRESHER = re.compile(r"new ?grad|graduate|fresher|entry|early career|campus|university|trainee|"
                     r"apprentice|associate|junior|\bjr\b|\bsde ?-?1\b|\bsde ?i\b|engineer ?-? ?i\b|"
                     r"engineer 1\b|\bl1\b|\bl3\b|level 1|\bswe ?i\b", re.I)
SENIOR = re.compile(r"senior|\bsr\b|staff|principal|lead|manager|director|head|architect|"
                    r"\bii\b|\biii\b|\biv\b|engineer 2|engineer 3|sde ?-?2|sde ?-?3|\bvp\b|expert|"
                    r"level ?[2-9]|\bl[2-9]\b", re.I)
ENGINEERING = re.compile(r"engineer|developer|software|sde|swe|machine learning|\bml\b|\bai\b|data|"
                         r"embedded|firmware|hardware|vlsi|asic|fpga|verification|silicon|chip|"
                         r"electronics|robotics|computer vision|research|analyst|scientist", re.I)


def get_json(url):
    import time
    import urllib.error
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 job-finder"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:  # the job board says "too many requests": wait, then retry
                time.sleep(8 * (attempt + 1))
                continue
            raise
        except TimeoutError:
            if attempt < 3:
                continue
            raise


def greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    for j in data.get("jobs", []):
        # The hosted form, not the company's own careers page (which often wraps it).
        yield (j["title"], (j.get("location") or {}).get("name", ""),
               f"https://job-boards.greenhouse.io/{slug}/jobs/{j['id']}")


def lever(slug):
    for j in get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        cat = j.get("categories") or {}
        locs = " / ".join(filter(None, [cat.get("location", "")] + cat.get("allLocations", [])))
        yield j["text"], locs, j["hostedUrl"].rstrip("/") + "/apply"


def ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    for j in data.get("jobs", []):
        locs = " / ".join(filter(None, [j.get("location", "")] +
                                 [s.get("location", "") for s in j.get("secondaryLocations", [])]))
        yield j["title"], locs, j.get("applyUrl") or j["jobUrl"]


def workable(slug):
    data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    for j in data.get("jobs", []):
        loc = ", ".join(filter(None, [j.get("city", ""), j.get("state", ""), j.get("country", "")]))
        if j.get("telecommuting"):
            loc += " / Remote"
        yield j["title"], loc, f"https://apply.workable.com/{slug}/j/{j['shortcode']}/apply/"


WORKDAY_SEARCHES = ["graduate", "intern", "fresher", "entry level", "associate engineer", "new college",
                    "university", "trainee", "early career", "engineer I", "junior"]


def workday(slug):
    """slug is "tenant.wdN|site", e.g. "nvidia.wd5|NVIDIAExternalCareerSite" (from the careers page address)."""
    host, site = slug.split("|")
    tenant = host.split(".")[0]
    url = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    seen = set()
    for text in WORKDAY_SEARCHES:
        for offset in (0, 20):
            body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": text}).encode()
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                posts = json.loads(r.read().decode("utf-8")).get("jobPostings", [])
            for p in posts:
                path = p.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                yield p.get("title", ""), p.get("locationsText", ""), f"https://{host}.myworkdayjobs.com/{site}{path}"
            if len(posts) < 20:
                break


SOURCES = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable, "workday": workday}


def fetch(src, slug):
    try:
        return src, slug, list(SOURCES[src](slug)), None
    except Exception as e:
        return src, slug, [], f"{e.__class__.__name__}: {e}"[:80]


def main():
    check = "--check" in sys.argv
    tasks = [(src, slug) for src, slugs in COMPANIES.items() for slug in slugs]
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(lambda t: fetch(*t), tasks))

    found, bad = [], []
    for src, slug, jobs, err in results:
        if err:
            bad.append(f"{src}/{slug}: {err}")
            continue
        for title, loc, url in jobs:
            if INDIA.search(loc) and ENGINEERING.search(title) and not SENIOR.search(title) \
                    and not re.search(r"\d+\+? ?years", title, re.I):
                level = "fresher" if FRESHER.search(title) else "no level stated"
                company = slug.split(".")[0] if src == "workday" else slug  # "nvidia.wd5|Site" -> "nvidia"
                found.append({"level": level, "company": company, "title": title, "location": loc[:80],
                              "url": url, "source": src, "found": date.today().isoformat()})
        if check:
            print(f"  ok  {src:<10} {slug:<28} {len(jobs)} open jobs")

    if bad:
        print("\nThese company names didn't work (remove or fix them in companies.json):")
        for b in bad:
            print("  x ", b)
    if check:
        return

    seen_file = BOT / "seen_jobs.json"
    seen = set(json.loads(seen_file.read_text(encoding="utf-8"))) if seen_file.is_file() else None
    for j in found:
        j["new"] = seen is not None and j["url"] not in seen
    seen_file.write_text(json.dumps(sorted((seen or set()) | {j["url"] for j in found})), encoding="utf-8")

    found.sort(key=lambda j: (not j["new"], j["level"] != "fresher", j["company"], j["title"]))
    (BOT / "found_jobs.json").write_text(json.dumps(found, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(BOT / "found_jobs.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["new", "level", "company", "title", "location", "url", "source", "found"])
        w.writeheader()
        w.writerows(found)
    fresher = [j for j in found if j["level"] == "fresher"]
    new = [j for j in found if j["new"]]
    print(f"\nEngineering jobs from {len(results) - len(bad)} companies: "
          f"{len(fresher)} fresher-level, {len(found) - len(fresher)} with no level stated, "
          f"{len(new)} new since last run.")
    print("\nFresher-level:")
    for j in fresher:
        print(f"  {'NEW ' if j['new'] else '    '}{j['company']:<14} {j['title'][:60]:<60} {j['location'][:26]}")
    print("\nThe rest are in found_jobs.csv (open in Excel).")
    if new and "--notify" in sys.argv:
        import ctypes
        top = "\n".join(f"{j['company']}: {j['title'][:50]}" for j in new[:8])
        ctypes.windll.user32.MessageBoxW(0, f"{len(new)} new job(s):\n{top}\n\nSee found_jobs.csv in the bot folder.",
                                         "Job alert", 0x40)


if __name__ == "__main__":
    main()
