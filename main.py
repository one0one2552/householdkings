"""Household Resource Planner – main application entry point."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta

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


def _month_dates(ref: date) -> list[date]:
    first = ref.replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    days = []
    d = first
    while d < next_month:
        days.append(d)
        d += timedelta(days=1)
    return days


def _parse_recurrence_days(rule: str | None) -> list[int]:
    if not rule or not rule.strip():
        return []
    try:
        return sorted(set(int(x.strip()) for x in rule.split(",") if x.strip().isdigit() and 0 <= int(x.strip()) <= 6))
    except ValueError:
        return []


def _recurrence_days_to_str(days: list[int]) -> str | None:
    if not days:
        return None
    return ",".join(str(d) for d in sorted(set(days)))


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
        rec_days = _parse_recurrence_days(task.recurrence_rule)
        if not rec_days:
            continue
        excluded = _get_excluded_dates(task)
        for d in dates:
            if d in excluded:
                continue
            if d.weekday() in rec_days and (task.id, d) not in existing:
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
    "completed":  ("#10b981", "rgba(16,185,129,0.10)",  "#10b981"),
    "assigned":   ("#00C2D1", "rgba(0,194,209,0.10)",   "#00C2D1"),
    "unassigned": ("#f59e0b", "rgba(245,158,11,0.08)",  "#f59e0b"),
    "overdue":    ("#ef4444", "rgba(239,68,68,0.10)",   "#ef4444"),
    "inactive":   ("#94a3b8", "rgba(148,163,184,0.10)", "#94a3b8"),
}

THEME_OPTIONS = {
    "owl_light": "OWL Light",
    "owl_dark": "OWL Dark",
    "industrial_light": "Industrial Light",
    "carbon_dark": "Carbon Dark",
}

DARK_THEMES = {"owl_dark", "carbon_dark"}


def _normalize_theme(theme: str | None) -> str:
    if theme == "light":
        return "owl_light"
    if theme == "dark":
        return "owl_dark"
    if theme in THEME_OPTIONS:
        return theme
    return "owl_light"


def _is_dark_theme(theme: str) -> bool:
    return theme in DARK_THEMES

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CUSTOM_CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;500;600;700&family=Exo+2:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {
    --owl-bg: #ffffff;
    --owl-surface: #ffffff;
    --owl-surface-soft: #f7f7f7;
    --owl-strong-surface: #1A1A1B;
    --owl-text: #1A1A1B;
    --owl-text-soft: #343438;
    --owl-muted: #64646a;
    --owl-structure: #a0a0a0;
    --owl-accent: #E69500;
    --owl-accent-soft: rgba(230,149,0,0.18);
    --owl-accent-strong: #cc8600;
    --owl-shadow: 0 14px 40px rgba(26, 26, 27, 0.09);
    --owl-border: rgba(160, 160, 160, 0.35);
    --owl-header-bg: rgba(255, 255, 255, 0.94);
    --owl-header-text: #1A1A1B;
    --owl-text-on-accent: #ffffff;
}

body[data-theme="owl_dark"] {
    --owl-bg: #1f2126;
    --owl-surface: #3a4556;
    --owl-surface-soft: #455267;
    --owl-strong-surface: #515f79;
    --owl-text: #ffffff;
    --owl-text-soft: #f8fbff;
    --owl-muted: #e9eef7;
    --owl-structure: #d8e1ee;
    --owl-accent: #FFB000;
    --owl-accent-soft: rgba(255,176,0,0.24);
    --owl-accent-strong: #ffbf33;
    --owl-shadow: 0 18px 50px rgba(0, 0, 0, 0.52);
    --owl-border: rgba(191, 202, 218, 0.62);
    --owl-header-bg: rgba(28, 31, 37, 0.95);
    --owl-header-text: #f7f8fa;
    --owl-text-on-accent: #1A1A1B;
}

body[data-theme="industrial_light"] {
    --owl-bg: #f2f2f1;
    --owl-surface: #ffffff;
    --owl-surface-soft: #f0f0ee;
    --owl-strong-surface: #202126;
    --owl-text: #19191a;
    --owl-text-soft: #2f2f33;
    --owl-muted: #5b5b60;
    --owl-structure: #8e8e93;
    --owl-accent: #cf8500;
    --owl-accent-soft: rgba(207,133,0,0.2);
    --owl-accent-strong: #b67400;
    --owl-shadow: 0 14px 34px rgba(0, 0, 0, 0.11);
    --owl-border: rgba(120, 120, 126, 0.3);
    --owl-header-bg: rgba(246, 246, 245, 0.95);
    --owl-header-text: #19191a;
    --owl-text-on-accent: #ffffff;
}

body[data-theme="carbon_dark"] {
    --owl-bg: #101114;
    --owl-surface: #394657;
    --owl-surface-soft: #445367;
    --owl-strong-surface: #50627a;
    --owl-text: #ffffff;
    --owl-text-soft: #f8fbff;
    --owl-muted: #e8eef8;
    --owl-structure: #d3ddeb;
    --owl-accent: #ffb000;
    --owl-accent-soft: rgba(255,176,0,0.27);
    --owl-accent-strong: #ffc23d;
    --owl-shadow: 0 20px 54px rgba(0, 0, 0, 0.56);
    --owl-border: rgba(188, 201, 222, 0.64);
    --owl-header-bg: rgba(22, 24, 30, 0.95);
    --owl-header-text: #f9fafb;
    --owl-text-on-accent: #161616;
}

body[data-theme="owl_dark"],
body[data-theme="carbon_dark"] {
    color-scheme: dark;
}

body, .q-page {
    background:
        radial-gradient(circle at 12% 18%, var(--owl-accent-soft), transparent 34%),
        radial-gradient(circle at 88% 12%, rgba(160,160,160,0.12), transparent 28%),
        var(--owl-bg) !important;
    font-family: 'Exo 2', sans-serif !important;
    color: var(--owl-text) !important;
}

.text-h5, .text-h6, .text-subtitle1, .text-subtitle2, h1, h2, h3 {
    font-family: 'Rajdhani', sans-serif !important;
    letter-spacing: 0.03em;
}

.q-header {
    background: var(--owl-header-bg) !important;
    color: var(--owl-header-text) !important;
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--owl-border);
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.08) !important;
}

.q-card {
    color: var(--owl-text) !important;
    background: linear-gradient(180deg, var(--owl-surface) 0%, var(--owl-surface-soft) 100%) !important;
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

body[data-theme="owl_dark"] .q-item__label,
body[data-theme="owl_dark"] .q-checkbox__label,
body[data-theme="owl_dark"] .q-toggle__label,
body[data-theme="owl_dark"] .q-field__label,
body[data-theme="owl_dark"] .q-field__native,
body[data-theme="owl_dark"] .q-field__input,
body[data-theme="carbon_dark"] .q-item__label,
body[data-theme="carbon_dark"] .q-checkbox__label,
body[data-theme="carbon_dark"] .q-toggle__label,
body[data-theme="carbon_dark"] .q-field__label,
body[data-theme="carbon_dark"] .q-field__native,
body[data-theme="carbon_dark"] .q-field__input {
    color: var(--owl-text) !important;
}

body[data-theme="owl_dark"] .q-card,
body[data-theme="carbon_dark"] .q-card,
body[data-theme="owl_dark"] .hrp-stat-card,
body[data-theme="carbon_dark"] .hrp-stat-card {
    color: var(--owl-text) !important;
}

.q-field--outlined .q-field__control {
    border-radius: 12px;
    border: 1px solid var(--owl-border);
    background: var(--owl-surface);
}

.q-btn {
    border-radius: 11px;
}

.q-btn--flat {
    color: var(--owl-text) !important;
}

.q-btn--unelevated {
    background: var(--owl-accent) !important;
    color: var(--owl-text-on-accent) !important;
}

.hrp-matrix-cell {
    min-width: 110px;
    min-height: 60px;
    transition: background 0.15s;
}

.hrp-matrix-cell:hover {
    filter: brightness(0.96);
}

.hrp-card {
    border-radius: 18px !important;
    background: linear-gradient(180deg, var(--owl-surface) 0%, var(--owl-surface-soft) 100%) !important;
    box-shadow: var(--owl-shadow) !important;
    border: 1px solid var(--owl-border);
    transition: transform 0.18s, box-shadow 0.18s;
}

.hrp-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 18px 40px rgba(0, 0, 0, 0.18) !important;
}

.hrp-stat-card {
    border-radius: 16px !important;
    background: linear-gradient(180deg, var(--owl-surface) 0%, var(--owl-surface-soft) 100%) !important;
    box-shadow: var(--owl-shadow) !important;
    border: 1px solid var(--owl-border);
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
    font-family: 'Rajdhani', sans-serif !important;
}

.hrp-wordmark {
    font-family: 'Rajdhani', sans-serif;
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 0.22em;
    color: var(--owl-text);
}

.hrp-submark {
    font-family: 'Exo 2', sans-serif;
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--owl-muted);
}

.hrp-theme-switch .q-toggle__inner {
    color: var(--owl-accent) !important;
}

.hrp-theme-select {
    min-width: 170px;
}

.hrp-theme-select .q-field__control {
    background: var(--owl-surface) !important;
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

body[data-theme="owl_dark"] [style*="color: #64748b"],
body[data-theme="owl_dark"] [style*="color:#64748b"],
body[data-theme="owl_dark"] [style*="color: #94a3b8"],
body[data-theme="owl_dark"] [style*="color:#94a3b8"],
body[data-theme="carbon_dark"] [style*="color: #64748b"],
body[data-theme="carbon_dark"] [style*="color:#64748b"],
body[data-theme="carbon_dark"] [style*="color: #94a3b8"],
body[data-theme="carbon_dark"] [style*="color:#94a3b8"] {
    color: var(--owl-text-soft) !important;
}
</style>
"""

