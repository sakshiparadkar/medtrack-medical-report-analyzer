import os
import re
import math
from datetime import datetime, date, timedelta
from collections import defaultdict
from io import BytesIO

from sqlalchemy import or_, and_

from flask import Flask, render_template, request, redirect, url_for, flash, abort, send_file, jsonify
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from werkzeug.utils import secure_filename
from markupsafe import Markup, escape
import plotly.graph_objects as go
from plotly.offline import plot as plotly_plot

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


from models import db, User, Report, TestResult, Goal
from analytics import SEVERITY_LABELS, SEVERITY_COLORS, classify_severity, analyze_series
from extract.pdf_extract import extract_text, OCRUnavailableError
from extract.nlp_extract import extract_test_results

from dotenv import load_dotenv
load_dotenv()

from chat_assistant import build_health_context, ask_assistant, allow_request, ChatError

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "instance", "uploads")
ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = "medtrack-fyp-secret-key-change-me"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'medtrack.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

db.init_app(app)

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "info"
login_manager.init_app(app)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ------------------------------------------------------- SHARED HELPERS ----

def compute_percentage_change(current_value, previous_value):
    """Pure math — no medical interpretation. Returns None if it can't be computed."""
    if current_value is None or previous_value in (None, 0):
        return None
    return round(((current_value - previous_value) / abs(previous_value)) * 100, 1)


def get_previous_result(user_id, test_name, before_date):
    """Most recent prior result for this test, before the given report date."""
    return (TestResult.query
            .join(Report)
            .filter(Report.user_id == user_id,
                    TestResult.test_name == test_name,
                    Report.report_date < before_date)
            .order_by(Report.report_date.desc())
            .first())


def group_results_by_test(reports):
    """{test_name: [(report_date, value, ref_min, ref_max, unit, flag), ...]} sorted by date."""
    by_test = defaultdict(list)
    for report in reports:
        for result in report.results:
            by_test[result.test_name].append(
                (report.report_date, result.value, result.ref_min, result.ref_max, result.unit, result.flag)
            )
    for rows in by_test.values():
        rows.sort(key=lambda r: r[0])
    return by_test


def detect_trending_tests(reports):
    """
    reports: list of Report objects (any order).
    Flags tests whose value has moved in the same direction across the last
    3 reports that contain a value for that test. Simple slope check only —
    no diagnosis, just a pattern flag.
    Returns: list of dicts with test_name, direction, points_considered,
             first_value, latest_value, pct
    """
    by_test = defaultdict(list)
    for report in reports:
        for result in report.results:
            by_test[result.test_name].append((report.report_date, result.value))

    trending = []
    for test_name, rows in by_test.items():
        rows.sort(key=lambda r: r[0])
        if len(rows) < 3:
            continue
        last_three = rows[-3:]
        v1, v2, v3 = last_three[0][1], last_three[1][1], last_three[2][1]

        direction = None
        if v1 < v2 < v3:
            direction = "increasing"
        elif v1 > v2 > v3:
            direction = "decreasing"

        if direction:
            trending.append({
                "test_name": test_name,
                "direction": direction,
                "points_considered": 3,
                "first_value": v1,
                "latest_value": v3,
                "pct": compute_percentage_change(v3, v1),
            })

    return trending


def generate_suggestions(latest_report, trending_tests):
    """
    Generic, safe, non-diagnostic lifestyle suggestions shown on the PDF
    summary. Deliberately does not name specific test values — just general
    wellness pointers that are relevant to nearly anyone.
    """
    do_now = [
        "Drink enough water through the day to stay well hydrated.",
        "Include more fresh fruits and vegetables in your daily meals.",
        "Try to get some form of daily movement — a walk, yoga, or light exercise.",
        "Practice a few minutes of meditation or deep breathing to manage stress.",
        "Aim for consistent, good-quality sleep every night.",
        "Keep up with regular check-ups so your doctor can track your progress.",
    ]

    avoid = [
        "Avoid skipping meals or going long hours without eating.",
        "Avoid excess sugar, fried, and heavily processed foods.",
        "Avoid smoking and limit alcohol intake.",
        "Avoid a sedentary lifestyle — try not to sit for very long stretches without moving.",
        "Avoid self-medicating or changing any treatment based only on this summary — always consult your doctor.",
    ]

    return do_now[:4], avoid[:4]


def build_highlighted_raw_text(raw_text, results):
    """
    Escape the raw report text for safe HTML rendering, then wrap any line
    that contains an abnormal test's name in a <span> so it can be styled
    red in the template. Returns a Markup object (safe to render unescaped).
    """
    if not raw_text:
        return Markup("")

    escaped = str(escape(raw_text))

    for r in results:
        if r.flag != "abnormal":
            continue
        pattern = re.compile(
            r"^.*" + re.escape(r.test_name) + r".*$",
            re.IGNORECASE | re.MULTILINE
        )
        escaped = pattern.sub(
            lambda m: f'<span class="mt-raw-abnormal">{m.group(0)}</span>',
            escaped
        )

    return Markup(escaped)

