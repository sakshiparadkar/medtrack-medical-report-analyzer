"""
MedTrack chat assistant — free, rule-based, no API key needed.

How it works:
1. The user's reports are read from the database (done in app.py).
2. The question is matched to an "intent" using keywords (English + Hinglish).
3. The answer is built ONLY from the user's own stored values, so it can never
   invent a number. Static, general explanations come from a small glossary.

Function names are the same as the earlier API version, so app.py and
chat.html work without any change.
"""

import re
import time
from collections import defaultdict

MAX_REPORTS_IN_CONTEXT = 15


class ChatError(Exception):
    """Friendly, user-facing error from the chat assistant."""


# ---------------------------------------------------------- GLOSSARY ----
# (keywords, general explanation). Order matters: more specific entries first.

GLOSSARY = [
    (("hdl",),
     "HDL is the 'good' cholesterol. It helps clear other cholesterol from the blood. "
     "Higher HDL is generally better; low HDL is worth discussing with a doctor."),
    (("ldl",),
     "LDL is the 'bad' cholesterol. When it stays high, it can build up in blood vessels over time. "
     "Diet, exercise and regular check-ups help keep it in range."),
    (("triglyceride", "triglycerides", "tg"),
     "Triglycerides are a type of fat in the blood. High levels are often linked with excess sugar, "
     "refined carbs, alcohol and low activity."),
    (("cholesterol", "chol"),
     "Total cholesterol is the overall amount of cholesterol in your blood (HDL + LDL + others). "
     "It is best read together with HDL, LDL and triglycerides."),
    (("hba1c", "a1c", "glycated", "glycosylated"),
     "HbA1c shows your average blood sugar over roughly the last 3 months, so it is more stable than a single sugar reading."),
    (("glucose", "sugar", "fbs", "rbs", "ppbs"),
     "Glucose (blood sugar) is your body's main energy source. Persistently high readings need a doctor's "
     "evaluation; very low readings can cause weakness and dizziness."),
    (("hemoglobin", "haemoglobin", "hb", "hgb"),
     "Hemoglobin is the protein in red blood cells that carries oxygen. Low levels are commonly linked with "
     "anaemia (often iron-related); high levels can happen with dehydration."),
    (("rbc",),
     "RBC count is the number of red blood cells, which carry oxygen around the body. It is read together with hemoglobin."),
    (("wbc", "tlc", "leukocyte", "leucocyte"),
     "WBC count is the number of white blood cells, your infection-fighting cells. It often rises during infections "
     "and inflammation."),
    (("platelet", "platelets", "plt"),
     "Platelets help your blood clot. Low counts can cause easy bruising or bleeding; high counts are less common "
     "and need a doctor's opinion."),
    (("creatinine",),
     "Creatinine is a waste product filtered by the kidneys. It is one of the common markers used to check kidney function."),
    (("urea", "bun"),
     "Urea is a waste product made when the body breaks down protein. It is checked along with creatinine to look at kidney health."),
    (("uric",),
     "Uric acid is a waste product from food and body cells. High levels can be linked with joint pain (gout) and are "
     "influenced by diet and hydration."),
    (("tsh",),
     "TSH is a hormone that controls your thyroid gland. High or low TSH usually points to an under- or over-active thyroid, "
     "so it needs a doctor's review."),
    (("sgpt", "alt"),
     "SGPT (ALT) is a liver enzyme. It can go up when the liver is stressed, for example by fatty liver, alcohol or some medicines."),
    (("sgot", "ast"),
     "SGOT (AST) is an enzyme found in the liver and muscles. It is usually read together with SGPT."),
    (("bilirubin",),
     "Bilirubin is a yellow pigment made when old red blood cells break down. It is processed by the liver."),
    (("vitamin d", "vit d"),
     "Vitamin D supports bones and immunity. Low levels are very common; sunlight and diet help, and a doctor "
     "can advise if any supplement is needed."),
    (("b12",),
     "Vitamin B12 is needed for nerves and red blood cells. Low levels are common in vegetarians and can cause tiredness or tingling."),
    (("calcium",),
     "Calcium is important for bones, muscles and nerves. It is closely linked with vitamin D."),
]


