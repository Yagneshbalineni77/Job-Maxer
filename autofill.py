"""
Job form auto-filler (government portals and company job sites).

Opens a Chrome window. You log in / register / open the application form
yourself, then press Enter in this terminal and the bot fills every field it
recognises from profile.json, answers it has learned (answers.json), and
attaches your documents from docs/.

It NEVER clicks Submit / Pay / Next, never touches OTP or captcha fields, and
never answers "I confirm / I agree / declaration" questions. You review and
submit yourself. Aadhaar / PAN / voter ID / passport numbers and scans are only
ever entered on government sites (see GOV_SITES).

Field colours after a fill:
  green  = filled from your profile      blue = filled from a remembered answer
  yellow = not recognised: fill it yourself, then press l so the bot learns it

Usage:
  python autofill.py            -> menu of jobs
  python autofill.py <url>      -> open any URL
"""
import csv
import difflib
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(errors="replace")  # job sites put symbols like ✱ in labels

BOT = Path(__file__).resolve().parent
PROFILE = json.loads((BOT / "profile.json").read_text(encoding="utf-8"))
DOCS = BOT / "docs"
ANSWERS_FILE = BOT / "answers.json"
APPLIED_FILE = BOT / "applied.csv"
ANSWERS = json.loads(ANSWERS_FILE.read_text(encoding="utf-8")) if ANSWERS_FILE.is_file() else {}

GREEN, BLUE, YELLOW = "#2e7d32", "#1565c0", "#f9a825"

# Identity numbers and ID scans are only filled on these sites.
SENSITIVE = {"aadhaar", "pan", "voter_id", "passport_no", "driving_licence",
             "aadhaar_card", "pan_card", "voter_id_card", "passport_scan"}
GOV_SITES = re.compile(r"(\.gov\.in|\.nic\.in|\.ac\.in|digialm\.com|ibps\.in|bel-india\.in|cdot\.in|"
                       r"cdac\.in|cris\.org\.in|ecil\.co\.in|ongcindia\.com|powergrid\.in|ntpc\.co\.in|"
                       r"aai\.aero|iocl\.com|hindustanpetroleum\.com|nabard\.org|rbi\.org\.in|"
                       r"sebi\.gov\.in|isro\.gov\.in|drdo\.gov\.in|barc\.gov\.in|rac\.gov\.in)$", re.I)

# Fields we must never touch. Checked against the field's own label only.
HARD_SKIP = re.compile(r"captcha|otp|one time|security code|verification code|password|"
                       r"enter the code|code shown|"
                       # signing something (an NDA, an e-signature) is always yours to do
                       r"non-?disclosure|\bnda\b|indicate your agreement|e-?signature|digital signature|"
                       r"sign (here|below)|by typing your (full )?name|type your (full )?name (below|to)", re.I)
# Confirmations: left for you, except that auto mode answers the standard ones (see AUTO).
CONFIRM = re.compile(r"declar|i agree|i hereby|i certify|i confirm|i acknowledge|i consent|my consent|consent to|"
                     r"privacy (notice|policy)|terms (and|&) conditions|i understand|i accept|"
                     r"(collect|store|process)\S*,? (store|and|or|process)\S* .{0,20}(my )?(personal )?data", re.I)
SKIP = re.compile(f"{HARD_SKIP.pattern}|{CONFIRM.pattern}", re.I)
PURPLE = "#6a1b9a"

# Set by apply_auto.py. None means manual mode: confirmations and unknown questions are left for you.
# Otherwise an object with:
#   consent(question) -> bool                       answer Yes to this confirmation?
#   answer(question, kind, options) -> str | None    kind is "text", "long" or "choice"
AUTO = None

