"""Household Resource Planner – main application entry point."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

from nicegui import app as nicegui_app, ui
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

from auth import create_access_token, decode_access_token, hash_password, verify_password
from models import (
    SessionLocal,
    Tag,
    Task,
    TaskInstance,
    TaskStatus,
    User,
    UserRole,
    init_db,
    task_instance_users,
    task_tags,
    Preset,
    PresetItem,
)

# Weekday helpers: 0=Monday … 6=Sunday (Python weekday())
WEEKDAY_LABELS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEEKDAY_MAP = {i: label for i, label in enumerate(WEEKDAY_LABELS)}

USER_COLORS = [
    "#6366f1", "#ec4899", "#f59e0b", "#10b981", "#3b82f6",
    "#ef4444", "#8b5cf6", "#14b8a6", "#f97316", "#06b6d4",
]

TAG_PRESET_COLORS = [
    "#6366f1", "#ec4899", "#f59e0b", "#10b981", "#3b82f6",
    "#ef4444", "#8b5cf6", "#14b8a6", "#f97316", "#06b6d4",
    "#a855f7", "#84cc16", "#f43f5e", "#0ea5e9",
]

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

init_db()


def _seed():
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.username == "admin").first():
            admin = User(
                id=str(uuid.uuid4()),
                username="admin",
                password_hash=hash_password("admin"),
                role=UserRole.ADMIN,
                daily_capacity_minutes=480,
            )
            db.add(admin)
            db.commit()
    finally:
        db.close()


_seed()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_db() -> Session:
    return SessionLocal()


def _current_user(storage: dict) -> User | None:
    token = storage.get("auth_token")
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    db = _get_db()
    user = db.query(User).filter(User.id == payload.get("sub")).first()
    db.close()
    return user


def _week_dates(ref: date) -> list[date]:
    start = ref - timedelta(days=ref.weekday())
    return [start + timedelta(days=i) for i in range(7)]


def _two_week_dates(ref: date) -> list[date]:
    start = ref - timedelta(days=ref.weekday())
    return [start + timedelta(days=i) for i in range(14)]


def _four_week_dates(ref: date) -> list[date]:
    start = ref - timedelta(days=ref.weekday())
    return [start + timedelta(days=i) for i in range(28)]


def _parse_weekday_csv(value: str | None) -> list[int]:
    if not value:
        return []
    try:
        return sorted(set(int(x.strip()) for x in value.split(",") if x.strip().isdigit() and 0 <= int(x.strip()) <= 6))
    except ValueError:
        return []


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _last_day_of_month(year: int, month: int) -> int:
    first = date(year, month, 1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    return (next_month - timedelta(days=1)).day


def _parse_recurrence_rule(rule: str | None) -> dict:
    config = {"kind": "weekly", "days": [], "anchor": None, "month_day": None}
    if not rule or not rule.strip():
        return config
    if rule.startswith("biweekly|"):
        parts = rule.split("|", 2)
        config["kind"] = "biweekly"
        config["days"] = _parse_weekday_csv(parts[1] if len(parts) > 1 else None)
        if len(parts) > 2:
            try:
                config["anchor"] = date.fromisoformat(parts[2])
            except ValueError:
                pass
        return config
    if rule.startswith("4weekly|"):
        parts = rule.split("|", 2)
        config["kind"] = "4weekly"
        config["days"] = _parse_weekday_csv(parts[1] if len(parts) > 1 else None)
        if len(parts) > 2:
            try:
                config["anchor"] = date.fromisoformat(parts[2])
            except ValueError:
                pass
        return config
    if rule.startswith("monthly|"):
        parts = rule.split("|", 2)
        config["kind"] = "monthly"
        if len(parts) > 1 and parts[1].isdigit():
            month_day = int(parts[1])
            if 1 <= month_day <= 31:
                config["month_day"] = month_day
        if len(parts) > 2:
            try:
                config["anchor"] = date.fromisoformat(parts[2])
            except ValueError:
                pass
        return config
    config["days"] = _parse_weekday_csv(rule)
    return config


def _parse_recurrence_days(rule: str | None) -> list[int]:
    return _parse_recurrence_rule(rule)["days"]


def _build_recurrence_rule(mode: str, days: list[int], anchor_date: date | None) -> str | None:
    selected_days = sorted(set(days))
    if mode == "monthly":
        anchor = anchor_date or date.today()
        return f"monthly|{anchor.day}|{anchor.isoformat()}"
    if not selected_days:
        return None
    if mode == "biweekly":
        anchor = anchor_date or date.today()
        return f"biweekly|{','.join(str(d) for d in selected_days)}|{anchor.isoformat()}"
    if mode == "4weekly":
        anchor = anchor_date or date.today()
        return f"4weekly|{','.join(str(d) for d in selected_days)}|{anchor.isoformat()}"
    return ",".join(str(d) for d in selected_days)


def _recurrence_matches(rule: str | None, d: date) -> bool:
    config = _parse_recurrence_rule(rule)
    if config["kind"] == "monthly":
        month_day = config["month_day"]
        anchor = config["anchor"]
        if not month_day:
            return False
        if anchor and d < anchor:
            return False
        return d.day == min(month_day, _last_day_of_month(d.year, d.month))

    rec_days = config["days"]
    if d.weekday() not in rec_days:
        return False
    if config["kind"] == "4weekly":
        anchor = config["anchor"]
        if not anchor or d < anchor:
            return False
        return (_week_start(d) - _week_start(anchor)).days % 28 == 0
    if config["kind"] != "biweekly":
        return True

    anchor = config["anchor"]
    if not anchor or d < anchor:
        return False
    return (_week_start(d) - _week_start(anchor)).days % 14 == 0


def _recurrence_label(rule: str | None) -> str:
    config = _parse_recurrence_rule(rule)
    if config["kind"] == "monthly":
        month_day = config["month_day"]
        return f"Monatlich am {month_day}." if month_day else "Monatlich"
    rec_days = config["days"]
    if not rec_days:
        return ""
    if len(rec_days) == 7:
        if config["kind"] == "4weekly":
            return "Alle 4 Wochen"
        return "Alle 2 Wochen" if config["kind"] == "biweekly" else "Täglich"
    day_list = ", ".join(WEEKDAY_MAP[dd] for dd in rec_days)
    if config["kind"] == "4weekly":
        return f"Alle 4 Wochen: {day_list}"
    if config["kind"] == "biweekly":
        return f"Alle 2 Wochen: {day_list}"
    return day_list


def _parse_assignment_mode_config(mode: str | None, fallback_date: date) -> dict:
    config = {"kind": "none", "weekday": fallback_date.weekday(), "anchor": fallback_date}
    if not mode or mode == "none":
        return config
    if mode == "immer":
        config["kind"] = "always"
        return config
    if mode.startswith("jeden_"):
        try:
            config["kind"] = "weekly"
            config["weekday"] = int(mode.split("_", 1)[1])
        except ValueError:
            pass
        return config
    if mode.startswith("2weeks|"):
        parts = mode.split("|", 2)
        config["kind"] = "biweekly"
        if len(parts) > 1 and parts[1].isdigit():
            config["weekday"] = int(parts[1])
        if len(parts) > 2:
            try:
                config["anchor"] = date.fromisoformat(parts[2])
            except ValueError:
                pass
        return config
    return config


def _build_biweekly_assignment_mode(instance_date: date) -> str:
    return f"2weeks|{instance_date.weekday()}|{instance_date.isoformat()}"


def _get_excluded_dates(task: Task) -> set[date]:
    if not task.excluded_dates:
        return set()
    result = set()
    for s in task.excluded_dates.split(","):
        s = s.strip()
        if s:
            try:
                result.add(date.fromisoformat(s))
            except ValueError:
                pass
    return result


def _add_excluded_date(db: Session, task: Task, d: date):
    excluded = _get_excluded_dates(task)
    excluded.add(d)
    task.excluded_dates = ",".join(dt.isoformat() for dt in excluded)
    db.commit()


def _remove_excluded_date(db: Session, task: Task, d: date):
    excluded = _get_excluded_dates(task)
    excluded.discard(d)
    task.excluded_dates = ",".join(dt.isoformat() for dt in excluded) or None
    db.commit()


def _ensure_recurring_instances(db: Session, tasks: list[Task], dates: list[date]):
    existing = set()
    for inst in db.query(TaskInstance.task_id, TaskInstance.date).filter(TaskInstance.date.in_(dates)).all():
        existing.add((inst.task_id, inst.date))
    created = False
    for task in tasks:
        config = _parse_recurrence_rule(task.recurrence_rule)
        if config["kind"] == "monthly":
            if not config["month_day"]:
                continue
        elif not config["days"]:
            continue
        excluded = _get_excluded_dates(task)
        for d in dates:
            if d in excluded:
                continue
            if _recurrence_matches(task.recurrence_rule, d) and (task.id, d) not in existing:
                inst = TaskInstance(
                    id=str(uuid.uuid4()),
                    task_id=task.id,
                    date=d,
                    status=TaskStatus.OPEN,
                )
                db.add(inst)
                created = True
    if created:
        db.commit()


def _get_assignment_mode(db: Session, instance_id: str, user_id: str) -> str | None:
    row = db.execute(
        text("SELECT assignment_mode FROM task_instance_users WHERE task_instance_id = :iid AND user_id = :uid"),
        {"iid": instance_id, "uid": user_id},
    ).fetchone()
    return row[0] if row else None


def _set_assignment_mode(db: Session, instance_id: str, user_id: str, mode: str | None):
    db.execute(
        text("UPDATE task_instance_users SET assignment_mode = :mode WHERE task_instance_id = :iid AND user_id = :uid"),
        {"mode": mode, "iid": instance_id, "uid": user_id},
    )
    db.commit()


def _remove_user_from_all_instances(db: Session, task_id: str, user_id: str, dates: list[date]):
    instances = (
        db.query(TaskInstance)
        .options(joinedload(TaskInstance.assigned_users))
        .filter(TaskInstance.task_id == task_id, TaskInstance.date.in_(dates))
        .all()
    )
    u_obj = db.query(User).get(user_id)
    if not u_obj:
        return
    for inst in instances:
        if u_obj in inst.assigned_users:
            inst.assigned_users.remove(u_obj)
    db.commit()


def _compute_user_minutes(
    db: Session, dates: list[date], users: list[User]
) -> dict[str, dict[date, float]]:
    instances = (
        db.query(TaskInstance)
        .options(joinedload(TaskInstance.assigned_users), joinedload(TaskInstance.task))
        .filter(TaskInstance.date.in_(dates))
        .all()
    )
    result: dict[str, dict[date, float]] = {
        u.id: {d: 0.0 for d in dates} for u in users
    }
    for inst in instances:
        n = len(inst.assigned_users)
        if n == 0:
            continue
        share = inst.task.base_duration_minutes / n
        for u in inst.assigned_users:
            if u.id in result:
                result[u.id][inst.date] = result[u.id].get(inst.date, 0.0) + share
    return result


def _user_color(index: int) -> str:
    return USER_COLORS[index % len(USER_COLORS)]


def _cell_status(inst: TaskInstance | None, d: date) -> str:
    if inst is None:
        return "inactive"
    if inst.status == TaskStatus.COMPLETED:
        return "completed"
    if d < date.today() and inst.status == TaskStatus.OPEN:
        return "overdue"
    if inst.assigned_users:
        return "assigned"
    return "unassigned"


CELL_STYLES = {
    "completed":  ("#10b981", "rgba(16,185,129,0.22)",  "#10b981"),
    "assigned":   ("#6366f1", "rgba(99,102,241,0.22)",   "#6366f1"),
    "unassigned": ("#f59e0b", "rgba(245,158,11,0.22)",   "#f59e0b"),
    "overdue":    ("#ef4444", "rgba(239,68,68,0.22)",    "#ef4444"),
    "inactive":   ("#94a3b8", "rgba(148,163,184,0.12)",  "#94a3b8"),
}

THEME_OPTIONS = {
    "sunforge": "Sunforge",
    "forest_circuit": "Forest Circuit",
    "twilight_relic": "Twilight Relic",
    "midnight_arcade": "Midnight Arcade",
}

DARK_THEMES = {"twilight_relic", "midnight_arcade"}


def _normalize_theme(theme: str | None) -> str:
    if theme == "light":
        return "sunforge"
    if theme == "dark":
        return "twilight_relic"
    if theme == "owl_light":
        return "sunforge"
    if theme == "industrial_light":
        return "forest_circuit"
    if theme == "owl_dark":
        return "twilight_relic"
    if theme == "carbon_dark":
        return "midnight_arcade"
    if theme in THEME_OPTIONS:
        return theme
    return "sunforge"


def _is_dark_theme(theme: str) -> bool:
    return theme in DARK_THEMES

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CUSTOM_CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Plus+Jakarta+Sans:wght@600;700;800&display=swap" rel="stylesheet">
<style>
/* bling.home – design system v2
   Adventure-View module hook: body[data-view="adventure"] { ... }
   To enable: ui.run_javascript("document.body.setAttribute('data-view','adventure')")
*/
:root {
    /* Sunforge (default light) */
    --owl-bg: #f5f0ff;
    --owl-bg-alt: #ede5ff;
    --owl-surface: #ffffff;
    --owl-surface-soft: #f3eeff;
    --owl-strong-surface: #3b1fa8;
    --owl-text: #1a1235;
    --owl-text-soft: #3d2f72;
    --owl-muted: #7c6faa;
    --owl-structure: #a78bfa;
    --owl-accent: #7c3aed;
    --owl-accent-2: #ec4899;
    --owl-accent-soft: rgba(124,58,237,0.15);
    --owl-accent-strong: #5b21b6;
    --owl-shadow: 0 16px 48px rgba(124, 58, 237, 0.18);
    --owl-border: rgba(124,58,237,0.14);
    --owl-header-bg: rgba(245,240,255,0.90);
    --owl-header-text: #1a1235;
    --owl-text-on-accent: #ffffff;
    /* Status colors */
    --bh-open: #f59e0b;
    --bh-open-bg: rgba(245,158,11,0.18);
    --bh-done: #10b981;
    --bh-done-bg: rgba(16,185,129,0.18);
    --bh-overdue: #ef4444;
    --bh-overdue-bg: rgba(239,68,68,0.18);
    --bh-assigned: #6366f1;
    --bh-assigned-bg: rgba(99,102,241,0.18);
}

body[data-theme="forest_circuit"] {
    --owl-bg: #f0fdf4;
    --owl-bg-alt: #dcfce7;
    --owl-surface: #ffffff;
    --owl-surface-soft: #ecfdf5;
    --owl-strong-surface: #065f46;
    --owl-text: #064e3b;
    --owl-text-soft: #065f46;
    --owl-muted: #059669;
    --owl-structure: #34d399;
    --owl-accent: #10b981;
    --owl-accent-2: #6366f1;
    --owl-accent-soft: rgba(16,185,129,0.16);
    --owl-accent-strong: #059669;
    --owl-shadow: 0 16px 44px rgba(16, 185, 129, 0.18);
    --owl-border: rgba(16, 185, 129, 0.14);
    --owl-header-bg: rgba(240,253,244,0.92);
    --owl-header-text: #064e3b;
    --owl-text-on-accent: #ffffff;
}

body[data-theme="twilight_relic"] {
    --owl-bg: #0f0a1e;
    --owl-bg-alt: #1a1235;
    --owl-surface: #1e1535;
    --owl-surface-soft: #2a1f4e;
    --owl-strong-surface: #3b1fa8;
    --owl-text: #ede9fd;
    --owl-text-soft: #c4b5fd;
    --owl-muted: #a78bfa;
    --owl-structure: #818cf8;
    --owl-accent: #a78bfa;
    --owl-accent-2: #f472b6;
    --owl-accent-soft: rgba(167,139,250,0.20);
    --owl-accent-strong: #7c3aed;
    --owl-shadow: 0 20px 58px rgba(0, 0, 0, 0.48);
    --owl-border: rgba(167,139,250,0.22);
    --owl-header-bg: rgba(15,10,30,0.88);
    --owl-header-text: #ede9fd;
    --owl-text-on-accent: #ffffff;
}

body[data-theme="midnight_arcade"] {
    --owl-bg: #0a0a0f;
    --owl-bg-alt: #111128;
    --owl-surface: #13131f;
    --owl-surface-soft: #1c1c32;
    --owl-strong-surface: #1e1b4b;
    --owl-text: #e2e8f0;
    --owl-text-soft: #c7d2fe;
    --owl-muted: #818cf8;
    --owl-structure: #6366f1;
    --owl-accent: #f472b6;
    --owl-accent-2: #38bdf8;
    --owl-accent-soft: rgba(244,114,182,0.20);
    --owl-accent-strong: #ec4899;
    --owl-shadow: 0 20px 62px rgba(0, 0, 0, 0.56);
    --owl-border: rgba(99,102,241,0.22);
    --owl-header-bg: rgba(10,10,15,0.90);
    --owl-header-text: #e2e8f0;
    --owl-text-on-accent: #ffffff;
}

body[data-theme="twilight_relic"],
body[data-theme="midnight_arcade"] {
    color-scheme: dark;
}

body, .q-page {
    background:
        radial-gradient(circle at 10% 12%, var(--owl-accent-soft), transparent 30%),
        radial-gradient(circle at 90% 8%, color-mix(in srgb, var(--owl-accent-2) 12%, transparent), transparent 28%),
        linear-gradient(135deg, var(--owl-bg) 0%, var(--owl-bg-alt) 100%),
        var(--owl-bg) !important;
    font-family: 'Inter', sans-serif !important;
    color: var(--owl-text) !important;
}

body::before {
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    background:
        radial-gradient(ellipse at 20% 80%, color-mix(in srgb, var(--owl-accent) 6%, transparent) 0%, transparent 60%),
        radial-gradient(ellipse at 80% 20%, color-mix(in srgb, var(--owl-accent-2) 6%, transparent) 0%, transparent 60%);
    z-index: 0;
}

.text-h5, .text-h6, .text-subtitle1, .text-subtitle2, h1, h2, h3 {
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    letter-spacing: -0.02em;
}

.q-header {
    background: var(--owl-header-bg) !important;
    color: var(--owl-header-text) !important;
    backdrop-filter: blur(20px) saturate(180%);
    -webkit-backdrop-filter: blur(20px) saturate(180%);
    border-bottom: 1px solid var(--owl-border);
    box-shadow: 0 1px 0 var(--owl-border), 0 4px 24px rgba(0,0,0,0.06) !important;
}

.q-card {
    color: var(--owl-text) !important;
    background: linear-gradient(180deg, color-mix(in srgb, var(--owl-surface) 82%, white) 0%, var(--owl-surface-soft) 100%) !important;
    border: 1px solid var(--owl-border);
}

.q-field__label, .q-field__native {
    color: var(--owl-text) !important;
}

.q-field__input,
.q-field__prefix,
.q-field__suffix,
.q-item__label,
.q-checkbox__label,
.q-toggle__label,
.q-radio__label,
.q-tab,
.q-chip,
.q-expansion-item__label,
.q-menu,
.q-select__dropdown-icon,
.q-select__dropdown-icon:before {
    color: var(--owl-text) !important;
}

body[data-theme="twilight_relic"] .q-item__label,
body[data-theme="twilight_relic"] .q-checkbox__label,
body[data-theme="twilight_relic"] .q-toggle__label,
body[data-theme="twilight_relic"] .q-field__label,
body[data-theme="twilight_relic"] .q-field__native,
body[data-theme="twilight_relic"] .q-field__input,
body[data-theme="midnight_arcade"] .q-item__label,
body[data-theme="midnight_arcade"] .q-checkbox__label,
body[data-theme="midnight_arcade"] .q-toggle__label,
body[data-theme="midnight_arcade"] .q-field__label,
body[data-theme="midnight_arcade"] .q-field__native,
body[data-theme="midnight_arcade"] .q-field__input {
    color: var(--owl-text) !important;
}

body[data-theme="twilight_relic"] .q-card,
body[data-theme="midnight_arcade"] .q-card,
body[data-theme="twilight_relic"] .hrp-stat-card,
body[data-theme="midnight_arcade"] .hrp-stat-card {
    color: var(--owl-text) !important;
}

body[data-theme="twilight_relic"] .q-card,
body[data-theme="midnight_arcade"] .q-card,
body[data-theme="twilight_relic"] .hrp-card,
body[data-theme="midnight_arcade"] .hrp-card,
body[data-theme="twilight_relic"] .hrp-stat-card,
body[data-theme="midnight_arcade"] .hrp-stat-card {
    background: linear-gradient(160deg, var(--owl-strong-surface) 0%, var(--owl-surface) 100%) !important;
    border-color: color-mix(in srgb, var(--owl-structure) 22%, var(--owl-border)) !important;
}

.q-field--outlined .q-field__control {
    border-radius: 16px;
    border: 1px solid var(--owl-border);
    background: color-mix(in srgb, var(--owl-surface) 90%, white);
}

body[data-theme="twilight_relic"] .q-field--outlined .q-field__control,
body[data-theme="midnight_arcade"] .q-field--outlined .q-field__control {
    background: linear-gradient(180deg, var(--owl-strong-surface) 0%, var(--owl-surface) 100%) !important;
}

.q-btn {
    border-radius: 12px;
    font-weight: 600;
    font-family: 'Inter', sans-serif !important;
    letter-spacing: 0.01em;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
}

.q-btn:hover {
    transform: translateY(-1px);
}

.q-btn--flat {
    color: var(--owl-text) !important;
}

.q-btn--unelevated {
    background: linear-gradient(135deg, var(--owl-accent) 0%, var(--owl-accent-strong) 100%) !important;
    color: var(--owl-text-on-accent) !important;
    box-shadow: 0 6px 20px color-mix(in srgb, var(--owl-accent) 38%, transparent);
}

.q-btn--unelevated:hover {
    box-shadow: 0 10px 28px color-mix(in srgb, var(--owl-accent) 52%, transparent);
}

.hrp-matrix-cell {
    min-width: 110px;
    min-height: 62px;
    transition: transform 0.16s ease, box-shadow 0.16s ease, filter 0.16s ease;
    border-radius: 10px;
    padding: 4px !important;
}

.hrp-matrix-cell:hover {
    transform: translateY(-2px) scale(1.03);
    box-shadow: 0 8px 24px rgba(0,0,0,0.14);
    filter: brightness(1.06);
    z-index: 2;
    position: relative;
}

/* Overdue glow pulse */
.hrp-matrix-cell[data-status="overdue"] {
    animation: bhOverduePulse 2.4s ease-in-out infinite;
}

@keyframes bhOverduePulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(239,68,68,0); }
    50% { box-shadow: 0 0 0 3px rgba(239,68,68,0.30); }
}

.hrp-card {
    border-radius: 24px !important;
    background: linear-gradient(160deg, color-mix(in srgb, var(--owl-surface) 88%, white) 0%, var(--owl-surface-soft) 100%) !important;
    box-shadow: var(--owl-shadow) !important;
    border: 1px solid var(--owl-border);
    transition: transform 0.18s, box-shadow 0.18s;
    position: relative;
    overflow: hidden;
}

.hrp-card::after,
.hrp-stat-card::after {
    content: "";
    position: absolute;
    inset: auto -20% 68% auto;
    width: 160px;
    height: 160px;
    border-radius: 999px;
    background: radial-gradient(circle, color-mix(in srgb, var(--owl-accent-2) 26%, transparent) 0%, transparent 72%);
    pointer-events: none;
}

.hrp-card:hover {
    transform: translateY(-4px) rotate(-0.25deg);
    box-shadow: 0 22px 46px rgba(0, 0, 0, 0.2) !important;
}

.hrp-stat-card {
    border-radius: 22px !important;
    background: linear-gradient(160deg, color-mix(in srgb, var(--owl-surface) 86%, white) 0%, var(--owl-surface-soft) 100%) !important;
    box-shadow: var(--owl-shadow) !important;
    border: 1px solid var(--owl-border);
    position: relative;
    overflow: hidden;
}

.hrp-tag {
    display: inline-flex;
    align-items: center;
    padding: 1px 8px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 600;
    color: var(--owl-text-on-accent);
    margin: 1px;
}

.hrp-user-chip {
    display: inline-flex;
    align-items: center;
    padding: 1px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    color: var(--owl-text-on-accent);
    margin: 1px;
}

.sortable-ghost {
    opacity: 0.4;
}

.sortable-drag {
    background: var(--owl-accent-soft) !important;
}

.hrp-table th {
    font-family: 'Plus Jakarta Sans', sans-serif !important;
}

.hrp-wordmark {
    font-family: 'Plus Jakarta Sans', sans-serif;
    font-size: 20px;
    font-weight: 800;
    letter-spacing: -0.03em;
    color: var(--owl-text);
    line-height: 1;
}

.hrp-wordmark span {
    color: var(--owl-accent);
}

.hrp-submark {
    font-family: 'Inter', sans-serif;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--owl-muted);
    line-height: 1;
}

.hrp-crest {
    width: 40px;
    height: 40px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, var(--owl-accent) 0%, var(--owl-accent-2) 100%);
    box-shadow: 0 6px 18px color-mix(in srgb, var(--owl-accent) 40%, transparent);
    font-size: 20px;
    line-height: 1;
    flex-shrink: 0;
}

.hrp-login-shell {
    border-radius: 30px !important;
}

.hrp-login-shell::before {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: inherit;
    pointer-events: none;
    background: linear-gradient(135deg, color-mix(in srgb, var(--owl-accent) 18%, transparent), transparent 36%, color-mix(in srgb, var(--owl-accent-2) 18%, transparent));
}

.hrp-theme-select {
    min-width: 190px;
}

.hrp-theme-select .q-field__control {
    background: color-mix(in srgb, var(--owl-surface) 88%, white) !important;
    border-radius: 16px !important;
}

.hrp-nav-card {
    border-radius: 24px !important;
    overflow: hidden;
}

.hrp-panel-title {
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    font-weight: 700;
    color: var(--owl-text) !important;
}

.hrp-quest-subtitle {
    color: var(--owl-muted) !important;
    letter-spacing: 0.02em;
}

.hrp-date-range {
    font-family: 'Inter', sans-serif !important;
    font-weight: 700;
    letter-spacing: -0.01em;
    color: var(--owl-text) !important;
}

.hrp-stat-heading {
    font-family: 'Plus Jakarta Sans', sans-serif !important;
    font-weight: 700;
    color: var(--owl-text) !important;
}

.hrp-stat-value {
    font-family: 'Inter', sans-serif !important;
    font-weight: 800;
    color: var(--owl-text) !important;
    letter-spacing: -0.02em;
}

.hrp-stat-label {
    color: var(--owl-muted) !important;
    font-size: 11px;
}

.hrp-matrix-task-col {
    background: var(--owl-surface) !important;
    border-right: 1px solid var(--owl-border);
    box-shadow: 2px 0 12px rgba(0,0,0,0.06);
}

body[data-theme="twilight_relic"] .hrp-matrix-task-col,
body[data-theme="midnight_arcade"] .hrp-matrix-task-col {
    background: var(--owl-surface-soft) !important;
    box-shadow: 2px 0 16px rgba(0,0,0,0.22);
}

.hrp-matrix-task-title {
    color: var(--owl-text) !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 600;
    font-size: 13px !important;
    line-height: 1.3;
}

.hrp-matrix-task-meta {
    color: var(--owl-muted) !important;
    font-size: 11px;
}

body[data-theme="twilight_relic"] .hrp-nav-card .q-btn,
body[data-theme="midnight_arcade"] .hrp-nav-card .q-btn,
body[data-theme="twilight_relic"] .hrp-nav-card .q-icon,
body[data-theme="midnight_arcade"] .hrp-nav-card .q-icon,
body[data-theme="twilight_relic"] .hrp-nav-card .q-checkbox__label,
body[data-theme="midnight_arcade"] .hrp-nav-card .q-checkbox__label,
body[data-theme="twilight_relic"] .hrp-nav-card .q-btn__content,
body[data-theme="midnight_arcade"] .hrp-nav-card .q-btn__content {
    color: var(--owl-text-soft) !important;
}

body[data-theme="twilight_relic"] .hrp-nav-card .q-btn[aria-pressed="true"],
body[data-theme="midnight_arcade"] .hrp-nav-card .q-btn[aria-pressed="true"],
body[data-theme="twilight_relic"] .hrp-nav-card .q-btn[aria-pressed="true"] .q-btn__content,
body[data-theme="midnight_arcade"] .hrp-nav-card .q-btn[aria-pressed="true"] .q-btn__content {
    color: var(--owl-text-soft) !important;
}

/* Map legacy inline colors to the new theme palette */
[style*="color: #0A2540"], [style*="color:#0A2540"], [style*="color: #0a2540"], [style*="color:#0a2540"] { color: var(--owl-text) !important; }
[style*="color: #64748b"], [style*="color:#64748b"], [style*="color: #64748B"], [style*="color:#64748B"] { color: var(--owl-muted) !important; }
[style*="color: #94a3b8"], [style*="color:#94a3b8"], [style*="color: #94A3B8"], [style*="color:#94A3B8"] { color: var(--owl-structure) !important; }
[style*="color: #00E5FF"], [style*="color:#00E5FF"], [style*="color: #00C2D1"], [style*="color:#00C2D1"] { color: var(--owl-accent) !important; }
[style*="background: #0A2540"], [style*="background:#0A2540"], [style*="background: #0a2540"], [style*="background:#0a2540"] { background: var(--owl-strong-surface) !important; }
[style*="background: #00C2D1"], [style*="background:#00C2D1"] { background: var(--owl-accent) !important; }
[style*="background: #ffffff"], [style*="background:#ffffff"] { background: var(--owl-surface) !important; }
[style*="background: #f8fafc"], [style*="background:#f8fafc"] { background: var(--owl-surface-soft) !important; }
[style*="color: #e2e8f0"], [style*="color:#e2e8f0"] { color: var(--owl-text-soft) !important; }
[style*="background: rgba(0,229,255,0.25)"], [style*="background: rgba(0,229,255,0.2)"] { background: var(--owl-accent-soft) !important; }

body[data-theme="twilight_relic"] [style*="color: #64748b"],
body[data-theme="twilight_relic"] [style*="color:#64748b"],
body[data-theme="twilight_relic"] [style*="color: #94a3b8"],
body[data-theme="twilight_relic"] [style*="color:#94a3b8"],
body[data-theme="midnight_arcade"] [style*="color: #64748b"],
body[data-theme="midnight_arcade"] [style*="color:#64748b"],
body[data-theme="midnight_arcade"] [style*="color: #94a3b8"],
body[data-theme="midnight_arcade"] [style*="color:#94a3b8"] {
    color: var(--owl-text-soft) !important;
}

/* Dark theme: fix all dialogs and cards with hardcoded colors */
body[data-theme="twilight_relic"] .q-dialog .q-card,
body[data-theme="midnight_arcade"] .q-dialog .q-card {
    background: var(--owl-surface) !important;
    border: 1px solid var(--owl-border);
}

body[data-theme="twilight_relic"] .q-dialog .q-card [style*="#0A2540"],
body[data-theme="midnight_arcade"] .q-dialog .q-card [style*="#0A2540"],
body[data-theme="twilight_relic"] .q-dialog .q-card [style*="#0a2540"],
body[data-theme="midnight_arcade"] .q-dialog .q-card [style*="#0a2540"] {
    color: var(--owl-text) !important;
}

body[data-theme="twilight_relic"] .q-dialog .q-card [style*="#64748b"],
body[data-theme="midnight_arcade"] .q-dialog .q-card [style*="#64748b"],
body[data-theme="twilight_relic"] .q-dialog .q-card [style*="#64748B"],
body[data-theme="midnight_arcade"] .q-dialog .q-card [style*="#64748B"] {
    color: var(--owl-muted) !important;
}

body[data-theme="twilight_relic"] .q-dialog .q-card [style*="background: #f8fafc"],
body[data-theme="midnight_arcade"] .q-dialog .q-card [style*="background: #f8fafc"],
body[data-theme="twilight_relic"] .q-dialog .q-card [style*="background: #ffffff"],
body[data-theme="midnight_arcade"] .q-dialog .q-card [style*="background: #ffffff"] {
    background: var(--owl-surface-soft) !important;
}

/* Also fix the nav card, user management cards, and any remaining hardcoded backgrounds */
body[data-theme="twilight_relic"] .hrp-nav-card,
body[data-theme="midnight_arcade"] .hrp-nav-card {
    background: var(--owl-surface) !important;
    border-color: var(--owl-border) !important;
}

/* Assign username labels in dark theme */
body[data-theme="twilight_relic"] [style*="color: #0A2540"],
body[data-theme="midnight_arcade"] [style*="color: #0A2540"],
body[data-theme="twilight_relic"] [style*="color: #0a2540"],
body[data-theme="midnight_arcade"] [style*="color: #0a2540"] {
    color: var(--owl-text) !important;
}

body[data-theme="twilight_relic"] [style*="background: #f8fafc"],
body[data-theme="midnight_arcade"] [style*="background: #f8fafc"] {
    background: var(--owl-surface-soft) !important;
}

body[data-theme="twilight_relic"] [style*="background: #ffffff"],
body[data-theme="midnight_arcade"] [style*="background: #ffffff"] {
    background: var(--owl-surface) !important;
}
</style>
"""

