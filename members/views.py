import hashlib

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.stepup import step_up_required
from audit.services import record as audit
from tenancy.permissions import requires
from tenancy.roles import Perm

from . import services
from .forms import ImportUploadForm, MemberForm, ReasonForm
from .importing import ImportFormatError, build_plan, parse_csv, plan_digest, summarise
from .models import ImportBatch, Invitation, Member


def _member(member_id) -> Member:
    # TenantManager + RLS: another organisation's member is simply "not found".
    return get_object_or_404(Member.objects, pk=member_id)


@require_GET
@requires(Perm.MEMBERS_VIEW)
def member_list(request, org_slug):
    q = request.GET.get("q", "").strip()[:100]
    status = request.GET.get("status", "active")
    members = Member.objects.all()
    if status in (Member.Status.ACTIVE, Member.Status.INACTIVE):
        members = members.filter(status=status)
    if q:
        members = members.filter(Q(full_name__icontains=q) | Q(member_number__icontains=q)
                                 | Q(email__icontains=q))
    page = Paginator(members, 50).get_page(request.GET.get("page"))
    return render(request, "members/member_list.html", {
        "page": page, "q": q, "status": status,
        "can_manage": Perm.MEMBERS_MANAGE in request.permissions,
    })


@require_http_methods(["GET", "POST"])
@requires(Perm.MEMBERS_MANAGE)
def member_create(request, org_slug):
    form = MemberForm(request.POST or None, org=request.org)
    if request.method == "POST" and form.is_valid():
        member = services.save_member(form.save(commit=False), actor=request.user, before=None)
        messages.success(request, "Member added.")
        return redirect("members:detail", org_slug=org_slug, member_id=member.id)
    return render(request, "members/member_form.html", {"form": form, "creating": True})


@require_GET
@requires(Perm.MEMBERS_VIEW)
def member_detail(request, org_slug, member_id):
    member = _member(member_id)
    invitation = (Invitation.objects.filter(member=member, accepted_at__isnull=True,
                                            revoked_at__isnull=True)
                  .order_by("-created_at").first())
    if member.user_id is not None and invitation is None and member.user.status == "active":
        account = "Account active"
    elif invitation is not None and invitation.is_open:
        account = f"Invitation sent, expires {invitation.expires_at:%d %b %Y %H:%M}"
    elif invitation is not None:
        account = "Invitation expired"
    else:
        account = "Not invited"
    return render(request, "members/member_detail.html", {
        "member": member, "account": account,
        "can_manage": Perm.MEMBERS_MANAGE in request.permissions,
    })


@require_http_methods(["GET", "POST"])
@requires(Perm.MEMBERS_MANAGE)
def member_edit(request, org_slug, member_id):
    member = _member(member_id)
    before = services._snapshot(member)
    form = MemberForm(request.POST or None, instance=member, org=request.org)
    if request.method == "POST" and form.is_valid():
        try:
            services.save_member(form.save(commit=False), actor=request.user, before=before)
        except services.MemberError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Member updated.")
            return redirect("members:detail", org_slug=org_slug, member_id=member.id)
    return render(request, "members/member_form.html", {"form": form, "member": member})


@require_POST
@requires(Perm.MEMBERS_MANAGE)
def member_invite(request, org_slug, member_id):
    member = _member(member_id)
    try:
        services.invite_member(member, actor=request.user)
    except services.MemberError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Invitation sent to {member.email}.")
    return redirect("members:detail", org_slug=org_slug, member_id=member.id)


@require_http_methods(["GET", "POST"])
@requires(Perm.MEMBERS_MANAGE)
@step_up_required
def member_deactivate(request, org_slug, member_id):
    member = _member(member_id)
    form = ReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.deactivate_member(member, reason=form.cleaned_data["reason"],
                                       actor=request.user)
        except services.MemberError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Member deactivated.")
            return redirect("members:detail", org_slug=org_slug, member_id=member.id)
    return render(request, "members/member_deactivate.html", {"form": form, "member": member})


@require_http_methods(["GET", "POST"])
@requires(Perm.MEMBERS_MANAGE)
@step_up_required
def member_reactivate(request, org_slug, member_id):
    member = _member(member_id)
    if request.method == "POST":
        try:
            services.reactivate_member(member, actor=request.user)
        except services.MemberError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Member reactivated.")
        return redirect("members:detail", org_slug=org_slug, member_id=member.id)
    return render(request, "members/member_reactivate.html", {"member": member})


@require_http_methods(["GET", "POST"])
@requires(Perm.MEMBERS_MANAGE)
def import_upload(request, org_slug):
    form = ImportUploadForm(request.POST or None, request.FILES or None)
    error = None
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        raw = upload.read(2 * 1024 * 1024 + 1)
        try:
            rows = parse_csv(raw)
        except ImportFormatError as exc:
            error = str(exc)
        else:
            plan = build_plan(request.org, rows)
            batch = ImportBatch.objects.create(
                organisation=request.org, created_by=request.user,
                file_sha256=hashlib.sha256(raw).hexdigest(), rows=rows,
                plan_digest=plan_digest(plan))
            return redirect("members:import_preview", org_slug=org_slug, batch_id=batch.id)
    return render(request, "members/import_upload.html", {"form": form, "error": error},
                  status=400 if error else 200)


@require_GET
@requires(Perm.MEMBERS_MANAGE)
def import_preview(request, org_slug, batch_id):
    batch = get_object_or_404(ImportBatch.objects, pk=batch_id)
    plan = build_plan(request.org, batch.rows)
    stale = plan_digest(plan) != batch.plan_digest
    return render(request, "members/import_preview.html", {
        "batch": batch, "plan": plan, "counts": summarise(plan), "stale": stale,
        "visible": [p for p in plan if p.action != "unchanged"],
    })


@require_http_methods(["GET", "POST"])
@requires(Perm.MEMBERS_MANAGE)
@step_up_required
def import_apply(request, org_slug, batch_id):
    batch = get_object_or_404(ImportBatch.objects, pk=batch_id)
    if request.method == "POST":
        try:
            counts = services.apply_import(batch, actor=request.user)
        except services.MemberError as exc:
            messages.error(request, str(exc))
            return redirect("members:import_preview", org_slug=org_slug, batch_id=batch.id)
        messages.success(request, f"Import applied: {counts['create']} added, "
                                  f"{counts['update']} updated.")
        return redirect("members:list", org_slug=org_slug)
    return redirect("members:import_preview", org_slug=org_slug, batch_id=batch.id)


@require_POST
@requires(Perm.MEMBERS_MANAGE)
def import_discard(request, org_slug, batch_id):
    batch = get_object_or_404(ImportBatch.objects, pk=batch_id,
                              status=ImportBatch.Status.PREVIEWED)
    batch.status = ImportBatch.Status.DISCARDED
    batch.rows = []  # data minimisation
    batch.save(update_fields=["status", "rows"])
    audit("member.import_discarded", actor=request.user, organisation_id=request.org.id,
          object_type="member_import_batch", object_id=batch.id)
    return redirect("members:list", org_slug=org_slug)