# ---------------------------------------------------------------- AUTH ----

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("Please fill in all fields.", "danger")
            return redirect(url_for("signup"))

        if User.query.filter_by(email=email).first():
            flash("An account with this email already exists.", "danger")
            return redirect(url_for("signup"))

        user = User(name=name, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        flash("Account created. Welcome to MedTrack!", "success")
        return redirect(url_for("dashboard"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "danger")
        return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/quiz")

def quiz():
    return render_template("quiz.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------- HOME ----

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("index.html")

@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/health-insights")
def health_insights():
    return render_template("health_insights.html")



# -------------------------------------------------------------- UPLOAD ----
@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        file = request.files.get("report_file")
        report_date_str = request.form.get("report_date")

        if not file or file.filename == "":
            flash("Please choose a PDF or a photo of your report to upload.", "danger")
            return redirect(url_for("upload"))

        if not allowed_file(file.filename):
            flash("Only PDF, JPG and PNG files are supported.", "danger")
            return redirect(url_for("upload"))

        try:
            report_date = datetime.strptime(report_date_str, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            report_date = date.today()

        safe_name = secure_filename(file.filename)
        stored_name = f"{current_user.id}_{int(datetime.utcnow().timestamp())}_{safe_name}"
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
        file.save(filepath)

        try:
            raw_text = extract_text(filepath)
        except OCRUnavailableError:
            os.remove(filepath)
            flash("This file needs text recognition (OCR), but OCR is not set up on this "
                  "computer yet. Please install Tesseract, or upload a digital PDF.", "danger")
            return redirect(url_for("upload"))
        except Exception:
            os.remove(filepath)
            flash("Sorry, this file could not be read. Please try a clearer PDF or photo.", "danger")
            return redirect(url_for("upload"))

        extracted = extract_test_results(raw_text)

        report = Report(
            user_id=current_user.id,
            filename=stored_name,
            original_name=safe_name,
            report_date=report_date,
            raw_text=raw_text,
        )
        db.session.add(report)
        db.session.flush()

        for item in extracted:
            db.session.add(TestResult(report_id=report.id, **item))

        db.session.commit()

        if extracted:
            flash(f"Report processed successfully — {len(extracted)} test values extracted.", "success")
        else:
            flash("Report uploaded, but no known test values could be extracted. "
                  "Try a clearer PDF, or a sharper, well-lit photo.", "warning")

        recent_reports = (
            Report.query.filter_by(user_id=current_user.id)
            .order_by(Report.uploaded_at.desc())
            .limit(5)
            .all()
        )

        return render_template(
            "upload.html",
            success=True,
            report=report,
            extracted_count=len(extracted),
            recent_reports=recent_reports,
        )

    recent_reports = (
        Report.query.filter_by(user_id=current_user.id)
        .order_by(Report.uploaded_at.desc())
        .limit(5)
        .all()
    )
    return render_template("upload.html", recent_reports=recent_reports)


# ------------------------------------------------------------ DASHBOARD ----

def build_trend_chart(test_name, rows, analysis=None):
    """rows: list of (report_date, value, ref_min, ref_max) sorted by date."""
    dates = [r[0] for r in rows]
    values = [r[1] for r in rows]
    ref_min = rows[-1][2]
    ref_max = rows[-1][3]

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=dates, y=values, mode="lines+markers", name=test_name,
        line=dict(color="#1F6F63", width=3),
        marker=dict(size=8, color="#1F6F63"),
    ))

    if ref_min is not None and ref_max is not None:
        fig.add_hrect(y0=ref_min, y1=ref_max,
                      fillcolor="#1F6F63", opacity=0.08, line_width=0)

    if analysis:
        # red ring around values outside the user's own pattern
        flagged = [(d, v) for d, v, c in zip(dates, values, analysis["checks"])
                   if c["is_anomaly"]]
        if flagged:
            fig.add_trace(go.Scatter(
                x=[d for d, _ in flagged], y=[v for _, v in flagged],
                mode="markers", hoverinfo="skip",
                marker=dict(size=18, color="rgba(0,0,0,0)",
                            line=dict(color=SEVERITY_COLORS["severe"], width=3)),
            ))

        # dotted line to the expected next value
        pred = analysis["prediction"]
        if pred:
            fig.add_trace(go.Scatter(
                x=[dates[-1], pred["date"]], y=[values[-1], pred["value"]],
                mode="lines+markers",
                line=dict(color="#1F6F63", width=2, dash="dot"),
                marker=dict(size=[0, 10], symbol="circle-open", line=dict(width=2)),
                hovertemplate="Expected: %{y:.1f}<extra></extra>",
            ))

    fig.update_layout(
        title=test_name,
        margin=dict(l=40, r=20, t=40, b=30),
        height=280,
        template="plotly_white",
        showlegend=False,
        font=dict(family="Inter, sans-serif", size=12, color="#213547"),
    )
    return plotly_plot(fig, output_type="div", include_plotlyjs=False)


def build_latest_bar_chart(latest_results):
    names = [r.test_name for r in latest_results]
    values = [r.value for r in latest_results]
    colors_ = ["#E4572E" if r.flag == "abnormal" else
               "#E8A33D" if r.flag == "needs_review" else
               "#1F6F63" for r in latest_results]

    fig = go.Figure(go.Bar(x=names, y=values, marker_color=colors_))
    fig.update_layout(
        title="Latest Report — All Test Values",
        margin=dict(l=40, r=20, t=40, b=80),
        height=340,
        template="plotly_white",
        font=dict(family="Inter, sans-serif", size=12, color="#213547"),
        xaxis_tickangle=-35,
    )
    return plotly_plot(fig, output_type="div", include_plotlyjs=False)


def build_normalized_chart(latest_results):
    """Each test on one 0-1 scale: 0 = lower limit, 1 = upper limit."""
    counts = {key: 0 for key in SEVERITY_LABELS}
    names, xs, cols, hover = [], [], [], []

    for r in latest_results:
        sev = classify_severity(r.value, r.ref_min, r.ref_max)
        if sev is None:
            continue
        counts[sev["level"]] += 1
        names.append(r.test_name)
        xs.append(max(-0.5, min(1.5, sev["norm"])))   # keep far-out dots on the chart
        cols.append(SEVERITY_COLORS[sev["level"]])
        hover.append(f"{r.test_name}: {r.value:g} {r.unit or ''}"
                     f"<br>{SEVERITY_LABELS[sev['level']]}"
                     + (f" ({sev['deviation_pct']}% {sev['direction']})" if sev["level"] != "normal" else ""))

    if not names:
        return None, counts

    fig = go.Figure(go.Scatter(
        x=xs, y=names, mode="markers",
        marker=dict(size=14, color=cols, line=dict(color="white", width=1.5)),
        hovertext=hover, hoverinfo="text",
    ))
    fig.add_vrect(x0=0, x1=1, fillcolor="#0F766E", opacity=0.10, line_width=0)
    fig.update_layout(
        margin=dict(l=40, r=20, t=20, b=40),
        height=max(260, 36 * len(names) + 100),
        template="plotly_white",
        showlegend=False,
        xaxis=dict(range=[-0.6, 1.6], tickvals=[0, 1],
                   ticktext=["Lower limit", "Upper limit"], zeroline=False),
        yaxis=dict(autorange="reversed"),
        font=dict(family="Inter, sans-serif", size=12, color="#213547"),
    )
    return plotly_plot(fig, output_type="div", include_plotlyjs=False), counts


# ---------------------------------------------------------------- GOALS ----

def get_latest_result(user_id, test_name):
    """Most recent value of this test across all of the user's reports."""
    return (TestResult.query
            .join(Report)
            .filter(Report.user_id == user_id, TestResult.test_name == test_name)
            .order_by(Report.report_date.desc(), TestResult.id.desc())
            .first())


def goal_progress(goal, current):
    """Returns (percent 0-100, goal_met)."""
    if goal.direction == "below":
        met = current <= goal.target_value
        gap = goal.start_value - goal.target_value
        done = goal.start_value - current
    else:
        met = current >= goal.target_value
        gap = goal.target_value - goal.start_value
        done = current - goal.start_value

    if met:
        return 100, True
    if gap <= 0:  # started inside the target but has slipped out of it
        return 0, False
    return max(0, min(99, round(done / gap * 100))), False


def build_goal_cards(user_id):
    goals = (Goal.query.filter_by(user_id=user_id)
             .order_by(Goal.created_at.desc()).all())
    cards = []
    for g in goals:
        latest = get_latest_result(user_id, g.test_name)
        if latest is None:
            continue
        pct, met = goal_progress(g, latest.value)
        left = (latest.value - g.target_value) if g.direction == "below" \
            else (g.target_value - latest.value)
        cards.append({
            "id": g.id,
            "test_name": g.test_name,
            "direction": g.direction,
            "target": f"{g.target_value:g}",
            "start": f"{g.start_value:g}",
            "current": f"{latest.value:g}",
            "unit": latest.unit or "",
            "left": f"{max(left, 0):g}",
            "pct": pct,
            "met": met,
        })
    return cards


def build_health_score(reports):
    """
    Simple score: share of tests in the latest report that are inside their
    normal range. Also compares with the report before it.
    """
    def score_of(report):
        total = len(report.results)
        if not total:
            return None
        ok = sum(1 for r in report.results if r.flag == "normal")
        return ok, total, round(ok / total * 100)

    if not reports:
        return None
    current = score_of(reports[-1])
    if current is None:
        return None

    ok, total, pct = current
    if pct >= 80:
        color, label = "#0F766E", "Looking good"
    elif pct >= 60:
        color, label = "#EAB308", "Mostly in range"
    else:
        color, label = "#DC2626", "Needs attention"

    delta = None
    if len(reports) >= 2:
        previous = score_of(reports[-2])
        if previous is not None:
            delta = pct - previous[2]

    return {"pct": pct, "ok": ok, "total": total,
            "color": color, "label": label, "delta": delta}


@app.route("/dashboard")
@login_required
def dashboard():
    reports = (Report.query
               .filter_by(user_id=current_user.id)
               .order_by(Report.report_date.asc())
               .all())

    total_reports = len(reports)
    total_abnormal = sum(r.abnormal_count for r in reports)
    total_review = sum(r.review_count for r in reports)
    last_upload = reports[-1].report_date if reports else None

    by_test = defaultdict(list)
    for report in reports:
        for result in report.results:
            by_test[result.test_name].append(
                (report.report_date, result.value, result.ref_min, result.ref_max)
            )

    latest_date = reports[-1].report_date if reports else None
    trend_charts, anomalies = [], []
    for test_name, rows in by_test.items():
        rows.sort(key=lambda r: r[0])
        if len(rows) < 2:
            continue
        analysis = analyze_series(rows)
        trend_charts.append(build_trend_chart(test_name, rows, analysis))

        # banner: only if the LATEST report's value is outside the usual pattern
        last_check = analysis["checks"][-1]
        if last_check["is_anomaly"] and rows[-1][0] == latest_date:
            anomalies.append({
                "test_name": test_name,
                "value": f"{rows[-1][1]:g}",
                "expected": f"{last_check['expected']:.1f}",
                "direction": "higher" if last_check["z"] > 0 else "lower",
                "z": last_check["z"],
            })

    normalized_chart, severity_counts = None, {k: 0 for k in SEVERITY_LABELS}
    if reports and reports[-1].results:
        normalized_chart, severity_counts = build_normalized_chart(reports[-1].results)

    goal_cards = build_goal_cards(current_user.id)
    goal_test_names = sorted(by_test.keys())

    health = build_health_score(reports)
    has_demo = any(r.filename.startswith(f"demo_{current_user.id}_") for r in reports)

    recent_reports = list(reversed(reports))[:8]
    trending_tests = detect_trending_tests(reports)

    return render_template(
        "dashboard.html",
        total_reports=total_reports,
        total_abnormal=total_abnormal,
        total_review=total_review,
        last_upload=last_upload,
        trend_charts=trend_charts,
        anomalies=anomalies,
        normalized_chart=normalized_chart,
        severity_counts=severity_counts,
        severity_labels=SEVERITY_LABELS,
        goal_cards=goal_cards,
        goal_test_names=goal_test_names,
        health=health,
        has_demo=has_demo,
        recent_reports=recent_reports,
        has_reports=bool(reports),
        trending_tests=trending_tests,
    )


@app.route("/goals/add", methods=["POST"])
@login_required
def add_goal():
    test_name = (request.form.get("test_name") or "").strip()
    direction = request.form.get("direction")
    try:
        target = parse_float(request.form.get("target_value"))
    except ValueError:
        target = None

    if direction not in ("below", "above") or target is None:
        flash("Please choose a test, a direction and a valid target number.", "danger")
        return redirect(url_for("dashboard"))

    latest = get_latest_result(current_user.id, test_name)
    if latest is None:
        flash("That test was not found in your reports.", "danger")
        return redirect(url_for("dashboard"))

    # one goal per test: replace the old one if it exists
    Goal.query.filter_by(user_id=current_user.id, test_name=latest.test_name).delete()
    db.session.add(Goal(
        user_id=current_user.id,
        test_name=latest.test_name,
        direction=direction,
        target_value=target,
        start_value=latest.value,
    ))
    db.session.commit()
    flash(f"Goal saved: {latest.test_name} {direction} {target:g}.", "success")
    return redirect(url_for("dashboard"))


@app.route("/goals/<int:goal_id>/delete", methods=["POST"])
@login_required
def delete_goal(goal_id):
    goal = db.session.get(Goal, goal_id)
    if not goal or goal.user_id != current_user.id:
        abort(404)
    db.session.delete(goal)
    db.session.commit()
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------- DEMO DATA ----

# name, unit, ref_min, ref_max, five values from oldest to newest report
DEMO_TESTS = [
    ("Hemoglobin", "g/dL", 12.0, 16.0, [11.2, 11.6, 12.1, 12.6, 13.0]),
    ("Fasting Glucose", "mg/dL", 70.0, 100.0, [92, 95, 94, 93, 128]),
    ("Total Cholesterol", "mg/dL", 125.0, 200.0, [184, 191, 198, 205, 212]),
    ("Vitamin D", "ng/mL", 30.0, 100.0, [19, 23, 28, 33, 37]),
    ("TSH", "uIU/mL", 0.4, 4.0, [2.1, 2.3, 2.0, 2.2, 2.1]),
    ("Creatinine", "mg/dL", 0.6, 1.2, [0.9, 0.8, 0.9, 1.0, 0.9]),
]
DEMO_GAP_DAYS = 30


def remove_demo_reports(user_id):
    """Delete only the sample reports (their file names start with 'demo_<id>_')."""
    prefix = f"demo_{user_id}_"
    removed = 0
    for report in Report.query.filter_by(user_id=user_id).all():
        if report.filename.startswith(prefix):
            db.session.delete(report)
            removed += 1
    return removed


@app.route("/demo/load", methods=["POST"])
@login_required
def load_demo_data():
    remove_demo_reports(current_user.id)   # never create duplicates

    today = date.today()
    for i in range(5):
        report_date = today - timedelta(days=DEMO_GAP_DAYS * (4 - i))
        lines = ["SAMPLE LAB REPORT (demo data)", f"Date: {report_date.strftime('%d %b %Y')}", ""]
        for name, unit, lo, hi, values in DEMO_TESTS:
            lines.append(f"{name} {float(values[i]):g} {unit} {lo:g}-{hi:g}")

        report = Report(
            user_id=current_user.id,
            filename=f"demo_{current_user.id}_{i + 1}.pdf",
            original_name=f"sample_report_{i + 1}.pdf",
            report_date=report_date,
            raw_text="\n".join(lines),
        )
        db.session.add(report)
        db.session.flush()

        for name, unit, lo, hi, values in DEMO_TESTS:
            value = float(values[i])
            db.session.add(TestResult(
                report_id=report.id,
                test_name=name,
                value=value,
                unit=unit,
                ref_min=lo,
                ref_max=hi,
                flag=compute_flag(value, lo, hi),
            ))

    db.session.commit()
    flash("Sample data loaded: 5 reports with trends, severity levels and one unusual value.", "success")
    return redirect(url_for("dashboard"))


@app.route("/demo/remove", methods=["POST"])
@login_required
def remove_demo_data():
    removed = remove_demo_reports(current_user.id)
    db.session.commit()
    if removed:
        flash("Sample data removed.", "info")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------- REPORTS LIST ----

@app.route("/reports")
@login_required
def all_reports():
    reports = (Report.query
               .filter_by(user_id=current_user.id)
               .order_by(Report.report_date.desc())
               .all())
    return render_template("all_reports.html", reports=reports)

@app.route("/reports/<int:report_id>")
@login_required
def report_detail(report_id):
    report = db.session.get(Report, report_id)
    if not report or report.user_id != current_user.id:
        abort(404)

    changes = {}
    for result in report.results:
        prev = get_previous_result(current_user.id, result.test_name, report.report_date)
        if prev:
            changes[result.id] = {
                "pct": compute_percentage_change(result.value, prev.value),
                "prev_value": prev.value,
            }

    highlighted_raw_text = build_highlighted_raw_text(report.raw_text, report.results)

    return render_template(
        "report_detail.html",
        report=report,
        changes=changes,
        highlighted_raw_text=highlighted_raw_text,
    )
@app.route("/reports/<int:report_id>/delete", methods=["POST"])
@login_required
def delete_report(report_id):
    report = db.session.get(Report, report_id)
    if not report or report.user_id != current_user.id:
        abort(404)

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], report.filename)
    if os.path.exists(filepath):
        os.remove(filepath)

    db.session.delete(report)
    db.session.commit()

    return redirect(request.referrer or url_for("all_reports"))

