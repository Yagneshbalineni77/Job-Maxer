"""
Automatic applying for company jobs (Greenhouse, Lever, Ashby, Workable forms).

For each new matching job:
  1. Gemini reads the job description and checks it suits an entry-level candidate
     (the roles and location set in settings.json; internships included).
  2. The form is filled from profile.json, saved answers, and Gemini for
     written questions. Standard privacy / accuracy confirmations get "Yes".
  3. It submits only if every required field is filled and no captcha appears.
     Anything else goes to the "needs you" list.

Results:  auto_log.csv (every job and what happened)
          ai_answers.jsonl (every answer Gemini wrote, per job)

Usage:
  python apply_auto.py              -> run with settings.json
  python apply_auto.py --test       -> fill forms but never submit (a test run)
  python apply_auto.py --notify     -> popup summary at the end (used by the daily task)
  python apply_auto.py --limit 3    -> try at most 3 jobs this run
  python apply_auto.py --only <part of a job link> --real   -> submit just that one job for real
"""
import csv
import ctypes
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

import ai
import autofill

sys.stdout.reconfigure(errors="replace")

BOT = Path(__file__).resolve().parent
SETTINGS_FILE = BOT / "settings.json"
LOG_FILE = BOT / "auto_log.csv"
ANSWER_LOG = BOT / "ai_answers.jsonl"
LOG_FIELDS = ["date", "time", "status", "company", "title", "url", "reason"]

DEFAULT_SETTINGS = {
    "max_per_day": 15,
    "test_mode": True,
    "gemini_models": ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash"],
    "show_browser": False,
    "captcha_wait_minutes": 3,
    "you_submit_sites": [],
    "manual_sites": ["lever.co"],
    "manual_companies": [],
    "hand_over_minutes": 5,
    "include_roles": r"software|\bsde\b|\bswe\b|developer|back-?end|full.?stack|front.?end|machine learning|\bml\b|"
                     r"\bai\b|artificial intelligence|data scien|computer vision|deep learning|\bllm|gen ?ai|"
                     r"generative|\bnlp\b|applied scientist|research engineer|agent|forward deployed|"
                     r"platform engineer|data engineer|intern",
    "exclude_roles": r"hardware|vlsi|asic|fpga|embedded|firmware|verification|\brtl\b|silicon|analog|\bpcb\b|"
                     r"customer|support|sales|account|consultant|value engineer|operations|recruit|marketing|"
                     r"legal|finance|manager|senior|\bsr\b|staff|principal|lead",
}

SUPPORTED = re.compile(r"(job-boards|boards)\.greenhouse\.io|jobs(\.eu)?\.lever\.co|jobs\.ashbyhq\.com|"
                       r"apply\.workable\.com|myworkdayjobs\.com", re.I)
SUCCESS = re.compile(r"thank(s| you) for (applying|your (application|interest))|application (was |has been )?"
                     r"(submitted|received|sent)|successfully (submitted|applied)|we('ve| have) received your "
                     r"application|you('ve| have) (successfully )?applied", re.I)
# Not "required field": Greenhouse shows "* indicates a required field" on every form.
FORM_ERROR = re.compile(r"(field|this|it) is required|missing entry for required field|form needs corrections|"
                        r"must be in the (past|future)|must be (before|after)|invalid date|"
                        r"please (fill|complete|enter|select|upload)|can't be blank|"
                        r"cannot be blank|is invalid|fix the errors", re.I)
# The site's bot detection refused the submission. The bot never retries these: they go to do_by_hand.csv.
BLOCKED = re.compile(r"flagged as (possible )?spam|couldn.t submit your application|error verifying your "
                     r"application|unusual (activity|traffic)|automated (traffic|submission|request)|"
                     r"suspected (bot|spam)|request (was )?blocked|access denied", re.I)
MANUAL_FILE = BOT / "do_by_hand.csv"
# The site emailed a "prove you're human" code: you type it, the bot only waits.
VERIFY_CODE = re.compile(r"verification code (was|has been) sent|security code|code to confirm you.re a human|"
                         r"enter the code (we|that was) sent", re.I)
REQUIRED_PROFILE = ["first_name", "last_name", "full_name", "email", "mobile", "city", "linkedin"]
NICE_TO_HAVE = ["expected_ctc", "notice_period", "start_date", "current_employer", "current_title",
                "degree_start_month", "degree_end_month", "grad_date"]


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.is_file():
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    else:
        SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, indent=2), encoding="utf-8")
    return s


