"""CSV import of the membership register: parse → preview plan → apply.

Rules:
* Rows are matched on ``member_number``. A file never deactivates or deletes anyone.
* The whole file is applied or nothing is: any row error blocks applying.
* Applying re-checks the plan against the current register and refuses if it
  changed since the preview.
"""

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field

from django import forms

from .models import DEFAULT_CATEGORIES, MEMBER_NUMBER_VALIDATOR, PHONE_VALIDATOR, Member

COLUMNS = ("member_number", "full_name", "email", "phone", "category",
           "subscription_status", "joined_on")
REQUIRED_COLUMNS = ("member_number", "full_name", "category", "subscription_status", "joined_on")
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 20000
COMPARED_FIELDS = ("full_name", "email", "phone", "category", "subscription_status", "joined_on")


class ImportFormatError(ValueError):
    pass


def categories_for(org) -> tuple[str, ...]:
    configured = (org.governance_settings or {}).get("member_categories")
    return tuple(configured) if configured else DEFAULT_CATEGORIES


class RowForm(forms.Form):
    member_number = forms.CharField(max_length=32, validators=[MEMBER_NUMBER_VALIDATOR])
    full_name = forms.CharField(max_length=200)
    email = forms.EmailField(max_length=254, required=False)
    phone = forms.CharField(max_length=24, required=False, validators=[PHONE_VALIDATOR])
    category = forms.CharField(max_length=32)
    subscription_status = forms.ChoiceField(choices=Member.Subscription.choices)
    joined_on = forms.DateField(input_formats=["%Y-%m-%d"])

    def __init__(self, *args, categories, **kwargs):
        super().__init__(*args, **kwargs)
        self.categories = categories

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean_category(self):
        value = self.cleaned_data["category"].strip().lower()
        if value not in self.categories:
            allowed = ", ".join(self.categories)
            raise forms.ValidationError(f"Unknown category. Use one of: {allowed}.")
        return value


def parse_csv(raw: bytes) -> list[dict]:
    if len(raw) > MAX_BYTES:
        raise ImportFormatError("The file is larger than 2 MB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFormatError("The file must be saved as UTF-8 CSV.") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise ImportFormatError("The file is empty.")
    header = [(h or "").strip().lower() for h in reader.fieldnames]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    unknown = [h for h in header if h not in COLUMNS]
    if missing:
        raise ImportFormatError(f"Missing column(s): {', '.join(missing)}.")
    if unknown:
        raise ImportFormatError(f"Unknown column(s): {', '.join(unknown)}.")
    if len(set(header)) != len(header):
        raise ImportFormatError("A column appears more than once.")
    reader.fieldnames = header
    rows = []
    for line, record in enumerate(reader, start=2):
        if None in record:
            raise ImportFormatError(f"Line {line} has more values than there are columns.")
        if not any((v or "").strip() for v in record.values()):
            continue
        rows.append({"line": line, **{c: (record.get(c) or "").strip() for c in COLUMNS}})
        if len(rows) > MAX_ROWS:
            raise ImportFormatError(f"The file has more than {MAX_ROWS} rows.")
    if not rows:
        raise ImportFormatError("The file has no member rows.")
    return rows


@dataclass
class RowPlan:
    line: int
    member_number: str
    action: str  # create | update | unchanged | error
    changes: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    cleaned: dict = field(default_factory=dict)
    note: str = ""


def build_plan(org, rows: list[dict]) -> list[RowPlan]:
    existing = {m.member_number: m for m in Member.objects.all()}
    email_owner = {m.email: m.member_number for m in existing.values() if m.email}
    categories = categories_for(org)
    seen_numbers: dict[str, int] = {}
    seen_emails: dict[str, int] = {}
    plan = []
    for row in rows:
        form = RowForm({c: row[c] for c in COLUMNS}, categories=categories)
        number = row["member_number"]
        item = RowPlan(line=row["line"], member_number=number, action="error")
        if not form.is_valid():
            item.errors = [f"{name}: {msg}" for name, msgs in form.errors.items() for msg in msgs]
            plan.append(item)
            continue
        data = form.cleaned_data
        if number in seen_numbers:
            item.errors.append(f"member_number repeats line {seen_numbers[number]}")
        seen_numbers.setdefault(number, row["line"])
        if data["email"]:
            if data["email"] in seen_emails:
                item.errors.append(f"email repeats line {seen_emails[data['email']]}")
            seen_emails.setdefault(data["email"], row["line"])
            owner = email_owner.get(data["email"])
            if owner is not None and owner != number:
                item.errors.append(f"email already belongs to member {owner}")
        current = existing.get(number)
        if current is not None:
            for name in COMPARED_FIELDS:
                old, new = getattr(current, name), data[name]
                if old != new:
                    item.changes[name] = [str(old), str(new)]
            if "email" in item.changes and current.user_id is not None:
                item.errors.append("email cannot change for a member who has an account")
            if not current.is_active:
                item.note = "inactive member: details updated, stays inactive"
        if not item.errors:
            item.action = "update" if item.changes else ("unchanged" if current else "create")
        item.cleaned = {k: (v.isoformat() if k == "joined_on" else v) for k, v in data.items()}
        plan.append(item)
    return plan


def plan_digest(plan: list[RowPlan]) -> str:
    canonical = [[p.line, p.member_number, p.action, sorted(p.changes.items()), p.errors]
                 for p in plan]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def summarise(plan: list[RowPlan]) -> dict:
    counts = {"create": 0, "update": 0, "unchanged": 0, "error": 0}
    for p in plan:
        counts[p.action] += 1
    return counts
