"""
Gemini helper for auto mode: checks whether a job fits, and answers questions
that the profile and saved answers don't cover.

The key is read from the GEMINI_API_KEY environment variable or from
gemini_key.txt in this folder. Never paste it into chat or any other file.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

BOT = Path(__file__).resolve().parent
KEY_FILE = BOT / "gemini_key.txt"
NOT_FACTS = {"aadhaar", "pan", "voter_id", "passport_no", "driving_licence", "docs", "_note"}


def api_keys():
    """All keys: GEMINI_API_KEY, then each non-empty line of gemini_key.txt (one key per line)."""
    keys = [os.environ.get("GEMINI_API_KEY", "").strip()]
    if KEY_FILE.is_file():
        keys += [ln.strip() for ln in KEY_FILE.read_text(encoding="utf-8-sig").splitlines()]
    return list(dict.fromkeys(k for k in keys if k and not k.startswith("#")))


def api_key():
    keys = api_keys()
    return keys[0] if keys else ""


def resume_text(path):
    try:
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)[:6000]
    except Exception:
        return ""


def candidate_facts(profile, docs):
    facts = {k: v for k, v in profile.items() if k not in NOT_FACTS and v not in ("", None, "DD/MM/YYYY")}
    pitch = docs / "pitch.txt"
    return (f"PROFILE (JSON):\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"RESUME:\n{resume_text(docs / profile.get('docs', {}).get('resume', 'resume.pdf'))}\n\n"
            f"ABOUT ME:\n{pitch.read_text(encoding='utf-8') if pitch.is_file() else ''}")


class QuotaExhausted(RuntimeError):
    """Every model in the list has used up its daily free allowance."""


class Gemini:
    """Calls Gemini. When a model runs out of daily quota it moves to the next model, and when every model
    on a key is used up it moves to the next key (keys from different Google Cloud projects have separate quotas)."""

    def __init__(self, keys, models):
        keys = [keys] if isinstance(keys, str) else list(keys)
        models = [models] if isinstance(models, str) else list(models)
        self.slots = [(k, m) for k in keys for m in models]
        self.calls = 0

    @property
    def model(self):
        return self.slots[0][1] if self.slots else ""

    def ask_json(self, prompt):
        while self.slots:
            key, model = self.slots[0]
            try:
                return self._ask(key, model, prompt)
            except QuotaExhausted:
                self.slots.pop(0)
                nxt = self.slots[0] if self.slots else None
                if nxt:
                    which = "next model" if nxt[0] == key else "next key"
                    print(f"  ({model} is out of today's free quota on this key; switching to the {which})")
        raise QuotaExhausted("every Gemini key and model is out of today's free quota; the rest waits for tomorrow")

    def _ask(self, key, model, prompt):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
        }).encode("utf-8")
        for attempt in range(4):
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json", "x-goog-api-key": key})
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=150) as r:
                    data = json.loads(r.read().decode("utf-8"))
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                if e.code == 429 and "PerDay" in detail:
                    raise QuotaExhausted(model)
                if e.code == 404:
                    raise QuotaExhausted(model)  # model retired for this key: treat like used up
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(20 * (attempt + 1))  # per-minute limit or a busy server: wait and retry
                    continue
                raise RuntimeError(f"Gemini error {e.code}: {detail[:300]}")
            except (KeyError, IndexError, json.JSONDecodeError, TimeoutError):
                if attempt < 3:
                    continue
                raise RuntimeError("Gemini returned no readable answer")


BATCH_PROMPT = """You help one candidate apply for a job. Do two things.

1. {fit_task}

2. Answer each numbered application question below in the candidate's own first-person voice.
Rules for answers:
- Use only facts given below. Never invent experience, skills, dates, numbers, certifications or offers.
- If a question asks for a fact the facts don't contain (a date, a yes/no about something not stated,
  a referral name, a test score), the answer is exactly "UNKNOWN".
- Motivation or opinion questions ("why us", "what does X mean to you"): a sincere, specific answer that links
  the candidate's real experience to this job. Plain English, no cliches, no exaggeration. 80 to 140 words.
- "text" questions: a short phrase or one sentence.
- "choice" questions: exactly one of the listed options, copied character for character.
  Prefer a "prefer not to say" style option for demographic questions (race, ethnicity, veteran, disability).

{facts}

JOB: {title} at {company}
DESCRIPTION:
{jd}

QUESTIONS:
{questions}

