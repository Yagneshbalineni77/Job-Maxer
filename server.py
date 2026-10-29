"""
Job Bot helper for the Chrome extension.

Run it (start.bat > option 9), leave the window open, and use the "Fill with Job Bot"
button in your normal Chrome. The extension sends the form's fields here; this works out
the answers (profile.json, answers.json, Gemini) and sends back what to type, tick and
attach. Nothing is ever submitted: you check the form and click Submit yourself.

Only listens on your own laptop (127.0.0.1) and only answers requests carrying the private
key written into the extension (extension/token.txt).
"""
import base64
import csv
import json
import mimetypes
import re
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import ai
import autofill as af

sys.stdout.reconfigure(errors="replace")

BOT = Path(__file__).resolve().parent
PORT = 8765
TOKEN = (BOT / "extension" / "token.txt").read_text(encoding="utf-8").strip()
CURRENT_ROLE = re.compile(r"current (role|job|position)|currently work here", re.I)


def doc_for(f):
    """Which document a file box wants: its own name first, then the text around it."""
    for text in (f["primary"], f["attrs"], f.get("question", ""), f["label"]):
        low = (text or "").lower()
        key = next((k for k, p in af.FILE_RULES if low and re.search(p, low)), None)
        if key:
            return key
    return None


def already_filled(f):
    if f["tag"] == "select":
        return bool(f["options"]) and f["selectedText"] != f["options"][0]
    return bool(f["value"] or f["current"]) and not re.match(r"select", f["current"] or "", re.I)


def ai_on():
    return af.AUTO is not None


def decide(fields, groups, gov):
    """The same decisions the bot makes when it fills a form itself, returned as a list of actions."""
    actions, needs = [], []
    radios, checkgroups = {}, {}
    for f in fields:
        t = f["type"]
        if t == "radio":
            radios.setdefault(f["name"] or f["idx"], []).append(f)
            continue
        if t == "checkbox":
            if f["name"] and sum(1 for g in fields if g["type"] == "checkbox" and g["name"] == f["name"]) > 1:
                checkgroups.setdefault(f["name"], []).append(f)
                continue
            text = f["own"] or f["primary"] or f["label"][:200]
            if f["checked"]:
                continue
            if CURRENT_ROLE.search(text) and str(af.PROFILE.get("employment_current", "")).lower().startswith("y"):
                actions.append({"idx": f["idx"], "kind": "check", "why": "current role"})
            elif af.CONFIRM.search(text) and ai_on() and af.AUTO.consent(text):
                actions.append({"idx": f["idx"], "kind": "check", "why": "standard consent"})
            elif f["required"]:
                needs.append(text[:90])
            continue
        if t == "file":
            if af.file_attached(f):
                continue
            key = doc_for(f)
            path = af.PROFILE.get("docs", {}).get(key or "", "")
            if key in af.SENSITIVE and not gov:
                path = ""
            if key in ("photo", "signature", "thumb") and not gov:
                path = ""  # company forms: no photos (crop pop-ups); attach it yourself if they insist
            if path and (af.DOCS / path).is_file():
                actions.append({"idx": f["idx"], "kind": "file", "key": key, "name": Path(path).name})
            elif f["required"]:
                needs.append(f"upload: {af.field_question(f)[:70]}")
            continue

        key = af.classify(f)
        question = af.field_question(f)
        if key == "HARD":
            if f["required"]:
                needs.append(question[:90])
            continue
        if already_filled(f):
            continue
        options = [o for o in (f["options"] or []) if o and not re.match(r"(--)?\s*(select|choose|please)", o, re.I)]
        val = af.profile_value(key, f, gov)
        if not val and key not in af.SENSITIVE and key != "CONFIRM":
            val = af.remembered(af.signature(f))
        if not val and key == "CONFIRM" and ai_on() and af.AUTO.consent(question) and options:
            i = af.yes_index(options)
            val = options[i] if i is not None else ""
        if not val and key not in ("CONFIRM",) and key not in af.SENSITIVE and ai_on():
            kind = "choice" if options else ("long" if f["tag"] == "textarea" else "text")
            val = af.ask_ai(question, kind, options or None) or ""
        if not val:
            if f["required"]:
                needs.append(question[:90])
            continue
        values = af.alternatives(key, val) if key else [val]
        if f["tag"] == "select":
            i = next((i for v in values if (i := af.pick_option(f["options"], v)) is not None), None)
            if i is None and ai_on() and options:
                pick = af.ask_ai(f"{question} (candidate's answer: {val})", "choice", options)
                i = af.pick_option(f["options"], pick) if pick else None
            if i is None:
                if f["required"]:
                    needs.append(question[:90])
                continue
            actions.append({"idx": f["idx"], "kind": "select", "index": i})
        elif f["combo"] or f["type"] == "listbox":
            actions.append({"idx": f["idx"], "kind": "combo", "values": values})
        elif f.get("datepicker"):
            actions.append({"idx": f["idx"], "kind": "date", "value": val})
        else:
            actions.append({"idx": f["idx"], "kind": "fill", "value": val})

    for group in radios.values():
        question = af.group_question(group)
        if af.HARD_SKIP.search(question[:120]) or any(r["checked"] for r in group):
            continue
        confirm = bool(af.CONFIRM.search(question[:160]))
        options = [r["own"] for r in group]
        key = None if confirm else af.rule_key(question)
        val = af.profile_value(key, group[0], gov)
        if not val and not confirm:
            val = af.remembered(af.signature(group[0], question))
        if not val and ai_on():
            if confirm:
                yi = af.yes_index(options) if af.AUTO.consent(question) else None
                val = options[yi] if yi is not None else ""
            elif key not in af.SENSITIVE:
                val = af.ask_ai(question, "choice", options) or ""
        i = af.pick_option(options, val) if val else None
        if i is None:
            if any(r["required"] for r in group):
                needs.append(question[:90])
            continue
        actions.append({"idx": group[i]["idx"], "kind": "check", "why": question[:60]})

    for group in checkgroups.values():
        question = af.group_question(group)
        if any(g["checked"] for g in group) or af.SKIP.search(question[:120]):
            continue
        options = [g["own"] for g in group]
        val = af.remembered(af.signature(group[0], question)) or (af.ask_ai(question, "choice", options) or "")
        i = af.pick_option(options, val) if val else None
        if i is not None:
            actions.append({"idx": group[i]["idx"], "kind": "check", "why": question[:60]})
        elif any(g["required"] for g in group):
            needs.append(question[:90])

    for grp in groups:
        question, options = grp["question"], grp["options"]
        if grp["pressed"] >= 0 or not question or af.HARD_SKIP.search(question[:120]):
            continue
        confirm = bool(af.CONFIRM.search(question[:160]))
        key = None if confirm else af.rule_key(question)
        val = af.profile_value(key, {"type": "buttons"}, gov)
        if not val and not confirm:
            val = af.remembered(af.norm(question)[:160])
        if not val and ai_on():
            if confirm:
                yi = af.yes_index(options) if af.AUTO.consent(question) else None
                val = options[yi] if yi is not None else ""
            elif key not in af.SENSITIVE:
                val = af.ask_ai(question, "choice", options) or ""
        i = af.pick_option(options, val) if val else None
        if i is None:
            if grp["required"]:
                needs.append(question[:90])
            continue
        actions.append({"sel": f"[data-afb='{grp['g']}-{i}']", "kind": "click", "why": question[:60]})
    return actions, needs