LOGIN_CSS = """
<style>
body, .q-page, .nicegui-content {
    background:
    radial-gradient(circle at 14% 18%, var(--owl-accent-soft), transparent 28%),
    radial-gradient(circle at 82% 14%, color-mix(in srgb, var(--owl-accent-2) 24%, transparent), transparent 22%),
    linear-gradient(135deg, var(--owl-bg) 0%, var(--owl-bg-alt) 100%),
        var(--owl-bg) !important;
}
</style>
"""

SORTABLE_JS = '<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js"></script>'

COMPLETION_EFFECT_ASSETS = """
<style>
.hrp-cell-actions {
    width: 100%;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
    margin-top: 8px;
}
.hrp-cell-actions .q-btn,
.hrp-list-actions .q-btn {
    min-width: 0 !important;
}
.hrp-list-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
    align-items: center;
    max-width: 240px;
}
.hrp-matrix-shell {
    border-radius: 20px;
    overflow-y: hidden;
    background: color-mix(in srgb, var(--owl-bg) 60%, var(--owl-surface)) !important;
    padding: 4px;
}
body[data-theme="twilight_relic"] .hrp-nav-card,
body[data-theme="midnight_arcade"] .hrp-nav-card,
body[data-theme="twilight_relic"] .hrp-stat-card,
body[data-theme="midnight_arcade"] .hrp-stat-card,
body[data-theme="twilight_relic"] .hrp-matrix-shell,
body[data-theme="midnight_arcade"] .hrp-matrix-shell {
    background: linear-gradient(180deg, var(--owl-strong-surface) 0%, var(--owl-surface) 100%) !important;
    border-color: color-mix(in srgb, var(--owl-structure) 26%, var(--owl-border)) !important;
}

body[data-theme="twilight_relic"] .hrp-matrix-cell,
body[data-theme="midnight_arcade"] .hrp-matrix-cell {
    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.04);
}
.hrp-celebration {
    position: fixed;
    inset: 0;
    z-index: 9999;
    display: flex;
    align-items: center;
    justify-content: center;
    background: radial-gradient(circle, rgba(255,255,255,0.10), rgba(0,0,0,0.16));
    backdrop-filter: blur(3px);
    animation: hrpCelebrateFade 0.9s ease forwards;
}
.hrp-celebration-panel {
    position: relative;
    min-width: 280px;
    padding: 22px 26px;
    border-radius: 24px;
    background: linear-gradient(135deg, #fff4bd 0%, #ffd166 45%, #7c4dff 100%);
    color: #1b1533;
    box-shadow: 0 20px 60px rgba(0,0,0,0.28);
    text-align: center;
    overflow: hidden;
}
.hrp-celebration-panel::before {
    content: "";
    position: absolute;
    inset: -40% auto auto -10%;
    width: 140px;
    height: 140px;
    background: radial-gradient(circle, rgba(255,255,255,0.68), transparent 70%);
}
.hrp-celebration-title {
    font-family: 'Orbitron', sans-serif;
    font-size: 20px;
    font-weight: 800;
    letter-spacing: 0.08em;
    margin-bottom: 6px;
}
.hrp-celebration-sub {
    font-family: 'Exo 2', sans-serif;
    font-size: 13px;
    letter-spacing: 0.06em;
}
.hrp-celebration-skip {
    position: absolute;
    top: 12px;
    right: 12px;
    border: 0;
    border-radius: 999px;
    padding: 5px 10px;
    background: rgba(27,21,51,0.14);
    color: #1b1533;
    font-weight: 700;
    cursor: pointer;
}
.hrp-spark {
    position: absolute;
    font-size: 22px;
    animation: hrpSparkFly 0.9s ease-out forwards;
    opacity: 0;
}
@keyframes hrpCelebrateFade {
    0% { opacity: 0; }
    8% { opacity: 1; }
    85% { opacity: 1; }
    100% { opacity: 0; }
}
@keyframes hrpSparkFly {
    0% { transform: translate(0, 0) scale(0.2) rotate(0deg); opacity: 0; }
    18% { opacity: 1; }
    100% { transform: translate(var(--dx), var(--dy)) scale(1.18) rotate(240deg); opacity: 0; }
}
</style>
<script>
window.hrpCelebrate = function() {
  if (window.__hrpCelebrateCleanup) {
    window.__hrpCelebrateCleanup();
  }
  const overlay = document.createElement('div');
  overlay.className = 'hrp-celebration';
  overlay.innerHTML = '<div class="hrp-celebration-panel"><button class="hrp-celebration-skip">Skip</button><div class="hrp-celebration-title">Quest Complete!</div><div class="hrp-celebration-sub">Shiny rewards. Zero dust monsters.</div></div>';
  const panel = overlay.querySelector('.hrp-celebration-panel');
  const sparkles = ['✦', '✧', '◆', '⚡', '🟡', '💠'];
  for (let i = 0; i < 18; i++) {
    const spark = document.createElement('div');
    spark.className = 'hrp-spark';
    spark.textContent = sparkles[i % sparkles.length];
    spark.style.left = '50%';
    spark.style.top = '50%';
    spark.style.setProperty('--dx', `${(Math.random() * 260 - 130).toFixed(0)}px`);
    spark.style.setProperty('--dy', `${(Math.random() * 220 - 160).toFixed(0)}px`);
    panel.appendChild(spark);
  }
  const cleanup = () => {
    if (overlay.isConnected) overlay.remove();
    if (window.__hrpCelebrateTimer) clearTimeout(window.__hrpCelebrateTimer);
    window.__hrpCelebrateCleanup = null;
  };
  overlay.addEventListener('click', cleanup);
  overlay.querySelector('.hrp-celebration-skip').addEventListener('click', (event) => {
    event.stopPropagation();
    cleanup();
  });
  document.body.appendChild(overlay);
  window.__hrpCelebrateCleanup = cleanup;
  window.__hrpCelebrateTimer = setTimeout(cleanup, 900);
};
</script>
"""