# ----------------------------------------------------- COMPARE REPORTS ----

@app.route("/reports/compare")
@login_required
def compare_reports():
    id1 = request.args.get("report1", type=int)
    id2 = request.args.get("report2", type=int)

    all_user_reports = (Report.query
                         .filter_by(user_id=current_user.id)
                         .order_by(Report.report_date.desc())
                         .all())

    comparison_rows = None
    report_a = report_b = None

    if id1 and id2:
        report_a = db.session.get(Report, id1)
        report_b = db.session.get(Report, id2)

        if (not report_a or report_a.user_id != current_user.id or
                not report_b or report_b.user_id != current_user.id):
            abort(404)

        if report_a.report_date > report_b.report_date:
            report_a, report_b = report_b, report_a

        values_a = {r.test_name: r for r in report_a.results}
        values_b = {r.test_name: r for r in report_b.results}

        comparison_rows = []
        for test_name in sorted(set(values_a) | set(values_b)):
            ra = values_a.get(test_name)
            rb = values_b.get(test_name)
            pct = compute_percentage_change(rb.value, ra.value) if ra and rb else None
            comparison_rows.append({
                "test_name": test_name,
                "value_a": ra.value if ra else None,
                "value_b": rb.value if rb else None,
                "pct_change": pct,
                "flag_b": rb.flag if rb else None,
            })

    return render_template(
        "compare_reports.html",
        all_user_reports=all_user_reports,
        report_a=report_a,
        report_b=report_b,
        comparison_rows=comparison_rows,
    )