def read_log():
    if not LOG_FILE.is_file():
        return []
    with open(LOG_FILE, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def write_log(job, status, reason):
    new = not LOG_FILE.is_file()
    now = datetime.now()
    with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"date": now.date().isoformat(), "time": now.strftime("%H:%M"), "status": status,
                    "company": job["company"], "title": job["title"], "url": job["url"], "reason": reason[:300]})
    print(f"  -> {status.upper()}: {reason[:120]}")
    if status in ("needs_you", "blocked", "manual"):
        add_to_manual_list(job, status, reason)


def add_to_manual_list(job, status, reason):
    """do_by_hand.csv: one row per job you need to finish yourself (newest reason wins)."""
    fields = ["date", "company", "title", "link", "why"]
    rows = []
    if MANUAL_FILE.is_file():
        with open(MANUAL_FILE, encoding="utf-8-sig") as fh:
            rows = [r for r in csv.DictReader(fh) if r["link"] != job["url"]]
    why = ("the site's bot check blocked it: apply in your normal Chrome" if status == "blocked" else reason)[:250]
    rows.append({"date": date.today().isoformat(), "company": job["company"], "title": job["title"],
                 "link": job["url"], "why": why})
    with open(MANUAL_FILE, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def remove_from_manual_list(url):
    if not MANUAL_FILE.is_file():
        return
    with open(MANUAL_FILE, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    keep = [r for r in rows if r["link"] != url]
    if len(keep) != len(rows):
        with open(MANUAL_FILE, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "company", "title", "link", "why"])
            w.writeheader()
            w.writerows(keep)


def form_url(url):
    host = urlparse(url).hostname or ""
    if "lever.co" in host and not url.rstrip("/").endswith("/apply"):
        return url.rstrip("/") + "/apply"
    if "ashbyhq.com" in host and not url.rstrip("/").endswith("/application"):
        return url.rstrip("/") + "/application"
    if "workable.com" in host and not url.rstrip("/").endswith("/apply"):
        return url.rstrip("/") + "/apply/"
    return url


def candidate_jobs(settings, done):
    jobs = []
    for name in ("found_jobs.json", "pdf_jobs.json"):
        f = BOT / name
        if f.is_file():
            jobs += json.loads(f.read_text(encoding="utf-8"))
    skip = {c.lower() for c in settings.get("skip_companies", [])}
    jobs = [j for j in jobs if j["company"].lower() not in skip]
    inc = re.compile(settings["include_roles"], re.I)
    exc = re.compile(settings["exclude_roles"], re.I)
    seen, out = set(), []
    for j in jobs:
        if j["url"] in seen or j["url"] in done:
            continue
        seen.add(j["url"])
        if not SUPPORTED.search(j["url"]) or not inc.search(j["title"]) or exc.search(j["title"]):
            continue
        if "passed" in j.get("note", ""):
            continue
        out.append(j)
    # Companies whose sites block the bot (you submit those) go last; then newest and fresher-level first.
    slow = set(settings.get("you_submit_companies", []))
    entry = re.compile(r"intern|apprentice|graduate|new grad|fresher|junior|trainee|entry|associate|sde ?-?(1|i)", re.I)
    out.sort(key=lambda j: (j["company"] in slow, not (j.get("level") == "fresher" or entry.search(j["title"])),
                            not j.get("new")))
    return out


def dismiss_cookies(page):
    for name in (r"^\s*accept( all)?( cookies)?\s*$", r"^\s*allow all\s*$", r"^\s*got it\s*$"):
        btn = page.get_by_role("button", name=re.compile(name, re.I))
        try:
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=3000)
                return
        except Exception:
            pass


def open_form(page, url):
    page.goto(form_url(url), wait_until="load", timeout=60000)
    page.wait_for_timeout(4000)
    dismiss_cookies(page)
    if page.locator("input:visible, textarea:visible").count() < 3:
        for role in ("link", "button"):
            el = page.get_by_role(role, name=re.compile(r"^\s*apply", re.I))
            if el.count():
                el.first.click()
                page.wait_for_timeout(5000)
                dismiss_cookies(page)
                break