# (profile key, pattern) -- order matters: first match wins.
RULES = [
    ("degree_start_month", r"start (date )?month|from month"),
    ("degree_start_year", r"start (date )?year|from year|year of (joining|admission)"),
    ("degree_end_month", r"end (date )?month|to month"),
    ("degree_year", r"end (date )?year|to year"),
    ("how_heard", r"how did you (hear|find|learn|come)|where did you (hear|find)|referral source|source of (application|hire)"),
    ("work_authorization", r"authori[sz]ed to work|work authori[sz]ation|right to work|legally (eligible|authori[sz]ed)|eligible to work"),
    ("needs_sponsorship", r"sponsor|\bvisa\b"),
    ("relocate", r"relocat"),
    ("notice_period", r"notice period"),
    ("expected_ctc", r"expected (ctc|salary|compensation|pay|stipend)|salary expectation|desired (salary|pay)"),
    ("current_ctc", r"current (ctc|salary|compensation|stipend)|present (ctc|salary)"),
    ("start_date", r"earliest start|start date|date of joining|available (from|to start)|when can you (start|join)"),
    ("grad_date", r"graduation date|expected graduation|graduat\w* (month|year)|year of graduation"),
    ("linkedin", r"linked ?in"),
    ("github", r"git ?hub"),
    ("twitter", r"twitter|\bx\.com\b|\bx profile|\bx \(formerly|\bx handle"),
    ("portfolio", r"portfolio|personal (site|website)|\bwebsite\b|other (url|link)"),
    ("preferred_location", r"preferred (location|city|work location)|location preference"),
    ("pronouns", r"pronoun"),
    ("languages", r"languages? known|language proficiency"),
    ("skills", r"\bskills?\b"),
    ("cover_letter", r"cover letter"),
    ("pitch", r"why should (we|you) (hire|be hired)|about yourself|describe yourself|introduce yourself"),
    ("father_occupation", r"(father|parent|guardian)\S* occupation"),
    ("mother_occupation", r"mother\S* occupation"),
    ("father_name", r"father"),
    ("mother_name", r"mother"),
    ("spouse_name", r"spouse|husband|wife"),
    ("family_income", r"annual (family )?income|family income"),
    ("dob", r"date of birth|\bdob\b|birth ?date"),
    ("gender", r"gender|\bsex\b"),
    ("pwbd", r"pwbd|\bpwd\b|disab|divyang|handicap|benchmark"),
    ("ex_serviceman", r"ex-? ?service|\besm\b|veteran"),
    ("category", r"category|caste|community"),
    ("domicile_state", r"domicile"),
    ("nationality", r"nationality|citizen"),
    ("religion", r"religion"),
    ("marital_status", r"marital"),
    ("identification_mark", r"identification mark|visible mark"),
    ("email", r"e-? ?mail"),
    ("alt_mobile", r"alternat\w* (mobile|phone|contact)"),
    ("mobile", r"mobile|phone|contact no|cell"),
    ("aadhaar", r"aadha?ar|\buid\b"),
    ("pan", r"\bpan\b|permanent account"),
    ("voter_id", r"voter|\bepic\b"),
    ("passport_no", r"passport"),
    ("driving_licence", r"driving licen"),
    ("pincode", r"pin ?code|postal code|\bzip\b|\bpin\b"),
    ("district", r"district"),
    ("state", r"\bstate\b|province|region"),
    ("country", r"\bcountry\b"),
    ("current_city", r"current (city|location)|\blocation\b|where are you( currently)? (based|located|living)|"
                     r"currently (located|based)|based in"),
    ("city", r"\bcity\b|town|village"),
    ("address_line2", r"address.*(line ?2|2)|street|locality|\barea\b"),
    ("address_line1", r"address|house|flat|door"),
    ("gate_reg_no", r"gate.*(reg|enrol|application)"),
    ("gate_paper", r"gate.*(paper|discipline|subject)"),
    ("gate_score", r"gate.*score"),
    ("gate_rank", r"gate.*rank"),
    ("gate_marks", r"gate.*marks"),
    ("gate_year", r"gate.*year"),
]
CLASS10 = r"10th|\bssc\b|sslc|matric|class ?x\b|\bx std|secondary school"
CLASS12 = r"12th|\bhsc\b|intermediate|class ?xii|xii std|higher secondary|senior secondary"
LATE_RULES = [
    ("backlogs", r"backlog|arrear|\bkt\b"),
    ("cgpa", r"cgpa|\bcpi\b|\bgpa\b"),
    ("degree_percentage", r"percent|% of marks|aggregate|marks obtained"),
    ("degree_year", r"year of passing|passing year|year of completion|completion year|end year"),
    ("university", r"university|board.*degree"),
    ("college", r"college|institute|institution|\bschool\b"),
    ("branch", r"branch|discipline|speciali[sz]ation|stream|major|field of study"),
    ("degree", r"qualification|degree|course"),
    ("experience_years", r"years of experience|total experience|experience \(years\)|work experience"),
    ("current_title", r"current (title|role|designation)|(recent|previous) job title|job title|designation|"
                      r"^\s*title\s*\*?\s*(\||$)"),
    ("current_employer", r"current (company|employer|organi[sz]ation)|(recent|previous|present) employer|"
                         r"employer( name)?\b|name of (your )?(current )?(employer|company|organi[sz]ation)|"
                         r"^\s*(company|organi[sz]ation)( name)?\s*\*?\s*(\||$)"),
    ("first_name", r"first name|given name"),
    ("middle_name", r"middle name"),
    ("last_name", r"last name|surname|family name"),
    ("full_name", r"\bname\b|applicant"),
]
FILE_RULES = [
    ("aadhaar_card", r"aadha?ar"),
    ("pan_card", r"\bpan\b"),
    ("voter_id_card", r"voter|\bepic\b"),
    ("passport_scan", r"passport(?! size)"),
    ("thumb", r"thumb"),
    ("handwritten_declaration", r"declaration"),
    ("photo", r"photo|picture|image of candidate"),
    ("signature", r"sign"),
    ("class10_marksheet", CLASS10),
    ("class12_marksheet", CLASS12),
    ("gate_scorecard", r"gate"),
    ("category_certificate", r"caste|category|community|obc|ews|\bsc\b|\bst\b"),
    ("pwbd_certificate", r"disab|pwbd|pwd"),
    ("domicile_certificate", r"domicile|residence|nativity"),
    ("bonafide_certificate", r"bonafide|bona fide|appearing|study certificate"),
    ("cgpa_conversion", r"conversion|equivalence"),
    ("experience_certificate", r"experience|relieving"),
    ("noc", r"\bnoc\b|no objection"),
    ("cover_letter_file", r"cover"),
    ("degree_certificate", r"degree|provisional|b\.?tech|b\.?e\b|marksheet|mark sheet|transcript|graduation|semester"),
    ("resume", r"resume|\bcv\b|bio-?data"),
]

