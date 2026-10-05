from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reports = db.relationship("Report", backref="user", cascade="all, delete-orphan")
    goals = db.relationship("Goal", backref="user", cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Report(db.Model):
    __tablename__ = "reports"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    report_date = db.Column(db.Date, nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    raw_text = db.Column(db.Text)

    results = db.relationship("TestResult", backref="report", cascade="all, delete-orphan")

    @property
    def abnormal_count(self):
        return sum(1 for r in self.results if r.flag == "abnormal")

    @property
    def review_count(self):
        return sum(1 for r in self.results if r.flag == "needs_review")


class TestResult(db.Model):
    __tablename__ = "test_results"

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("reports.id"), nullable=False)
    test_name = db.Column(db.String(120), nullable=False)
    value = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(40))
    ref_min = db.Column(db.Float)
    ref_max = db.Column(db.Float)
    flag = db.Column(db.String(20), default="normal")  # normal | abnormal | needs_review


class Goal(db.Model):
    __tablename__ = "goals"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    test_name = db.Column(db.String(120), nullable=False)
    direction = db.Column(db.String(10), nullable=False)  # below | above
    target_value = db.Column(db.Float, nullable=False)
    start_value = db.Column(db.Float, nullable=False)     # latest value when the goal was set
    created_at = db.Column(db.DateTime, default=datetime.utcnow)