def captcha_challenge(page):
    sel = ("iframe[src*='bframe'], iframe[src*='hcaptcha'][src*='challenge'], iframe[title*='challenge' i], "
           "iframe[src*='challenges.cloudflare.com'], iframe[title*='cloudflare' i]")  # Cloudflare "Verify you are human"
    try:
        if any(f.is_visible() for f in page.locator(sel).all()):
            return True
    except Exception:
        pass
    # Cloudflare Turnstile sits in a closed part of the page the bot can't see into: go by its visible text
    text = page_text(page)
    return bool(re.search(r"verify you are human|verifying you are human", text, re.I)) \
        and not re.search(r"\bsuccess!?\s*$", text[-400:], re.I | re.M)


def show_window(page):
    """Move the bot's (possibly off-screen) Chrome window onto the screen and bring it to the front."""
    try:
        cdp = page.context.new_cdp_session(page)
        win = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"left": 60, "top": 40,
                                                                          "width": 1300, "height": 900}})
    except Exception:
        pass
    page.bring_to_front()


SCREENS = BOT / "screens"
CURRENT_JOB = {"name": "job"}


def snap(page, step):
    """Save a screenshot of the whole form for this step (screens/ folder), so you can see what happened."""
    try:
        SCREENS.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        page.screenshot(path=str(SCREENS / f"{stamp}_{CURRENT_JOB['name']}_{step}.png"), full_page=True)
    except Exception:
        pass


def page_text(page):
    """Text of the page and every embedded frame (some confirmations live inside an iframe)."""
    parts = []
    for frame in page.frames:
        try:
            parts.append(frame.inner_text("body"))
        except Exception:
            pass
    return "\n".join(parts)


def succeeded(page, start_url):
    return bool(SUCCESS.search(page_text(page))) or (
        page.url != start_url and bool(re.search(r"confirm|thank|success|submitted|applied", page.url, re.I)))


def moved_off_form(page, start_url):
    """After Submit the browser went to a different site (often the company's own thank-you page)."""
    a, b = urlparse(start_url).hostname or "", urlparse(page.url).hostname or ""
    return bool(b) and a != b


def wait_for_human_captcha(page, start_url, minutes, kind="captcha"):
    """Show the window, alert you, and wait for you. True if the application went through.

    kind "captcha": you solve it. Some sites (Lever) need Submit pressed again afterwards and may clear the
    resume meanwhile, so once the captcha is gone without a confirmation the bot re-fills and presses Submit.
    kind "code": the site emailed you a security code; you type it and press Submit, the bot only waits.
    """
    import threading
    import winsound
    show_window(page)
    snap(page, kind)
    if kind == "code":
        say = ("A job site emailed you a security code (to check you're human).\n"
               "Type it into the Chrome window that just opened, then click Submit application.")
        print(f"  EMAIL CODE: type the code from your email into the Chrome window and click Submit "
              f"(waiting up to {minutes} min)...")
    else:
        say = ("A job form needs you to solve a captcha.\nSolve it in the Chrome window that just opened;\n"
               "the bot finishes the application by itself.")
        print(f"  CAPTCHA: solve it in the Chrome window that just appeared (waiting up to {minutes} min)...")
    for _ in range(3):
        winsound.Beep(880, 250)
    threading.Thread(target=ctypes.windll.user32.MessageBoxW, daemon=True,
                     args=(0, say, "Job bot: your turn", 0x40 | 0x40000)).start()
    gone_for, resubmits = 0, 0
    for _ in range(minutes * 60):
        page.wait_for_timeout(1000)
        if succeeded(page, start_url):
            return True
        if re.search(r"missing entry for required field|form needs corrections", page_text(page), re.I) \
                and not captcha_challenge(page):
            print("  the form shows missing required fields, so waiting longer won't help")
            return False
        if kind != "captcha":
            continue
        gone_for = 0 if captcha_challenge(page) else gone_for + 1
        if gone_for == 4 and resubmits < 3:  # captcha solved 4 s ago but no confirmation yet
            resubmits += 1
            print("  captcha solved: re-checking the form and pressing Submit again")
            autofill.fill_page(page, quiet=True)  # re-attach the resume etc. if the site cleared them
            page.wait_for_timeout(3000)            # let uploads finish
            missing = autofill.required_missing(page)
            if missing:
                print("  still empty after the captcha:", "; ".join(missing[:4]))
            snap(page, f"after-captcha-{resubmits}")
            button = find_submit(page)
            if button:
                press(button)
            gone_for = -6  # give the site time to respond before trying again
    return False