COLLECT_JS = r"""
() => {
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const camel = s => (s || '').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[_\-.\[\]]+/g, ' ');
  const byIds = ids => (ids || '').split(/\s+/).map(id => document.getElementById(id))
                        .filter(Boolean).map(e => e.innerText).join(' ');
  const out = [];
  const sel = 'input, select, textarea, button[aria-haspopup="listbox"]';
  document.querySelectorAll(sel).forEach((el, i) => {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'button' ? 'listbox' : (el.type || '').toLowerCase();
    if (['hidden','submit','button','reset','image','password','search'].includes(type)) return;
    if (el.disabled) return;
    // never touch the site's search box or menus
    if (el.closest('header, nav, [role=search], [role=banner], [role=navigation]')
        || /^\s*search/i.test(el.placeholder || '') || /search/i.test(el.getAttribute('aria-label') || '')) return;
    // the hidden checkbox behind Ashby's Yes/No buttons is handled with the buttons, not on its own
    if (type === 'checkbox' && el.parentElement && el.parentElement.querySelector('button[aria-pressed]')) return;
    const styled = type === 'file' || type === 'checkbox' || type === 'radio';  // often visually hidden on purpose
    if (!styled && (el.tabIndex === -1 || el.getAttribute('aria-hidden') === 'true')) return;
    const r = el.getBoundingClientRect();
    if (!styled && r.width === 0 && r.height === 0) return;
    const own = [];
    if (el.labels) for (const l of el.labels) own.push(l.innerText);
    if (!own.length && type !== 'radio' && type !== 'checkbox') {
      // Ashby and similar: the question label sits in the field's box instead of being linked to the input
      const entry = el.closest('[class*="field-entry"], [class*="fieldEntry"]');
      const lab = entry && entry.querySelector('label, [class*="question-title"]');
      if (lab) own.push(lab.innerText);
    }
    const primary = txt([...own, byIds(el.getAttribute('aria-labelledby')), el.getAttribute('aria-label')]
                        .filter(Boolean).join(' | '));
    const ph = /^select/i.test(el.placeholder || '') ? '' : el.placeholder;
    const attrs = txt([ph, camel(el.name), camel(el.id), el.title,
                       camel(el.getAttribute('data-automation-id')), camel(el.getAttribute('data-qa'))]
                      .filter(Boolean).join(' | '));
    const ctx = [];
    const cell = el.closest('td,th');
    if (cell && cell.previousElementSibling) ctx.push(cell.previousElementSibling.innerText);
    const fs = el.closest('fieldset');
    if (fs && fs.querySelector('legend')) ctx.push(fs.querySelector('legend').innerText);
    const prev = el.previousElementSibling;
    if (prev && prev.innerText && prev.innerText.length < 80) ctx.push(prev.innerText);
    const par = el.parentElement;
    if (par && par.innerText && par.innerText.length < 120) ctx.push(par.innerText);
    const grand = par && par.parentElement;
    if (grand && grand.innerText && grand.innerText.length < 160) ctx.push(grand.innerText);
    let ownText = txt(own.join(' '));
    let question = '';
    if (type === 'radio' || type === 'checkbox') {
      if (!ownText) {
        const n = el.nextSibling;
        ownText = txt(n && (n.textContent || n.innerText)) || el.value;
      }
      // the visible question: first text line of the smallest container holding the whole group
      let box = el.parentElement;
      for (let k = 0; box && k < 6; k++, box = box.parentElement) {
        if (box.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`).length > 1) break;
      }
      if (box) {
        const g = el.closest('fieldset') || box.closest('fieldset');
        const legend = g && g.querySelector('legend');
        question = txt(legend ? legend.innerText : byIds(box.getAttribute('aria-labelledby')));
        // Otherwise climb until a line appears that isn't one of the group's options (Lever puts the
        // question two levels above the Yes/No list).
        const opts = new Set(Array.from(box.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`))
          .map(r => txt((r.labels && r.labels[0] && r.labels[0].innerText) || (r.nextSibling && r.nextSibling.textContent) || r.value)));
        for (let b = box, k = 0; !question && b && k < 5; k++, b = b.parentElement) {
          const line = (b.innerText || '').split('\n').map(txt).find(s => s && !opts.has(s) && s.length > 2);
          if (line && line.length < 300) question = line;
        }
      }
    }
    // Fields with no label of their own (Lever custom questions): the question is usually the first line of
    // the nearest container whose text is more than the field's own options and placeholder.
    if (!question && !primary) {
      const own = new Set([...(tag === 'select' ? Array.from(el.options).map(o => txt(o.text)) : []),
                           txt(el.placeholder), 'Select...', '']);
      let b = el.parentElement;
      for (let k = 0; b && k < 5 && !question; k++, b = b.parentElement) {
        const line = (b.innerText || '').split('\n').map(txt).find(s => s && !own.has(s) && s.length > 3);
        if (line && line.length < 300) question = line;
      }
    }
    // what a custom dropdown currently shows (react-select style, Workday buttons)
    let current = '';
    if (tag === 'button') current = txt(el.innerText);
    else if (el.getAttribute('role') === 'combobox') {
      const box = el.closest('[class*="control"]') || el.closest('[class*="container"]');
      const sv = box && box.querySelector('[class*="single-value"], [class*="singleValue"], ' +
                                          '[class*="multi-value"], [class*="multiValue"]');
      current = sv ? txt(sv.innerText) : '';
    }
    el.setAttribute('data-af', String(i));
    out.push({
      idx: i, tag, type, name: el.name || '',
      value: tag === 'button' ? '' : (el.value || ''), readOnly: !!el.readOnly, checked: !!el.checked,
      combo: el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete'),
      primary, attrs,
      label: txt([primary, attrs, ...ctx].filter(Boolean).join(' | ')).slice(0, 400),
      datepicker: /pick date|dd\/mm|mm\/dd|yyyy/i.test(el.placeholder || '') || !!el.closest('[class*="datepicker"]'),
      own: ownText, current, question, ctx0: txt(ctx.find(c => txt(c)) || '').slice(0, 300),
      required: el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(primary || attrs)
                || (() => { const e = el.closest('[class*="field-entry"], [class*="fieldEntry"]');   // Ashby
                            const l = e && e.querySelector('label, [class*="question-title"]');
                            return !!l && /required/i.test(l.className || ''); })()
                || (type !== 'checkbox' && type !== 'radio' && /\*\s*($|\|)/.test(ctx.join(' | ').slice(0, 120))),
      fileCount: type === 'file' && el.files ? el.files.length : 0,
      // Some sites (Ashby) clear the file box after upload and show the file name next to it instead.
      boxText: type === 'file' ? (() => { let b = el; for (let k = 0; k < 4 && b.parentElement; k++) b = b.parentElement;
                                          return txt(b.innerText).slice(0, 400); })() : '',
      selectedText: tag === 'select' && el.selectedIndex >= 0 ? txt(el.options[el.selectedIndex].text) : '',
      options: tag === 'select' ? Array.from(el.options).map(o => txt(o.text)) : null,
    });
  });
  return out;
}
"""

# Questions answered with a row of toggle buttons (Ashby's Yes / No) instead of a normal field.
BUTTONS_JS = r"""
() => {
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const groups = [];
  const seen = new Set();
  document.querySelectorAll('button[aria-pressed]').forEach(btn => {
    const box = btn.parentElement;
    if (!box || seen.has(box)) return;
    const buttons = Array.from(box.querySelectorAll(':scope > button[aria-pressed]'));
    if (buttons.length < 2 || buttons.length > 6) return;
    seen.add(box);
    const entry = box.closest('[class*="field-entry"], [class*="fieldEntry"], fieldset, .form-group, li') || box.parentElement;
    const label = entry && entry.querySelector('label, legend, [class*="question-title"]');
    const question = txt(label ? label.innerText : (entry ? entry.innerText.split('\n')[0] : ''));
    const g = groups.length;
    buttons.forEach((b, j) => b.setAttribute('data-afb', `${g}-${j}`));
    groups.push({
      g, question,
      options: buttons.map(b => txt(b.innerText)),
      pressed: buttons.findIndex(b => b.getAttribute('aria-pressed') === 'true'),
      required: !!label && (/required/i.test(label.className) || /\*\s*$/.test(label.innerText || '')),
    });
  });
  return groups;
}
"""