# ------------------------------------------------------------- PDF EXPORT ----

@app.route("/export/summary")
@login_required
def export_summary_pdf():
    reports = (Report.query
               .filter_by(user_id=current_user.id)
               .order_by(Report.report_date.asc())
               .all())

    if not reports:
        flash("No reports to export yet.", "warning")
        return redirect(url_for("dashboard"))

    latest_report = reports[-1]

    trending_tests = detect_trending_tests(reports)
    do_now, avoid = generate_suggestions(latest_report, trending_tests)

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=2 * cm, bottomMargin=2 * cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleStyle", parent=styles["Title"], textColor=colors.HexColor("#1F6F63"))
    section_style = ParagraphStyle("SectionStyle", parent=styles["Heading2"], textColor=colors.HexColor("#1F6F63"),
                                    spaceBefore=10, spaceAfter=6)
    bullet_style = ParagraphStyle("BulletStyle", parent=styles["Normal"], fontSize=9.3, leading=13,
                                   spaceAfter=5, leftIndent=6)

    story = [
        Paragraph("MedTrack — Personal Health Summary", title_style),
        Paragraph(f"Patient: {current_user.name}", styles["Normal"]),
        Paragraph(f"Email: {current_user.email}", styles["Normal"]),
        Paragraph(f"Generated: {date.today().strftime('%d %b %Y')}", styles["Normal"]),
        Paragraph(
            f"Reports included: {len(reports)} (from {reports[0].report_date.strftime('%d %b %Y')} "
            f"to {reports[-1].report_date.strftime('%d %b %Y')})", styles["Normal"]
        ),
        Spacer(1, 0.6 * cm),
    ]

    # ---------- Latest report values, with colour-coded status ----------
    story.append(Paragraph(f"Latest Report — {latest_report.report_date.strftime('%d %b %Y')}", section_style))

    latest_data = [["Test", "Value", "Reference Range", "Status"]]
    row_flag_colors = [None]  # header has no flag colour
    for r in latest_report.results:
        ref = f"{r.ref_min}\u2013{r.ref_max}" if r.ref_min is not None and r.ref_max is not None else "\u2014"
        status = {"abnormal": "Abnormal", "needs_review": "Needs Review"}.get(r.flag, "Normal")
        latest_data.append([r.test_name, f"{r.value} {r.unit}", ref, status])
        row_flag_colors.append(
            colors.HexColor("#E4572E") if r.flag == "abnormal" else
            colors.HexColor("#C9860A") if r.flag == "needs_review" else
            colors.HexColor("#1F6F63")
        )

    latest_table = Table(latest_data, colWidths=[5.5 * cm, 3.5 * cm, 3.5 * cm, 3.5 * cm])
    table_style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F6F63")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7F6")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (3, 1), (3, -1), "Helvetica-Bold"),
    ]
    for i, flag_color in enumerate(row_flag_colors):
        if flag_color:
            table_style_cmds.append(("TEXTCOLOR", (3, i), (3, i), flag_color))
    latest_table.setStyle(TableStyle(table_style_cmds))
    story.append(latest_table)
    story.append(Spacer(1, 0.8 * cm))

    # ---------- Personalized suggestions ----------
    story.append(Paragraph("What You Should Consider Doing Now", section_style))
    for item in do_now:
        story.append(Paragraph(f"\u2022 {item}", bullet_style))
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("What You Should Avoid", section_style))
    for item in avoid:
        story.append(Paragraph(f"\u2022 {item}", bullet_style))

    story.append(Spacer(1, 1 * cm))
    story.append(Paragraph(
        "This summary is generated automatically from uploaded reports and is intended "
        "for personal tracking only. It is not a medical diagnosis \u2014 please consult a "
        "qualified doctor for interpretation.",
        ParagraphStyle("Disclaimer", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    ))

    doc.build(story)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"medtrack_summary_{date.today().isoformat()}.pdf",
        mimetype="application/pdf",
    )