def hand_over(page, minutes, needs=None):
    """The form is filled as far as the bot can; you finish it (needs = fields only you can answer) and click Submit.
    Used for sites that reject automated submits (Lever) and for forms with questions only you can answer."""
    import threading
    import winsound
    start_url = page.url
    snap(page, "ready-for-you")
    show_window(page)
    button = find_submit(page)
    if button:
        button.scroll_into_view_if_needed()
    if needs:
        listing = "\n".join(f"  - {n[:90]}" for n in needs[:6])
        say = f"This form needs you for:\n{listing}\n\nFill those in the Chrome window, then click Submit."
        print(f"  YOUR TURN: this form needs you for: {'; '.join(n[:60] for n in needs[:6])}")
    else:
        say = ("A job form is filled in and waiting for you.\nCheck it in the Chrome window, then click Submit\n"
               "(and solve the captcha if one appears).")
        print(f"  READY: check the form in the Chrome window and click Submit yourself")
    print(f"  (waiting up to {minutes} min)")
    for _ in range(2):
        winsound.Beep(660, 300)
    threading.Thread(target=ctypes.windll.user32.MessageBoxW, daemon=True,
                     args=(0, say, "Job bot: your turn", 0x40 | 0x40000)).start()
    for _ in range(minutes * 60):
        page.wait_for_timeout(1000)
        if succeeded(page, start_url):
            return "submitted", "application submitted (you clicked Submit)"
    return after_wait(page, "form was filled and waiting, but Submit wasn't clicked")


def press(button):
    """Click Submit; if a cookie banner or overlay covers it, click it directly."""
    try:
        button.click(timeout=8000)
    except Exception:
        try:
            button.click(force=True, timeout=5000)
        except Exception:
            button.evaluate("e => e.click()")


def find_submit(page):
    for frame in page.frames:
        cands = frame.locator("button, input[type=submit]").filter(
            has_text=re.compile(r"submit|send application", re.I))
        if not cands.count():
            cands = frame.locator("input[type=submit][value*='ubmit' i]")
        for i in range(cands.count()):
            if cands.nth(i).is_visible():
                return cands.nth(i)
    return None


def blocked_reason(body):
    m = BLOCKED.search(body)
    return f"site's bot check blocked it: \"{body[max(0, m.start() - 40): m.end() + 40].strip()}\"".replace("\n", " ")


def after_wait(page, what):
    """After waiting for you without a confirmation: was it really a block (spam / verification error)?"""
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    if BLOCKED.search(body):
        return "blocked", blocked_reason(body)
    return "needs_you", f"{what}: open the link and apply yourself"


def submit(page, captcha_minutes=3):
    """Click the form's submit button. Returns (status, reason)."""
    button = find_submit(page)
    if button is None:
        return "needs_you", "couldn't find the Submit button"
    start_url = page.url
    button.scroll_into_view_if_needed()
    page.wait_for_timeout(2000)  # let file uploads (resume) finish before submitting
    missing = autofill.required_missing(page)
    if missing:  # e.g. an upload the site dropped: fill again once, then re-check
        autofill.fill_page(page, quiet=True)
        page.wait_for_timeout(2000)
        missing = autofill.required_missing(page)
        if missing:
            snap(page, "missing")
            return "needs_you", "required fields empty right before Submit: " + "; ".join(missing[:5])
    snap(page, "before-submit")
    press(button)
    for _ in range(25):
        page.wait_for_timeout(1000)
        if succeeded(page, start_url):
            return "submitted", "application submitted"
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        if BLOCKED.search(body):
            return "blocked", blocked_reason(body)
        if VERIFY_CODE.search(body):
            if captcha_minutes and wait_for_human_captcha(page, start_url, max(captcha_minutes, 5), kind="code"):
                return "submitted", "application submitted (you entered the email code)"
            return after_wait(page, "the site emailed a security code that wasn't entered in time")
        if captcha_challenge(page):
            if captcha_minutes and wait_for_human_captcha(page, start_url, captcha_minutes):
                return "submitted", "application submitted (you solved the captcha)"
            return after_wait(page, "captcha appeared after Submit and wasn't solved")
    body = page.inner_text("body") if page.locator("body").count() else ""
    if BLOCKED.search(body):
        return "blocked", blocked_reason(body)
    m = FORM_ERROR.search(body)
    if m:
        snippet = body[max(0, m.start() - 60): m.end() + 30].replace("\n", " ")
        return "needs_you", f"form showed an error: ...{snippet}..."
    if moved_off_form(page, start_url):
        return "submitted", (f"probably submitted: after Submit the site moved to {page.url[:120]} "
                             f"(check your email for a confirmation)")
    return "needs_you", (f"no confirmation seen after Submit (page: {page.url[:100]}): check your email; "
                         f"if nothing came, apply by hand")