Reply as JSON: {{"apply": true or false, "reason": "one short sentence", "answers": {{"1": "...", "2": "..."}}}}"""

def _settings():
    try:
        return json.loads((BOT / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# The kind of job and the location are set in settings.json ("fit_roles", "fit_avoid", "fit_location").
FIT_ROLES = _settings().get("fit_roles") or \
    "software engineering / SDE / backend / full-stack, or ML / AI / data science / GenAI / LLM / agents"
FIT_AVOID = _settings().get("fit_avoid") or \
    "hardware, VLSI, embedded, firmware, sales, support, consulting or operations"
FIT_LOCATION = _settings().get("fit_location") or "India"

FIT_TASK = f"""Decide if the candidate should apply. Apply only if ALL are true:
- The role is {FIT_ROLES}.
  NOT {FIT_AVOID}.
- It is entry level: an internship, new-grad / fresher / graduate / trainee role, or it requires NO prior
  full-time experience (internships and projects are enough), including ranges that start at 0 (e.g. "0-2 years").
  A role asking for "1+ years" of full-time experience is NOT entry level. If the posting states no experience requirement
  at all and the title isn't senior / lead / staff, count it as entry level. Skip roles that require 2 or more
  years minimum, or clearly expect an experienced professional.
- It is located in {FIT_LOCATION} or is remote and open to people in {FIT_LOCATION}.
- The candidate meets the hard requirements (degree, graduation year, availability) based on the facts below.
  Skip roles that only accept graduates from earlier years than the candidate's graduation year."""


# Confirmations auto mode may answer Yes to, and topics that are never "standard" even if phrased as a confirmation.
STANDARD_CONSENT = re.compile(r"accura|true|correct|complete|privacy|personal (data|information)|data protection|"
                              r"process(ing)? (of )?my|consent|gdpr|terms|policy|notice|stor(e|ing|age) my|retain|"
                              r"contact me|communications?|read and understood", re.I)
NOT_STANDARD = re.compile(r"availab|start|join|relocat|proficien|fluent|years|degree|graduat|full[- ]time|"
                          r"visa|background|drug|criminal|reference|salary|commit|notice period|on-?site|office|"
                          r"travel|shift|bond|age\b|citizen|authori[sz]ed|"
                          # signed statements that you personally completed the form are yours to make
                          r"undersigned|personally completed|signature|signed", re.I)


class AutoAnswerer:
    """Plugged into autofill.AUTO for one job at a time.

    Uses one Gemini call per job: a first fill pass only collects the questions (answer() returns None and
    queues them), then resolve() sends the fit check and every queued question together. The next fill pass
    reads the answers from the cache. Questions that only appear after earlier answers get one more call.
    """

    def __init__(self, gemini, facts, job, jd, log):
        self.gemini, self.facts, self.job, self.jd, self.log = gemini, facts, job, jd[:6000], log
        self.cache, self.pending = {}, []
        self.fit_result = None

    def consent(self, question):
        ok = bool(STANDARD_CONSENT.search(question)) and not NOT_STANDARD.search(question)
        if not any(e["question"] == question[:300] for e in self.log if e["kind"] == "consent"):
            self.log.append({"kind": "consent", "question": question[:300], "answer": "Yes" if ok else "(left)"})
        return ok

    def answer(self, question, kind, options):
        k = (question, kind, tuple(options or ()))
        if k in self.cache:
            return self.cache[k] or None
        if k not in self.pending:
            self.pending.append(k)
        return None

    def resolve(self):
        """Ask Gemini about the fit (first time only) and every pending question. Returns (apply, reason)."""
        if not self.pending and self.fit_result is not None:
            return self.fit_result
        lines = []
        for i, (q, kind, opts) in enumerate(self.pending, 1):
            lines.append(f"{i}. [{kind}] {q}")
            if opts:
                lines += [f"     - {o}" for o in opts]
        fit_task = FIT_TASK if self.fit_result is None else \
            'Fit was already decided: just return "apply": true and "reason": "".'
        prompt = BATCH_PROMPT.format(fit_task=fit_task, facts=self.facts, title=self.job["title"],
                                     company=self.job["company"], jd=self.jd,
                                     questions="\n".join(lines) or "(none)")
        r = self.gemini.ask_json(prompt)
        if self.fit_result is None:
            self.fit_result = (bool(r.get("apply")), str(r.get("reason", ""))[:200])
        answers = r.get("answers") or {}
        for i, k in enumerate(self.pending, 1):
            q, kind, opts = k
            ans = str(answers.get(str(i), "")).strip()
            if ans.upper() == "UNKNOWN" or (opts and ans not in opts):
                ans = ""
            self.cache[k] = ans
            self.log.append({"kind": kind, "question": q[:300], "answer": ans or "(unknown: left empty)"})
        self.pending = []
        return self.fit_result