def plan(payload):
    host = urlparse(payload.get("url", "")).hostname or ""
    gov = bool(af.GOV_SITES.search(host))
    fields, groups = payload.get("fields", []), payload.get("buttons", [])
    helper, log = None, []
    keys = ai.api_keys()
    if keys and payload.get("useAI", True):
        settings_file = BOT / "settings.json"
        settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.is_file() else {}
        helper = ai.AutoAnswerer(ai.Gemini(keys, settings.get("gemini_models") or ["gemini-3.5-flash"]),
                                 ai.candidate_facts(af.PROFILE, af.DOCS),
                                 {"company": host, "title": payload.get("title", "")[:120]},
                                 payload.get("text", "")[:6000], log)
        helper.fit_result = (True, "you chose this job")
    note = ""
    af.AUTO = helper
    try:
        actions, needs = decide(fields, groups, gov)  # pass 1 also collects questions for Gemini
        if helper and helper.pending:
            helper.resolve()                           # one Gemini request for all of them
            actions, needs = decide(fields, groups, gov)
    except ai.QuotaExhausted:
        af.AUTO = None
        actions, needs = decide(fields, groups, gov)
        note = "Gemini is out of today's free quota: filled from your profile and saved answers only."
    except Exception as e:
        af.AUTO = None
        actions, needs = decide(fields, groups, gov)
        note = f"Gemini had a problem ({str(e)[:80]}): filled from your profile only."
    finally:
        af.AUTO = None
    gemini = [{"q": e["question"][:120], "a": e["answer"][:300]} for e in log if e["kind"] in ("text", "long", "choice")]
    print(f"[{date.today()}] {host}: {len(actions)} to fill, {len(needs)} for you, {len(gemini)} Gemini answers")
    return {"actions": actions, "needs": needs, "gemini": gemini, "note": note}


def file_response(key, host):
    gov = bool(af.GOV_SITES.search(host or ""))
    if key in af.SENSITIVE and not gov:
        return None
    path = af.PROFILE.get("docs", {}).get(key or "", "")
    full = af.DOCS / path if path else None
    if not full or not full.is_file():
        return None
    return {"name": full.name, "type": mimetypes.guess_type(full.name)[0] or "application/octet-stream",
            "b64": base64.b64encode(full.read_bytes()).decode("ascii")}


def record_applied(payload):
    f = BOT / "applied.csv"
    new = not f.is_file()
    with open(f, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "company", "title", "url", "page"])
        if new:
            w.writeheader()
        w.writerow({"date": date.today().isoformat(), "company": urlparse(payload.get("url", "")).hostname or "",
                    "title": payload.get("title", "")[:150], "url": payload.get("url", ""), "page": payload.get("url", "")})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.headers.get("X-JobBot-Token") != TOKEN:
            return self.reply(403, {"error": "wrong key"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/ping":
                return self.reply(200, {"ok": True})
            if self.path == "/plan":
                return self.reply(200, plan(payload))
            if self.path == "/file":
                data = file_response(payload.get("key"), payload.get("host"))
                return self.reply(200 if data else 404, data or {"error": "no such document"})
            if self.path == "/applied":
                record_applied(payload)
                return self.reply(200, {"ok": True})
            return self.reply(404, {"error": "unknown"})
        except Exception as e:
            print("  error:", e)
            return self.reply(500, {"error": str(e)[:200]})


if __name__ == "__main__":
    print(f"Job Bot helper is running. Keep this window open while you use the Chrome extension.")
    print(f"(listening on 127.0.0.1:{PORT}; close the window to stop it)\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