# ------------------------------------------------------------ AI CHAT ----

@app.route("/chat")
@login_required
def chat():
    has_reports = Report.query.filter_by(user_id=current_user.id).count() > 0
    return render_template("chat.html", has_reports=has_reports)


@app.route("/api/chat", methods=["POST"])
@login_required
def chat_api():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    history = payload.get("history") or []

    if not message:
        return jsonify(error="Please type a question."), 400
    if len(message) > 1000:
        return jsonify(error="Question is too long. Please keep it under 1000 characters."), 400
    if not allow_request(current_user.id):
        return jsonify(error="You're asking too fast. Please wait a few minutes and try again."), 429

    reports = (Report.query
               .filter_by(user_id=current_user.id)
               .order_by(Report.report_date.asc())
               .all())
    if not reports:
        return jsonify(error="Upload at least one report first, then ask me about it."), 400

    trending = detect_trending_tests(reports)
    context = build_health_context(current_user.name, reports, trending)

    try:
        answer = ask_assistant(context, history, message)
    except ChatError as e:
        return jsonify(error=str(e)), 503

    return jsonify(answer=answer)


# ------------------------------------------------------------ TIMELINE ----

def fmt_num(v):
    """Clean number for display: 13.0 -> '13', 13.25 -> '13.25'."""
    if v is None:
        return "–"
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def result_direction(result):
    """'low' / 'high' if the value is outside its reference range, else ''."""
    v = result.value
    if v is None:
        return ""
    if result.ref_min is not None and v < result.ref_min:
        return "low"
    if result.ref_max is not None and v > result.ref_max:
        return "high"
    return ""