def norm(s):
    return re.sub(r"[^a-z0-9%]+", " ", str(s).lower()).strip()


def rule_key(text):
    low = text.lower()
    for key, pat in RULES:
        if re.search(pat, low):
            return key
    for prefix, pat in (("class10", CLASS10), ("class12", CLASS12)):
        if re.search(pat, low):
            if re.search(r"board|school", low):
                return f"{prefix}_board"
            if re.search(r"year", low):
                return f"{prefix}_year"
            if re.search(r"percent|marks|%|cgpa|grade|aggregate", low):
                return f"{prefix}_percentage"
            return None
    for key, pat in LATE_RULES:
        if re.search(pat, low):
            return key
    return None


BLOCKED = ("HARD", "CONFIRM")


def classify(f):
    """Profile key for a field, 'HARD' (never touch), 'CONFIRM' (a confirmation question), or None."""
    own = f["primary"] or f["attrs"]
    head = own if f["primary"] else f"{own} {f['label'][:80]}"
    if HARD_SKIP.search(head):
        return "HARD"
    if CONFIRM.search(head):
        return "CONFIRM"
    key = ((f["primary"] and rule_key(f["primary"])) or (f.get("question") and rule_key(f["question"]))
           or rule_key(f["label"]))
    # "What interests you about this company?" is a question for Gemini, not a short profile field
    q = (f["primary"] or f.get("question") or "").strip()
    if key and key not in LONG_ANSWER_KEYS and (f["tag"] == "textarea" or QUESTION_WORDS.match(q)) \
            and not re.search(r"\b(name|e-?mail|phone|mobile|url|link|number|ctc|salary|notice)\b", q[:60], re.I):
        return None
    # Greenhouse uses the same "Start date month" label for education and employment; employment fields are
    # named "start date month 0" behind the scenes, education ones "start month 0".
    if key in EMPLOYMENT_DATE and re.search(r"\bdate (month|year)\b", f["attrs"]):
        key = EMPLOYMENT_DATE[key]
    return key


LONG_ANSWER_KEYS = {"cover_letter", "pitch", "skills", "address_line1", "address_line2", "how_heard",
                    "expected_ctc", "current_ctc", "notice_period", "start_date", "languages"}
QUESTION_WORDS = re.compile(r"^(what|why|how|describe|tell|explain|share|please (describe|explain|share)|"
                            r"in (a few|\d+) (words|sentences)|give us|walk us)\b", re.I)
EMPLOYMENT_DATE = {"degree_start_month": "employment_start_month", "degree_start_year": "employment_start_year",
                   "degree_end_month": "employment_end_month", "degree_year": "employment_end_year"}


def readable(question):
    """False for 'questions' that are really internal ids or just the options (e.g. 'cards 8599... field0')."""
    words = re.findall(r"[A-Za-z]{3,}", re.sub(r"\b(cards|field\d*|question|select|input)\b", " ", question, flags=re.I))
    # one real word ("Hobbies") is enough; internal ids like "cards 8599a1b2 field0" are not
    return (len(words) >= 2 or any(len(w) >= 4 for w in words)) and not re.search(r"\b[0-9a-f]{8}\b", question)


def ask_ai(question, kind, options):
    """Ask auto mode's Gemini, but only about questions the bot could actually read."""
    if AUTO is None or not readable(question):
        return None
    return AUTO.answer(question, kind, options)


def field_question(f):
    """Human-readable question text for a single field."""
    for text in (f["primary"], f.get("question", ""), f["own"], f.get("ctx0", ""), f["attrs"], f["label"]):
        text = re.sub(r"\s*\|.*$", "", text or "").strip()
        if text and not re.fullmatch(r"(type your (response|answer)|start typing\.*|select\.*|enter.*here)", text, re.I):
            return text
    return f["label"][:150]


YES = re.compile(r"^(yes|i agree|agree|i confirm|confirm|i accept|accept|i acknowledge|acknowledge|i consent|i do)\b", re.I)


def yes_index(options):
    return next((i for i, o in enumerate(options) if YES.search(o.strip())), None)


def signature(f, group_question=None):
    return norm(group_question or f["primary"] or f["attrs"] or f["label"])[:160]


def remembered(sig):
    if not sig:
        return ""
    if sig in ANSWERS:
        return ANSWERS[sig]
    close = difflib.get_close_matches(sig, list(ANSWERS), n=1, cutoff=0.9)
    return ANSWERS[close[0]] if close else ""


def pick_option(options, value):
    """Index of the option that best matches value, or None."""
    v = norm(value)
    if not v:
        return None
    opts = [norm(o) for o in options]
    word = re.compile(rf"\b{re.escape(v)}\b")
    for test in (lambda o: o == v,
                 lambda o: o.startswith(v + " ") or (v.startswith(o) and len(o) > 2),
                 lambda o: bool(word.search(o)) or (len(o) > 2 and re.search(rf"\b{re.escape(o)}\b", v))):
        for i, o in enumerate(opts):
            if o and o not in ("select", "please select", "choose", "select one", "select an option") and test(o):
                return i
    return None


def profile_value(key, field, gov):
    if not key or key in BLOCKED or (key in SENSITIVE and not gov):
        return ""
    if key in ("cover_letter", "pitch"):
        f = DOCS / f"{key}.txt"
        return f.read_text(encoding="utf-8").strip() if f.is_file() else ""
    val = str(PROFILE.get(key, "") or "").strip()
    if val.upper() == "DD/MM/YYYY":
        return ""
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", val)
    if m and field["type"] == "date":
        val = f"{m[3]}-{m[2].zfill(2)}-{m[1].zfill(2)}"
    if key == "start_date" and field.get("datepicker") and not m and re.search(r"immediate|asap|now", val, re.I):
        # a calendar field can't take the word "Immediately": use today's date, as date pickers expect
        val = date.today().strftime("%m/%d/%Y")
    return val