def preflight(settings):
    problems = []
    for k in REQUIRED_PROFILE:
        if not str(autofill.PROFILE.get(k, "")).strip():
            problems.append(f"profile.json: '{k}' is empty")
    resume = autofill.DOCS / autofill.PROFILE.get("docs", {}).get("resume", "resume.pdf")
    if not resume.is_file():
        problems.append(f"missing resume file: {resume}")
    if not ai.api_key():
        problems.append("no Gemini key: put it in gemini_key.txt in this folder")
    missing_nice = [k for k in NICE_TO_HAVE if not str(autofill.PROFILE.get(k, "")).strip()]
    return problems, missing_nice


def workday_description(url):
    """The job description from Workday's public job feed (the careers pages load it the same way)."""
    import html
    import urllib.request
    p = urlparse(url)
    tenant = p.hostname.split(".")[0]
    site, _, rest = p.path.lstrip("/").partition("/")
    api = f"https://{p.hostname}/wday/cxs/{tenant}/{site}/{rest}"
    req = urllib.request.Request(api, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        info = json.loads(r.read().decode("utf-8")).get("jobPostingInfo", {})
    text = re.sub(r"<[^>]+>", " ", html.unescape(info.get("jobDescription", "")))
    return f"{info.get('title', '')}\n{info.get('location', '')}\n{re.sub(r'\s+', ' ', text)}"[:8000]


def remember_blocking_company(settings, company):
    s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.is_file() else dict(settings)
    companies = s.setdefault("you_submit_companies", [])
    if company not in companies:
        companies.append(company)
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
        settings["you_submit_companies"] = companies
        print(f"  (remembered: {company}'s site blocks automated submits, so you'll click Submit for its jobs)")


def arg_value(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv[:-1] else None


def single_instance():
    """Hold a lock for the whole run so two auto-apply runs never overlap (Windows frees it on exit)."""
    import msvcrt
    fh = open(BOT / ".apply_auto.lock", "a+")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return None
    return fh


def main():
    lock = single_instance()
    if lock is None:
        print("Another auto-apply run is still going. Wait for it to finish (or close it) and try again.")
        return
    settings = load_settings()
    # --real submits even when settings.json still says test_mode (for one deliberate run).
    test = ("--test" in sys.argv or settings["test_mode"]) and "--real" not in sys.argv
    problems, missing_nice = preflight(settings)
    if problems:
        print("Can't start yet:")
        for p in problems:
            print("  -", p)
        return
    if missing_nice:
        print("Tip: more forms can be completed if you fill these in profile.json:", ", ".join(missing_nice))

    log = read_log()
    # "needs you" jobs are tried again once you change profile.json or answers.json after that attempt.
    changed = max((f.stat().st_mtime for f in (BOT / "profile.json", autofill.ANSWERS_FILE) if f.is_file()),
                  default=0)
    changed_at = datetime.fromtimestamp(changed).strftime("%Y-%m-%d %H:%M")
    # Never retry a job where Submit was already clicked without a confirmation: it may have gone through,
    # and a second application would be a duplicate. Those stay on do_by_hand.csv for you to check.
    clicked = re.compile(r"no confirmation seen after submit|probably submitted|wasn't solved|wasn't entered|"
                         r"submit wasn't clicked", re.I)
    done = {r["url"] for r in log if r["status"] in ("submitted", "skipped", "blocked", "manual")
            or (r["status"] == "needs_you" and (clicked.search(r["reason"])
                                                 or f"{r['date']} {r['time']}" >= changed_at))}
    if (BOT / "applied.csv").is_file():
        with open(BOT / "applied.csv", encoding="utf-8") as fh:
            done |= {r["url"] for r in csv.DictReader(fh)}
    today = date.today().isoformat()
    # The bot's "day" starts at day_starts_at (default 8 AM): a late-night session counts as the day before.
    start_hour = int(settings.get("day_starts_at", 8))
    work_day = lambda d, t: (datetime.fromisoformat(f"{d} {t or '00:00'}") - timedelta(hours=start_hour)).date()
    this_day = work_day(today, datetime.now().strftime("%H:%M"))
    submitted_today = sum(1 for r in log if r["status"] == "submitted" and work_day(r["date"], r["time"]) == this_day)
    budget = settings["max_per_day"] - submitted_today
    if arg_value("--limit"):
        budget = min(budget, int(arg_value("--limit")))
    if arg_value("--only"):  # an explicit retry of one job: only skip it if it was already sent
        done = {r["url"] for r in log if r["status"] == "submitted"}
    jobs = candidate_jobs(settings, done)
    if arg_value("--only"):
        jobs = [j for j in jobs if arg_value("--only") in j["url"]]
    print(f"{'TEST RUN (nothing is submitted). ' if test else ''}"
          f"{len(jobs)} matching jobs not tried yet; up to {max(budget, 0)} applications left today.")
    if budget <= 0 or not jobs:
        return

    models = settings.get("gemini_models") or [settings["gemini_model"]]
    gemini = ai.Gemini(ai.api_keys(), models)
    facts = ai.candidate_facts(autofill.PROFILE, autofill.DOCS)
    counts = {"submitted": 0, "needs_you": 0, "blocked": 0, "manual": 0, "skipped": 0, "error": 0, "test": 0}
    args = [] if settings["show_browser"] else ["--window-position=-32000,-32000"]
    with sync_playwright() as p:
        def launch():
            return p.chromium.launch_persistent_context(str(BOT / ".chrome-auto"), channel="chrome",
                                                        headless=False, args=args,
                                                        viewport={"width": 1300, "height": 900})
        ctx = launch()
        retried = set()
        for job in jobs:
            if counts["submitted"] + counts["test"] >= budget:
                break
            print(f"\n[{job['company']}] {job['title']}")
            CURRENT_JOB["name"] = re.sub(r"[^A-Za-z0-9]+", "-", f"{job['company']}-{job['title']}")[:60]
            if any(s in job["url"] for s in settings.get("manual_sites", [])) \
                    or job["company"] in settings.get("manual_companies", []):
                # these sites reject the bot's browser even when you click Submit: apply in your normal Chrome
                if "myworkdayjobs.com" in job["url"]:
                    # check the fit first (Workday's public feed gives the job description), so the list only
                    # gets roles a fresher can really apply to
                    try:
                        jd = workday_description(job["url"])
                        ok, reason = ai.AutoAnswerer(gemini, facts, job, jd, []).resolve()
                    except ai.QuotaExhausted as e:
                        print(f"\nStopping: {e}.")
                        break
                    except Exception:
                        ok, reason = True, ""
                    if not ok:
                        write_log(job, "skipped", f"not a fit: {reason}")
                        counts["skipped"] += 1
                        continue
                    why = ("Workday: sign in or create an account in your normal Chrome, then click the Job Bot "
                           "(J) icon on each page of the form")
                else:
                    why = "this site blocks the bot's browser: apply in your normal Chrome"
                write_log(job, "manual", why)
                counts["manual"] += 1
                continue
            try:
                page = ctx.new_page()
            except Exception:  # Chrome was closed (by you or a crash): open it again and carry on
                print("  (the bot's Chrome was closed: reopening it)")
                ctx = launch()
                page = ctx.new_page()
            page.set_default_timeout(10000)  # boxes that vanish on a form rebuild: give up fast, next pass finds the new one
            answers = []
            stop = False
            try:
                open_form(page, job["url"])
                if not any(f.locator("input[type=file]").count() for f in page.frames):
                    # every real application form has a resume upload; this is the company's own careers
                    # page wrapping the job (e.g. Squarepoint), so you apply there
                    write_log(job, "manual", f"the company's own careers site shows this job ({page.url[:80]}): "
                                             f"open the link and apply there")
                    counts["manual"] += 1
                    continue
                jd = page.inner_text("body")[:8000]
                helper = ai.AutoAnswerer(gemini, facts, job, jd, answers)
                autofill.AUTO = helper
                autofill.fill_page(page, quiet=True)  # pass 1: profile answers in, Gemini questions collected
                ok, reason = helper.resolve()          # one Gemini call: fit check + all collected questions
                if not ok:
                    write_log(job, "skipped", f"not a fit: {reason}")
                    counts["skipped"] += 1
                    continue
                autofill.fill_page(page, quiet=True)   # pass 2: Gemini's answers in
                page.wait_for_timeout(1500)
                if helper.pending:                     # questions that appeared after earlier answers
                    helper.resolve()
                    autofill.fill_page(page, quiet=True)
                    page.wait_for_timeout(1500)
                missing = autofill.required_missing(page)
                if missing and test:
                    write_log(job, "needs_you", "required fields left: " + "; ".join(missing[:6]))
                    counts["needs_you"] += 1
                    continue
                if missing:  # ask you for just those fields, then you submit
                    status, reason = hand_over(page, settings.get("hand_over_minutes", 5), needs=missing)
                    if status != "submitted":
                        reason = f"{reason}. It needed you for: " + "; ".join(missing[:6])
                    snap(page, f"final-{status}")
                    write_log(job, status, reason)
                    counts[status] += 1
                    if status == "submitted":
                        remove_from_manual_list(job["url"])
                    continue
                if test:
                    write_log(job, "test", "form complete; would submit now (test run)")
                    counts["test"] += 1
                    continue
                you_submit = settings.get("you_submit_sites", ["lever.co"])
                if any(s in (urlparse(page.url).hostname or "") for s in you_submit) \
                        or job["company"] in settings.get("you_submit_companies", []):
                    status, reason = hand_over(page, settings.get("hand_over_minutes", 5))
                else:
                    status, reason = submit(page, settings.get("captcha_wait_minutes", 3))
                snap(page, f"final-{status}")
                write_log(job, status, reason)
                counts[status] += 1
                if status == "submitted":
                    remove_from_manual_list(job["url"])
                if status == "blocked":  # from now on: the bot fills this company's forms, you click Submit
                    remember_blocking_company(settings, job["company"])
            except ai.QuotaExhausted as e:
                print(f"\nStopping: {e}.")
                stop = True
            except RuntimeError as e:
                if re.search(r"Gemini error (400|401|403)", str(e)):
                    print(f"\nStopping: Gemini rejected the key ({str(e)[:160]}). Check gemini_key.txt.")
                    stop = True
                else:
                    write_log(job, "error", f"{str(e).splitlines()[0][:150]}")
                    counts["error"] += 1
            except Exception as e:
                if "has been closed" in str(e) and job["url"] not in retried:
                    # Chrome closed mid-job: reopen it and try this job again at the end of the list
                    print("  (the bot's Chrome was closed mid-job: reopening it; this job will be retried)")
                    retried.add(job["url"])
                    jobs.append(job)
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    ctx = launch()
                else:
                    write_log(job, "error", f"{e.__class__.__name__}: {str(e).splitlines()[0][:150]}")
                    counts["error"] += 1
            finally:
                autofill.AUTO = None
                if answers:
                    with open(ANSWER_LOG, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"date": today, "company": job["company"], "title": job["title"],
                                             "url": job["url"], "answers": answers}, ensure_ascii=False) + "\n")
                try:
                    page.close()
                except Exception:
                    pass
            if stop:
                break
        try:
            ctx.close()
        except Exception:
            pass
    print(f"\nGemini calls used this run: {gemini.calls}")

    summary = (f"Submitted {counts['submitted']}, test-filled {counts['test']}, needs you {counts['needs_you']}, "
               f"blocked by the site {counts['blocked']}, skipped (not a fit) {counts['skipped']}, "
               f"errors {counts['error']}. Jobs to finish yourself: do_by_hand.csv")
    print("\n" + summary + "\nDetails: auto_log.csv   Gemini's answers: ai_answers.jsonl")
    if "--notify" in sys.argv and any(counts.values()):
        ctypes.windll.user32.MessageBoxW(0, summary + "\n\nSee auto_log.csv in JobApplyBot.", "Job bot", 0x40)


if __name__ == "__main__":
    main()