def build_timeline(reports):
    """
    reports: Report objects sorted oldest -> newest.
    Returns one event dict per report (oldest -> newest). Every event after the
    first is compared with the report just before it:
      improved  = was abnormal, now not abnormal
      worsened  = was not abnormal, now abnormal
      moved     = any other test that changed by 10% or more
      new_tests = tests that were not in the previous report
    Pure comparison of stored values, no diagnosis.
    """
    events = []
    prev_report = None

    for report in reports:
        results = list(report.results)
        abnormal = [r for r in results if r.flag == "abnormal"]
        review = [r for r in results if r.flag == "needs_review"]

        event = {
            "id": report.id,
            "date": report.report_date,
            "name": report.original_name,
            "test_count": len(results),
            "abnormal_count": len(abnormal),
            "review_count": len(review),
            "status": "abnormal" if abnormal else ("review" if review else "normal"),
            "abnormal_items": [
                {"name": r.test_name, "value": fmt_num(r.value),
                 "unit": r.unit or "", "dir": result_direction(r)}
                for r in abnormal
            ],
            "is_first": prev_report is None,
            "gap_days": None,
            "improved": [],
            "worsened": [],
            "moved": [],
            "new_tests": [],
            "test_values": {
                r.test_name: {"value": fmt_num(r.value), "unit": r.unit or "",
                              "flag": r.flag, "dir": result_direction(r)}
                for r in results
            },
        }

        if prev_report is not None:
            event["gap_days"] = (report.report_date - prev_report.report_date).days
            prev_map = {r.test_name: r for r in prev_report.results}

            for r in results:
                p = prev_map.get(r.test_name)
                if p is None:
                    event["new_tests"].append(r.test_name)
                    continue

                pct = compute_percentage_change(r.value, p.value)
                item = {
                    "name": r.test_name,
                    "prev": fmt_num(p.value),
                    "value": fmt_num(r.value),
                    "unit": r.unit or "",
                    "pct": f"{pct:+g}%" if pct is not None else "",
                }
                if p.flag == "abnormal" and r.flag != "abnormal":
                    event["improved"].append(item)
                elif p.flag != "abnormal" and r.flag == "abnormal":
                    event["worsened"].append(item)
                elif pct is not None and abs(pct) >= 10:
                    event["moved"].append(item)

        events.append(event)
        prev_report = report

    return events