TICK_JS = """i => {
  const e = document.querySelector(`[data-af='${i}']`);
  if (!e) return false;
  if (!e.checked) e.click();                                   // what a real click on the box does
  if (!e.checked && e.id) {
    const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
    if (l) l.click();                                          // styled boxes listen on their label
  }
  if (!e.checked) {                                            // last resort: set it and tell the page
    e.checked = true;
    e.dispatchEvent(new Event('input', {bubbles: true}));
    e.dispatchEvent(new Event('change', {bubbles: true}));
  }
  return e.checked;
}"""


UPLOAD_STATE_JS = """([i, name]) => {
  const e = document.querySelector(`[data-af='${i}']`);
  if (!e) return 'gone';
  let b = e; for (let k = 0; k < 4 && b.parentElement; k++) b = b.parentElement;
  const t = (b.innerText || '').toLowerCase();
  if (/uploading|processing|parsing|analy[sz]ing/.test(t)) return 'busy';
  if (/upload failed|failed to upload|too large|not supported|invalid file/.test(t)) return 'failed';
  if (t.includes(name) || /success|uploaded|attached\\b/.test(t)) return 'ok';
  return (e.files && e.files.length) ? 'selected' : 'empty';
}"""


def attach(frame, idx, path, tries=2):
    """Attach a file and wait until the site shows it (file name, 'Success!'). True when confirmed."""
    name = Path(path).name.lower()
    state = "empty"
    for attempt in range(tries):
        try:
            frame.locator(f"[data-af='{idx}']").set_input_files(str(path), timeout=15000 if attempt == 0 else 5000)
        except Exception:
            # First try: the box vanished (the site re-rendered after another upload); the next fill pass
            # finds the new box. Later try: the site swapped the box for its own file view, so it took the file.
            return attempt > 0 and state in ("selected", "busy", "empty")
        state = "busy"
        for n in range(30):  # up to 15 s for the site's upload / resume parsing
            frame.page.wait_for_timeout(500)
            state = frame.evaluate(UPLOAD_STATE_JS, [idx, name])
            if state in ("ok", "failed", "gone") or (state == "selected" and n >= 6):
                break
        if state == "ok" or state == "gone":  # 'gone': the site replaced the box with its own file view
            return True
        if state == "selected":               # plain file box with no visible confirmation: that's normal
            return True
    return False


def close_upload_dialog(frame):
    """Some sites open a pop-up after an upload (Workable's 'Crop your photo'): confirm it so it doesn't
    sit on top of the form and block Submit."""
    try:
        dialog = frame.locator("[role='dialog']:visible, [aria-modal='true']:visible").first
        if not dialog.count():
            return
        btn = dialog.get_by_role("button", name=re.compile(r"^\s*(upload|save|done|apply|crop|confirm|ok)\s*$", re.I))
        if btn.count():
            btn.first.click(timeout=5000)
            frame.page.wait_for_timeout(1500)
    except Exception:
        pass


def file_attached(f):
    if f["fileCount"]:
        return True
    names = [Path(p).name.lower() for p in PROFILE.get("docs", {}).values() if p]
    return any(n in f["boxText"].lower() for n in names)


def tick(frame, idx):
    """Tick a checkbox or radio, including ones hidden behind custom styling. False if it couldn't
    (for example the site rebuilt the form a moment ago; the next fill pass finds the new box)."""
    try:
        frame.locator(f"[data-af='{idx}']").check(force=True, timeout=3000)
        return True
    except Exception:
        try:
            return bool(frame.evaluate(TICK_JS, idx))
        except Exception:
            return False


def mark(frame, idx, color):
    frame.evaluate("([i, c]) => { const e = document.querySelector(`[data-af='${i}']`);"
                   " if (e) { e.style.outline = `3px solid ${c}`; e.style.outlineOffset = '1px'; } }",
                   [idx, color])


def choose_from_listbox(frame, values, wait_ms):
    """Click the visible dropdown option that best matches one of values (in order). True if clicked."""
    opts = frame.locator("[role='option']:visible")
    try:
        opts.first.wait_for(timeout=wait_ms)
    except Exception:
        return False
    texts = opts.all_inner_texts()
    for v in values:
        i = pick_option(texts, v)
        if i is not None:
            opts.nth(i).click()
            return True
    return False


def alternatives(key, val):
    """The value plus other ways forms list it (profile.json "alternatives"), most specific first."""
    alts = PROFILE.get("alternatives", {}).get(key, []) if key else []
    return [val] + [a for a in alts if norm(a) != norm(val)]


def set_value(frame, f, val, key=None):
    loc = frame.locator(f"[data-af='{f['idx']}']")
    values = alternatives(key, val)
    if f["tag"] == "select":
        i = next((i for v in values if (i := pick_option(f["options"], v)) is not None), None)
        if i is None:
            raise ValueError("no matching option")
        loc.select_option(index=i)
    elif f["type"] == "listbox":  # Workday-style button dropdown
        loc.click()
        if not choose_from_listbox(frame, values, 3000):
            frame.page.keyboard.press("Escape")
            raise ValueError("no matching option")
    elif f["combo"]:  # searchable dropdown (Greenhouse, Lever, React forms)
        loc.scroll_into_view_if_needed()
        loc.click(force=True)
        # Search with the full text first, then each alternative, then just the first word of each.
        terms = list(dict.fromkeys(values + [v.split()[0].strip(",") for v in values if v.split()]))
        for term in terms:
            loc.fill(term, force=True)
            if choose_from_listbox(frame, values, 2500):
                return
        loc.fill("", force=True)
        frame.page.keyboard.press("Escape")
        raise ValueError("no matching option")
    elif f.get("datepicker"):
        loc.fill(val)
        loc.press("Enter")  # confirm the typed date and close the calendar
        frame.page.keyboard.press("Escape")
    else:
        loc.fill(val)


def combo_options(frame, f):
    """Open a custom dropdown, read its option texts, close it again."""
    loc = frame.locator(f"[data-af='{f['idx']}']")
    try:
        loc.scroll_into_view_if_needed()
        loc.click(force=True)
        opts = frame.locator("[role='option']:visible")
        opts.first.wait_for(timeout=3000)
        texts = [t.strip() for t in opts.all_inner_texts()]
    except Exception:
        texts = []
    frame.page.keyboard.press("Escape")
    return texts[:60]