# ---------------------------------------------------------------------------
# NiceGUI pages
# ---------------------------------------------------------------------------


@ui.page("/login")
def login_page():
    theme_key = _normalize_theme(nicegui_app.storage.user.get("theme", "owl_light"))
    dark_mode = ui.dark_mode(_is_dark_theme(theme_key))
    ui.add_head_html(CUSTOM_CSS)
    ui.add_head_html(LOGIN_CSS)
    ui.run_javascript(f"document.body.setAttribute('data-theme', '{theme_key}')")

    with ui.card().classes("absolute-center w-[460px] rounded-2xl px-5 py-4 hrp-login-shell").style(
        "box-shadow: var(--owl-shadow); border: 1px solid var(--owl-border);"
    ):
        with ui.row().classes("w-full items-center justify-between mb-2"):
            with ui.row().classes("items-center gap-2"):
                ui.html('<div class="hrp-crest">💎</div>')
                with ui.column().classes("gap-0"):
                    ui.html('<div class="hrp-wordmark">bling<span>.</span>home</div>')
                    ui.label("Haushaltsplaner").classes("hrp-submark")
            theme_select = ui.select(THEME_OPTIONS, value=theme_key, label="Theme", on_change=lambda e: set_theme(e.value)).props("outlined dense options-dense").classes("hrp-theme-select")

        with ui.column().classes("w-full items-center gap-2 py-3"):
            ui.label("Willkommen zurück").classes("text-h5 font-bold hrp-panel-title")
            ui.label("Dein Haushaltsplaner.").classes("text-caption mb-1 hrp-quest-subtitle")
            ui.label("Aufgaben organisiert. Familie koordiniert.").classes("text-caption mb-2 hrp-quest-subtitle")

        username_input = ui.input("Benutzername").props("outlined rounded").classes("w-full")
        password_input = ui.input("Passwort", password=True, password_toggle_button=True).props("outlined rounded").classes("w-full")
        error_label = ui.label("").classes("text-red text-center w-full")

        def set_theme(theme: str):
            theme_value = _normalize_theme(theme)
            nicegui_app.storage.user["theme"] = theme_value
            if _is_dark_theme(theme_value):
                dark_mode.enable()
            else:
                dark_mode.disable()
            ui.run_javascript(f"document.body.setAttribute('data-theme', '{theme_value}')")

        def do_login():
            db = _get_db()
            user = db.query(User).filter(User.username == username_input.value.strip()).first()
            if not user or not verify_password(password_input.value, user.password_hash):
                error_label.text = "Ungültiger Benutzername oder Passwort"
                db.close()
                return
            token = create_access_token({"sub": user.id, "role": user.role.value})
            nicegui_app.storage.user["auth_token"] = token
            nicegui_app.storage.user["user_id"] = user.id
            nicegui_app.storage.user["username"] = user.username
            nicegui_app.storage.user["role"] = user.role.value
            db.close()
            ui.navigate.to("/")

        ui.button("Anmelden", on_click=do_login).props("rounded unelevated size=lg").classes("w-full mt-2")