def build_timeline_chart(events):
    """Bar chart: number of abnormal values in each report over time."""
    labels, seen = [], {}
    for e in events:
        label = e["date"].strftime("%d %b %Y")
        seen[label] = seen.get(label, 0) + 1
        labels.append(label if seen[label] == 1 else f"{label} ({seen[label]})")

    counts = [e["abnormal_count"] for e in events]
    bar_colors = ["#E4572E" if c > 0 else "#1F6F63" for c in counts]

    fig = go.Figure(go.Bar(
        x=labels, y=counts, marker_color=bar_colors,
        text=[c if c else "" for c in counts], textposition="outside", cliponaxis=False,
        hovertemplate="%{x}<br>Abnormal values: %{y}<extra></extra>",
    ))
    fig.update_layout(
        title="Abnormal values per report",
        margin=dict(l=40, r=20, t=50, b=40),
        height=280,
        template="plotly_white",
        showlegend=False,
        xaxis=dict(type="category"),
        yaxis=dict(rangemode="tozero", tickformat="d"),
        font=dict(family="Inter, sans-serif", size=12, color="#213547"),
    )
    return plotly_plot(fig, output_type="div", include_plotlyjs=False)


@app.route("/timeline")
@login_required
def timeline():
    reports = (Report.query
               .filter_by(user_id=current_user.id)
               .order_by(Report.report_date.asc(), Report.id.asc())
               .all())

    if not reports:
        return render_template("timeline.html", has_reports=False)

    events = build_timeline(reports)

    span_days = (events[-1]["date"] - events[0]["date"]).days
    if span_days >= 60:
        span_text = f"{span_days // 30} months"
    elif span_days >= 1:
        span_text = f"{span_days} day{'s' if span_days != 1 else ''}"
    else:
        span_text = "Same day"

    summary = {
        "total": len(events),
        "span_text": span_text,
        "abnormal_now": events[-1]["abnormal_count"],
        "improved_total": sum(len(e["improved"]) for e in events),
    }

    chart = build_timeline_chart(events) if len(events) >= 2 else None
    test_names = sorted({name for e in events for name in e["test_values"]})

    return render_template(
        "timeline.html",
        has_reports=True,
        events=list(reversed(events)),  # newest first
        summary=summary,
        chart=chart,
        test_names=test_names,
        trending_tests=detect_trending_tests(reports),
    )


# ------------------------------------------- REVIEW / CORRECTION SCREEN ----