def options_of(frame, f):
    if f["tag"] == "select":
        return [o for o in f["options"] if o and not re.match(r"(--)?\s*(select|choose|please)", o, re.I)]
    if f["combo"] or f["type"] == "listbox":
        return combo_options(frame, f)
    return None


def auto_value(frame, f, key):
    """Auto mode: an answer for a field the profile and saved answers don't cover."""
    if AUTO is None or key == "HARD" or key in SENSITIVE:
        return ""
    options = options_of(frame, f)
    question = field_question(f)
    if key == "CONFIRM":
        if not AUTO.consent(question):
            return ""
        if options:
            i = yes_index(options)
            return options[i] if i is not None else ""
        return ""
    kind = "choice" if options else ("long" if f["tag"] == "textarea" else "text")
    return ask_ai(question, kind, options) or ""


def fill_frame(frame, report, gov):
    try:
        fields = frame.evaluate(COLLECT_JS)
    except Exception:
        return
    radios, checkgroups = {}, {}
    for f in fields:
        if f["type"] == "radio":
            radios.setdefault(f["name"] or f["idx"], []).append(f)
            continue

        if f["type"] == "checkbox":
            if f["name"] and sum(1 for g in fields if g["type"] == "checkbox" and g["name"] == f["name"]) > 1:
                checkgroups.setdefault(f["name"], []).append(f)  # "pick one or more" question
                continue
            # Only auto mode ticks single boxes, and only standard confirmations (privacy, accuracy, consent).
            text = f["own"] or f["primary"] or f["label"][:200]
            if not f["checked"] and re.search(r"current (role|job|position)|currently work here", text, re.I) \
                    and str(PROFILE.get("employment_current", "")).lower().startswith("y"):
                if tick(frame, f['idx']):
                    mark(frame, f["idx"], GREEN)
                    report["filled"].append("employment_current = ticked 'current role'")
                continue
            if AUTO and not f["checked"] and CONFIRM.search(text) and AUTO.consent(text):
                if tick(frame, f['idx']):
                    mark(frame, f["idx"], PURPLE)
                    report["filled"].append(f"consent = ticked: {text[:50]}")
            continue

        if f["type"] == "file":
            if file_attached(f):
                continue
            # The field's own name first (e.g. "cover letter" in its id), only then the text around it,
            # which on some forms also mentions the neighbouring "Resume/CV" field.
            doc_key = None
            for text in (f["primary"], f["attrs"], f.get("question", ""), f["label"]):
                low = (text or "").lower()
                doc_key = next((k for k, p in FILE_RULES if low and re.search(p, low)), None)
                if doc_key:
                    break
            path = PROFILE.get("docs", {}).get(doc_key or "", "")
            if doc_key in SENSITIVE and not gov:
                path = ""
            if doc_key in ("photo", "signature", "thumb") and not gov:
                continue  # company forms: never attach photos (crop pop-ups); if one insists, it's handed to you
            full = DOCS / path if path else None
            if full and full.is_file():
                ok = attach(frame, f["idx"], full)
                close_upload_dialog(frame)
                mark(frame, f["idx"], GREEN if ok else YELLOW)
                if ok:
                    report["filled"].append(f"[file] {doc_key} <- {path}")
                else:
                    report["manual"].append(f"[file] {doc_key}: site didn't confirm the upload of {path}")
            else:
                mark(frame, f["idx"], YELLOW)
                report["manual"].append(f"[file] {(f['primary'] or f['label'])[:70]}")
            continue

        key = classify(f)
        if key == "HARD" or (key == "CONFIRM" and AUTO is None):
            continue
        if f["tag"] == "select":
            already = bool(f["options"]) and f["selectedText"] != f["options"][0]
        else:
            already = bool(f["value"] or f["current"]) and not re.match(r"select", f["current"], re.I)
        if already:
            continue  # never overwrite what's already there
        val, color, source = profile_value(key, f, gov), GREEN, key
        if not val and key not in SENSITIVE and key != "CONFIRM":
            val, color, source = remembered(signature(f)), BLUE, "remembered"
        if not val and not f["readOnly"]:
            val, color, source = auto_value(frame, f, key), PURPLE, ("consent" if key == "CONFIRM" else "ai")
        if not val or f["readOnly"]:
            mark(frame, f["idx"], YELLOW)
            why = ""
            if key in SENSITIVE and not gov:
                why = "  (ID number: only filled on government sites)"
            elif key:
                why = f"  (add '{key}' to profile.json)"
            report["manual"].append(f"{(f['primary'] or f['label'])[:70]}{why}")
            continue
        try:
            try:
                set_value(frame, f, val, key)
            except ValueError:
                # Profile value isn't one of this form's options: in auto mode let Gemini pick the closest.
                options = options_of(frame, f) if AUTO is not None else None
                pick = ask_ai(f"{field_question(f)} (candidate's answer: {val})", "choice", options) \
                    if options else None
                if not pick:
                    raise
                val, color, source = pick, PURPLE, f"ai-matched {source}"
                set_value(frame, f, val)
            mark(frame, f["idx"], color)
            report["filled"].append(f"{source} = {val[:60]}")
        except Exception as e:
            mark(frame, f["idx"], YELLOW)
            report["manual"].append(f"{(f['primary'] or f['label'])[:70]}  ({source}: {e.__class__.__name__})")

    for group in radios.values():
        question = group_question(group)
        if HARD_SKIP.search(question[:120]) or any(r["checked"] for r in group):
            continue
        confirm = bool(CONFIRM.search(question[:120]))
        if confirm and AUTO is None:
            continue
        options = [r["own"] for r in group]
        key = None if confirm else rule_key(question)
        val, color, source = profile_value(key, group[0], gov), GREEN, key
        if not val and not confirm:
            val, color, source = remembered(signature(group[0], question)), BLUE, "remembered"
        if not val and AUTO is not None:
            color = PURPLE
            if confirm:
                yi = yes_index(options) if AUTO.consent(question) else None
                val, source = (options[yi] if yi is not None else ""), "consent"
            elif key not in SENSITIVE:
                val, source = ask_ai(question, "choice", options) or "", "ai"
        i = pick_option(options, val) if val else None
        if i is None:
            for r in group:
                mark(frame, r["idx"], YELLOW)
            report["manual"].append(f"[choice] {question[:70]}")
            continue
        if not tick(frame, group[i]['idx']):
            report["manual"].append(f"[choice] {question[:70]}  (couldn't tick it)")
            continue
        mark(frame, group[i]["idx"], color)
        report["filled"].append(f"{source} = {group[i]['own']}")

    # "Tick all that apply" groups: saved answer first, then (auto mode) Gemini picks the best single option.
    for group in checkgroups.values():
        question = group_question(group)
        if any(g["checked"] for g in group) or SKIP.search(question[:120]):
            continue
        options = [g["own"] for g in group]
        val, color, source = remembered(signature(group[0], question)), BLUE, "remembered"
        if not val and AUTO is not None:
            val, color, source = ask_ai(question, "choice", options) or "", PURPLE, "ai"
        i = pick_option(options, val) if val else None
        if i is None:
            for g in group:
                mark(frame, g["idx"], YELLOW)
            report["manual"].append(f"[tick boxes] {question[:70]}")
            continue
        if not tick(frame, group[i]['idx']):
            report["manual"].append(f"[choice] {question[:70]}  (couldn't tick it)")
            continue
        mark(frame, group[i]["idx"], color)
        report["filled"].append(f"{source} = {group[i]['own']}")

    fill_button_groups(frame, report, gov)