LOGIN_CSS = """
<style>
body, .q-page, .nicegui-content {
    background:
        radial-gradient(circle at 14% 18%, var(--owl-accent-soft), transparent 40%),
        radial-gradient(circle at 82% 14%, rgba(160,160,160,0.15), transparent 30%),
        var(--owl-bg) !important;
}
</style>
"""

SORTABLE_JS = '<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js"></script>'


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

    with ui.card().classes("absolute-center w-[420px] rounded-2xl px-4 py-3").style(
        "box-shadow: var(--owl-shadow); border: 1px solid var(--owl-border);"
    ):
        with ui.row().classes("w-full items-center justify-between mb-2"):
            with ui.row().classes("items-center gap-2"):
                ui.image("ci/logo (1).jpg").classes("w-11 h-11 rounded-full object-cover")
                with ui.column().classes("gap-0"):
                    ui.label("O.W.L.").classes("hrp-wordmark")
                    ui.label("Open Work Lab").classes("hrp-submark")
            theme_select = ui.select(THEME_OPTIONS, value=theme_key, label="Theme", on_change=lambda e: set_theme(e.value)).props("outlined dense options-dense").classes("hrp-theme-select")

        with ui.column().classes("w-full items-center gap-2 py-2"):
            ui.label("Haushalts-Planer").classes("text-h5 font-bold")
            ui.label("Anmelden, um fortzufahren").classes("text-caption mb-2").style("color: var(--owl-muted);")

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
        "ref_date": _today,
        "display": "matrix",
        "custom_start": _wstart,
        "custom_end": _wstart + timedelta(days=6),
        "start_today": False,
    }

    def get_dates():
        today = date.today()
        if state["view_mode"] == "custom":
            s, e = state["custom_start"], state["custom_end"]
            if e < s:
                e = s
            days, d = [], s
            while d <= e and len(days) < 62:
                days.append(d)
                d += timedelta(days=1)
            return days
        start = today if state["start_today"] else state["ref_date"]
        if state["view_mode"] == "week":
            if state["start_today"]:
                return [today + timedelta(days=i) for i in range(7)]
            return _week_dates(start)
        if state["view_mode"] == "2weeks":
            if state["start_today"]:
                return [today + timedelta(days=i) for i in range(14)]
            return _two_week_dates(start)
        # month
        if state["start_today"]:
            return [today + timedelta(days=i) for i in range(30)]
        return _month_dates(start)

    # --------------- Header ---------------
    with ui.header().classes("items-center justify-between px-6 py-2"):
        with ui.row().classes("items-center gap-3"):
            ui.image("ci/logo (1).jpg").classes("w-9 h-9 rounded-full object-cover")
            with ui.column().classes("gap-0"):
                ui.label("O.W.L.").classes("hrp-wordmark")
                ui.label("Open Work Lab").classes("hrp-submark")
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
    _custom_row_holder: list = [None]

    with ui.card().classes("w-full rounded-xl mx-4 mt-3 px-4 py-3").style(
        "background: #ffffff; box-shadow: 0 2px 8px rgba(10,37,64,0.08); border-bottom: 2px solid rgba(0,229,255,0.4);"
    ):
        with ui.row().classes("w-full items-center justify-center gap-4 flex-wrap"):
            def _prev():
                if state["view_mode"] != "custom":
                    delta = {"week": timedelta(weeks=1), "2weeks": timedelta(weeks=2)}.get(state["view_mode"], timedelta(days=30))
                    state["ref_date"] -= delta
                rebuild()

            def _next():
                if state["view_mode"] != "custom":
                    delta = {"week": timedelta(weeks=1), "2weeks": timedelta(weeks=2)}.get(state["view_mode"], timedelta(days=30))
                    state["ref_date"] += delta
                rebuild()

            ui.button(icon="chevron_left", on_click=_prev).props("flat round dense").style("color: #0A2540;")
            date_label = ui.label("").classes("text-subtitle1 font-bold min-w-[200px] text-center").style("color: #0A2540;")
            ui.button(icon="chevron_right", on_click=_next).props("flat round dense").style("color: #0A2540;")

            ui.separator().props("vertical").classes("h-6")

            def toggle_view(val):
                state["view_mode"] = val
                if _custom_row_holder[0]:
                    _custom_row_holder[0].set_visibility(val == "custom")
                rebuild()

            ui.toggle(
                {"week": "1 Woche", "2weeks": "2 Wochen", "month": "Monat", "custom": "Benutzerdefiniert"},
                value="week",
                on_change=lambda e: toggle_view(e.value),
            ).props("rounded dense no-caps").style("color: #0A2540;")

            def toggle_start_today(e):
                state["start_today"] = e.value
                rebuild()
            ui.checkbox("Ab Heute", value=False, on_change=toggle_start_today).style("color: #0A2540; font-size: 13px;")

            ui.separator().props("vertical").classes("h-6")

            def toggle_display(val):
                state["display"] = val
                rebuild()

            ui.toggle(
                {"matrix": "Matrix", "list": "Liste", "day": "Heute"},
                value="matrix",
                on_change=lambda e: toggle_display(e.value),
            ).props("rounded dense no-caps").style("color: #0A2540;")

            def go_today():
                state["ref_date"] = date.today()
                rebuild()
            ui.button("Heute", icon="today", on_click=go_today).props("flat rounded dense no-caps").style("color: #0A2540;")

        custom_row = ui.row().classes("w-full justify-center gap-3 items-center mt-1 pb-1")
        _custom_row_holder[0] = custom_row
        custom_row.set_visibility(False)
        with custom_row:
            ui.label("Von:").classes("text-sm font-medium").style("color: #0A2540;")
            cs_input = ui.input(value=state["custom_start"].isoformat()).props("outlined rounded dense type=date")
            ui.label("Bis:").classes("text-sm font-medium").style("color: #0A2540;")
            ce_input = ui.input(value=state["custom_end"].isoformat()).props("outlined rounded dense type=date")
            def apply_custom():
                try:
                    state["custom_start"] = date.fromisoformat(cs_input.value)
                    state["custom_end"] = date.fromisoformat(ce_input.value)
                    rebuild()
                except ValueError:
                    ui.notify("Ungültiges Datum", type="negative")
            ui.button("Anwenden", on_click=apply_custom).props("rounded unelevated no-caps size=sm").style("background: #00C2D1; color: white;")

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
    def _weekday_picker(initial_days: list[int] | None = None) -> tuple[dict[int, ui.checkbox], ui.checkbox]:
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
        return checkboxes, daily_cb

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

        with ui.dialog() as dlg, ui.card().classes("w-[500px] rounded-xl").style("background: #ffffff;"):
            ui.label("Personen zuweisen").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
            ui.label(f"{task.title} – {instance_date.strftime('%A, %d.%m.%Y')}").classes("text-caption mb-2").style("color: #64748b;")

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
                            },
                            value=mode or "none",
                            label="Modus",
                        ).props("dense outlined rounded").classes("flex-1")
                        if is_assigned:
                            remove_scope_select = ui.select(
                                {"single": "Nur diese", "all": "Alle im Zeitraum"},
                                value="single",
                                label="Entfernen:",
                            ).props("dense outlined rounded").style("min-width: 120px;")
                        else:
                            remove_scope_select = None
                        rows.append({"user_id": u.id, "cb": cb, "mode": mode_select, "was_assigned": is_assigned, "remove_scope": remove_scope_select})

            def save_assign():
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
                _apply_assignment_rules(db2, instance, rows)
                # Remove from all instances in date range if requested
                dates_now = get_dates()
                for r in rows:
                    if r.get("was_assigned") and not r["cb"].value and r.get("remove_scope") and r["remove_scope"].value == "all":
                        _remove_user_from_all_instances(db2, instance.task_id, r["user_id"], dates_now)
                db2.close()
                dlg.close()
                ui.notify("Zuweisung gespeichert", type="positive")
                rebuild()

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat rounded no-caps")
                ui.button("Speichern", on_click=save_assign).props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
        db.close()
        dlg.open()

    def _apply_assignment_rules(db: Session, source_instance: TaskInstance, rows: list[dict]):
        dates = get_dates()
        other_instances = (
            db.query(TaskInstance)
            .options(joinedload(TaskInstance.assigned_users))
            .filter(
                TaskInstance.task_id == source_instance.task_id,
                TaskInstance.date.in_(dates),
                TaskInstance.id != source_instance.id,
            )
            .all()
        )
        for r in rows:
            if not r["cb"].value:
                continue
            mode_val = r["mode"].value
            if mode_val == "none":
                continue
            uid = r["user_id"]
            for other_inst in other_instances:
                should_assign = False
                if mode_val == "immer":
                    should_assign = True
                elif mode_val.startswith("jeden_"):
                    weekday = int(mode_val.split("_")[1])
                    if other_inst.date.weekday() == weekday:
                        should_assign = True
                if should_assign and uid not in [u.id for u in other_inst.assigned_users]:
                    u = db.query(User).get(uid)
                    if u:
                        other_inst.assigned_users.append(u)
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

            day_cbs, daily_cb = _weekday_picker()
            ui.separator().classes("my-1")
            ui.label("Datum (für einmalige Aufgabe, wenn kein Wochentag ausgewählt):").classes("text-caption").style("color: #64748b;")
            date_in = ui.input(value=date.today().isoformat()).props("outlined rounded dense type=date").classes("w-full")

            def save():
                selected = _selected_days_from_checkboxes(day_cbs, daily_cb)
                db2 = _get_db()
                max_order = db2.query(Task.sort_order).order_by(Task.sort_order.desc()).first()
                next_order = (max_order[0] + 1) if max_order and max_order[0] is not None else 0
                t = Task(
                    id=str(uuid.uuid4()),
                    title=title_in.value.strip(),
                    description=desc_in.value.strip() or None,
                    base_duration_minutes=int(dur_in.value),
                    is_recurring=len(selected) > 0,
                    recurrence_rule=_recurrence_days_to_str(selected),
                    sort_order=next_order,
                )
                if tag_select and tag_select.value:
                    for tid in tag_select.value:
                        tag = db2.query(Tag).get(tid)
                        if tag:
                            t.tags.append(tag)
                db2.add(t)
                db2.flush()
                if not selected and date_in.value:
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
        task = db.query(Task).options(joinedload(Task.tags)).get(task_id)
        if not task:
            db.close()
            return
        current_days = _parse_recurrence_days(task.recurrence_rule)
        current_tag_ids = [t.id for t in task.tags]
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

            day_cbs, daily_cb = _weekday_picker(current_days)

            def save():
                selected = _selected_days_from_checkboxes(day_cbs, daily_cb)
                db2 = _get_db()
                t = db2.query(Task).options(joinedload(Task.tags)).get(task_id)
                t.title = title_in.value.strip()
                t.description = desc_in.value.strip() or None
                t.base_duration_minutes = int(dur_in.value)
                t.is_recurring = len(selected) > 0
                t.recurrence_rule = _recurrence_days_to_str(selected)
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
                    ui.button("Aufgabe erstellen", on_click=_open_add_task_dialog, icon="add_task").props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
                    ui.button("Tags", on_click=_open_manage_tags_dialog, icon="label").props("rounded flat no-caps").style("color: #0A2540;")
                    ui.button("Benutzer", on_click=_open_manage_users_dialog, icon="group").props("rounded flat no-caps").style("color: #0A2540;")

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

            with ui.element("div").props('id="hrp-scroll-container"').classes("w-full overflow-x-auto rounded-xl").style(
                "background: #ffffff; box-shadow: 0 2px 8px rgba(10,37,64,0.06);"
            ):
                with ui.element("table").classes("w-full border-collapse text-xs"):
                    with ui.element("thead"):
                        with ui.element("tr"):
                            with ui.element("th").classes("p-2 text-left font-bold sticky left-0 z-10 min-w-[220px]").style("background: #0A2540;"):
                                ui.label("Aufgabe").style("color: #00E5FF; font-family: Outfit, sans-serif;")
                            for d in dates:
                                is_today = d == date.today()
                                bg = "background: rgba(0,229,255,0.25);" if is_today else "background: #0A2540;"
                                with ui.element("th").classes("p-2 text-center min-w-[110px] font-bold").style(bg):
                                    weekday = WEEKDAY_LABELS[d.weekday()]
                                    is_weekend = d.weekday() >= 5
                                    col_style = "color: #00E5FF;" if is_today else ("color: #fb923c;" if is_weekend else "color: #e2e8f0;")
                                    ui.label(weekday).classes("text-[11px]").style(col_style)
                                    ui.label(d.strftime("%d.%m")).style(col_style)

                    with ui.element("tbody").props('id="task-tbody"'):
                        for task in tasks:
                            rec_days = _parse_recurrence_days(task.recurrence_rule)
                            is_daily = len(rec_days) == 7
                            rec_label = "Täglich" if is_daily else (", ".join(WEEKDAY_MAP[dd] for dd in rec_days) if rec_days else "")

                            with ui.element("tr").props(f'data-task-id="{task.id}"').classes("cursor-move"):
                                with ui.element("td").classes("p-2 sticky left-0 z-10 border-b border-gray-200").style("background: #f8fafc;"):
                                    with ui.row().classes("items-center gap-2 no-wrap"):
                                        if is_admin:
                                            ui.icon("drag_indicator", size="16px", color="grey").classes("drag-handle cursor-grab")
                                        ui.icon("task_alt", size="16px").style("color: #00C2D1;")
                                        with ui.column().classes("gap-0"):
                                            with ui.row().classes("items-center gap-1"):
                                                ui.label(task.title).classes("font-bold text-sm").style("color: #0A2540;")
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
                                    cell_bg = f"background: {bg_c}; border-left: 3px solid {border_c};"
                                    if is_today:
                                        cell_bg += " box-shadow: inset 0 0 0 2px rgba(0,194,209,0.4);"

                                    with ui.element("td").classes("p-1 text-center align-top border-b border-gray-100 hrp-matrix-cell").style(cell_bg):
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
                ui.button(icon="add", on_click=activate).props("flat round dense size=sm").style("color: #00C2D1;")
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
                ui.html(f'<span style="color:{icon_c}; font-size:10px;">⚠ offen</span>')

            with ui.column().classes("items-center gap-0 mt-1"):
                with ui.row().classes("gap-0 justify-center"):
                    if is_admin:
                        def open_assign(iid=inst.id, dt=d, t=task, aids=assigned_ids):
                            _open_assign_dialog(iid, dt, t, users_all, aids)
                        ui.button(icon="group_add", on_click=open_assign).props("flat round dense size=sm").style("color: #00C2D1;")
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
                            ui.button(icon="person_add", on_click=add_self).props("flat round dense size=sm").style("color: #00C2D1;")

                    if is_admin or user.id in assigned_ids:
                        def toggle_status(iid=inst.id):
                            db2 = _get_db()
                            instance = db2.query(TaskInstance).get(iid)
                            if instance:
                                instance.status = TaskStatus.COMPLETED if instance.status == TaskStatus.OPEN else TaskStatus.OPEN
                                db2.commit()
                            db2.close()
                            ui.notify("Status aktualisiert", type="positive")
                            rebuild()
                        icon_name = "check_circle" if completed else "radio_button_unchecked"
                        ui.button(icon=icon_name, on_click=toggle_status).props(f"flat round dense size=sm color={'green' if completed else 'grey'}")

                with ui.row().classes("gap-0 justify-center"):
                    # Notes button
                    def open_notes(iid=inst.id, dt=d, tt=task.title):
                        _open_notes_dialog(iid, dt, tt)
                    note_color = "#00C2D1" if inst.notes else "#94a3b8"
                    ui.button(icon="sticky_note_2", on_click=open_notes).props("flat round dense size=sm").style(f"color: {note_color};")

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
                        ui.button(icon="close", on_click=deactivate).props("flat round dense size=sm color=red")

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
                with ui.row().classes("gap-2 mb-3"):
                    ui.button("Aufgabe erstellen", on_click=_open_add_task_dialog, icon="add_task").props("rounded unelevated no-caps").style("background: #00C2D1; color: white;")
                    ui.button("Tags", on_click=_open_manage_tags_dialog, icon="label").props("rounded flat no-caps").style("color: #0A2540;")
                    ui.button("Benutzer", on_click=_open_manage_users_dialog, icon="group").props("rounded flat no-caps").style("color: #0A2540;")

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
                    day_style = "color: #00C2D1; font-family: Outfit, sans-serif;" if is_today else ("color: #f97316; font-family: Outfit, sans-serif;" if is_weekend else "color: #0A2540; font-family: Outfit, sans-serif;")
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
                                    ui.icon("check_circle", size="24px", color="#10b981")
                                elif status == "overdue":
                                    ui.icon("warning", size="24px", color="#ef4444")
                                elif status == "assigned":
                                    ui.icon("person", size="24px").style("color: #00C2D1;")
                                elif status == "unassigned":
                                    ui.icon("help_outline", size="24px", color="#f59e0b")
                                else:
                                    ui.icon("radio_button_unchecked", size="24px", color="#94a3b8")

                                with ui.column().classes("gap-0"):
                                    ui.label(task.title).classes("text-subtitle2 font-bold").style("color: #0A2540;")
                                    if task.description:
                                        ui.label(task.description).classes("text-xs italic").style("color: #64748b;")
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.badge(f"{task.base_duration_minutes} min", color="cyan").props("rounded")
                                        _render_tags(tags)
                                        if inst:
                                            _render_user_chips(inst.assigned_users, users_all)
                                            if inst.notes:
                                                ui.html(f'<span style="font-size:10px;color:#64748b;">📝 {inst.notes[:40]}{"..." if len(inst.notes) > 40 else ""}</span>')
                                        elif status == "inactive":
                                            ui.label("Nicht aktiv").classes("text-xs italic").style("color: #94a3b8;")

                            with ui.row().classes("items-center gap-1"):
                                if inst is not None:
                                    assigned_ids = [u.id for u in inst.assigned_users]
                                    if is_admin:
                                        def open_a(iid=inst.id, dt=d, t=task, aids=assigned_ids):
                                            _open_assign_dialog(iid, dt, t, users_all, aids)
                                        ui.button(icon="group_add", on_click=open_a).props("flat round dense").style("color: #00C2D1;")
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
                                            ui.button(icon="person_add", on_click=add_self_l).props("flat round dense").style("color: #00C2D1;")

                                    if is_admin or user.id in assigned_ids:
                                        completed = inst.status == TaskStatus.COMPLETED
                                        def toggle_list(iid=inst.id):
                                            db2 = _get_db()
                                            instance = db2.query(TaskInstance).get(iid)
                                            if instance:
                                                instance.status = TaskStatus.COMPLETED if instance.status == TaskStatus.OPEN else TaskStatus.OPEN
                                                db2.commit()
                                            db2.close()
                                            ui.notify("Status aktualisiert", type="positive")
                                            rebuild()
                                        if completed:
                                            ui.button("Erledigt", on_click=toggle_list, icon="check_circle", color="green").props("rounded unelevated no-caps size=sm")
                                        else:
                                            ui.button("Erledigen", on_click=toggle_list, icon="radio_button_unchecked").props("rounded unelevated no-caps size=sm").style("background: #00C2D1; color: white;")

                                    # Notes button in list
                                    def open_notes_l(iid=inst.id, dt=d, tt=task.title):
                                        _open_notes_dialog(iid, dt, tt)
                                    note_col = "#00C2D1" if inst.notes else "#94a3b8"
                                    ui.button(icon="sticky_note_2", on_click=open_notes_l).props("flat round dense").style(f"color: {note_col};")

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
                                        ui.button(icon="close", on_click=deactivate_l).props("flat round dense size=sm color=red")
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
                                        ui.button("Aktivieren", on_click=activate_l, icon="add_circle_outline").props("flat rounded no-caps size=sm").style("color: #00C2D1;")

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
                ui.label("Offene Aufgaben ohne Zuweisung").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")
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
                                    ui.label(inst.task.title).classes("text-subtitle2 font-bold").style("color: #0A2540;")
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.badge(f"{inst.task.base_duration_minutes} min", color="cyan").props("rounded")
                                        _render_tags(inst.task.tags)
                            if is_admin:
                                def open_a(iid=inst.id, t=inst.task, aids=[]):
                                    _open_assign_dialog(iid, today, t, users_all, aids)
                                ui.button("Zuweisen", on_click=open_a, icon="group_add").props("rounded unelevated no-caps size=sm").style("background: #00C2D1; color: white;")

            ui.separator().classes("my-4")

            # --------------- User cards ---------------
            with ui.row().classes("items-center gap-2 mb-3"):
                ui.icon("group", size="28px").style("color: #00C2D1;")
                ui.label("Haushaltsmitglieder").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")

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
                        f"background: #ffffff; border-top: 4px solid {color}; box-shadow: 0 4px 16px rgba(10,37,64,0.12);"
                    ) as detail_card:
                        with ui.row().classes("w-full items-center justify-between mb-3"):
                            with ui.row().classes("items-center gap-3"):
                                ui.html(f'<span class="hrp-user-chip" style="background:{color}; font-size:14px; padding: 4px 14px;">{target_user.username}</span>')
                                ui.label(f"Kapazität: {target_user.daily_capacity_minutes} Min/Tag").classes("text-sm").style("color: #64748b;")
                            ui.button(icon="close", on_click=lambda: user_detail_container.clear()).props("flat round dense color=grey")

                        def _render_task_section(title: str, icon_name: str, icon_color: str, task_list: list[TaskInstance], show_date: bool = False):
                            with ui.row().classes("items-center gap-2 mt-2 mb-1"):
                                ui.icon(icon_name, size="20px", color=icon_color)
                                ui.label(title).classes("text-subtitle2 font-bold").style("color: #0A2540;")
                                ui.badge(str(len(task_list)), color="grey").props("rounded")
                            if not task_list:
                                ui.label("Keine Aufgaben").classes("text-xs italic ml-7").style("color: #94a3b8;")
                            else:
                                for inst in task_list:
                                    status = _cell_status(inst, inst.date)
                                    border_c, bg_c, _ = CELL_STYLES[status]
                                    with ui.card().classes("w-full mb-1 py-1 px-3 rounded-lg").style(
                                        f"background: #f8fafc; border-left: 3px solid {border_c};"
                                    ):
                                        with ui.row().classes("items-center justify-between w-full"):
                                            with ui.row().classes("items-center gap-2"):
                                                if status == "completed":
                                                    ui.icon("check_circle", size="18px", color="#10b981")
                                                elif status == "overdue":
                                                    ui.icon("warning", size="18px", color="#ef4444")
                                                else:
                                                    ui.icon("radio_button_unchecked", size="18px").style("color: #00C2D1;")
                                                ui.label(inst.task.title).classes("text-sm font-medium").style("color: #0A2540;")
                                                ui.badge(f"{inst.task.base_duration_minutes} min", color="cyan").props("rounded")
                                                _render_tags(inst.task.tags)
                                                if show_date:
                                                    ui.label(inst.date.strftime("%d.%m")).classes("text-xs").style("color: #64748b;")
                                            if is_admin or user.id in [u.id for u in inst.assigned_users]:
                                                completed = inst.status == TaskStatus.COMPLETED
                                                def toggle_s(iid=inst.id):
                                                    db3 = _get_db()
                                                    instance = db3.query(TaskInstance).get(iid)
                                                    if instance:
                                                        instance.status = TaskStatus.COMPLETED if instance.status == TaskStatus.OPEN else TaskStatus.OPEN
                                                        db3.commit()
                                                    db3.close()
                                                    ui.notify("Status aktualisiert", type="positive")
                                                    rebuild()
                                                if completed:
                                                    ui.button(icon="check_circle", on_click=toggle_s).props("flat round dense size=xs color=green")
                                                else:
                                                    ui.button(icon="radio_button_unchecked", on_click=toggle_s).props("flat round dense size=xs color=grey")

                        _render_task_section(f"Heute – {today.strftime('%d.%m.%Y')}", "today", "#3b82f6", user_today)
                        _render_task_section(f"Morgen – {tomorrow.strftime('%d.%m.%Y')}", "event", "#8b5cf6", user_tomorrow)
                        if user_overdue:
                            _render_task_section("Überfällig", "warning", "#ef4444", user_overdue, show_date=True)

                        # Summary
                        today_mins = sum(i.task.base_duration_minutes / max(len(i.assigned_users), 1) for i in user_today)
                        with ui.row().classes("mt-3 items-center gap-2"):
                            ui.icon("schedule", size="18px", color=color)
                            ui.label(f"Heute geplant: {today_mins:.0f} / {target_user.daily_capacity_minutes} Min").classes("text-sm font-medium").style("color: #0A2540;")

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
                                    ui.label(f"{user_today_count}").classes("text-lg font-bold").style("color: #0A2540;")
                                    ui.label("Aufgaben").classes("text-[10px] uppercase").style("color: #64748b;")
                                with ui.column().classes("items-center gap-0"):
                                    ui.label(f"{today_mins:.0f}").classes("text-lg font-bold").style("color: #0A2540;")
                                    ui.label("Minuten").classes("text-[10px] uppercase").style("color: #64748b;")
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
                ui.label("Statistik").classes("text-h6 font-bold").style("color: #0A2540; font-family: Outfit, sans-serif;")

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
                            ui.label(u.username).classes("font-bold").style("color: #0A2540;")
                        with ui.row().classes("gap-4 flex-wrap"):
                            with ui.column().classes("gap-0"):
                                ui.label("Geplant").classes("text-[10px] uppercase").style("color: #64748b;")
                                ui.label(f"{total:.0f} min").classes("text-lg font-bold").style("color: #0A2540;")
                            with ui.column().classes("gap-0"):
                                ui.label("Ø/Tag").classes("text-[10px] uppercase").style("color: #64748b;")
                                ui.label(f"{avg_per_day:.0f} min").classes("text-lg font-bold").style("color: #0A2540;")
                            with ui.column().classes("gap-0"):
                                ui.label("Auslastung").classes("text-[10px] uppercase").style("color: #64748b;")
                                util_color = "#ef4444" if utilization > 100 else ("#f59e0b" if utilization > 80 else "#10b981")
                                ui.label(f"{utilization:.0f}%").classes("text-lg font-bold").style(f"color: {util_color};")

            with ui.card().classes("w-full hrp-stat-card px-4 py-3").style("border-left: 3px solid #00C2D1;"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("functions", size="20px").style("color: #00C2D1;")
                    ui.label(f"Gesamttotal: {total_all:.0f} Minuten im Zeitraum").classes("font-bold").style("color: #0A2540;")

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