@ui.page("/")
def main_page():
    theme_key = _normalize_theme(nicegui_app.storage.user.get("theme", "owl_light"))
    dark_mode = ui.dark_mode(_is_dark_theme(theme_key))
    ui.add_head_html(CUSTOM_CSS)
    ui.add_head_html(SORTABLE_JS)
    ui.add_head_html(COMPLETION_EFFECT_ASSETS)
    ui.run_javascript(f"document.body.setAttribute('data-theme', '{theme_key}')")
    user = _current_user(nicegui_app.storage.user)
    if not user:
        ui.navigate.to("/login")
        return

    is_admin = user.role == UserRole.ADMIN

    _today = date.today()
    _wstart = _today - timedelta(days=_today.weekday())
    state = {
        "view_mode": "week",
        "ref_date": _wstart,
        "display": "matrix",
    }

    def get_dates():
        start = state["ref_date"]
        if state["view_mode"] == "week":
            return _week_dates(start)
        if state["view_mode"] == "2weeks":
            return _two_week_dates(start)
        if state["view_mode"] == "4weeks":
            return _four_week_dates(start)
        return _week_dates(start)

    def _celebrate_completion():
        ui.run_javascript("window.hrpCelebrate && window.hrpCelebrate();")

    # --------------- Header ---------------
    with ui.header().classes("items-center justify-between px-6 py-3").style("position: relative !important;"):
        with ui.row().classes("items-center gap-3"):
            ui.html('<div class="hrp-crest">💎</div>')
            with ui.column().classes("gap-0"):
                ui.html('<div class="hrp-wordmark">bling<span>.</span>home</div>')
                ui.label("Haushaltsplaner").classes("hrp-submark")
        with ui.row().classes("items-center gap-3"):
            ui.icon("person", size="20px").style("color: var(--owl-accent);")
            ui.label(user.username).classes("text-sm font-medium").style("color: var(--owl-text);")

            def set_theme(theme: str):
                theme_value = _normalize_theme(theme)
                nicegui_app.storage.user["theme"] = theme_value
                if _is_dark_theme(theme_value):
                    dark_mode.enable()
                else:
                    dark_mode.disable()
                ui.run_javascript(f"document.body.setAttribute('data-theme', '{theme_value}')")

            ui.select(THEME_OPTIONS, value=theme_key, label="Theme", on_change=lambda e: set_theme(e.value)).props("outlined dense options-dense").classes("hrp-theme-select")
            ui.button("Logout", on_click=lambda: _logout(), icon="logout").props("flat rounded size=sm")

    def _logout():
        nicegui_app.storage.user.clear()
        ui.navigate.to("/login")

    # --------------- Navigation ---------------

    with ui.card().classes("w-full rounded-xl mx-4 mt-3 px-4 py-3 hrp-nav-card").style(
        "background: var(--owl-surface); box-shadow: 0 2px 8px var(--owl-border); border-bottom: 1px solid var(--owl-border); position: sticky; top: 0; z-index: 100;"
    ):
        with ui.row().classes("w-full items-center justify-center gap-4 flex-wrap"):
            def _prev():
                delta = {"week": timedelta(weeks=1), "2weeks": timedelta(weeks=2), "4weeks": timedelta(weeks=4)}.get(state["view_mode"], timedelta(weeks=1))
                state["ref_date"] -= delta
                rebuild()

            def _next():
                delta = {"week": timedelta(weeks=1), "2weeks": timedelta(weeks=2), "4weeks": timedelta(weeks=4)}.get(state["view_mode"], timedelta(weeks=1))
                state["ref_date"] += delta
                rebuild()

            ui.button(icon="chevron_left", on_click=_prev).props("flat round dense").style("color: var(--owl-text);")
            date_label = ui.label("").classes("text-subtitle1 min-w-[200px] text-center hrp-date-range")
            ui.button(icon="chevron_right", on_click=_next).props("flat round dense").style("color: var(--owl-text);")

            ui.separator().props("vertical").classes("h-6")

            def toggle_view(val):
                state["view_mode"] = val
                rebuild()

            ui.toggle(
                {"week": "1 Woche", "2weeks": "2 Wochen", "4weeks": "4 Wochen"},
                value="week",
                on_change=lambda e: toggle_view(e.value),
            ).props("rounded dense no-caps").style("color: var(--owl-text);")

            ui.separator().props("vertical").classes("h-6")

            def toggle_display(val):
                state["display"] = val
                rebuild()

            ui.toggle(
                {"matrix": "Matrix", "list": "Liste", "day": "Heute"},
                value="matrix",
                on_change=lambda e: toggle_display(e.value),
            ).props("rounded dense no-caps").style("color: var(--owl-text);")

            def go_today():
                today = date.today()
                state["ref_date"] = today - timedelta(days=today.weekday())
                rebuild()
            ui.button("Heute", icon="today", on_click=go_today).props("flat rounded dense no-caps").style("color: var(--owl-text);")

    # --------------- Containers ---------------
    matrix_container = ui.column().classes("w-full px-4 mt-3")
    mobile_container = ui.column().classes("w-full px-4 mt-3")
    day_container = ui.column().classes("w-full px-4 mt-3")
    stats_container = ui.column().classes("w-full px-4 mt-4 mb-8")

    # --------------- Helpers ---------------
    def _render_tags(tags: list[Tag]):
        if not tags:
            return
        with ui.row().classes("gap-1 flex-wrap"):
            for tag in tags:
                ui.html(f'<span class="hrp-tag" style="background:{tag.color}">{tag.name}</span>')

    def _render_user_chips(assigned_users: list, all_users: list[User]):
        if not assigned_users:
            return
        user_index = {u.id: idx for idx, u in enumerate(all_users)}
        with ui.row().classes("gap-1 flex-wrap"):
            for u in assigned_users:
                idx = user_index.get(u.id, 0)
                color = _user_color(idx)
                ui.html(f'<span class="hrp-user-chip" style="background:{color}">{u.username[:2].upper()} {u.username}</span>')

    # --------------- Weekday picker with "Täglich" ---------------
    def _weekday_picker(initial_days: list[int] | None = None, initial_mode: str = "weekly") -> tuple[dict[int, ui.checkbox], ui.checkbox, ui.toggle]:
        if initial_days is None:
            initial_days = []
        all_selected = set(initial_days) == set(range(7))
        ui.label("Wiederholen an:").classes("text-sm mt-2").style("color: #64748b;")
        checkboxes: dict[int, ui.checkbox] = {}

        def on_daily_change(e):
            for cb in checkboxes.values():
                cb.value = daily_cb.value

        with ui.row().classes("gap-1 flex-wrap items-center"):
            daily_cb = ui.checkbox("Täglich", value=all_selected, on_change=on_daily_change)
            ui.separator().props("vertical").classes("h-5 mx-1")
            for i, label in enumerate(WEEKDAY_LABELS):
                cb = ui.checkbox(label, value=(i in initial_days))
                checkboxes[i] = cb
            ui.separator().props("vertical").classes("h-5 mx-1")
            recurrence_mode = ui.toggle(
                {"weekly": "Wöchentlich", "biweekly": "Alle 2 Wochen", "4weekly": "Alle 4 Wochen"},
                value=initial_mode,
            ).props("rounded dense no-caps")
        return checkboxes, daily_cb, recurrence_mode

    def _selected_days_from_checkboxes(checkboxes: dict[int, ui.checkbox], daily_cb: ui.checkbox) -> list[int]:
        if daily_cb.value:
            return list(range(7))
        return [i for i, cb in checkboxes.items() if cb.value]

    # --------------- Assign dialog with "immer" / "jeden X" ---------------
    def _open_assign_dialog(instance_id: str, instance_date: date, task: Task, all_users: list[User], current_ids: list[str]):
        db = _get_db()
        # Always re-fetch users from a fresh session to avoid DetachedInstanceError
        fresh_users = db.query(User).order_by(User.username).all()
        weekday_label = WEEKDAY_LABELS[instance_date.weekday()]
        biweekly_mode = _build_biweekly_assignment_mode(instance_date)

        # Default until = 1 year from the instance date so recurring rules propagate properly
        _default_until = instance_date + timedelta(days=365)

        with ui.dialog() as dlg, ui.card().classes("w-[540px] rounded-xl").style("background: #ffffff;"):
            ui.label("Personen zuweisen").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
            ui.label(f"{task.title} – {instance_date.strftime('%A, %d.%m.%Y')}").classes("text-caption mb-2").style("color: #64748b;")

            with ui.row().classes("items-center gap-3 mb-2"):
                ui.label("Eintragen bis:").classes("text-sm font-medium").style("color: var(--owl-text);")
                until_input = ui.input(value=_default_until.isoformat()).props("outlined rounded dense type=date").style("max-width: 180px;")
                ui.label("(gilt für Intervall-Zuweisungen)").classes("text-xs").style("color: #94a3b8;")

            rows: list[dict] = []
            for idx, u in enumerate(fresh_users):
                color = _user_color(idx)
                is_assigned = u.id in current_ids
                mode = _get_assignment_mode(db, instance_id, u.id) if is_assigned else None

                with ui.card().classes("w-full mb-1 py-2 px-3 rounded-lg").style(
                    f"background: #f8fafc; border-left: 3px solid {color};"
                ):
                    with ui.row().classes("items-center gap-3 w-full"):
                        cb = ui.checkbox(value=is_assigned)
                        ui.html(f'<span class="hrp-user-chip" style="background:{color}">{u.username}</span>')
                        ui.label("→").classes("text-gray-500")
                        mode_select = ui.select(
                            {
                                "none": "Nur dieses Mal",
                                "immer": "⟳ Immer",
                                f"jeden_{instance_date.weekday()}": f"📅 Jeden {weekday_label}",
                                biweekly_mode: f"🗓️ Alle 2 Wochen ab diesem {weekday_label}",
                            },
                            value=mode or "none",
                            label="Modus",
                        ).props("dense outlined rounded").classes("flex-1")
                        if is_assigned:
                            remove_scope_select = ui.select(
                                {"single": "Nur diese", "all": "Alle im Zeitraum", "always": "Immer"},
                                value="single",
                                label="Entfernen:",
                            ).props("dense outlined rounded").style("min-width: 120px;")
                        else:
                            remove_scope_select = None
                        rows.append({"user_id": u.id, "cb": cb, "mode": mode_select, "was_assigned": is_assigned, "remove_scope": remove_scope_select})

            def save_assign():
                try:
                    until_date = date.fromisoformat(until_input.value) if until_input.value else _default_until
                except ValueError:
                    until_date = _default_until
                db2 = _get_db()
                instance = db2.query(TaskInstance).options(joinedload(TaskInstance.assigned_users)).get(instance_id)
                if not instance:
                    db2.close()
                    dlg.close()
                    return

                instance.assigned_users.clear()
                db2.flush()

                for r in rows:
                    if r["cb"].value:
                        u = db2.query(User).get(r["user_id"])
                        if u:
                            instance.assigned_users.append(u)
                            db2.flush()
                            mode_val = r["mode"].value
                            if mode_val != "none":
                                _set_assignment_mode(db2, instance_id, r["user_id"], mode_val)

                db2.commit()
                _apply_assignment_rules(db2, instance, rows, until_date)
                # Remove from instances if requested
                dates_now = get_dates()
                for r in rows:
                    if r.get("was_assigned") and not r["cb"].value and r.get("remove_scope"):
                        scope = r["remove_scope"].value
                        if scope == "all":
                            _remove_user_from_all_instances(db2, instance.task_id, r["user_id"], dates_now)
                        elif scope == "always":
                            all_insts = (
                                db2.query(TaskInstance)
                                .options(joinedload(TaskInstance.assigned_users))
                                .filter(TaskInstance.task_id == instance.task_id)
                                .all()
                            )
                            u_obj = db2.query(User).get(r["user_id"])
                            if u_obj:
                                for ai in all_insts:
                                    if u_obj in ai.assigned_users:
                                        ai.assigned_users.remove(u_obj)
                            db2.commit()
                db2.close()
                dlg.close()
                ui.notify("Zuweisung gespeichert", type="positive")
                rebuild()

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat rounded no-caps")
                ui.button("Speichern", on_click=save_assign).props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
        db.close()
        dlg.open()

    def _apply_assignment_rules(db: Session, source_instance: TaskInstance, rows: list[dict], until_date: date | None = None):
        task = db.query(Task).get(source_instance.task_id)
        if not task:
            return

        # Build date range: day after source instance up to until_date
        end_date = until_date or get_dates()[-1]
        all_dates: list[date] = []
        d = source_instance.date + timedelta(days=1)
        while d <= end_date:
            all_dates.append(d)
            d += timedelta(days=1)
        if not all_dates:
            return

        excluded = _get_excluded_dates(task)

        # Load existing instances in the range
        existing_instances = (
            db.query(TaskInstance)
            .options(joinedload(TaskInstance.assigned_users))
            .filter(TaskInstance.task_id == source_instance.task_id, TaskInstance.date.in_(all_dates))
            .all()
        )
        existing_by_date: dict[date, TaskInstance] = {inst.date: inst for inst in existing_instances}

        for r in rows:
            if not r["cb"].value:
                continue
            mode_val = r["mode"].value
            if mode_val == "none":
                continue
            uid = r["user_id"]
            u = db.query(User).get(uid)
            if not u:
                continue
            for d in all_dates:
                if d in excluded:
                    continue
                # Check assignment mode interval
                should_assign = False
                if mode_val == "immer":
                    should_assign = True
                elif mode_val.startswith("jeden_"):
                    weekday = int(mode_val.split("_")[1])
                    if d.weekday() == weekday:
                        should_assign = True
                elif mode_val.startswith("2weeks|"):
                    assignment_cfg = _parse_assignment_mode_config(mode_val, source_instance.date)
                    if d >= assignment_cfg["anchor"] and d.weekday() == assignment_cfg["weekday"]:
                        should_assign = (_week_start(d) - _week_start(assignment_cfg["anchor"])).days % 14 == 0
                if not should_assign:
                    continue
                # Check task recurrence
                if not _recurrence_matches(task.recurrence_rule, d):
                    continue
                # Get or create instance
                if d in existing_by_date:
                    inst = existing_by_date[d]
                else:
                    inst = TaskInstance(
                        id=str(uuid.uuid4()),
                        task_id=task.id,
                        date=d,
                        status=TaskStatus.OPEN,
                    )
                    db.add(inst)
                    db.flush()
                    existing_by_date[d] = inst
                if uid not in [u2.id for u2 in inst.assigned_users]:
                    inst.assigned_users.append(u)
        db.commit()

    # --------------- Task dialogs ---------------
    def _open_add_task_dialog():
        db = _get_db()
        all_tags = db.query(Tag).order_by(Tag.name).all()
        tag_options = {t.id: t.name for t in all_tags}

        with ui.dialog() as dlg, ui.card().classes("w-[480px] rounded-xl").style("background: #ffffff;"):
            ui.label("Neue Aufgabe").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
            title_in = ui.input("Titel").props("outlined rounded").classes("w-full")
            desc_in = ui.textarea("Beschreibung (optional)").props("outlined rounded autogrow").classes("w-full")
            dur_in = ui.number("Dauer (Min.)", value=30, min=1, max=1440).props("outlined rounded").classes("w-full")

            if tag_options:
                tag_select = ui.select(options=tag_options, multiple=True, label="Tags").props("outlined rounded dense use-chips emit-value map-options").classes("w-full")
            else:
                tag_select = None

            day_cbs, daily_cb, recurrence_mode = _weekday_picker()
            ui.separator().classes("my-1")
            ui.label("Datum (für einmalige Aufgaben sowie als Startdatum für 'Alle 2 Wochen' oder 'Alle 4 Wochen'): ").classes("text-caption").style("color: #64748b;")
            date_in = ui.input(value=date.today().isoformat()).props("outlined rounded dense type=date").classes("w-full")

            def save():
                selected = _selected_days_from_checkboxes(day_cbs, daily_cb)
                recurrence_kind = recurrence_mode.value or "weekly"
                try:
                    anchor_date = date.fromisoformat(date_in.value) if date_in.value else date.today()
                except ValueError:
                    anchor_date = date.today()
                recurrence_rule = _build_recurrence_rule(recurrence_kind, selected, anchor_date)
                db2 = _get_db()
                max_order = db2.query(Task.sort_order).order_by(Task.sort_order.desc()).first()
                next_order = (max_order[0] + 1) if max_order and max_order[0] is not None else 0
                t = Task(
                    id=str(uuid.uuid4()),
                    title=title_in.value.strip(),
                    description=desc_in.value.strip() or None,
                    base_duration_minutes=int(dur_in.value),
                    is_recurring=bool(recurrence_rule),
                    recurrence_rule=recurrence_rule,
                    sort_order=next_order,
                )
                if tag_select and tag_select.value:
                    for tid in tag_select.value:
                        tag = db2.query(Tag).get(tid)
                        if tag:
                            t.tags.append(tag)
                db2.add(t)
                db2.flush()
                if not recurrence_rule and date_in.value:
                    try:
                        one_time_date = date.fromisoformat(date_in.value)
                        inst_obj = TaskInstance(id=str(uuid.uuid4()), task_id=t.id, date=one_time_date, status=TaskStatus.OPEN)
                        db2.add(inst_obj)
                    except ValueError:
                        pass
                db2.commit()
                db2.close()
                dlg.close()
                ui.notify("Aufgabe erstellt!", type="positive")
                rebuild()

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat rounded no-caps")
                ui.button("Speichern", on_click=save).props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
        db.close()
        dlg.open()

    def _open_edit_task_dialog(task_id: str):
        db = _get_db()
        task = db.query(Task).options(joinedload(Task.tags), joinedload(Task.instances)).get(task_id)
        if not task:
            db.close()
            return
        recurrence_cfg = _parse_recurrence_rule(task.recurrence_rule)
        current_days = recurrence_cfg["days"]
        current_mode = recurrence_cfg["kind"]
        current_tag_ids = [t.id for t in task.tags]
        first_instance_date = min((inst.date for inst in task.instances), default=date.today())
        if recurrence_cfg["anchor"]:
            current_anchor = recurrence_cfg["anchor"]
        elif recurrence_cfg["kind"] == "monthly" and recurrence_cfg["month_day"]:
            today = date.today()
            current_anchor = date(today.year, today.month, min(recurrence_cfg["month_day"], _last_day_of_month(today.year, today.month)))
        else:
            current_anchor = first_instance_date
        all_tags = db.query(Tag).order_by(Tag.name).all()
        tag_options = {t.id: t.name for t in all_tags}

        with ui.dialog() as dlg, ui.card().classes("w-[480px] rounded-xl").style("background: #ffffff;"):
            ui.label("Aufgabe bearbeiten").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
            title_in = ui.input("Titel", value=task.title).props("outlined rounded").classes("w-full")
            desc_in = ui.textarea("Beschreibung", value=task.description or "").props("outlined rounded autogrow").classes("w-full")
            dur_in = ui.number("Dauer (Min.)", value=task.base_duration_minutes, min=1, max=1440).props("outlined rounded").classes("w-full")

            if tag_options:
                tag_select = ui.select(options=tag_options, value=current_tag_ids, multiple=True, label="Tags").props("outlined rounded dense use-chips emit-value map-options").classes("w-full")
            else:
                tag_select = None

            day_cbs, daily_cb, recurrence_mode = _weekday_picker(current_days, current_mode)
            ui.separator().classes("my-1")
            ui.label("Datum (für einmalige Aufgaben sowie als Startdatum für 'Alle 2 Wochen' oder 'Alle 4 Wochen'): ").classes("text-caption").style("color: #64748b;")
            date_in = ui.input(value=current_anchor.isoformat()).props("outlined rounded dense type=date").classes("w-full")

            def save():
                selected = _selected_days_from_checkboxes(day_cbs, daily_cb)
                recurrence_kind = recurrence_mode.value or "weekly"
                try:
                    anchor_date = date.fromisoformat(date_in.value) if date_in.value else date.today()
                except ValueError:
                    anchor_date = date.today()
                recurrence_rule = _build_recurrence_rule(recurrence_kind, selected, anchor_date)
                db2 = _get_db()
                t = db2.query(Task).options(joinedload(Task.tags)).get(task_id)
                t.title = title_in.value.strip()
                t.description = desc_in.value.strip() or None
                t.base_duration_minutes = int(dur_in.value)
                t.is_recurring = bool(recurrence_rule)
                t.recurrence_rule = recurrence_rule
                t.tags.clear()
                if tag_select and tag_select.value:
                    for tid in tag_select.value:
                        tag = db2.query(Tag).get(tid)
                        if tag:
                            t.tags.append(tag)
                db2.commit()
                db2.close()
                dlg.close()
                ui.notify("Aufgabe aktualisiert!", type="positive")
                rebuild()

            def delete():
                db2 = _get_db()
                t = db2.query(Task).get(task_id)
                if t:
                    db2.delete(t)
                    db2.commit()
                db2.close()
                dlg.close()
                ui.notify("Aufgabe gelöscht", type="warning")
                rebuild()

            with ui.row().classes("w-full justify-between mt-3"):
                ui.button("Löschen", on_click=delete, icon="delete").props("flat rounded no-caps color=red")
                with ui.row().classes("gap-2"):
                    ui.button("Abbrechen", on_click=dlg.close).props("flat rounded no-caps")
                    ui.button("Speichern", on_click=save).props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
        db.close()
        dlg.open()

    # --------------- Notes dialog ---------------
    def _open_notes_dialog(instance_id: str, instance_date: date, task_title: str):
        db = _get_db()
        inst = db.query(TaskInstance).get(instance_id)
        current_note = inst.notes or "" if inst else ""
        db.close()

        with ui.dialog() as dlg, ui.card().classes("w-[440px] rounded-xl").style("background: #ffffff;"):
            ui.label("Notiz").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
            ui.label(f"{task_title} – {instance_date.strftime('%d.%m.%Y')}").classes("text-caption mb-2").style("color: #64748b;")
            note_area = ui.textarea("Notiz", value=current_note).props("outlined rounded autogrow").classes("w-full")

            def save_note():
                db2 = _get_db()
                inst2 = db2.query(TaskInstance).get(instance_id)
                if inst2:
                    inst2.notes = note_area.value.strip() or None
                    db2.commit()
                db2.close()
                dlg.close()
                ui.notify("Notiz gespeichert", type="positive")
                rebuild()

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat rounded no-caps")
                ui.button("Speichern", on_click=save_note).props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
        dlg.open()

    # --------------- Preset dialogs ---------------
    def _open_save_preset_dialog():
        db = _get_db()
        dates = get_dates()
        tasks = db.query(Task).options(joinedload(Task.tags)).order_by(Task.sort_order, Task.title).all()
        
        # Get all instances in the current date range
        instances = (
            db.query(TaskInstance)
            .options(joinedload(TaskInstance.assigned_users), joinedload(TaskInstance.task))
            .filter(TaskInstance.date.in_(dates))
            .all()
        )
        
        # Group instances by task
        task_instances_map = defaultdict(list)
        for inst in instances:
            if inst.assigned_users:  # Only include instances with assignments
                task_instances_map[inst.task_id].append(inst)
        
        # Filter tasks that have at least one assigned instance
        tasks_with_assignments = [t for t in tasks if t.id in task_instances_map]
        
        db.close()
        
        if not tasks_with_assignments:
            ui.notify("Keine zugewiesenen Aufgaben im aktuellen Zeitraum", type="warning")
            return
        
        with ui.dialog() as dlg, ui.card().classes("w-[600px] rounded-xl max-h-[80vh] overflow-y-auto").style("background: #ffffff;"):
            ui.label("Preset speichern").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
            ui.label("Wählen Sie die Aufgaben und Zuweisungen, die Sie als Preset speichern möchten.").classes("text-caption mb-3").style("color: #64748b;")
            
            preset_name_input = ui.input("Preset Name").props("outlined rounded").classes("w-full mb-3")
            
            # Period type selection
            period_type_toggle = ui.toggle(
                {"week": "1 Woche", "two_weeks": "2 Wochen", "four_weeks": "4 Wochen"},
                value="week"
            ).props("rounded dense no-caps").classes("mb-3")
            
            # Start date selection (default to current view's start date)
            start_date_val = dates[0] if dates else date.today()
            start_date_input = ui.input(value=start_date_val.isoformat()).props("outlined rounded dense type=date").classes("w-full mb-3")
            ui.label("Startdatum (Referenz für das Preset)").classes("text-xs mb-3").style("color: #64748b;")
            
            ui.separator().classes("my-2")
            ui.label("Aufgaben auswählen:").classes("text-subtitle2 font-bold mb-2")
            
            task_checkboxes = {}
            
            for task in tasks_with_assignments:
                with ui.card().classes("w-full mb-2 py-2 px-3 rounded-lg").style("background: #f8fafc;"):
                    task_cb = ui.checkbox(task.title, value=True).classes("font-medium")
                    task_checkboxes[task.id] = task_cb
                    
                    # Show assigned instances for this task
                    task_insts = task_instances_map[task.id]
                    with ui.column().classes("ml-6 mt-1 gap-1"):
                        for inst in task_insts:
                            if inst.assigned_users:
                                users_str = ", ".join([u.username for u in inst.assigned_users])
                                ui.label(f"📅 {inst.date.strftime('%d.%m.%Y')} → {users_str}").classes("text-xs").style("color: #64748b;")
            
            def save_preset():
                preset_name = preset_name_input.value.strip()
                if not preset_name:
                    ui.notify("Bitte geben Sie einen Preset-Namen ein", type="negative")
                    return
                
                selected_task_ids = [tid for tid, cb in task_checkboxes.items() if cb.value]
                if not selected_task_ids:
                    ui.notify("Bitte wählen Sie mindestens eine Aufgabe aus", type="negative")
                    return
                
                try:
                    ref_start_date = date.fromisoformat(start_date_input.value)
                except ValueError:
                    ui.notify("Ungültiges Startdatum", type="negative")
                    return
                
                period_type = period_type_toggle.value
                max_days = 7 if period_type == "week" else 28 if period_type == "four_weeks" else 14
                
                db2 = _get_db()
                
                # Create preset
                preset = Preset(
                    id=str(uuid.uuid4()),
                    name=preset_name,
                    period_type=period_type,
                    start_date=ref_start_date,
                    created_at=datetime.now().isoformat()
                )
                db2.add(preset)
                db2.flush()
                
                # Add preset items
                for task_id in selected_task_ids:
                    task_insts = task_instances_map[task_id]
                    task_obj = db2.query(Task).get(task_id)
                    if not task_obj:
                        continue
                    
                    for inst in task_insts:
                        if not inst.assigned_users:
                            continue
                        
                        # Calculate day offset from reference start date
                        day_offset = (inst.date - ref_start_date).days
                        if day_offset < 0 or day_offset >= max_days:
                            continue  # Skip instances outside the period
                        
                        # Create a preset item for each assigned user
                        for user in inst.assigned_users:
                            preset_item = PresetItem(
                                id=str(uuid.uuid4()),
                                preset_id=preset.id,
                                task_id=task_obj.id,
                                task_title=task_obj.title,
                                day_offset=day_offset,
                                assigned_user_id=user.id,
                                assigned_username=user.username
                            )
                            db2.add(preset_item)
                
                db2.commit()
                db2.close()
                
                dlg.close()
                ui.notify(f"Preset '{preset_name}' gespeichert!", type="positive")
            
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat rounded no-caps")
                ui.button("Speichern", on_click=save_preset, icon="save").props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
        
        dlg.open()

    def _open_apply_preset_dialog():
        db = _get_db()
        presets = db.query(Preset).order_by(Preset.created_at.desc()).all()
        db.close()
        
        if not presets:
            ui.notify("Keine Presets vorhanden. Bitte erstellen Sie zuerst ein Preset.", type="warning")
            return
        
        # Get current view's start date
        dates = get_dates()
        current_start_date = dates[0] if dates else date.today()
        
        with ui.dialog() as dlg, ui.card().classes("w-[600px] rounded-xl max-h-[80vh] overflow-y-auto").style("background: #ffffff;"):
            ui.label("Preset anwenden").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
            ui.label("Wählen Sie ein Preset und konfigurieren Sie die Anwendung.").classes("text-caption mb-3").style("color: #64748b;")
            
            # Preset selection
            preset_options = {p.id: f"{p.name} ({p.period_type}, {p.start_date.strftime('%d.%m.%Y')})" for p in presets}
            preset_select = ui.select(preset_options, label="Preset auswählen").props("outlined rounded").classes("w-full mb-3")
            
            # Start date for application
            apply_start_date = ui.input(value=current_start_date.isoformat()).props("outlined rounded dense type=date").classes("w-full mb-2")
            ui.label("Startdatum für die Anwendung").classes("text-xs mb-3").style("color: #64748b;")
            
            # Repeat count
            repeat_count = ui.number("Wiederholungen", value=1, min=1, max=52).props("outlined rounded dense").classes("w-full mb-3")
            ui.label("Anzahl der Wiederholungen (z.B. 2 = zweimal anwenden)").classes("text-xs mb-3").style("color: #64748b;")
            
            # Task selection container
            task_selection_container = ui.column().classes("w-full")
            
            def update_task_selection():
                task_selection_container.clear()
                if not preset_select.value:
                    return
                
                db2 = _get_db()
                preset = db2.query(Preset).options(joinedload(Preset.items)).get(preset_select.value)
                if not preset:
                    db2.close()
                    return
                
                # Group items by task_id
                task_items_map = defaultdict(list)
                for item in preset.items:
                    task_items_map[item.task_id].append(item)
                
                with task_selection_container:
                    ui.separator().classes("my-2")
                    ui.label("Aufgaben im Preset:").classes("text-subtitle2 font-bold mb-2")
                    
                    task_checkboxes = {}
                    for task_id, items in task_items_map.items():
                        task_title = items[0].task_title
                        with ui.card().classes("w-full mb-2 py-2 px-3 rounded-lg").style("background: #f8fafc;"):
                            task_cb = ui.checkbox(task_title, value=True).classes("font-medium")
                            task_checkboxes[task_id] = task_cb
                            
                            # Show details
                            with ui.column().classes("ml-6 mt-1 gap-1"):
                                for item in items:
                                    ui.label(f"Tag {item.day_offset} → {item.assigned_username}").classes("text-xs").style("color: #64748b;")
                    
                    # Store checkboxes for save function
                    task_selection_container.task_checkboxes = task_checkboxes
                
                db2.close()
            
            preset_select.on_value_change(lambda: update_task_selection())
            
            def apply_preset():
                if not preset_select.value:
                    ui.notify("Bitte wählen Sie ein Preset aus", type="negative")
                    return
                
                try:
                    apply_start = date.fromisoformat(apply_start_date.value)
                except ValueError:
                    ui.notify("Ungültiges Startdatum", type="negative")
                    return
                
                repeat_times = int(repeat_count.value)
                
                # Get selected tasks
                selected_task_ids = []
                if hasattr(task_selection_container, 'task_checkboxes'):
                    selected_task_ids = [tid for tid, cb in task_selection_container.task_checkboxes.items() if cb.value]
                
                if not selected_task_ids:
                    ui.notify("Bitte wählen Sie mindestens eine Aufgabe aus", type="negative")
                    return
                
                db2 = _get_db()
                preset = db2.query(Preset).options(joinedload(Preset.items)).get(preset_select.value)
                if not preset:
                    db2.close()
                    ui.notify("Preset nicht gefunden", type="negative")
                    return
                
                # Save preset name before closing session
                preset_name = preset.name
                period_days = 7 if preset.period_type == "week" else 28 if preset.period_type == "four_weeks" else 14
                
                # Apply preset for each repetition
                for rep in range(repeat_times):
                    rep_start_date = apply_start + timedelta(days=rep * period_days)
                    
                    for item in preset.items:
                        if item.task_id not in selected_task_ids:
                            continue
                        
                        # Calculate the actual date for this item
                        actual_date = rep_start_date + timedelta(days=item.day_offset)
                        
                        # Find or create task
                        task = db2.query(Task).get(item.task_id)
                        if not task:
                            # Task doesn't exist, create it
                            task = Task(
                                id=item.task_id,
                                title=item.task_title,
                                base_duration_minutes=30,
                                is_recurring=False,
                                sort_order=0
                            )
                            db2.add(task)
                            db2.flush()
                        
                        # Find or create task instance
                        inst = db2.query(TaskInstance).filter(
                            TaskInstance.task_id == task.id,
                            TaskInstance.date == actual_date
                        ).first()
                        
                        if not inst:
                            inst = TaskInstance(
                                id=str(uuid.uuid4()),
                                task_id=task.id,
                                date=actual_date,
                                status=TaskStatus.OPEN,
                                notes=f"Hinzugefügt am {date.today().isoformat()} durch Preset '{preset_name}'"
                            )
                            db2.add(inst)
                            db2.flush()
                        else:
                            # Append to existing notes
                            if inst.notes:
                                inst.notes += f"\nPreset '{preset_name}' angewendet am {date.today().isoformat()}"
                            else:
                                inst.notes = f"Preset '{preset_name}' angewendet am {date.today().isoformat()}"
                        
                        # Find or create user
                        user = db2.query(User).get(item.assigned_user_id)
                        if not user:
                            # User doesn't exist anymore, skip assignment
                            continue
                        
                        # Add user to instance if not already assigned
                        if user not in inst.assigned_users:
                            inst.assigned_users.append(user)
                
                db2.commit()
                db2.close()
                
                ui.notify(f"Preset '{preset_name}' erfolgreich angewendet!", type="positive")
                dlg.close()
                rebuild()
            
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat rounded no-caps")
                ui.button("Anwenden", on_click=apply_preset, icon="play_arrow").props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
        
        # Initial load
        if presets:
            preset_select.value = presets[0].id
            update_task_selection()
        
        dlg.open()

    def _open_manage_presets_dialog():
        with ui.dialog() as dlg, ui.card().classes("w-[600px] rounded-xl max-h-[80vh] overflow-y-auto").style("background: #ffffff;"):
            ui.label("Presets verwalten").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
            presets_container = ui.column().classes("w-full mt-3")
            
            def refresh_presets():
                presets_container.clear()
                db = _get_db()
                presets = db.query(Preset).options(joinedload(Preset.items)).order_by(Preset.created_at.desc()).all()
                
                with presets_container:
                    if not presets:
                        ui.label("Keine Presets vorhanden").classes("text-gray-400 text-sm")
                    else:
                        for preset in presets:
                            period_label = "1 Woche" if preset.period_type == "week" else "2 Wochen"
                            item_count = len(preset.items)
                            unique_tasks = len(set(item.task_id for item in preset.items))
                            
                            with ui.card().classes("w-full mb-2 py-2 px-3 rounded-lg").style("background: #f8fafc; border-left: 3px solid #00C2D1;"):
                                with ui.row().classes("w-full items-center justify-between"):
                                    with ui.column().classes("gap-1"):
                                        ui.label(preset.name).classes("font-bold text-base").style("color: #0A2540;")
                                        ui.label(f"{period_label} | Start: {preset.start_date.strftime('%d.%m.%Y')}").classes("text-xs").style("color: #64748b;")
                                        ui.label(f"{unique_tasks} Aufgaben, {item_count} Zuweisungen").classes("text-xs").style("color: #64748b;")
                                    
                                    with ui.row().classes("gap-1"):
                                        def view_preset(pid=preset.id):
                                            db2 = _get_db()
                                            p = db2.query(Preset).options(joinedload(Preset.items)).get(pid)
                                            if p:
                                                task_items = defaultdict(list)
                                                for item in p.items:
                                                    task_items[item.task_title].append((item.day_offset, item.assigned_username))
                                                
                                                details = []
                                                for task_title, assignments in task_items.items():
                                                    details.append(f"**{task_title}**")
                                                    for day_offset, username in sorted(assignments):
                                                        details.append(f"  Tag {day_offset}: {username}")
                                                
                                                with ui.dialog() as view_dlg, ui.card().classes("w-[500px] rounded-xl").style("background: #ffffff;"):
                                                    ui.label(f"Preset: {p.name}").classes("text-h6 font-bold mb-3").style("color: #0A2540;")
                                                    with ui.column().classes("w-full gap-1"):
                                                        for detail in details:
                                                            if detail.startswith("**"):
                                                                ui.label(detail.strip("*")).classes("font-bold mt-2").style("color: #0A2540;")
                                                            else:
                                                                ui.label(detail).classes("text-sm ml-4").style("color: #64748b;")
                                                    ui.button("Schließen", on_click=view_dlg.close).props("flat rounded no-caps").classes("mt-3")
                                                view_dlg.open()
                                            db2.close()
                                        
                                        ui.button(icon="visibility", on_click=view_preset).props("flat round dense size=sm").style("color: #00C2D1;")
                                        
                                        def delete_preset(pid=preset.id):
                                            db2 = _get_db()
                                            p = db2.query(Preset).get(pid)
                                            if p:
                                                db2.delete(p)
                                                db2.commit()
                                            db2.close()
                                            refresh_presets()
                                            ui.notify("Preset gelöscht", type="warning")
                                        
                                        ui.button(icon="delete", on_click=delete_preset).props("flat round dense size=sm color=red")
                
                db.close()
            
            refresh_presets()
            
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("Schließen", on_click=dlg.close).props("flat rounded no-caps")
        
        dlg.open()

    def _open_manage_tags_dialog():
        with ui.dialog() as dlg, ui.card().classes("w-[450px] rounded-xl").style("background: #ffffff;"):
            ui.label("Tags verwalten").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
            tags_container = ui.column().classes("w-full")

            def refresh_tags():
                tags_container.clear()
                db = _get_db()
                tags = db.query(Tag).order_by(Tag.name).all()
                with tags_container:
                    if not tags:
                        ui.label("Noch keine Tags erstellt").classes("text-gray-400 text-sm")
                    for t in tags:
                        with ui.row().classes("items-center gap-2 w-full"):
                            ui.html(f'<span class="hrp-tag" style="background:{t.color}">{t.name}</span>')
                            ui.button(icon="delete", on_click=lambda tid=t.id: del_tag(tid)).props("flat round dense size=xs color=red")
                db.close()

            def del_tag(tid):
                db = _get_db()
                t = db.query(Tag).get(tid)
                if t:
                    db.delete(t)
                    db.commit()
                db.close()
                refresh_tags()

            refresh_tags()

            ui.separator().classes("my-2")
            ui.label("Neuen Tag erstellen").classes("text-subtitle2 font-bold")
            tag_name_in = ui.input("Name").props("outlined rounded dense").classes("w-full")
            tag_color_in = ui.select(
                {c: f"● {c}" for c in TAG_PRESET_COLORS},
                value=TAG_PRESET_COLORS[0],
            ).props("outlined rounded dense emit-value").classes("w-full")

            def add_tag():
                db = _get_db()
                name = tag_name_in.value.strip()
                if not name:
                    ui.notify("Name erforderlich", type="negative")
                    db.close()
                    return
                if db.query(Tag).filter(Tag.name == name).first():
                    ui.notify("Tag existiert bereits", type="negative")
                    db.close()
                    return
                t = Tag(id=str(uuid.uuid4()), name=name, color=tag_color_in.value)
                db.add(t)
                db.commit()
                db.close()
                ui.notify("Tag erstellt!", type="positive")
                tag_name_in.value = ""
                refresh_tags()

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Schließen", on_click=dlg.close).props("flat rounded no-caps")
                ui.button("Tag erstellen", on_click=add_tag, icon="new_label").props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
        dlg.open()

    # --------------- User management ---------------
    def _open_manage_users_dialog():
        with ui.dialog() as dlg, ui.card().classes("w-[600px] rounded-xl").style("background: #ffffff;"):
            ui.label("Benutzerverwaltung").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
            users_list_container = ui.column().classes("w-full")

            def refresh_users_list():
                users_list_container.clear()
                db = _get_db()
                all_users = db.query(User).all()
                with users_list_container:
                    for idx, u in enumerate(all_users):
                        color = _user_color(idx)
                        with ui.card().classes("w-full mb-1 rounded-lg py-2 px-3").style(
                            f"background: #f8fafc; border-left: 3px solid {color};"
                        ):
                            with ui.row().classes("w-full items-center justify-between"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.html(f'<span class="hrp-user-chip" style="background:{color}">{u.username[:2].upper()}</span>')
                                    ui.label(u.username).classes("font-medium")
                                    ui.badge(u.role.value, color=("cyan" if u.role == UserRole.ADMIN else "grey")).props("rounded")
                                    if u.can_self_assign:
                                        ui.badge("Selbst-Zuweisung", color="teal").props("rounded outline")
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(f"{u.daily_capacity_minutes} Min/Tag").classes("text-xs").style("color: #64748b;")
                                    if u.username != "admin":
                                        ui.button(icon="edit", on_click=lambda uid=u.id: _edit_user(uid)).props("flat round dense size=sm").style("color: #00C2D1;")
                                        ui.button(icon="delete", on_click=lambda uid=u.id: _delete_user(uid)).props("flat round dense size=sm color=red")
                db.close()

            def _edit_user(uid):
                db = _get_db()
                u = db.query(User).get(uid)
                if not u:
                    db.close()
                    return
                u_role = u.role.value
                u_cap = u.daily_capacity_minutes
                u_sa = u.can_self_assign
                db.close()
                with ui.dialog() as edit_dlg, ui.card().classes("w-[400px] rounded-xl").style("background: #ffffff;"):
                    ui.label(f"Benutzer: {u.username}").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
                    edit_role = ui.select({"ADMIN": "Admin", "USER": "User"}, value=u_role, label="Rolle").props("outlined rounded dense").classes("w-full")
                    edit_cap = ui.number("Kapazität (Min/Tag)", value=u_cap, min=0, max=1440).props("outlined rounded dense").classes("w-full")
                    edit_sa = ui.checkbox("Darf sich selbst Aufgaben zuweisen", value=u_sa)
                    edit_pw = ui.input("Neues Passwort (leer = unverändert)", password=True).props("outlined rounded dense").classes("w-full")

                    def save_edit(uid=uid):
                        db2 = _get_db()
                        u2 = db2.query(User).get(uid)
                        if u2:
                            u2.role = UserRole(edit_role.value)
                            u2.daily_capacity_minutes = int(edit_cap.value)
                            u2.can_self_assign = edit_sa.value
                            if edit_pw.value.strip():
                                u2.password_hash = hash_password(edit_pw.value.strip())
                            db2.commit()
                        db2.close()
                        edit_dlg.close()
                        ui.notify("Benutzer aktualisiert!", type="positive")
                        refresh_users_list()
                        rebuild()

                    with ui.row().classes("w-full justify-end gap-2 mt-3"):
                        ui.button("Abbrechen", on_click=edit_dlg.close).props("flat rounded no-caps")
                        ui.button("Speichern", on_click=save_edit).props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
                edit_dlg.open()

            def _delete_user(uid):
                db = _get_db()
                u = db.query(User).get(uid)
                if u:
                    db.delete(u)
                    db.commit()
                db.close()
                refresh_users_list()
                ui.notify("Benutzer gelöscht", type="warning")

            refresh_users_list()

            ui.separator().classes("my-3")
            ui.label("Neuen Benutzer anlegen").classes("text-subtitle1 font-bold").style("color: #0A2540;")
            new_user = ui.input("Benutzername").props("outlined rounded dense").classes("w-full")
            new_pw = ui.input("Passwort", password=True).props("outlined rounded dense").classes("w-full")
            new_role = ui.select({"ADMIN": "Admin", "USER": "User"}, value="USER").props("outlined rounded dense").classes("w-full")
            new_cap = ui.number("Kapazität (Min/Tag)", value=480, min=0, max=1440).props("outlined rounded dense").classes("w-full")
            new_self_assign = ui.checkbox("Darf sich selbst Aufgaben zuweisen", value=False)

            def add_user():
                db = _get_db()
                if db.query(User).filter(User.username == new_user.value.strip()).first():
                    ui.notify("Benutzername existiert bereits", type="negative")
                    db.close()
                    return
                u = User(
                    id=str(uuid.uuid4()),
                    username=new_user.value.strip(),
                    password_hash=hash_password(new_pw.value),
                    role=UserRole(new_role.value),
                    daily_capacity_minutes=int(new_cap.value),
                    can_self_assign=new_self_assign.value,
                )
                db.add(u)
                db.commit()
                db.close()
                ui.notify("Benutzer erstellt!", type="positive")
                refresh_users_list()
                rebuild()

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("Schließen", on_click=dlg.close).props("flat rounded no-caps")
                ui.button("Benutzer anlegen", on_click=add_user, icon="person_add").props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
        dlg.open()

    # --------------- Build / Rebuild ---------------

    def rebuild():
        matrix_container.clear()
        mobile_container.clear()
        day_container.clear()
        if state["display"] == "matrix":
            _build_matrix()
        elif state["display"] == "list":
            _build_list()
        elif state["display"] == "day":
            _build_day_view()
        _build_stats()

    def _build_matrix():
        matrix_container.clear()
        db = _get_db()
        tasks = db.query(Task).options(joinedload(Task.tags)).order_by(Task.sort_order, Task.title).all()
        users_all = db.query(User).order_by(User.username).all()
        dates = get_dates()

        _ensure_recurring_instances(db, tasks, dates)

        instances: list[TaskInstance] = (
            db.query(TaskInstance)
            .options(joinedload(TaskInstance.assigned_users))
            .filter(TaskInstance.date.in_(dates))
            .all()
        )
        inst_map: dict[tuple[str, date], TaskInstance] = {}
        for inst in instances:
            inst_map[(inst.task_id, inst.date)] = inst

        with matrix_container:
            if is_admin:
                with ui.row().classes("gap-2 mb-3"):
                    ui.button("Aufgabe erstellen", on_click=_open_add_task_dialog, icon="add_task").props("rounded unelevated no-caps").style("background: linear-gradient(135deg, var(--owl-accent), var(--owl-accent-strong)); color: white;")
                    ui.button("Tags", on_click=_open_manage_tags_dialog, icon="label").props("rounded flat no-caps").style("color: var(--owl-text);")
                    ui.button("Benutzer", on_click=_open_manage_users_dialog, icon="group").props("rounded flat no-caps").style("color: var(--owl-text);")
                    ui.separator().props("vertical").classes("h-6")
                    ui.button("Preset speichern", on_click=_open_save_preset_dialog, icon="bookmark_add").props("rounded flat no-caps").style("color: var(--owl-text);")
                    ui.button("Preset anwenden", on_click=_open_apply_preset_dialog, icon="bookmark").props("rounded flat no-caps").style("color: var(--owl-text);")
                    ui.button("Presets", on_click=_open_manage_presets_dialog, icon="bookmarks").props("rounded flat no-caps").style("color: var(--owl-text);")

            if not tasks:
                with ui.card().classes("w-full rounded-xl py-12").style("background: #ffffff; box-shadow: 0 2px 8px rgba(10,37,64,0.06);"):
                    with ui.column().classes("w-full items-center gap-2"):
                        ui.icon("inbox", size="48px", color="grey")
                        ui.label("Noch keine Aufgaben erstellt").style("color: #64748b;")
                db.close()
                return

            date_label.text = f"{dates[0].strftime('%d.%m.%Y')} – {dates[-1].strftime('%d.%m.%Y')}"

            # Color legend
            with ui.row().classes("gap-4 mb-3 flex-wrap"):
                for lbl, key in [("Erledigt", "completed"), ("Zugewiesen", "assigned"), ("Offen", "unassigned"), ("Überfällig", "overdue")]:
                    bc, _, _ = CELL_STYLES[key]
                    with ui.row().classes("items-center gap-1"):
                        ui.html(f'<span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:{bc}"></span>')
                        ui.label(lbl).classes("text-xs").style("color: #64748b;")

            with ui.element("div").props('id="hrp-scroll-container"').classes("w-full overflow-x-auto rounded-xl hrp-matrix-shell").style(
                "background: transparent; box-shadow: none;"
            ):
                with ui.element("table").classes("w-full text-xs").style("border-collapse: separate; border-spacing: 2px 3px;"):
                    with ui.element("thead"):
                        with ui.element("tr"):
                            with ui.element("th").classes("p-2 text-left font-bold sticky left-0 z-10 min-w-[220px]").style("background: var(--owl-strong-surface); border-radius: 8px 0 0 8px;"):
                                ui.label("Aufgabe").style("color: var(--owl-accent); font-family: 'Plus Jakarta Sans', sans-serif; font-weight: 700; font-size: 13px;")
                            for d in dates:
                                is_today = d == date.today()
                                bg = "background: color-mix(in srgb, var(--owl-accent) 22%, var(--owl-strong-surface)); box-shadow: 0 0 0 2px var(--owl-accent);" if is_today else "background: var(--owl-strong-surface);"
                                with ui.element("th").classes("p-2 text-center min-w-[110px] font-bold").style(bg + " border-radius: 8px;"):
                                    weekday = WEEKDAY_LABELS[d.weekday()]
                                    is_weekend = d.weekday() >= 5
                                    col_style = "color: var(--owl-accent); font-weight: 800;" if is_today else ("color: var(--owl-accent-2);" if is_weekend else "color: rgba(255,255,255,0.82);")
                                    ui.label(weekday).classes("text-[11px]").style(col_style)
                                    ui.label(d.strftime("%d.%m")).style(col_style + " font-weight: 700;")

                    with ui.element("tbody").props('id="task-tbody"'):
                        for task in tasks:
                            rec_label = _recurrence_label(task.recurrence_rule)

                            with ui.element("tr").props(f'data-task-id="{task.id}"').classes("cursor-move"):
                                with ui.element("td").classes("p-2 sticky left-0 z-10 hrp-matrix-task-col"):
                                    with ui.row().classes("items-center gap-2 no-wrap"):
                                        if is_admin:
                                            ui.icon("drag_indicator", size="16px", color="grey").classes("drag-handle cursor-grab")
                                        ui.icon("auto_awesome", size="16px").style("color: #00C2D1;")
                                        with ui.column().classes("gap-0"):
                                            with ui.row().classes("items-center gap-1"):
                                                ui.label(task.title).classes("font-bold text-sm hrp-matrix-task-title")
                                                if task.description:
                                                    ui.tooltip(task.description)
                                                if is_admin:
                                                    ui.button(icon="edit", on_click=lambda tid=task.id: _open_edit_task_dialog(tid)).props("flat round dense size=xs").style("color: #00C2D1;")
                                            with ui.row().classes("gap-1 items-center flex-wrap"):
                                                ui.badge(f"{task.base_duration_minutes} min", color="cyan").props("rounded")
                                                if rec_label:
                                                    ui.badge(f"🔁 {rec_label}", color="grey").props("rounded outline")
                                                _render_tags(task.tags)

                                for d in dates:
                                    inst = inst_map.get((task.id, d))
                                    status = _cell_status(inst, d)
                                    border_c, bg_c, icon_c = CELL_STYLES[status]
                                    is_today = d == date.today()
                                    cell_bg = f"background: {bg_c};"
                                    if is_today:
                                        cell_bg += " box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--owl-accent) 60%, transparent);"

                                    with ui.element("td").classes("p-1 text-center align-top hrp-matrix-cell").style(cell_bg).props(f'data-status="{status}"'):
                                        _build_cell(task, d, inst, users_all, status)

            if is_admin:
                ui.run_javascript("""
                    setTimeout(() => {
                        const tbody = document.getElementById('task-tbody');
                        if (tbody && typeof Sortable !== 'undefined') {
                            new Sortable(tbody, {
                                animation: 150,
                                handle: '.drag-handle',
                                ghostClass: 'sortable-ghost',
                                dragClass: 'sortable-drag',
                                onEnd: function(evt) {
                                    const rows = tbody.querySelectorAll('tr[data-task-id]');
                                    const order = Array.from(rows).map(r => r.getAttribute('data-task-id'));
                                    fetch('/api/reorder-tasks', {
                                        method: 'POST',
                                        headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify({task_ids: order})
                                    });
                                }
                            });
                        }
                    }, 500);
                """)

        db.close()

    def _build_cell(task: Task, d: date, inst: TaskInstance | None, users_all: list[User], status: str):
        border_c, bg_c, icon_c = CELL_STYLES[status]

        if inst is None:
            if is_admin:
                def activate(t_id=task.id, dt=d):
                    db2 = _get_db()
                    task_obj = db2.query(Task).get(t_id)
                    if task_obj and task_obj.is_recurring:
                        _remove_excluded_date(db2, task_obj, dt)
                    new_inst = TaskInstance(id=str(uuid.uuid4()), task_id=t_id, date=dt, status=TaskStatus.OPEN)
                    db2.add(new_inst)
                    db2.commit()
                    db2.close()
                    rebuild()
                ui.button(icon="add_circle", on_click=activate).props("flat round dense size=sm").style("color: #00C2D1;")
            else:
                ui.label("–").style("color: #94a3b8;")
        else:
            completed = inst.status == TaskStatus.COMPLETED
            assigned_ids = [u.id for u in inst.assigned_users]

            if inst.assigned_users:
                user_index = {u.id: idx for idx, u in enumerate(users_all)}
                with ui.row().classes("gap-0 justify-center flex-wrap"):
                    for u in inst.assigned_users:
                        idx = user_index.get(u.id, 0)
                        color = _user_color(idx)
                        strike = "text-decoration: line-through;" if completed else ""
                        ui.html(f'<span class="hrp-user-chip" style="background:{color};{strike}">{u.username[:2].upper()}</span>')
            else:
                ui.html(f'<span style="color:{icon_c}; font-size:11px; font-weight:600;">⚠ offen</span>')

            with ui.column().classes("items-center gap-0 mt-1 w-full"):
                with ui.row().classes("hrp-cell-actions"):
                    if is_admin:
                        def open_assign(iid=inst.id, dt=d, t=task, aids=assigned_ids):
                            _open_assign_dialog(iid, dt, t, users_all, aids)
                        ui.button(icon="groups", on_click=open_assign).props("flat round dense size=sm").style("color: var(--owl-accent);")
                    elif user.can_self_assign:
                        if user.id in assigned_ids:
                            def remove_self(iid=inst.id, uid=user.id):
                                db2 = _get_db()
                                inst2 = db2.query(TaskInstance).options(joinedload(TaskInstance.assigned_users)).get(iid)
                                if inst2:
                                    u_obj = db2.query(User).get(uid)
                                    if u_obj and u_obj in inst2.assigned_users:
                                        inst2.assigned_users.remove(u_obj)
                                        db2.commit()
                                db2.close()
                                rebuild()
                            ui.button(icon="person_remove", on_click=remove_self).props("flat round dense size=sm color=orange")
                        else:
                            def add_self(iid=inst.id, uid=user.id):
                                db2 = _get_db()
                                inst2 = db2.query(TaskInstance).options(joinedload(TaskInstance.assigned_users)).get(iid)
                                if inst2:
                                    u_obj = db2.query(User).get(uid)
                                    if u_obj and u_obj not in inst2.assigned_users:
                                        inst2.assigned_users.append(u_obj)
                                        db2.commit()
                                db2.close()
                                rebuild()
                            ui.button(icon="person_add_alt_1", on_click=add_self).props("flat round dense size=sm").style("color: #38bdf8;")

                    if is_admin or user.id in assigned_ids:
                        def toggle_status(iid=inst.id):
                            db2 = _get_db()
                            did_complete = False
                            instance = db2.query(TaskInstance).get(iid)
                            if instance:
                                was_open = instance.status == TaskStatus.OPEN
                                instance.status = TaskStatus.COMPLETED if was_open else TaskStatus.OPEN
                                did_complete = was_open
                                db2.commit()
                            db2.close()
                            if did_complete:
                                _celebrate_completion()
                            ui.notify("Status aktualisiert", type="positive")
                            rebuild()
                        icon_name = "verified" if completed else "flare"
                        ui.button(icon=icon_name, on_click=toggle_status).props(f"flat round dense size=sm color={'green' if completed else 'grey'}")

                    def open_notes(iid=inst.id, dt=d, tt=task.title):
                        _open_notes_dialog(iid, dt, tt)
                    note_color = "#8b5cf6" if inst.notes else "#94a3b8"
                    ui.button(icon="menu_book", on_click=open_notes).props("flat round dense size=sm").style(f"color: {note_color};")

                    if is_admin:
                        def deactivate(iid=inst.id, dt=d, t_id=task.id):
                            db2 = _get_db()
                            instance = db2.query(TaskInstance).get(iid)
                            if instance:
                                task_obj = db2.query(Task).get(t_id)
                                if task_obj and task_obj.is_recurring:
                                    _add_excluded_date(db2, task_obj, dt)
                                db2.delete(instance)
                                db2.commit()
                            db2.close()
                            rebuild()
                        ui.button(icon="delete_forever", on_click=deactivate).props("flat round dense size=sm color=red")

    def _build_list():
        mobile_container.clear()
        db = _get_db()
        dates = get_dates()
        tasks = db.query(Task).options(joinedload(Task.tags)).order_by(Task.sort_order, Task.title).all()
        users_all = db.query(User).order_by(User.username).all()

        _ensure_recurring_instances(db, tasks, dates)

        instances = (
            db.query(TaskInstance)
            .options(joinedload(TaskInstance.assigned_users), joinedload(TaskInstance.task))
            .filter(TaskInstance.date.in_(dates))
            .all()
        )
        inst_map: dict[tuple[str, date], TaskInstance] = {}
        for inst in instances:
            inst_map[(inst.task_id, inst.date)] = inst

        task_tag_map: dict[str, list[Tag]] = {t.id: list(t.tags) for t in tasks}

        today = date.today()
        sorted_dates = [d for d in sorted(dates) if d >= today]

        with mobile_container:
            if is_admin:
                with ui.row().classes("gap-2 mb-3 flex-wrap"):
                    ui.button("Aufgabe erstellen", on_click=_open_add_task_dialog, icon="add_task").props("rounded unelevated no-caps").style("background: linear-gradient(135deg, var(--owl-accent), var(--owl-accent-strong)); color: white;")
                    ui.button("Tags", on_click=_open_manage_tags_dialog, icon="label").props("rounded flat no-caps").style("color: var(--owl-text);")
                    ui.button("Benutzer", on_click=_open_manage_users_dialog, icon="group").props("rounded flat no-caps").style("color: var(--owl-text);")
                    ui.separator().props("vertical").classes("h-6")
                    ui.button("Preset speichern", on_click=_open_save_preset_dialog, icon="bookmark_add").props("rounded flat no-caps").style("color: var(--owl-text);")
                    ui.button("Preset anwenden", on_click=_open_apply_preset_dialog, icon="bookmark").props("rounded flat no-caps").style("color: var(--owl-text);")
                    ui.button("Presets", on_click=_open_manage_presets_dialog, icon="bookmarks").props("rounded flat no-caps").style("color: var(--owl-text);")

            if not tasks:
                with ui.card().classes("w-full rounded-xl py-12").style("background: #ffffff; box-shadow: 0 2px 8px rgba(10,37,64,0.06);"):
                    with ui.column().classes("w-full items-center gap-2"):
                        ui.icon("inbox", size="48px", color="grey")
                        ui.label("Noch keine Aufgaben erstellt").style("color: #64748b;")
                db.close()
                return

            date_label.text = f"{dates[0].strftime('%d.%m.%Y')} – {dates[-1].strftime('%d.%m.%Y')}"

            # Color legend
            with ui.row().classes("gap-4 mb-3 flex-wrap"):
                for lbl, key in [("Erledigt", "completed"), ("Zugewiesen", "assigned"), ("Offen", "unassigned"), ("Überfällig", "overdue")]:
                    bc, _, _ = CELL_STYLES[key]
                    with ui.row().classes("items-center gap-1"):
                        ui.html(f'<span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:{bc}"></span>')
                        ui.label(lbl).classes("text-xs").style("color: #64748b;")

            for d in sorted_dates:
                is_today = d == today
                is_weekend = d.weekday() >= 5

                with ui.row().classes("items-center gap-2 mt-5 mb-2"):
                    if is_today:
                        ui.badge("HEUTE", color="cyan").props("rounded")
                    day_style = "color: var(--owl-accent); font-weight: 800;" if is_today else ("color: var(--owl-accent-2); font-weight: 700;" if is_weekend else "color: var(--owl-text); font-weight: 700;")
                    ui.label(f"{WEEKDAY_LABELS[d.weekday()]}, {d.strftime('%d.%m.%Y')}").classes("text-subtitle1 font-bold").style(day_style)

                for task in tasks:
                    inst = inst_map.get((task.id, d))
                    status = _cell_status(inst, d)
                    if status == "inactive":
                        continue
                    border_c, bg_c, icon_c = CELL_STYLES[status]
                    tags = task_tag_map.get(task.id, [])

                    with ui.card().classes("w-full mb-2 hrp-card").style(f"border-left: 4px solid {border_c};"):
                        with ui.row().classes("items-center justify-between w-full"):
                            with ui.row().classes("items-center gap-3"):
                                if status == "completed":
                                    ui.icon("verified", size="24px", color="#22c55e")
                                elif status == "overdue":
                                    ui.icon("warning_amber", size="24px", color="#ff4d6d")
                                elif status == "assigned":
                                    ui.icon("shield", size="24px").style("color: #38bdf8;")
                                elif status == "unassigned":
                                    ui.icon("explore", size="24px", color="#ffb703")
                                else:
                                    ui.icon("radio_button_unchecked", size="24px", color="#94a3b8")

                                with ui.column().classes("gap-0"):
                                    ui.label(task.title).classes("text-subtitle2 font-bold").style("color: var(--owl-text);")
                                    if task.description:
                                        ui.label(task.description).classes("text-xs italic").style("color: var(--owl-muted);")
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.badge(f"{task.base_duration_minutes} min", color="cyan").props("rounded")
                                        _render_tags(tags)
                                        if inst:
                                            _render_user_chips(inst.assigned_users, users_all)
                                            if inst.notes:
                                                ui.html(f'<span style="font-size:10px;color:var(--owl-muted);">📝 {inst.notes[:40]}{"..." if len(inst.notes) > 40 else ""}</span>')
                                        elif status == "inactive":
                                            ui.label("Nicht aktiv").classes("text-xs italic").style("color: var(--owl-muted);")

                            with ui.row().classes("hrp-list-actions"):
                                if inst is not None:
                                    assigned_ids = [u.id for u in inst.assigned_users]
                                    if is_admin:
                                        def open_a(iid=inst.id, dt=d, t=task, aids=assigned_ids):
                                            _open_assign_dialog(iid, dt, t, users_all, aids)
                                        ui.button(icon="groups", on_click=open_a).props("flat round dense").style("color: var(--owl-accent);")
                                    elif user.can_self_assign:
                                        if user.id in assigned_ids:
                                            def rem_self_l(iid=inst.id, uid=user.id):
                                                db2 = _get_db()
                                                i2 = db2.query(TaskInstance).options(joinedload(TaskInstance.assigned_users)).get(iid)
                                                if i2:
                                                    uo = db2.query(User).get(uid)
                                                    if uo and uo in i2.assigned_users:
                                                        i2.assigned_users.remove(uo)
                                                        db2.commit()
                                                db2.close()
                                                rebuild()
                                            ui.button(icon="person_remove", on_click=rem_self_l).props("flat round dense color=orange")
                                        else:
                                            def add_self_l(iid=inst.id, uid=user.id):
                                                db2 = _get_db()
                                                i2 = db2.query(TaskInstance).options(joinedload(TaskInstance.assigned_users)).get(iid)
                                                if i2:
                                                    uo = db2.query(User).get(uid)
                                                    if uo and uo not in i2.assigned_users:
                                                        i2.assigned_users.append(uo)
                                                        db2.commit()
                                                db2.close()
                                                rebuild()
                                            ui.button(icon="person_add_alt_1", on_click=add_self_l).props("flat round dense").style("color: var(--owl-accent);")

                                    if is_admin or user.id in assigned_ids:
                                        completed = inst.status == TaskStatus.COMPLETED
                                        def toggle_list(iid=inst.id):
                                            db2 = _get_db()
                                            did_complete = False
                                            instance = db2.query(TaskInstance).get(iid)
                                            if instance:
                                                was_open = instance.status == TaskStatus.OPEN
                                                instance.status = TaskStatus.COMPLETED if was_open else TaskStatus.OPEN
                                                did_complete = was_open
                                                db2.commit()
                                            db2.close()
                                            if did_complete:
                                                _celebrate_completion()
                                            ui.notify("Status aktualisiert", type="positive")
                                            rebuild()
                                        if completed:
                                            ui.button("Erledigt", on_click=toggle_list, icon="verified", color="green").props("rounded unelevated no-caps size=sm")
                                        else:
                                            ui.button("Erledigen", on_click=toggle_list, icon="flare").props("rounded unelevated no-caps size=sm").style("background: var(--owl-accent); color: white;")

                                    # Notes button in list
                                    def open_notes_l(iid=inst.id, dt=d, tt=task.title):
                                        _open_notes_dialog(iid, dt, tt)
                                    note_col = "#8b5cf6" if inst.notes else "#94a3b8"
                                    ui.button(icon="menu_book", on_click=open_notes_l).props("flat round dense").style(f"color: {note_col};")

                                    if is_admin:
                                        def deactivate_l(iid=inst.id, dt=d, t_id=task.id):
                                            db2 = _get_db()
                                            instance = db2.query(TaskInstance).get(iid)
                                            if instance:
                                                task_obj = db2.query(Task).get(t_id)
                                                if task_obj and task_obj.is_recurring:
                                                    _add_excluded_date(db2, task_obj, dt)
                                                db2.delete(instance)
                                                db2.commit()
                                            db2.close()
                                            rebuild()
                                        ui.button(icon="delete_forever", on_click=deactivate_l).props("flat round dense size=sm color=red")
                                else:
                                    if is_admin:
                                        def activate_l(t_id=task.id, dt=d):
                                            db2 = _get_db()
                                            task_obj = db2.query(Task).get(t_id)
                                            if task_obj and task_obj.is_recurring:
                                                _remove_excluded_date(db2, task_obj, dt)
                                            new_inst = TaskInstance(id=str(uuid.uuid4()), task_id=t_id, date=dt, status=TaskStatus.OPEN)
                                            db2.add(new_inst)
                                            db2.commit()
                                            db2.close()
                                            rebuild()
                                        ui.button("Aktivieren", on_click=activate_l, icon="add_circle").props("flat rounded no-caps size=sm").style("color: var(--owl-accent);")

        db.close()

    # --------------- Day view (Heute) ---------------
    def _build_day_view():
        day_container.clear()
        db = _get_db()
        today = date.today()
        tomorrow = today + timedelta(days=1)
        tasks = db.query(Task).options(joinedload(Task.tags)).order_by(Task.sort_order, Task.title).all()
        users_all = db.query(User).order_by(User.username).all()

        _ensure_recurring_instances(db, tasks, [today, tomorrow])

        instances_today = (
            db.query(TaskInstance)
            .options(joinedload(TaskInstance.assigned_users), joinedload(TaskInstance.task).joinedload(Task.tags))
            .filter(TaskInstance.date == today)
            .all()
        )
        instances_tomorrow = (
            db.query(TaskInstance)
            .options(joinedload(TaskInstance.assigned_users), joinedload(TaskInstance.task).joinedload(Task.tags))
            .filter(TaskInstance.date == tomorrow)
            .all()
        )
        overdue_instances = (
            db.query(TaskInstance)
            .options(joinedload(TaskInstance.assigned_users), joinedload(TaskInstance.task).joinedload(Task.tags))
            .filter(TaskInstance.date < today, TaskInstance.status == TaskStatus.OPEN)
            .all()
        )

        unassigned_today = [i for i in instances_today if not i.assigned_users and i.status == TaskStatus.OPEN]

        with day_container:
            date_label.text = f"Tagesansicht – {WEEKDAY_LABELS[today.weekday()]}, {today.strftime('%d.%m.%Y')}"

            # --------------- Unassigned tasks today ---------------
            with ui.row().classes("items-center gap-2 mt-2 mb-3"):
                ui.icon("assignment_late", size="28px", color="#f59e0b")
                ui.label("Offene Aufgaben ohne Zuweisung").classes("text-h6 font-bold").style("color: var(--owl-text);")
                if unassigned_today:
                    ui.badge(str(len(unassigned_today)), color="orange").props("rounded")

            if not unassigned_today:
                with ui.card().classes("w-full rounded-xl py-6").style("background: rgba(16,185,129,0.08); border-left: 4px solid #10b981;"):
                    with ui.row().classes("items-center gap-3 px-4"):
                        ui.icon("check_circle", size="32px", color="#10b981")
                        ui.label("Alle heutigen Aufgaben sind zugewiesen!").classes("text-subtitle1 font-medium").style("color: #10b981;")
            else:
                for inst in unassigned_today:
                    border_c, bg_c, icon_c = CELL_STYLES["unassigned"]
                    with ui.card().classes("w-full mb-2 hrp-card").style(f"border-left: 4px solid {border_c};"):
                        with ui.row().classes("items-center justify-between w-full"):
                            with ui.row().classes("items-center gap-3"):
                                ui.icon("help_outline", size="24px", color="#f59e0b")
                                with ui.column().classes("gap-0"):
                                    ui.label(inst.task.title).classes("text-subtitle2 font-bold").style("color: var(--owl-text);")
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.badge(f"{inst.task.base_duration_minutes} min", color="cyan").props("rounded")
                                        _render_tags(inst.task.tags)
                            if is_admin:
                                def open_a(iid=inst.id, t=inst.task, aids=[]):
                                    _open_assign_dialog(iid, today, t, users_all, aids)
                                ui.button("Zuweisen", on_click=open_a, icon="group_add").props("rounded unelevated no-caps size=sm").style("background: var(--owl-accent); color: white;")

            ui.separator().classes("my-4")

            # --------------- User cards ---------------
            with ui.row().classes("items-center gap-2 mb-3"):
                ui.icon("group", size="28px").style("color: var(--owl-accent);")
                ui.label("Haushaltsmitglieder").classes("text-h6 font-bold").style("color: var(--owl-text);")

            user_detail_container = ui.column().classes("w-full")

            def _open_user_detail(uid: str):
                user_detail_container.clear()
                db2 = _get_db()
                target_user = db2.query(User).get(uid)
                if not target_user:
                    db2.close()
                    return

                user_today = [i for i in instances_today if target_user.id in [u.id for u in i.assigned_users]]
                user_tomorrow = [i for i in instances_tomorrow if target_user.id in [u.id for u in i.assigned_users]]
                user_overdue = [i for i in overdue_instances if target_user.id in [u.id for u in i.assigned_users]]

                user_idx = next((idx for idx, u in enumerate(users_all) if u.id == uid), 0)
                color = _user_color(user_idx)

                with user_detail_container:
                    with ui.card().classes("w-full rounded-xl").style(
                        f"background: var(--owl-surface); border-top: 4px solid {color}; box-shadow: 0 4px 16px var(--owl-border);"
                    ) as detail_card:
                        with ui.row().classes("w-full items-center justify-between mb-3"):
                            with ui.row().classes("items-center gap-3"):
                                ui.html(f'<span class="hrp-user-chip" style="background:{color}; font-size:14px; padding: 4px 14px;">{target_user.username}</span>')
                                ui.label(f"Kapazität: {target_user.daily_capacity_minutes} Min/Tag").classes("text-sm").style("color: var(--owl-muted);")
                            ui.button(icon="close", on_click=lambda: user_detail_container.clear()).props("flat round dense color=grey")

                        def _render_task_section(title: str, icon_name: str, icon_color: str, task_list: list[TaskInstance], show_date: bool = False):
                            with ui.row().classes("items-center gap-2 mt-2 mb-1"):
                                ui.icon(icon_name, size="20px", color=icon_color)
                                ui.label(title).classes("text-subtitle2 font-bold").style("color: var(--owl-text);")
                                ui.badge(str(len(task_list)), color="grey").props("rounded")
                            if not task_list:
                                ui.label("Keine Aufgaben").classes("text-xs italic ml-7").style("color: var(--owl-muted);")
                            else:
                                for inst in task_list:
                                    status = _cell_status(inst, inst.date)
                                    border_c, bg_c, _ = CELL_STYLES[status]
                                    with ui.card().classes("w-full mb-1 py-1 px-3 rounded-lg").style(
                                        f"background: var(--owl-surface-soft); border-left: 3px solid {border_c};"
                                    ):
                                        with ui.row().classes("items-center justify-between w-full"):
                                            with ui.row().classes("items-center gap-2"):
                                                if status == "completed":
                                                    ui.icon("verified", size="18px", color="#10b981")
                                                elif status == "overdue":
                                                    ui.icon("warning_amber", size="18px", color="#ef4444")
                                                else:
                                                    ui.icon("shield", size="18px").style("color: var(--owl-accent);")
                                                ui.label(inst.task.title).classes("text-sm font-medium").style("color: var(--owl-text);")
                                                ui.badge(f"{inst.task.base_duration_minutes} min", color="cyan").props("rounded")
                                                _render_tags(inst.task.tags)
                                                if show_date:
                                                    ui.label(inst.date.strftime("%d.%m")).classes("text-xs").style("color: var(--owl-muted);")
                                            if is_admin or user.id in [u.id for u in inst.assigned_users]:
                                                completed = inst.status == TaskStatus.COMPLETED
                                                def toggle_s(iid=inst.id):
                                                    db3 = _get_db()
                                                    did_complete = False
                                                    instance = db3.query(TaskInstance).get(iid)
                                                    if instance:
                                                        was_open = instance.status == TaskStatus.OPEN
                                                        instance.status = TaskStatus.COMPLETED if was_open else TaskStatus.OPEN
                                                        did_complete = was_open
                                                        db3.commit()
                                                    db3.close()
                                                    if did_complete:
                                                        _celebrate_completion()
                                                    ui.notify("Status aktualisiert", type="positive")
                                                    rebuild()
                                                if completed:
                                                    ui.button(icon="verified", on_click=toggle_s).props("flat round dense size=xs color=green")
                                                else:
                                                    ui.button(icon="flare", on_click=toggle_s).props("flat round dense size=xs color=grey")

                        _render_task_section(f"Heute – {today.strftime('%d.%m.%Y')}", "today", "#3b82f6", user_today)
                        _render_task_section(f"Morgen – {tomorrow.strftime('%d.%m.%Y')}", "event", "#8b5cf6", user_tomorrow)
                        if user_overdue:
                            _render_task_section("Überfällig", "warning", "#ef4444", user_overdue, show_date=True)

                        # Summary
                        today_mins = sum(i.task.base_duration_minutes / max(len(i.assigned_users), 1) for i in user_today)
                        with ui.row().classes("mt-3 items-center gap-2"):
                            ui.icon("schedule", size="18px", color=color)
                            ui.label(f"Heute geplant: {today_mins:.0f} / {target_user.daily_capacity_minutes} Min").classes("text-sm font-medium").style("color: var(--owl-text);")

                db2.close()

                # Auto-close after 10 seconds
                ui.timer(10.0, lambda: user_detail_container.clear(), once=True)

            with ui.row().classes("w-full gap-3 flex-wrap"):
                for idx, u in enumerate(users_all):
                    color = _user_color(idx)
                    user_today_count = sum(1 for i in instances_today if u.id in [usr.id for usr in i.assigned_users])
                    user_overdue_count = sum(1 for i in overdue_instances if u.id in [usr.id for usr in i.assigned_users])
                    today_mins = sum(
                        i.task.base_duration_minutes / max(len(i.assigned_users), 1)
                        for i in instances_today if u.id in [usr.id for usr in i.assigned_users]
                    )

                    with ui.card().classes("hrp-card cursor-pointer flex-1 min-w-[180px]").style(
                        f"border-top: 3px solid {color};"
                    ).on("click", lambda uid=u.id: _open_user_detail(uid)):
                        with ui.column().classes("items-center gap-2 py-2"):
                            ui.html(f'<span class="hrp-user-chip" style="background:{color}; font-size:14px; padding: 4px 14px;">{u.username}</span>')
                            with ui.row().classes("gap-3"):
                                with ui.column().classes("items-center gap-0"):
                                    ui.label(f"{user_today_count}").classes("text-lg font-bold").style("color: var(--owl-text);")
                                    ui.label("Aufgaben").classes("text-[10px] uppercase").style("color: var(--owl-muted);")
                                with ui.column().classes("items-center gap-0"):
                                    ui.label(f"{today_mins:.0f}").classes("text-lg font-bold").style("color: var(--owl-text);")
                                    ui.label("Minuten").classes("text-[10px] uppercase").style("color: var(--owl-muted);")
                            if user_overdue_count > 0:
                                ui.badge(f"{user_overdue_count} überfällig", color="red").props("rounded")

        db.close()

    # --------------- Stats ---------------
    def _build_stats():
        stats_container.clear()
        db = _get_db()
        dates = get_dates()
        users_all = db.query(User).order_by(User.username).all()
        # Exclude the system "admin" account from work resource calculations
        resource_users = [u for u in users_all if u.username != "admin"]
        minutes_map = _compute_user_minutes(db, dates, resource_users)

        with stats_container:
            with ui.row().classes("items-center gap-2 mt-2 mb-3"):
                ui.icon("bar_chart", size="28px").style("color: #00C2D1;")
                ui.label("Statistik").classes("text-h6 font-bold hrp-stat-heading")

            total_all = 0.0
            with ui.row().classes("w-full gap-3 flex-wrap mb-4"):
                for idx, u in enumerate(resource_users):
                    total = sum(minutes_map[u.id].values())
                    total_all += total
                    avg_per_day = total / max(len(dates), 1)
                    utilization = (avg_per_day / u.daily_capacity_minutes * 100) if u.daily_capacity_minutes > 0 else 0
                    days_over = sum(1 for d in dates if minutes_map[u.id][d] > u.daily_capacity_minutes)
                    color = _user_color(idx)
                    border_color = "#ef4444" if days_over > 0 else color

                    with ui.card().classes("hrp-stat-card flex-1 min-w-[200px]").style(f"border-top: 3px solid {border_color};"):
                        with ui.row().classes("items-center gap-2 mb-2"):
                            ui.html(f'<span class="hrp-user-chip" style="background:{color}">{u.username[:2].upper()}</span>')
                            ui.label(u.username).classes("font-bold hrp-stat-value")
                        with ui.row().classes("gap-4 flex-wrap"):
                            with ui.column().classes("gap-0"):
                                ui.label("Geplant").classes("text-[10px] uppercase hrp-stat-label")
                                ui.label(f"{total:.0f} min").classes("text-lg font-bold hrp-stat-value")
                            with ui.column().classes("gap-0"):
                                ui.label("Ø/Tag").classes("text-[10px] uppercase hrp-stat-label")
                                ui.label(f"{avg_per_day:.0f} min").classes("text-lg font-bold hrp-stat-value")
                            with ui.column().classes("gap-0"):
                                ui.label("Auslastung").classes("text-[10px] uppercase hrp-stat-label")
                                util_color = "#ef4444" if utilization > 100 else ("#f59e0b" if utilization > 80 else "#10b981")
                                ui.label(f"{utilization:.0f}%").classes("text-lg font-bold").style(f"color: {util_color};")

            with ui.card().classes("w-full hrp-stat-card px-4 py-3").style("border-left: 3px solid #00C2D1;"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("functions", size="20px").style("color: #00C2D1;")
                    ui.label(f"Gesamttotal: {total_all:.0f} Minuten im Zeitraum").classes("font-bold hrp-stat-value")

            if is_admin and resource_users:
                with ui.expansion("Tagesdetails anzeigen", icon="table_chart").classes("w-full mt-3").props("dense"):
                    with ui.element("div").classes("overflow-x-auto rounded-lg mt-2").style("background: #ffffff; box-shadow: 0 2px 8px rgba(10,37,64,0.06);"):
                        with ui.element("table").classes("border-collapse text-xs w-full"):
                            with ui.element("thead"):
                                with ui.element("tr"):
                                    with ui.element("th").classes("p-2 text-left font-bold").style("background: #0A2540;"):
                                        ui.label("Benutzer").style("color: #00E5FF;")
                                    for d in dates:
                                        is_today = d == date.today()
                                        bg = "background: rgba(0,229,255,0.2);" if is_today else "background: #0A2540;"
                                        with ui.element("th").classes("p-2 text-center").style(bg):
                                            ui.label(d.strftime("%d.%m")).style("color: #e2e8f0;")
                            with ui.element("tbody"):
                                for idx, u in enumerate(resource_users):
                                    color = _user_color(idx)
                                    with ui.element("tr"):
                                        with ui.element("td").classes("p-2 border-b border-gray-200").style(f"background: #f8fafc; border-left: 3px solid {color};"):
                                            ui.label(u.username).classes("font-medium").style("color: #0A2540;")
                                        for d in dates:
                                            val = minutes_map[u.id][d]
                                            over = val > u.daily_capacity_minutes
                                            is_today = d == date.today()
                                            style = ""
                                            if over:
                                                style = "background: rgba(239,68,68,0.1);"
                                            elif is_today:
                                                style = "background: rgba(0,194,209,0.05);"
                                            with ui.element("td").classes("p-2 text-center border-b border-gray-100").style(style):
                                                if val > 0:
                                                    col = "#ef4444" if over else "#0A2540"
                                                    ui.label(f"{val:.0f}").style(f"color: {col}; {'font-weight: bold;' if over else ''}")
                                                else:
                                                    ui.label("–").style("color: #94a3b8;")
        db.close()

    # Initial build – install persistent scroll-save/restore handler once
    ui.run_javascript(
        "(function(){"
        "  function bind(sc){"
        "    if(sc.__hrpB)return; sc.__hrpB=1;"
        "    var sv=parseInt(sessionStorage.getItem('hrp_sl')||'0',10);"
        "    if(sv>0){"
        "      sc.style.visibility='hidden';"
        "      var tries=0;"
        "      function trySet(){"
        "        sc.scrollLeft=sv;"
        "        if(sc.scrollLeft>=sv-1||tries>=40){sc.style.visibility='';}"
        "        else{tries++;requestAnimationFrame(trySet);}"
        "      }"
        "      requestAnimationFrame(trySet);"
        "    }"
        "    sc.addEventListener('scroll',function(){"
        "      sessionStorage.setItem('hrp_sl',sc.scrollLeft);"
        "    },{passive:true});"
        "  }"
        "  var sc=document.getElementById('hrp-scroll-container');"
        "  if(sc)bind(sc);"
        "  new MutationObserver(function(){"
        "    var sc=document.getElementById('hrp-scroll-container');"
        "    if(sc)bind(sc);"
        "  }).observe(document.body,{childList:true,subtree:true});"
        "})()"
    )
    rebuild()


# ---------------------------------------------------------------------------
# API: task reordering (SortableJS callback)
# ---------------------------------------------------------------------------

async def _reorder_tasks_handler(request: StarletteRequest):
    data = await request.json()
    task_ids = data.get("task_ids", [])
    db = _get_db()
    for idx, tid in enumerate(task_ids):
        task = db.query(Task).get(tid)
        if task:
            task.sort_order = idx
    db.commit()
    db.close()
    return JSONResponse({"ok": True})


nicegui_app.add_route("/api/reorder-tasks", _reorder_tasks_handler, methods=["POST"])


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

ui.run(
    title="Household Resource Planner",
    port=8080,
    storage_secret="hrp-storage-secret-change-me",
    dark=True,
    reload=False,
)