def fill_button_groups(frame, report, gov):
    """Yes / No style questions shown as toggle buttons (Ashby)."""
    try:
        groups = frame.evaluate(BUTTONS_JS)
    except Exception:
        return
    for grp in groups:
        question, options = grp["question"], grp["options"]
        if grp["pressed"] >= 0 or not question or HARD_SKIP.search(question[:120]):
            continue
        confirm = bool(CONFIRM.search(question[:160]))
        if confirm and AUTO is None:
            continue
        key = None if confirm else rule_key(question)
        val, source = profile_value(key, {"type": "buttons"}, gov), key
        if not val and not confirm:
            val, source = remembered(norm(question)[:160]), "remembered"
        if not val and AUTO is not None:
            if confirm:
                yi = yes_index(options) if AUTO.consent(question) else None
                val, source = (options[yi] if yi is not None else ""), "consent"
            elif key not in SENSITIVE:
                val, source = ask_ai(question, "choice", options) or "", "ai"
        i = pick_option(options, val) if val else None
        if i is None:
            report["manual"].append(f"[buttons] {question[:70]}")
            continue
        try:
            frame.locator(f"[data-afb='{grp['g']}-{i}']").click(timeout=5000)
            report["filled"].append(f"{source} = {options[i]}")
        except Exception:
            report["manual"].append(f"[buttons] {question[:70]}  (couldn't click)")


def group_question(group):
    """The question a radio group asks, without the option texts."""
    options = sorted((r["own"] for r in group if r["own"]), key=len, reverse=True)

    def strip(text):
        for o in options:
            text = text.replace(o, " ")
        return re.sub(r"(\s*\|\s*)+", " | ", text).strip(" |")

    q = strip(group[0].get("question") or "")
    if len(re.sub(r"\W", "", q)) >= 3:  # real question text, not just the options run together
        return q
    return strip(group[0]["label"])


def required_missing(page):
    """Labels of required fields that are still empty, across all frames."""
    missing = []
    for frame in page.frames:
        try:
            fields = frame.evaluate(COLLECT_JS)
        except Exception:
            continue
        groups = {}
        for f in fields:
            if f["type"] in ("radio", "checkbox") and f["name"]:
                groups.setdefault((f["type"], f["name"]), []).append(f)
                continue
            if not f["required"]:
                continue
            if f["type"] == "file":
                empty = not file_attached(f)
            elif f["tag"] == "select":
                empty = not f["options"] or f["selectedText"] == f["options"][0]
            elif f["type"] == "checkbox":
                empty = not f["checked"]
            else:
                empty = not (f["value"] or f["current"]) or bool(re.match(r"select", f["current"], re.I))
            if empty:
                missing.append(field_question(f)[:80])
        for (kind, _), group in groups.items():
            if any(g["required"] for g in group) and not any(g["checked"] for g in group):
                q = group_question(group) if kind == "radio" else field_question(group[0])
                missing.append(q[:80])
        try:
            for grp in frame.evaluate(BUTTONS_JS):
                if grp["required"] and grp["pressed"] < 0:
                    missing.append(grp["question"][:80])
        except Exception:
            pass
    return missing


def fill_page(page, quiet=False):
    host = urlparse(page.url).hostname or ""
    gov = bool(GOV_SITES.search(host))
    report = {"filled": [], "manual": []}
    for frame in page.frames:
        fill_frame(frame, report, gov)
    if quiet:
        return report
    print(f"\n  {host}  ({'government site' if gov else 'company site: ID numbers withheld'})")
    print(f"  Filled {len(report['filled'])} field(s):")
    for line in report["filled"]:
        print("   +", line)
    if report["manual"]:
        print(f"  Fill these yourself ({len(report['manual'])}), shown in yellow. Then press l to teach the bot:")
        for line in report["manual"]:
            print("   ?", line)
    print("  Captcha, OTP, 'I confirm' questions, Submit and fees are left for you.\n")
    return report