def parse_float(raw):
    """'' -> None, '12.5' -> 12.5, anything else raises ValueError."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "":
        return None
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("not a finite number")
    return value


def fmt_input(v):
    """Number as text for an <input type=number>; blank when there is no value."""
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if f.is_integer() else str(f)


def range_text(ref_min, ref_max):
    """'12–16', '≥ 12', '≤ 100' or '—' for display."""
    lo, hi = fmt_input(ref_min), fmt_input(ref_max)
    if lo and hi:
        return f"{lo}\u2013{hi}"
    if lo:
        return f"\u2265 {lo}"
    if hi:
        return f"\u2264 {hi}"
    return "\u2014"


def compute_flag(value, ref_min, ref_max):
    """abnormal if outside the reference range, else normal. No range = normal."""
    if value is None:
        return "needs_review"
    if ref_min is not None and value < ref_min:
        return "abnormal"
    if ref_max is not None and value > ref_max:
        return "abnormal"
    return "normal"


def get_previous_report(user_id, report):
    """The report just before this one (by date, then id) for the same user."""
    return (Report.query
            .filter(Report.user_id == user_id,
                    Report.id != report.id,
                    or_(Report.report_date < report.report_date,
                        and_(Report.report_date == report.report_date,
                             Report.id < report.id)))
            .order_by(Report.report_date.desc(), Report.id.desc())
            .first())


def build_change_summary(report):
    """'What changed?' for one report vs the report before it."""
    prev = get_previous_report(report.user_id, report)
    if prev is None:
        return {
            "has_previous": False,
            "headline": "This is your first report. It becomes the starting point "
                        "that future reports are compared against.",
        }

    ev = build_timeline([prev, report])[1]
    prev_date = prev.report_date.strftime("%d %b %Y")

    parts = []
    if ev["improved"]:
        parts.append(f"{len(ev['improved'])} back in range")
    if ev["worsened"]:
        parts.append(f"{len(ev['worsened'])} newly out of range")
    if ev["moved"]:
        n = len(ev["moved"])
        parts.append(f"{n} other change{'s' if n != 1 else ''} of 10% or more")

    if parts:
        headline = f"Compared with your previous report ({prev_date}): " + ", ".join(parts) + "."
    else:
        headline = f"No major changes compared with your previous report ({prev_date})."

    return {
        "has_previous": True,
        "headline": headline,
        "improved": ev["improved"],
        "worsened": ev["worsened"],
        "moved": ev["moved"],
        "new_tests": ev["new_tests"],
    }


@app.route("/reports/<int:report_id>/review", methods=["GET", "POST"])
@login_required
def review_report(report_id):
    report = db.session.get(Report, report_id)
    if not report or report.user_id != current_user.id:
        abort(404)

    if request.method == "POST":
        updated = added = removed = skipped = 0
        date_changed = False

        # report date
        try:
            new_date = datetime.strptime(request.form.get("report_date", ""), "%Y-%m-%d").date()
            if new_date != report.report_date:
                report.report_date = new_date
                date_changed = True
        except (TypeError, ValueError):
            pass  # keep the old date

        # existing values (only rows that belong to this report are touched)
        existing_names = set()
        for r in list(report.results):
            if request.form.get(f"delete_{r.id}"):
                db.session.delete(r)
                removed += 1
                continue

            existing_names.add((r.test_name or "").strip().lower())

            try:
                new_value = parse_float(request.form.get(f"value_{r.id}"))
                new_min = parse_float(request.form.get(f"ref_min_{r.id}"))
                new_max = parse_float(request.form.get(f"ref_max_{r.id}"))
            except ValueError:
                skipped += 1
                continue
            if new_value is None:
                skipped += 1
                continue

            new_unit = (request.form.get(f"unit_{r.id}") or "").strip()[:30]
            changed = (new_value != r.value or new_min != r.ref_min or
                       new_max != r.ref_max or new_unit != (r.unit or ""))
            if changed:
                updated += 1

            r.value, r.ref_min, r.ref_max, r.unit = new_value, new_min, new_max, new_unit
            # keep the extractor's flag for untouched rows, but re-check anything
            # the user edited or that was waiting for review
            if changed or r.flag == "needs_review":
                r.flag = compute_flag(new_value, new_min, new_max)

        # tests added by hand
        for i in range(10):
            name = (request.form.get(f"new_name_{i}") or "").strip()
            if not name:
                continue
            try:
                value = parse_float(request.form.get(f"new_value_{i}"))
                ref_min = parse_float(request.form.get(f"new_ref_min_{i}"))
                ref_max = parse_float(request.form.get(f"new_ref_max_{i}"))
            except ValueError:
                skipped += 1
                continue
            if value is None or name.lower() in existing_names:
                skipped += 1
                continue

            unit = (request.form.get(f"new_unit_{i}") or "").strip()[:30]
            db.session.add(TestResult(
                report_id=report.id,
                test_name=name[:100],
                value=value,
                unit=unit,
                ref_min=ref_min,
                ref_max=ref_max,
                flag=compute_flag(value, ref_min, ref_max),
            ))
            existing_names.add(name.lower())
            added += 1

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("Sorry, the changes could not be saved. Please try again.", "danger")
            return redirect(url_for("review_report", report_id=report.id))

        bits = []
        if updated:
            bits.append(f"{updated} value{'s' if updated != 1 else ''} updated")
        if added:
            bits.append(f"{added} added")
        if removed:
            bits.append(f"{removed} removed")
        if date_changed:
            bits.append("report date updated")

        if bits:
            flash("Saved: " + ", ".join(bits) + ".", "success")
        else:
            flash("No changes to save.", "info")
        if skipped:
            flash(f"{skipped} row{'s' if skipped != 1 else ''} could not be saved "
                  "(missing or invalid number, or a duplicate test name).", "warning")

        return redirect(url_for("review_report", report_id=report.id))

    # ---- GET ----
    rows = [{
        "id": r.id,
        "name": r.test_name,
        "value": fmt_input(r.value),
        "unit": r.unit or "",
        "ref_min": fmt_input(r.ref_min),
        "ref_max": fmt_input(r.ref_max),
        "range_text": range_text(r.ref_min, r.ref_max),
        "flag": r.flag,
        "dir": result_direction(r),
    } for r in sorted(report.results, key=lambda x: x.id)]

    return render_template(
        "review.html",
        report=report,
        rows=rows,
        change=build_change_summary(report),
        blank_count=3 if rows else 6,
    )


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)