def _has_word(text, word):
    return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text) is not None


def _glossary_for(text):
    """First glossary entry whose keyword appears in `text` (lowercase)."""
    text = (text or "").lower()
    for keys, info in GLOSSARY:
        if any(_has_word(text, k) for k in keys):
            return info
    return None


# ----------------------------------------------------------- FORMATTING ----

def _fmt(v):
    if v is None:
        return "n/a"
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_date(d):
    try:
        return d.strftime("%d %b %Y")
    except AttributeError:
        return str(d)


def _pct(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return round(((cur - prev) / abs(prev)) * 100, 1)


def _range_text(r):
    if r["ref_min"] is not None and r["ref_max"] is not None:
        return f"{_fmt(r['ref_min'])}-{_fmt(r['ref_max'])}"
    return "n/a"


def _direction(r):
    v = r["value"]
    if v is None:
        return ""
    if r["ref_min"] is not None and v < r["ref_min"]:
        return "low"
    if r["ref_max"] is not None and v > r["ref_max"]:
        return "high"
    return ""


def _status(r):
    if r["flag"] == "abnormal":
        return _direction(r) or "abnormal"
    if r["flag"] == "needs_review":
        return "needs review"
    return "normal"


def _line(r):
    unit = f" {r['unit']}" if r.get("unit") else ""
    return f"- {r['name']}: {_fmt(r['value'])}{unit} (range {_range_text(r)}) — {_status(r)}"


# -------------------------------------------------------------- CONTEXT ----

def build_health_context(user_name, reports, trending_tests):
    """
    reports: Report objects sorted oldest -> newest.
    Returns a plain dict with everything the assistant needs.
    """
    reports = reports[-MAX_REPORTS_IN_CONTEXT:]
    data = []
    for rep in reports:
        data.append({
            "date": rep.report_date,
            "results": [{
                "name": r.test_name,
                "value": r.value,
                "unit": r.unit,
                "ref_min": r.ref_min,
                "ref_max": r.ref_max,
                "flag": r.flag,
            } for r in rep.results],
        })
    return {
        "first_name": (user_name or "there").split(" ")[0],
        "reports": data,
        "trending": trending_tests or [],
    }


# ----------------------------------------------------------- RATE LIMIT ----

_hits = defaultdict(list)


def allow_request(user_id, limit=40, window_seconds=600):
    """Tiny in-memory limit, just to stop accidental spamming."""
    now = time.time()
    _hits[user_id] = [t for t in _hits[user_id] if now - t < window_seconds]
    if len(_hits[user_id]) >= limit:
        return False
    _hits[user_id].append(now)
    return True


# --------------------------------------------------------- INTENT WORDS ----

MEDICINE_WORDS = {
    "tablet", "tablets", "medicine", "medicines", "medication", "dose", "dosage",
    "dawai", "dawa", "syrup", "capsule", "capsules", "supplement", "supplements",
    "antibiotic", "antibiotics", "prescription", "injection",
}
GREETING_WORDS = {"hi", "hii", "hello", "hey", "namaste", "hlo", "yo"}
THANKS_WORDS = {"thanks", "thank", "thankyou", "shukriya", "dhanyavad", "thx"}
TREND_WORDS = {
    "trend", "trends", "trending", "increasing", "decreasing", "rising", "falling",
    "badh", "badhi", "badhna", "ghat", "ghati", "ghatna", "up", "down",
}
CHANGE_WORDS = {
    "change", "changed", "changes", "compare", "comparison", "difference",
    "previous", "pichli", "pichla", "badla", "badle", "improve", "improved",
    "improvement", "worse", "better", "since",
}
ABNORMAL_WORDS = {
    "abnormal", "high", "low", "range", "problem", "problems", "kharab", "galat",
    "bahar", "concern", "worry", "issue", "issues", "risky", "critical",
}
SUMMARY_WORDS = {
    "summary", "summarize", "summarise", "overall", "explain", "simple", "samjhao",
    "samjha", "kaisi", "kaisa", "kaise", "health", "status", "overview",
}

STOPWORDS = {
    "what", "which", "values", "value", "test", "tests", "report", "reports", "latest",
    "last", "previous", "about", "meri", "mera", "mere", "kya", "hai", "how", "with",
    "from", "this", "that", "have", "been", "show", "tell", "explain", "please",
    "normal", "abnormal", "high", "low", "trend", "trends", "change", "changed",
    "since", "kaisi", "kaisa", "kitna", "results", "result", "level", "levels",
    "count", "blood", "total", "serum", "my", "are", "the", "and", "for", "you",
    "can", "does", "did", "was", "when", "why", "who", "any", "all", "give", "into",
    "words", "simple", "words", "means", "mean", "not", "our", "your", "there",
}


def _tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


# ------------------------------------------------------- TEST MATCHING ----

def _all_test_names(ctx):
    seen, names = set(), []
    for rep in ctx["reports"]:
        for r in rep["results"]:
            if r["name"] not in seen:
                seen.add(r["name"])
                names.append(r["name"])
    return names


def _matching_tests(ctx, message):
    """Test names in the user's data that the message is talking about."""
    msg = message.lower()
    tokens = [t for t in _tokens(msg) if len(t) >= 3 and t not in STOPWORDS]
    matched = []

    for name in _all_test_names(ctx):
        lname = name.lower()
        hit = False

        if lname in msg:
            hit = True
        else:
            # glossary keys shared between the message and the test name
            for keys, _ in GLOSSARY:
                if any(_has_word(msg, k) for k in keys) and any(_has_word(lname, k) for k in keys):
                    hit = True
                    break
            # plain word overlap (e.g. "calcium", "sodium")
            if not hit:
                name_tokens = set(_tokens(lname))
                if any(t in name_tokens for t in tokens):
                    hit = True

        if hit:
            matched.append(name)

    return matched[:4]


# -------------------------------------------------------------- ANSWERS ----

def _answer_abnormal(ctx):
    latest = ctx["reports"][-1]
    bad = [r for r in latest["results"] if r["flag"] == "abnormal"]
    review = [r for r in latest["results"] if r["flag"] == "needs_review"]

    if not bad and not review:
        return (f"Good news, {ctx['first_name']}! All {len(latest['results'])} values in your latest report "
                f"({_fmt_date(latest['date'])}) are within their reference ranges.")

    lines = [f"In your latest report ({_fmt_date(latest['date'])}):"]
    if bad:
        lines.append(f"{len(bad)} value(s) are outside the reference range:")
        lines += [_line(r) for r in bad]
    if review:
        lines.append(f"{len(review)} value(s) need a manual review (the range or value could not be read clearly):")
        lines += [_line(r) for r in review]
    lines.append("Please discuss these with your doctor. This is not a diagnosis.")
    return "\n".join(lines)


def _answer_trends(ctx):
    if len(ctx["reports"]) < 3:
        return ("Trend detection needs at least 3 reports, and you have "
                f"{len(ctx['reports'])} so far. Upload more reports and I can spot trends for you.")

    trending = ctx["trending"]
    if not trending:
        return ("I checked your last 3 reports and no test is moving steadily in one direction right now.")

    lines = ["These tests moved in the same direction across your last 3 reports:"]
    for t in trending:
        pct = f", {t['pct']:+g}%" if t.get("pct") is not None else ""
        lines.append(f"- {t['test_name']} is {t['direction']} ({_fmt(t['first_value'])} → {_fmt(t['latest_value'])}{pct})")
    lines.append("A steady trend is worth mentioning to your doctor, even if the values are still inside the range.")
    return "\n".join(lines)


def _answer_changes(ctx):
    reps = ctx["reports"]
    if len(reps) < 2:
        return ("You have only one report so far, so there is nothing to compare yet. "
                "Upload another report and ask me again.")

    prev, cur = reps[-2], reps[-1]
    prev_map = {r["name"]: r for r in prev["results"]}
    better, worse, moved = [], [], []

    for r in cur["results"]:
        p = prev_map.get(r["name"])
        if not p:
            continue
        pct = _pct(r["value"], p["value"])
        unit = f" {r['unit']}" if r.get("unit") else ""
        pct_txt = f" ({pct:+g}%)" if pct is not None else ""
        line = f"- {r['name']}: {_fmt(p['value'])} → {_fmt(r['value'])}{unit}{pct_txt}"

        if p["flag"] == "abnormal" and r["flag"] != "abnormal":
            better.append(line)
        elif p["flag"] != "abnormal" and r["flag"] == "abnormal":
            worse.append(line)
        elif pct is not None and abs(pct) >= 10:
            moved.append(line)

    lines = [f"Comparing {_fmt_date(cur['date'])} with your previous report ({_fmt_date(prev['date'])}):"]
    if better:
        lines.append("Back within range:")
        lines += better
    if worse:
        lines.append("Newly out of range:")
        lines += worse
    if moved:
        lines.append("Other changes of 10% or more:")
        lines += moved
    if not (better or worse or moved):
        lines.append("No major changes — your values look fairly stable.")
    return "\n".join(lines)


def _answer_summary(ctx):
    latest = ctx["reports"][-1]
    results = latest["results"]
    bad = [r for r in results if r["flag"] == "abnormal"]
    review = [r for r in results if r["flag"] == "needs_review"]

    lines = [
        f"Quick overview of your latest report ({_fmt_date(latest['date'])}):",
        f"- {len(results)} tests checked, {len(bad)} abnormal, {len(review)} need review",
    ]
    if bad:
        parts = [f"{r['name']} ({_status(r)})" for r in bad[:6]]
        lines.append("- Out of range: " + ", ".join(parts))
    else:
        lines.append("- Everything is within the reference range")
    if ctx["trending"]:
        names = ", ".join(t["test_name"] for t in ctx["trending"][:5])
        lines.append(f"- Steady trends: {names}")
    lines.append("Ask me about any single test, for example: \"How is my hemoglobin?\"")
    lines.append("This is a tracking summary, not a diagnosis. Please check with your doctor.")
    return "\n".join(lines)


def _answer_test(ctx, name):
    history = []
    last = None
    for rep in ctx["reports"]:
        for r in rep["results"]:
            if r["name"] == name:
                history.append((rep["date"], r))
                last = (rep["date"], r)
    if not last:
        return None

    date, r = last
    unit = f" {r['unit']}" if r.get("unit") else ""
    lines = [
        f"{name} — latest value ({_fmt_date(date)}): {_fmt(r['value'])}{unit}",
        f"- Reference range: {_range_text(r)}",
        f"- Status: {_status(r)}",
    ]

    if len(history) >= 2:
        prev_date, prev_r = history[-2]
        pct = _pct(r["value"], prev_r["value"])
        pct_txt = f" ({pct:+g}%)" if pct is not None else ""
        lines.append(f"- Previous ({_fmt_date(prev_date)}): {_fmt(prev_r['value'])}{unit}{pct_txt}")
        if len(history) >= 3:
            recent = "; ".join(f"{_fmt_date(d)}: {_fmt(x['value'])}" for d, x in history[-5:])
            lines.append(f"- Recent history: {recent}")

    info = _glossary_for(name)
    if info:
        lines.append(info)
    if r["flag"] == "abnormal":
        lines.append("Since this is outside the range, please discuss it with your doctor.")
    return "\n".join(lines)


def _answer_medicine():
    return ("I can't advise on medicines, tablets or doses — that needs a doctor who knows your full health "
            "history. What I can do is show your values and trends, so you can share them with your doctor.")


def _answer_greeting(ctx):
    return (f"Hi {ctx['first_name']}! I can answer questions about your uploaded reports. You can ask me:\n"
            "- Which values are abnormal?\n"
            "- What changed since my previous report?\n"
            "- Which values are trending up or down?\n"
            "- How is my hemoglobin? (any test name)")


def _answer_fallback(ctx):
    return ("I'm not sure I understood that. I can help with questions about your reports, for example:\n"
            "- Which values are abnormal?\n"
            "- What changed since my previous report?\n"
            "- Which values are trending up or down?\n"
            "- How is my glucose? (or any test name)\n"
            "- What is hemoglobin?")


# ------------------------------------------------- DIET / LIFESTYLE TIPS ----
# General, non-diagnostic wellness tips only. No medicines, no doses,
# no personal diet plan. Order matters: more specific entries first.

LIFESTYLE = [
    (("hdl",), {
        "low": [
            "Do brisk walking or another activity for about 30 minutes on most days.",
            "Avoid smoking.",
            "Choose healthy fats in moderation: nuts, seeds, mustard or olive oil.",
            "Cut down refined carbs and sugary foods.",
        ],
        "high": ["A higher HDL is generally considered good. Keep up regular activity and healthy fats."],
    }),
    (("ldl",), {
        "high": [
            "Reduce fried food, bakery items, processed snacks and excess ghee/butter.",
            "Add fibre: oats, fruits, vegetables, pulses and whole grains.",
            "Include nuts and seeds in small portions.",
            "Exercise regularly, around 30 minutes on most days.",
        ],
    }),
    (("triglyceride", "triglycerides", "tg"), {
        "high": [
            "Cut down sugar, sweets, sugary drinks and refined carbs (maida, white bread).",
            "Limit alcohol.",
            "Add fibre and omega-3 sources such as walnuts, flaxseed and fish (if you eat it).",
            "Stay active most days of the week.",
        ],
    }),
    (("cholesterol", "chol"), {
        "high": [
            "Reduce fried food, bakery items, processed snacks and excess ghee/butter.",
            "Add fibre: oats, fruits, vegetables, pulses and whole grains.",
            "Exercise regularly, around 30 minutes on most days.",
            "Avoid smoking.",
        ],
    }),
    (("glucose", "sugar", "fbs", "rbs", "ppbs", "hba1c", "a1c"), {
        "high": [
            "Cut down sugary drinks, sweets and refined foods (maida, white bread, white rice in large portions).",
            "Choose whole grains, millets, pulses and plenty of vegetables.",
            "Take a 15 to 30 minute walk after meals when you can.",
            "Keep regular meal timings and avoid skipping meals.",
        ],
        "low": [
            "Do not skip meals; eat regular, balanced meals.",
            "If you often feel shaky, sweaty or dizzy, tell your doctor soon.",
        ],
    }),
    (("hemoglobin", "haemoglobin", "hb", "hgb", "rbc"), {
        "low": [
            "Eat iron-rich foods: leafy greens, lentils, beans, chickpeas, dates, jaggery, nuts, and eggs or lean meat if you eat them.",
            "Add vitamin C (lemon, amla, orange, guava) to meals; it helps iron absorption.",
            "Keep tea and coffee away from meal time, as they reduce iron absorption.",
            "Ask your doctor before starting any iron supplement.",
        ],
        "high": [
            "Drink enough water through the day.",
            "Avoid smoking.",
            "High values have several possible causes, so a doctor's review is important.",
        ],
    }),
    (("creatinine", "urea", "bun"), {
        "high": [
            "Drink enough water through the day, unless your doctor has limited your fluids.",
            "Avoid self-medicating with painkillers.",
            "Do not start heavy protein supplements without medical advice.",
            "Kidney values need a doctor's review; diet alone is not enough.",
        ],
    }),
    (("uric",), {
        "high": [
            "Drink plenty of water.",
            "Limit red meat, organ meat, alcohol (especially beer) and sugary drinks.",
            "Maintain a healthy weight with regular activity.",
        ],
    }),
    (("sgpt", "sgot", "alt", "ast", "bilirubin"), {
        "high": [
            "Avoid alcohol.",
            "Reduce fried, oily and heavily processed food and sugary drinks.",
            "Maintain a healthy weight with regular activity.",
            "Avoid self-medication and unnecessary supplements.",
        ],
    }),
    (("vitamin d", "vit d"), {
        "low": [
            "Get some safe daily sunlight, as your doctor advises.",
            "Foods that help: eggs, fish, fortified milk or cereals, mushrooms.",
            "Ask your doctor whether a supplement is needed and at what dose.",
        ],
    }),
    (("b12",), {
        "low": [
            "B12 comes mainly from animal foods: milk, curd, paneer, eggs, fish.",
            "Vegetarians and vegans often need a doctor-guided supplement.",
        ],
    }),
    (("calcium",), {
        "low": [
            "Include milk, curd, paneer, ragi, sesame seeds and leafy greens.",
            "Vitamin D helps calcium absorption, so ask your doctor about that too.",
        ],
    }),
    (("tsh",), {
        "any": [
            "Thyroid values need a doctor's review; diet alone cannot correct them.",
            "Keep your follow-up tests on schedule.",
        ],
    }),
    (("platelet", "platelets", "plt"), {
        "any": [
            "Stay hydrated and rest well.",
            "Avoid self-medicating with painkillers.",
            "Platelet changes need a doctor's evaluation, so get it checked soon.",
        ],
    }),
    (("wbc", "tlc"), {
        "any": [
            "Rest, drink enough fluids and eat light, balanced meals.",
            "WBC changes are often linked with infection or inflammation; a doctor should review it.",
        ],
    }),
]

GENERIC_TIPS = {
    "high": [
        "Drink enough water and eat balanced meals with plenty of vegetables.",
        "Stay active most days and sleep well.",
        "Share this value with your doctor.",
    ],
    "low": [
        "Eat regular, balanced meals and stay hydrated.",
        "Sleep well and avoid skipping meals.",
        "Share this value with your doctor.",
    ],
    "any": [
        "Eat balanced meals, drink enough water and stay active.",
        "Share this value with your doctor.",
    ],
}

GENERAL_HABITS = [
    "Drink enough water through the day.",
    "Eat more vegetables, fruits, pulses and whole grains; cut down on sugar and fried food.",
    "Move for about 30 minutes on most days.",
    "Sleep 7 to 8 hours and manage stress.",
    "Get regular check-ups so your doctor can follow your progress.",
]

LIFESTYLE_WORDS = {
    "diet", "food", "foods", "eat", "eating", "khana", "khane", "khaun", "exercise",
    "exercises", "workout", "lifestyle", "tips", "tip", "plan", "precaution",
    "precautions", "avoid",
}
LIFESTYLE_PHRASES = (
    "what should i do", "kya karu", "kya karna", "how to improve", "how can i improve",
    "how to reduce", "how to lower", "how to increase", "how to control",
    "kaise sudharu", "kaise kam", "kaise badhau",
)


def _tips_for(name, direction):
    lname = name.lower()
    for keys, tips in LIFESTYLE:
        if any(_has_word(lname, k) for k in keys):
            return tips.get(direction) or tips.get("any")
    return None


def _answer_lifestyle(ctx, tests):
    latest_map = {}
    for rep in ctx["reports"]:
        for r in rep["results"]:
            latest_map[r["name"]] = r

    if tests:
        targets = [latest_map[t] for t in tests if t in latest_map]
    else:
        targets = [r for r in ctx["reports"][-1]["results"] if r["flag"] == "abnormal"][:4]

    lines = []
    if not targets:
        lines.append("All your latest values are within range, so no specific changes are needed. "
                     "These general habits help keep it that way:")
        lines += ["- " + h for h in GENERAL_HABITS]
    else:
        for r in targets:
            unit = f" {r['unit']}" if r.get("unit") else ""
            value = f"{_fmt(r['value'])}{unit}"
            if r["flag"] == "abnormal":
                d = _direction(r)
                tips = _tips_for(r["name"], d) or GENERIC_TIPS.get(d) or GENERIC_TIPS["any"]
                lines.append(f"{r['name']} ({value}, {d or 'outside range'}) — general tips:")
                lines += ["- " + t for t in tips]
            elif r["flag"] == "needs_review":
                lines.append(f"{r['name']} ({value}) could not be read clearly, so please check it with your doctor.")
            else:
                lines.append(f"{r['name']} ({value}) is within its range, so nothing to fix. "
                             "Keep up balanced meals, regular activity and good sleep.")
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()

    lines.append("")
    lines.append("These are general wellness tips, not a personal diet or exercise plan. "
                 "For a plan made for you, please see your doctor or a dietitian.")
    return "\n".join(lines)


# ----------------------------------------------------------------- MAIN ----

def ask_assistant(context, history, question):
    """
    context: dict from build_health_context().
    history: ignored (each question is answered from the data directly).
    question: user's message.
    """
    ctx = context
    if not ctx or not ctx.get("reports"):
        raise ChatError("Upload at least one report first, then ask me about it.")

    msg = (question or "").strip().lower()
    tokens = set(_tokens(msg))
    if not tokens:
        raise ChatError("Please type a question.")

    # 1. Medicine / dose questions: never answer
    if tokens & MEDICINE_WORDS or "should i take" in msg or "can i take" in msg:
        return _answer_medicine()

    # 2. Small talk
    if tokens & THANKS_WORDS and len(tokens) <= 4:
        return "You're welcome! Ask me anything else about your reports."
    if tokens & GREETING_WORDS and len(tokens) <= 3:
        return _answer_greeting(ctx)

    # 2b. Diet / lifestyle questions. Checked before the test lookup so that
    #     "how to improve my hemoglobin" gives tips, not just the value.
    if (tokens & LIFESTYLE_WORDS) or any(p in msg for p in LIFESTYLE_PHRASES):
        return _answer_lifestyle(ctx, _matching_tests(ctx, msg))

    # 3. Specific test(s) from the user's own reports
    tests = _matching_tests(ctx, msg)
    if tests:
        parts = [a for a in (_answer_test(ctx, t) for t in tests) if a]
        if parts:
            return "\n\n".join(parts)

    # 4. General explanation of a test that is not in the user's reports
    if any(w in tokens for w in ("what", "explain", "meaning", "mean", "means", "kya")):
        info = _glossary_for(msg)
        if info:
            return info + "\n(This test is not in your uploaded reports, so I have no value to show.)"

    # 5. Report-level questions
    if tokens & TREND_WORDS:
        return _answer_trends(ctx)
    if tokens & CHANGE_WORDS:
        return _answer_changes(ctx)
    if tokens & ABNORMAL_WORDS:
        return _answer_abnormal(ctx)
    if tokens & SUMMARY_WORDS:
        return _answer_summary(ctx)

    return _answer_fallback(ctx)


# ------------------------------------------------------- FOLLOW-UPS ----

def get_suggestions(context, question, history=None, limit=4):
    """Return up to `limit` follow-up questions the assistant can answer,
    skipping anything the user has already asked."""
    ctx = context
    if not ctx or not ctx.get("reports"):
        return []

    reps = ctx["reports"]
    latest = reps[-1]["results"]
    q = (question or "").strip().lower()

    asked = {q}
    for h in (history or []):
        if isinstance(h, dict) and h.get("role") == "user" and isinstance(h.get("content"), str):
            asked.add(h["content"].strip().lower())

    tokens = set(_tokens(q))
    talked = {t.lower() for t in _matching_tests(ctx, q)}

    abnormal = [r["name"] for r in latest if r["flag"] == "abnormal"]
    trending = [t["test_name"] for t in ctx["trending"]]
    others = [r["name"] for r in latest if r["name"] not in abnormal]

    def drill(names):
        return [f"How is my {n}?" for n in names if n.lower() not in talked]

    g_abn = ["Which values are abnormal in my latest report?"]
    g_chg = ["What changed since my previous report?"] if len(reps) >= 2 else []
    g_trd = ["Which values are trending up or down?"] if len(reps) >= 3 else []
    g_sum = ["Explain my results in simple words"]
    g_life = [f"Diet and lifestyle tips for my {n}" for n in abnormal if n.lower() not in talked][:2]

    if talked:
        groups = [g_chg, g_trd, g_life, drill(abnormal), drill(others), g_sum, g_abn]
    elif tokens & TREND_WORDS:
        groups = [drill(trending), g_abn, g_chg, g_life, drill(abnormal), g_sum]
    elif tokens & CHANGE_WORDS:
        groups = [g_abn, g_trd, g_life, drill(abnormal), g_sum, drill(others)]
    elif tokens & ABNORMAL_WORDS:
        groups = [drill(abnormal), g_life, g_trd, g_chg, g_sum, drill(others)]
    else:
        groups = [g_abn, g_chg, g_trd, g_life, drill(abnormal), drill(others)]

    picked, idx = [], 0
    while len(picked) < limit and any(idx < len(g) for g in groups):
        for g in groups:
            if idx < len(g):
                s = g[idx]
                if s.lower() not in asked and s not in picked:
                    picked.append(s)
                    if len(picked) == limit:
                        break
        idx += 1
    return picked