def learn_page(page):
    """Remember the answers you typed into fields the profile doesn't cover."""
    learned, hints = 0, set()
    for frame in page.frames:
        try:
            fields = frame.evaluate(COLLECT_JS)
        except Exception:
            continue
        radios = {}
        for f in fields:
            if f["type"] == "radio":
                radios.setdefault(f["name"] or f["idx"], []).append(f)
                continue
            if f["type"] in ("file", "checkbox"):
                continue
            key = classify(f)
            if key in BLOCKED or key in SENSITIVE or (key and PROFILE.get(key)):
                continue
            val = f["selectedText"] if f["tag"] == "select" else (f["current"] or f["value"])
            if not val or (f["tag"] == "select" and f["options"] and val == f["options"][0]):
                continue
            if key:
                hints.add(key)
            ANSWERS[signature(f)] = val
            learned += 1
        for group in radios.values():
            question = group_question(group)
            chosen = next((r["own"] for r in group if r["checked"]), "")
            if chosen and not SKIP.search(question[:120]):
                ANSWERS[signature(group[0], question)] = chosen
                learned += 1
        try:
            for grp in frame.evaluate(BUTTONS_JS):
                if grp["pressed"] >= 0 and grp["question"] and not SKIP.search(grp["question"][:120]):
                    ANSWERS[norm(grp["question"])[:160]] = grp["options"][grp["pressed"]]
                    learned += 1
        except Exception:
            pass
    ANSWERS_FILE.write_text(json.dumps(ANSWERS, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Learned {learned} answer(s); {len(ANSWERS)} remembered in total (answers.json).")
    if hints:
        print("  Tip: these are common fields, add them to profile.json too:", ", ".join(sorted(hints)))


def applied_urls():
    if not APPLIED_FILE.is_file():
        return set()
    with open(APPLIED_FILE, encoding="utf-8") as fh:
        return {row["url"] for row in csv.DictReader(fh)}


def mark_applied(page, job):
    new = not APPLIED_FILE.is_file()
    with open(APPLIED_FILE, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "company", "title", "url", "page"])
        if new:
            w.writeheader()
        w.writerow({"date": date.today().isoformat(), "company": job.get("company", ""),
                    "title": job.get("title", ""), "url": job.get("url", page.url), "page": page.url})
    print("  Saved to applied.csv.")


def load_jobs():
    gov = json.loads((BOT / "jobs.json").read_text(encoding="utf-8"))
    company = []
    for name in ("pdf_jobs.json", "found_jobs.json"):
        f = BOT / name
        if f.is_file():
            company += json.loads(f.read_text(encoding="utf-8"))
    return gov, company


def choose_job():
    """Returns a job dict with at least 'url'."""
    if len(sys.argv) > 1 and not choose_job.used_argv:
        choose_job.used_argv = True
        return {"url": sys.argv[1]}
    gov, company = load_jobs()
    done = applied_urls()
    print("\nGovernment jobs:")
    for i, j in enumerate(gov, 1):
        print(f"  {i}. {j['name']}  (closes {j['closes']})" + ("  [applied]" if j["url"] in done else ""))
    print(f"  c. company jobs ({len(company)}: your job_links PDF + jobs found by finder.py)")
    print("  or paste any URL")
    ans = input("Pick: ").strip()
    if ans.isdigit() and 1 <= int(ans) <= len(gov):
        job = gov[int(ans) - 1]
        if job.get("check"):
            print(f"\n  Before applying: {job['check']}")
        return {"url": job["url"], "company": job["name"], "title": job["name"]}
    if ans.lower() == "c":
        word = input("Filter by word (e.g. agent, genai, stripe, intern) or Enter for all: ").strip().lower()
        shown = [j for j in company
                 if word in f"{j.get('level', '')} {j['company']} {j['title']}".lower()][:80]
        for i, j in enumerate(shown, 1):
            flag = "[applied]" if j["url"] in done else ("!" if j.get("note") else "")
            dl = f" (by {j['deadline']})" if j.get("deadline") else ""
            print(f"  {i:>2}. {j.get('level', '')[:12]:<12} {j['company'][:16]:<16} {j['title'][:48]}{dl} {flag}")
        pick = input("Number: ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(shown):
            job = shown[int(pick) - 1]
            if job.get("note"):
                print(f"\n  Note: {job['note']}")
            return job
        return {"url": "about:blank"}
    return {"url": ans or "about:blank"}


choose_job.used_argv = False


def main():
    job = choose_job()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(BOT / ".chrome-profile"), channel="chrome", headless=False, no_viewport=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(job["url"])
        print("\nChrome is open. Log in and open the application form yourself.")
        print("Commands:  Enter = fill   a = fill + Gemini answers the screening questions   l = learn my answers")
        print("           d = I submitted it   n = next job   q = quit")
        print("For Wellfound / Naukri / Indeed: you browse and click Apply; when a form opens, type a and Enter.")
        while True:
            cmd = input("> ").strip().lower()
            if cmd == "q":
                break
            open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
            if cmd == "n":
                job = choose_job()
                page = ctx.new_page()
                page.goto(job["url"])
                continue
            if not open_pages:
                print("  No open tab.")
                continue
            target = open_pages[-1]  # portals often open the form in a new tab
            target.bring_to_front()
            if cmd == "l":
                learn_page(target)
            elif cmd == "d":
                mark_applied(target, job)
            elif cmd == "a":
                ai_fill_page(target)
            else:
                fill_page(target)
        ctx.close()


def ai_fill_page(page):
    """Assist mode: fill the page, then let Gemini answer the screening questions. You review and submit."""
    global AUTO
    import ai
    keys = ai.api_keys()
    if not keys:
        print("  No Gemini key in gemini_key.txt, so filling without it.")
        fill_page(page)
        return
    settings_file = BOT / "settings.json"
    settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.is_file() else {}
    models = settings.get("gemini_models") or ["gemini-3.5-flash"]
    job = {"company": urlparse(page.url).hostname or "the company", "title": page.title()[:120]}
    log = []
    helper = ai.AutoAnswerer(ai.Gemini(keys, models), ai.candidate_facts(PROFILE, DOCS), job,
                             page.inner_text("body")[:6000], log)
    helper.fit_result = (True, "assist mode")  # you chose this job: no fit check
    AUTO = helper
    try:
        fill_page(page, quiet=True)            # profile answers in, screening questions collected
        if helper.pending:
            print(f"  Asking Gemini about {len(helper.pending)} question(s)...")
            helper.resolve()
        report = fill_page(page)               # Gemini's answers in (purple), report printed
        for entry in log:
            if entry["kind"] in ("text", "long", "choice"):
                print(f"   [gemini] {entry['question'][:70]}\n            -> {entry['answer'][:160]}")
        missing = required_missing(page)
        if missing:
            print("  Still needs you:", "; ".join(m[:60] for m in missing[:6]))
        print("  Check the form, then submit it yourself. Type d after submitting to record it.")
    except ai.QuotaExhausted as e:
        print(f"  Gemini is out of today's free quota ({e}); filled from your profile only.")
    finally:
        AUTO = None


if __name__ == "__main__":
    main()
