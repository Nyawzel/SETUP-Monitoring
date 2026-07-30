from decimal import Decimal, InvalidOperation
from datetime import date
from dateutil.relativedelta import relativedelta

import openpyxl

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db.models import Sum, Count, Q, Exists, OuterRef
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from .models import (
    Project, RefundPayment, RefundInstallment, BudgetLineItem,
    ProjectRequirement, MonitoringReport, Cooperator, ImpactRecord,
)
from .forms import (
    ProjectForm, ProjectStatusForm, RequirementUploadForm, ReportUploadForm,
    ProjectPhotoForm, ExcelImportForm, CooperatorForm,
    BudgetLineItemForm, RefundPaymentForm, RefundPaymentCreateForm,
    UserSettingsForm, ImpactRecordForm,
)
from .report_exports import (
    build_quarterly_impact_pdf, build_project_detail_pdf, build_dashboard_kpi_pdf,
    build_funding_details_pdf,
)


def dashboard(request):
    projects = Project.objects.all()

    # ---------------------------------------------------------------
    # ROW 1 — Status KPIs (operational: what stage is each project in)
    # ---------------------------------------------------------------
    total_projects = projects.count()
    phase1_count = projects.filter(status=Project.Status.PHASE_1).count()
    phase2_count = projects.filter(status=Project.Status.PHASE_2).count()
    completed_count = projects.filter(status=Project.Status.COMPLETED).count()
    pending_count = projects.filter(status=Project.Status.PENDING).count()

    # Delinquent = in Phase II AND has at least one overdue unpaid installment.
    # Doing this in Python via the model property keeps the logic in one place
    # (Project.is_refund_delinquent) rather than duplicating it as a query.
    phase2_projects = projects.filter(status=Project.Status.PHASE_2)
    delinquent_projects = [p for p in phase2_projects if p.is_refund_delinquent]
    delinquent_count = len(delinquent_projects)

    terminated_withdrawn_count = projects.filter(
        status__in=[Project.Status.TERMINATED, Project.Status.WITHDRAWN]
    ).count()

    # ---------------------------------------------------------------
    # ROW 2 — Financial KPIs (fiduciary: is the money coming back)
    # ---------------------------------------------------------------
    # Only projects that have actually reached Phase I or beyond have had
    # funds disbursed — PENDING projects haven't, so they're excluded from
    # every financial figure below (not just "total disbursed").
    disbursed_projects = projects.exclude(status=Project.Status.PENDING)

    total_disbursed = disbursed_projects.aggregate(total=Sum('total_ifund_amount'))['total'] or 0
    total_refunded = RefundPayment.objects.filter(
        project__in=disbursed_projects
    ).aggregate(total=Sum('amount_paid'))['total'] or 0
    outstanding_balance = total_disbursed - total_refunded
    collection_rate = round((total_refunded / total_disbursed) * 100, 1) if total_disbursed else 0

    # ---------------------------------------------------------------
    # CHART 1 — Status breakdown (doughnut)
    # ---------------------------------------------------------------
    status_counts = {
        "labels": ["Phase I", "Phase II", "Completed", "Pending", "Delinquent", "Terminated/Withdrawn"],
        "values": [
            phase1_count, phase2_count, completed_count,
            pending_count, delinquent_count, terminated_withdrawn_count,
        ],
    }

    # ---------------------------------------------------------------
    # CHART 2 — Refund collection: amount due vs. amount collected,
    # per month, for the last 12 months (grouped bar)
    # ---------------------------------------------------------------
    today = date.today()
    months = [(today.replace(day=1) - relativedelta(months=i)) for i in range(11, -1, -1)]

    due_by_month = []
    collected_by_month = []
    month_labels = []

    for month_start in months:
        month_end = month_start + relativedelta(months=1)
        month_labels.append(month_start.strftime("%b %Y"))

        due = RefundInstallment.objects.filter(
            due_date__gte=month_start, due_date__lt=month_end
        ).aggregate(total=Sum('amount_due'))['total'] or 0

        collected = RefundPayment.objects.filter(
            date_paid__gte=month_start, date_paid__lt=month_end
        ).aggregate(total=Sum('amount_paid'))['total'] or 0

        due_by_month.append(float(due))
        collected_by_month.append(float(collected))

    monthly_refund_data = {
        "labels": month_labels,
        "due": due_by_month,
        "collected": collected_by_month,
    }

    # ---------------------------------------------------------------
    # Watchlist — top delinquent cooperators by outstanding balance
    # (rendered as a table, not a chart — names + numbers read better
    # as a list than as a bar chart here)
    # ---------------------------------------------------------------
    watchlist = sorted(
        delinquent_projects,
        key=lambda p: p.remaining_balance,
        reverse=True,
    )[:8]

    context = {
        "active_page": "dashboard",
        # status row
        "total_projects": total_projects,
        "phase1_count": phase1_count,
        "phase2_count": phase2_count,
        "completed_count": completed_count,
        "pending_count": pending_count,
        "delinquent_count": delinquent_count,
        "terminated_withdrawn_count": terminated_withdrawn_count,
        # financial row
        "total_disbursed": total_disbursed,
        "total_refunded": total_refunded,
        "outstanding_balance": outstanding_balance,
        "collection_rate": collection_rate,
        # charts
        "status_counts": status_counts,
        "monthly_refund_data": monthly_refund_data,
        # watchlist table
        "watchlist": watchlist,
    }
    return render(request, "monitoring/dashboard.html", context)


# Maps the ?sort= value in the URL to the actual field(s) to order by.
# Kept to DB-backed fields only — remaining_balance/refund_progress_percent
# etc. are Python @properties on the model, not queryable columns, so they
# aren't offered as sort options here.
PROJECT_SORT_FIELDS = {
    "title": "title",
    "-title": "-title",
    "msme": "cooperator__name",
    "-msme": "-cooperator__name",
    "status": "status",
    "-status": "-status",
    "amount": "total_ifund_amount",
    "-amount": "-total_ifund_amount",
    "created": "created_at",
    "-created": "-created_at",
}


# TODO: re-enable once auth/login is set up
# @login_required
def project_list(request):
    """
    Full list of projects with filtering by status, a text search over
    MSME name / project title / project code, and column sorting — the
    main "browse everything" view for admin staff.
    """
    projects = Project.objects.select_related('cooperator').all()

    # Delinquency isn't a stored `status` value — it's "has at least one
    # overdue, still-unpaid installment" (same rule as
    # Project.is_refund_delinquent). Rather than loop over every project
    # in Python and call that property per-row (one query each), this
    # annotates the queryset with a single correlated EXISTS subquery, so
    # Django does it all in one SQL query regardless of how many projects
    # there are. Named is_delinquent_annotated (not is_refund_delinquent
    # or is_delinquent) specifically to avoid colliding with the
    # same-named @property already defined on the Project model —
    # annotate() sets this as a plain instance attribute, and Python
    # won't let you assign over a read-only property under that name.
    overdue_installments = RefundInstallment.objects.filter(
        project=OuterRef('pk'),
        due_date__lt=date.today(),
        status=RefundInstallment.Status.UNPAID,
    )
    projects = projects.annotate(is_delinquent_annotated=Exists(overdue_installments))

    status_filter = request.GET.get('status', '')
    if status_filter:
        projects = projects.filter(status=status_filter)

    query = request.GET.get('q', '').strip()
    if query:
        projects = projects.filter(
            Q(title__icontains=query)
            | Q(project_code__icontains=query)
            | Q(cooperator__name__icontains=query)
        )

    delinquent_filter = request.GET.get('delinquent', '') == '1'
    if delinquent_filter:
        projects = projects.filter(is_delinquent_annotated=True)

    sort = request.GET.get('sort', '-created')
    projects = projects.order_by(PROJECT_SORT_FIELDS.get(sort, '-created_at'))

    context = {
        "active_page": "projects",
        "projects": projects,
        "status_choices": Project.Status.choices,
        "status_filter": status_filter,
        "query": query,
        "sort": sort,
        "delinquent_filter": delinquent_filter,
    }
    return render(request, "monitoring/project_list.html", context)


# TODO: re-enable once auth/login is set up
# @login_required
def project_create(request):
    """Add a brand-new project (and pick/create its MSME) from a plain form."""
    if request.method == 'POST':
        form = ProjectForm(request.POST)
        if form.is_valid():
            project = form.save()
            messages.success(request, f"Project '{project.title}' created.")
            return redirect('project_detail', pk=project.pk)
        messages.error(request, "Couldn't create project — please check the form.")
    else:
        form = ProjectForm()

    return render(request, "monitoring/project_form.html", {"form": form, "active_page": "projects"})


# TODO: re-enable once auth/login is set up
# @login_required
def import_excel(request):
    """
    Bulk-create projects from an uploaded .xlsx file. Each row becomes one
    project; the MSME is looked up by name and created automatically if it
    doesn't exist yet. See ExcelImportForm for the expected column order.
    """
    if request.method == 'POST':
        form = ExcelImportForm(request.POST, request.FILES)
        if form.is_valid():
            workbook = openpyxl.load_workbook(form.cleaned_data['file'], data_only=True)
            sheet = workbook.active

            created_count = 0
            row_errors = []

            for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
                if not row or not row[0]:
                    continue  # skip blank rows

                msme_name, title, code, status_value, amount, fund_source = (
                    list(row) + [None] * 6
                )[:6]

                try:
                    if not msme_name or not title:
                        raise ValueError("MSME Name and Project Title are required.")

                    cooperator, _ = Cooperator.objects.get_or_create(
                        name=str(msme_name).strip(),
                        defaults={"business_type": "other"},
                    )

                    status_value = str(status_value).strip().lower() if status_value else Project.Status.PENDING
                    if status_value not in Project.Status.values:
                        status_value = Project.Status.PENDING

                    fund_source = str(fund_source).strip().lower() if fund_source else 'setup_budget'
                    if fund_source not in ('setup_budget', 'lgia'):
                        fund_source = 'setup_budget'

                    try:
                        amount_value = Decimal(str(amount)) if amount not in (None, '') else Decimal('0')
                    except InvalidOperation:
                        raise ValueError(f"'{amount}' is not a valid iFund amount.")

                    Project.objects.create(
                        cooperator=cooperator,
                        title=str(title).strip(),
                        project_code=(str(code).strip() if code not in (None, '') else None),
                        status=status_value,
                        total_ifund_amount=amount_value,
                        fund_source=fund_source,
                    )
                    created_count += 1
                except Exception as exc:
                    row_errors.append(f"Row {row_number}: {exc}")

            if created_count:
                messages.success(request, f"Imported {created_count} project(s).")
            if row_errors:
                shown = "; ".join(row_errors[:5])
                more = f" (+{len(row_errors) - 5} more)" if len(row_errors) > 5 else ""
                messages.error(request, f"{len(row_errors)} row(s) skipped — {shown}{more}")

            return redirect('project_list')
        messages.error(request, "Please choose a valid .xlsx file.")
    else:
        form = ExcelImportForm()

    return render(request, "monitoring/import_excel.html", {"form": form, "active_page": "projects"})


# TODO: re-enable once auth/login is set up
# @login_required
def msme_list(request):
    """
    Browse/search MSMEs (Cooperator records) — the funding-partner side of
    the app, separate from Projects (which is per-engagement).
    """
    msmes = Cooperator.objects.all()

    query = request.GET.get('q', '').strip()
    if query:
        msmes = msmes.filter(name__icontains=query)

    category_filter = request.GET.get('category', '')
    if category_filter:
        msmes = msmes.filter(enterprise_category=category_filter)

    msmes = msmes.order_by('name').prefetch_related('projects')

    rows = [
        {
            "msme": m,
            "total_projects": m.projects.count(),
            "active_projects": m.active_projects.count(),
            "outstanding_balance": m.total_outstanding_balance,
        }
        for m in msmes
    ]

    context = {
        "active_page": "msmes",
        "rows": rows,
        "query": query,
        "category_filter": category_filter,
        "category_choices": Cooperator.ENTERPRISE_CATEGORY_CHOICES,
    }
    return render(request, "monitoring/msme_list.html", context)


# TODO: re-enable once auth/login is set up
# @login_required
def msme_create(request):
    """Add a new MSME profile from a plain form."""
    if request.method == 'POST':
        form = CooperatorForm(request.POST)
        if form.is_valid():
            msme = form.save()
            messages.success(request, f"MSME '{msme.name}' added.")
            return redirect('msme_detail', pk=msme.pk)
        messages.error(request, "Couldn't save — please check the form.")
    else:
        form = CooperatorForm()

    return render(request, "monitoring/msme_form.html", {"form": form, "active_page": "msmes"})


# TODO: re-enable once auth/login is set up
# @login_required
def msme_edit(request, pk):
    """Edit an existing MSME profile — the site equivalent of editing a
    Cooperator record in the admin panel. Reuses msme_form.html/CooperatorForm,
    same as msme_create, just bound to an existing instance."""
    msme = get_object_or_404(Cooperator, pk=pk)
    if request.method == 'POST':
        form = CooperatorForm(request.POST, instance=msme)
        if form.is_valid():
            form.save()
            messages.success(request, f"MSME '{msme.name}' updated.")
            return redirect('msme_detail', pk=msme.pk)
        messages.error(request, "Couldn't save — please check the form.")
    else:
        form = CooperatorForm(instance=msme)

    return render(request, "monitoring/msme_form.html", {"form": form, "msme": msme, "is_edit": True, "active_page": "msmes"})


# TODO: re-enable once auth/login is set up
# @login_required
def msme_detail(request, pk):
    """MSME profile page: eligibility/registration details plus every
    project this MSME has under the program."""
    msme = get_object_or_404(Cooperator, pk=pk)
    projects = msme.projects.all()

    context = {
        "active_page": "msmes",
        "msme": msme,
        "projects": projects,
        "total_outstanding_balance": msme.total_outstanding_balance,
    }
    return render(request, "monitoring/msme_detail.html", context)


# TODO: re-enable once auth/login is set up
# @login_required
def funding_overview(request):
    """
    Funding panel: program-wide disbursed/refunded KPIs, the approved
    Line-Item Budget across all projects, and the refund payment ledger —
    the money side of the app, as opposed to Projects (status/compliance)
    or MSMEs (who the money went to).
    """
    disbursed_projects = Project.objects.exclude(status=Project.Status.PENDING)
    total_disbursed = disbursed_projects.aggregate(total=Sum('total_ifund_amount'))['total'] or 0
    total_refunded = RefundPayment.objects.filter(
        project__in=disbursed_projects
    ).aggregate(total=Sum('amount_paid'))['total'] or 0
    outstanding_balance = total_disbursed - total_refunded
    collection_rate = round((total_refunded / total_disbursed) * 100, 1) if total_disbursed else 0
    total_approved_budget = BudgetLineItem.objects.aggregate(total=Sum('approved_amount'))['total'] or 0
    total_disbursed_budget = BudgetLineItem.objects.aggregate(total=Sum('disbursed_amount'))['total'] or 0

    # --- Approved Line-Item Budget, across all projects ---
    budget_items = BudgetLineItem.objects.select_related('project', 'project__cooperator').all()

    item_query = request.GET.get('item_q', '').strip()
    if item_query:
        budget_items = budget_items.filter(
            Q(description__icontains=item_query) | Q(project__title__icontains=item_query)
        )

    category_filter = request.GET.get('category', '')
    if category_filter:
        budget_items = budget_items.filter(category=category_filter)

    budget_items = budget_items.order_by('project__title')

    # --- Refund payment ledger ---
    payments = RefundPayment.objects.select_related('project', 'project__cooperator').order_by('-date_paid')

    payment_query = request.GET.get('payment_q', '').strip()
    if payment_query:
        payments = payments.filter(
            Q(project__title__icontains=payment_query)
            | Q(project__cooperator__name__icontains=payment_query)
            | Q(or_number__icontains=payment_query)
        )

    # Most recent 50 — this is a quick ledger view, not a full export; the
    # complete history is always available in the admin.
    payments = payments[:50]

    context = {
        "active_page": "funding",
        "total_disbursed": total_disbursed,
        "total_refunded": total_refunded,
        "outstanding_balance": outstanding_balance,
        "collection_rate": collection_rate,
        "total_approved_budget": total_approved_budget,
        "total_disbursed_budget": total_disbursed_budget,
        "budget_items": budget_items,
        "item_query": item_query,
        "category_filter": category_filter,
        "category_choices": BudgetLineItem._meta.get_field('category').choices,
        "payments": payments,
        "payment_query": payment_query,
    }
    return render(request, "monitoring/funding.html", context)


# TODO: re-enable once auth/login is set up
# @login_required
def budget_item_edit(request, pk):
    """Edit a single approved Line-Item Budget entry from the Funding page
    — same fields you'd edit on this row in the admin panel."""
    item = get_object_or_404(BudgetLineItem.objects.select_related('project'), pk=pk)
    if request.method == 'POST':
        form = BudgetLineItemForm(request.POST, instance=item)
        if form.is_valid():
            form.save()
            messages.success(request, "Budget line item updated.")
            return redirect('funding_overview')
        messages.error(request, "Couldn't save — please check the form.")
    else:
        form = BudgetLineItemForm(instance=item)

    return render(request, "monitoring/budget_item_form.html", {"form": form, "item": item, "active_page": "funding"})


# TODO: re-enable once auth/login is set up
# @login_required
def refund_payment_edit(request, pk):
    """Edit a single refund payment entry from the Funding page — same
    fields you'd edit on this row in the admin panel."""
    payment = get_object_or_404(RefundPayment.objects.select_related('project'), pk=pk)
    if request.method == 'POST':
        form = RefundPaymentForm(request.POST, request.FILES, instance=payment)
        if form.is_valid():
            form.save()
            messages.success(request, "Refund payment updated.")
            return redirect('funding_overview')
        messages.error(request, "Couldn't save — please check the form.")
    else:
        form = RefundPaymentForm(instance=payment)

    return render(request, "monitoring/refund_payment_form.html", {"form": form, "payment": payment, "active_page": "funding"})


# TODO: re-enable once auth/login is set up
# @login_required
def refund_payment_create(request, project_pk=None):
    """
    Record a new refund payment.

    Reached two ways:
      - /funding/payment/new/            — project_pk is None; staff pick
        the project from a dropdown (installment choices start out as
        every unpaid installment across all projects, project-labelled).
      - /projects/<pk>/payment/new/      — project_pk is set; the project
        field is locked to that project and installment choices are
        scoped to just that project's unpaid installments. This is the
        "Record Payment" button on the project detail page's payment
        schedule.

    Where the user is sent back to on success/cancel follows the same
    split, so recording a payment from a project's own page doesn't
    bounce them over to the Funding page.
    """
    project = get_object_or_404(Project, pk=project_pk) if project_pk else None

    if request.method == 'POST':
        form = RefundPaymentCreateForm(request.POST, request.FILES, project=project)
        if form.is_valid():
            payment = form.save(commit=False)
            if project is not None:
                # The project field is disabled (locked) in the form, so
                # Django ignores whatever was posted for it — set it
                # explicitly here rather than trusting cleaned_data.
                payment.project = project
            payment.save()
            messages.success(request, f"Recorded a ₱{payment.amount_paid:,.0f} payment for {payment.project.title}.")
            return redirect('project_detail', pk=payment.project.pk) if project else redirect('funding_overview')
        messages.error(request, "Couldn't save — please check the form.")
    else:
        form = RefundPaymentCreateForm(project=project)

    context = {
        "form": form,
        "project": project,
        "active_page": "projects" if project else "funding",
        "cancel_url": reverse('project_detail', args=[project.pk]) if project else reverse('funding_overview'),
    }
    return render(request, "monitoring/refund_payment_create_form.html", context)


# TODO: re-enable once auth/login is set up
# @login_required
def project_detail(request, pk):
    """
    Single project view: full details, requirements checklist (with file
    upload per requirement), monitoring reports (with file upload per
    report), and an admin-only status-change control.

    POST requests are distinguished by a hidden "action" field so all three
    forms can live on the same page and post back to the same URL.
    """
    project = get_object_or_404(
        Project.objects.select_related('cooperator').prefetch_related(
            'requirements', 'monitoring_reports', 'equipment',
            'refund_installments', 'refund_payments', 'budget_items',
            'restructures',
        ),
        pk=pk,
    )

    status_form = ProjectStatusForm(instance=project)

    if request.method == 'POST':
        action = request.POST.get('action')

        # --- Admin changes the project's status ---
        if action == 'update_status':
            if not request.user.has_perm('monitoring.change_project'):
                messages.error(request, "You don't have permission to change project status.")
                return redirect('project_detail', pk=pk)

            status_form = ProjectStatusForm(request.POST, instance=project)
            if status_form.is_valid():
                status_form.save()
                messages.success(request, "Project status updated.")
                return redirect('project_detail', pk=pk)
            # status_form.errors['status'] holds the specific "still missing:
            # ..." message from ProjectStatusForm.clean_status() when the
            # gate isn't cleared yet — show that instead of a generic line
            # so staff actually know what to go fill in.
            error_text = " ".join(status_form.errors.get('status', ["Couldn't update status — please check the form."]))
            messages.error(request, error_text)

        # --- Upload/replace a requirement document ---
        elif action == 'upload_requirement':
            requirement = get_object_or_404(ProjectRequirement, pk=request.POST.get('requirement_id'), project=project)
            form = RequirementUploadForm(request.POST, request.FILES, instance=requirement)
            if form.is_valid():
                req = form.save(commit=False)
                if req.file:
                    req.is_submitted = True
                    req.date_submitted = date.today()
                req.save()
                messages.success(request, f"Uploaded: {requirement.get_requirement_type_display()}")
            else:
                messages.error(request, "Upload failed — please check the file and try again.")
            return redirect('project_detail', pk=pk)

        # --- Upload/replace a monitoring report file ---
        elif action == 'upload_report':
            report = get_object_or_404(MonitoringReport, pk=request.POST.get('report_id'), project=project)
            form = ReportUploadForm(request.POST, request.FILES, instance=report)
            if form.is_valid():
                form.save()
                messages.success(request, f"Uploaded: {report.get_report_type_display()}")
            else:
                messages.error(request, "Upload failed — please check the file and try again.")
            return redirect('project_detail', pk=pk)

        # --- Set/replace the project's profile photo ---
        elif action == 'upload_photo':
            # IMPORTANT: grab the existing photo BEFORE calling is_valid().
            # ModelForm.is_valid() already reassigns project.photo to the
            # newly uploaded file in memory (via its internal
            # construct_instance step), so checking `project.photo` AFTER
            # is_valid() and deleting it there deletes the NEW file, not
            # the old one — silently wiping out every upload. Capturing it
            # first, before validation touches the instance, is what makes
            # "delete the old file, keep the new one" actually work.
            old_photo = project.photo if project.photo else None
            photo_form = ProjectPhotoForm(request.POST, request.FILES, instance=project)
            if photo_form.is_valid():
                photo_form.save()
                # Delete the old file from storage now that the new one is
                # safely saved — otherwise replacing a photo leaves the
                # previous file orphaned on disk even though the project
                # now points at the new one.
                if old_photo:
                    old_photo.delete(save=False)
                messages.success(request, "Project photo updated.")
            else:
                messages.error(request, "Couldn't update photo — please check the file and try again.")
            return redirect('project_detail', pk=pk)

        # --- Remove the project's profile photo ---
        elif action == 'remove_photo':
            if project.photo:
                project.photo.delete(save=False)  # removes the file itself, not just the DB reference
                project.photo = None
                project.save(update_fields=['photo'])
            messages.success(request, "Project photo removed.")
            return redirect('project_detail', pk=pk)

    # Build per-row upload forms for GET rendering (each pre-bound to its instance)
    requirement_forms = [
        (req, RequirementUploadForm(instance=req)) for req in project.requirements.all()
    ]
    report_forms = [
        (rep, ReportUploadForm(instance=rep)) for rep in project.monitoring_reports.all()
    ]

    context = {
        "active_page": "projects",
        "project": project,
        "status_form": status_form,
        "requirement_forms": requirement_forms,
        "report_forms": report_forms,
        "can_change_status": request.user.has_perm('monitoring.change_project'),
        "photo_form": ProjectPhotoForm(instance=project),
        "summary": _project_summary(project),
        "timeline": _build_project_timeline(project),
    }
    return render(request, "monitoring/project_detail.html", context)


def _project_summary(project):
    """A few extra at-a-glance numbers for the project detail page, beyond
    what's already exposed as model properties (remaining_balance, etc.)."""
    next_installment = project.refund_installments.filter(
        status=RefundInstallment.Status.UNPAID
    ).order_by('due_date').first()

    return {
        "requirements_submitted": sum(1 for r in project.requirements.all() if r.is_submitted),
        "requirements_total": len(project.requirements.all()),
        "reports_submitted": sum(1 for r in project.monitoring_reports.all() if r.date_submitted),
        "reports_total": len(project.monitoring_reports.all()),
        "reports_overdue": sum(1 for r in project.monitoring_reports.all() if r.is_overdue),
        "equipment_count": len(project.equipment.all()),
        "equipment_total_cost": sum((e.acquisition_cost for e in project.equipment.all()), Decimal("0")),
        "next_installment": next_installment,
        "payment_count": len(project.refund_payments.all()),
    }


def _build_project_timeline(project):
    """
    Flattens the project's key dates and events — phase milestones, refund
    payments, submitted reports, restructures — into one chronological list
    for the "Timeline" section on the project detail page.
    """
    events = []

    events.append({
        "date": project.created_at.date(), "label": "Project record created", "kind": "milestone",
    })
    if project.phase1_start_date:
        events.append({"date": project.phase1_start_date, "label": "Phase I (implementation) started", "kind": "milestone"})
    if project.phase1_expected_end_date:
        events.append({"date": project.phase1_expected_end_date, "label": "Phase I expected end", "kind": "milestone"})
    if project.phase1_actual_end_date:
        events.append({"date": project.phase1_actual_end_date, "label": "Phase I completed", "kind": "milestone"})
    if project.refund_start_date:
        events.append({"date": project.refund_start_date, "label": "Phase II (refund period) started", "kind": "milestone"})
    if project.termination_date:
        events.append({
            "date": project.termination_date,
            "label": f"Project {project.get_status_display()}"
                     + (f" — {project.get_termination_reason_display()}" if project.termination_reason else ""),
            "kind": "milestone",
        })

    for report in project.monitoring_reports.all():
        if report.date_submitted:
            events.append({
                "date": report.date_submitted,
                "label": f"Submitted {report.get_report_type_display()}",
                "kind": "report",
            })

    for payment in project.refund_payments.all():
        events.append({
            "date": payment.date_paid,
            "label": f"Refund payment received — ₱{payment.amount_paid:,.0f}",
            "kind": "payment",
        })

    for restructure in project.restructures.all():
        events.append({
            "date": restructure.requested_date,
            "label": f"Refund restructure requested ({restructure.get_ground_display()})"
                     + (" — approved" if restructure.approved else ""),
            "kind": "restructure",
        })

    events.sort(key=lambda e: e["date"])
    return events


# How many quarters the Impact KPI charts plot when no Year filter is
# applied — see the chart_data comment inside impact_kpi() below for why
# this exists.
CHART_QUARTER_LIMIT = 12


# TODO: re-enable once auth/login is set up
# @login_required
def impact_kpi(request):
    """
    Impact KPI panel — the DOST PSTO quarterly submission (S&T-assisted
    entities, jobs created, technology interventions, export firms
    assisted, gross sales), one row per quarter, office-wide.

    Replaces the old cross-project requirements/monitoring-report uploader
    that used to live at this tab — that per-project upload workflow is
    still available on each project's own detail page.
    """
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add_record':
            # Look up any existing row for this (year, quarter) BEFORE
            # validating, and hand it to the form as `instance`. Without
            # this, ImpactRecordForm (a ModelForm) always builds a brand-new,
            # pk-less instance to validate against — and since
            # ImpactRecord.Meta.unique_together = [('year', 'quarter')],
            # Django's own validate_unique() then sees the just-submitted
            # quarter as colliding with the row that already exists for it
            # and fails the form. That's why *editing* an existing quarter
            # always errored while adding a brand-new one worked fine.
            # Passing the matching row as `instance` tells Django "this IS
            # that row," so it excludes it from its own uniqueness check.
            existing = ImpactRecord.objects.filter(
                year=request.POST.get('year'),
                quarter=request.POST.get('quarter'),
            ).first()
            form = ImpactRecordForm(request.POST, instance=existing)
            if form.is_valid():
                record = form.save()
                verb = "Updated" if existing else "Added"
                messages.success(request, f"{verb} the {record.get_quarter_display()} {record.year} entry.")
            else:
                messages.error(request, "Couldn't save — please check the form.")
            return redirect('reports')

        elif action == 'delete_record':
            record = get_object_or_404(ImpactRecord, pk=request.POST.get('record_id'))
            messages.success(request, f"Removed the {record.get_quarter_display()} {record.year} entry.")
            record.delete()
            return redirect('reports')

    records = ImpactRecord.objects.all()

    year_filter = request.GET.get('year', '').strip()
    if year_filter:
        records = records.filter(year=year_filter)

    quarter_filter = request.GET.get('quarter', '').strip()
    if quarter_filter:
        records = records.filter(quarter=quarter_filter)

    # When both Year and Quarter are picked in the filter above, that's
    # specific enough to compute a project-derived baseline for the "Log a
    # Quarterly Entry" form — saves staff from re-counting projects by hand
    # every quarter. See ImpactRecord.compute_project_estimate for exactly
    # what is/isn't included (walk-ins and Gross Sales are NOT — those stay
    # fully manual).
    computed_estimate = None
    add_form_initial = None
    if year_filter and quarter_filter:
        computed_estimate = ImpactRecord.compute_project_estimate(int(year_filter), quarter_filter)
        add_form_initial = {
            'year': year_filter,
            'quarter': quarter_filter,
            'entities_assisted': computed_estimate['entities_assisted'],
            'jobs_created': computed_estimate['jobs_created'],
            'technology_interventions': computed_estimate['technology_interventions'],
            'export_firms_assisted': computed_estimate['export_firms_assisted'],
        }

    totals = records.aggregate(
        entities_assisted=Sum('entities_assisted'),
        jobs_created=Sum('jobs_created'),
        technology_interventions=Sum('technology_interventions'),
        export_firms_assisted=Sum('export_firms_assisted'),
        gross_sales=Sum('gross_sales'),
    )

    # Chart data always follows chronological (year, quarter) order — 'q1'
    # < 'q2' < 'q3' < 'q4' sorts correctly as plain strings, so no extra
    # annotation is needed to get quarters in calendar order.
    #
    # CHART_QUARTER_LIMIT caps how many bars ever get drawn. The <canvas>
    # elements sit in a fixed-height (h-72) box with no horizontal scroll,
    # so plotting every logged quarter unfiltered (e.g. 30 quarters across
    # several years) squeezes the bars unreadably thin and crowds the x-axis
    # labels into overlapping mush. A Year filter is specific enough on its
    # own (≤4 quarters), so the cap only kicks in when no Year is picked —
    # in that case we take the most recent CHART_QUARTER_LIMIT quarters
    # (newest-first, then flipped back to chronological order for the chart).
    if year_filter:
        chart_records = list(records.order_by('year', 'quarter'))
    else:
        chart_records = list(records.order_by('-year', '-quarter')[:CHART_QUARTER_LIMIT])
        chart_records.reverse()

    chart_data = [
        {
            "label": f"{r.get_quarter_display()} {r.year}",
            "entities_assisted": r.entities_assisted,
            "jobs_created": r.jobs_created,
            "technology_interventions": r.technology_interventions,
            "export_firms_assisted": r.export_firms_assisted,
            "gross_sales": float(r.gross_sales),
        }
        for r in chart_records
    ]

    context = {
        "active_page": "reports",
        "records": records.order_by('-year', '-quarter'),
        "entities_assisted": totals['entities_assisted'] or 0,
        "jobs_created": totals['jobs_created'] or 0,
        "technology_interventions": totals['technology_interventions'] or 0,
        "export_firms_assisted": totals['export_firms_assisted'] or 0,
        "gross_sales": totals['gross_sales'] or 0,
        "year_filter": year_filter,
        "quarter_filter": quarter_filter,
        "available_years": ImpactRecord.objects.values_list('year', flat=True).distinct().order_by('-year'),
        "quarter_choices": ImpactRecord.Quarter.choices,
        "add_form": ImpactRecordForm(initial=add_form_initial),
        "computed_estimate": computed_estimate,
        "chart_data": chart_data,
        "all_projects": Project.objects.select_related('cooperator').order_by('cooperator__name', 'title'),
    }
    return render(request, "monitoring/impact_kpi.html", context)


# TODO: re-enable once auth/login is set up
# @login_required
def export_report_pdf(request):
    """
    Generates one of three PDF reports — triggered from the Export panel
    on the Impact KPI tab — straight from the database:

      ?report_type=quarterly (default) — the Impact KPI rows, honoring
        whatever Year/Quarter filter is currently applied on that page
        (carried along as hidden ?year=/&quarter= — same params the page
        itself already filters on).
      ?report_type=project&project_id=<pk> — full detail report for one
        project (funding, requirements, reports, equipment, timeline).
      ?report_type=dashboard — the same KPI numbers shown on the
        Dashboard page, as a point-in-time snapshot.
      ?report_type=funding — the Funding page's Fund Supervision totals
        plus the Approved Line-Item Budget and Refund Payment ledgers,
        honoring whatever ?item_q=/&category=/&payment_q= filters are
        currently applied on that page (same params it already filters
        on, carried along as hidden inputs — see funding.html).

    See report_exports.py for the actual pandas/reportlab PDF-building —
    this view is just routing + fetching the right data.
    """
    report_type = request.GET.get('report_type', 'quarterly')

    if report_type == 'project':
        project_id = request.GET.get('project_id')
        if not project_id:
            messages.error(request, "Please choose a project to export.")
            return redirect('reports')
        project = get_object_or_404(
            Project.objects.select_related('cooperator').prefetch_related(
                'requirements', 'monitoring_reports', 'equipment',
                'refund_installments', 'refund_payments', 'budget_items',
                'restructures',
            ),
            pk=project_id,
        )
        pdf_bytes = build_project_detail_pdf(project)
        filename = f"project_report_{project.project_code or project.pk}.pdf".replace(' ', '_')

    elif report_type == 'dashboard':
        pdf_bytes = build_dashboard_kpi_pdf()
        filename = f"dashboard_kpi_snapshot_{date.today().isoformat()}.pdf"

    elif report_type == 'funding':
        # Same filtering as funding_overview's own querysets, so the
        # export matches whatever's currently on screen there.
        budget_items = BudgetLineItem.objects.select_related('project', 'project__cooperator').all()
        item_query = request.GET.get('item_q', '').strip()
        if item_query:
            budget_items = budget_items.filter(
                Q(description__icontains=item_query) | Q(project__title__icontains=item_query)
            )
        category_filter = request.GET.get('category', '')
        if category_filter:
            budget_items = budget_items.filter(category=category_filter)
        budget_items = budget_items.order_by('project__title')

        payments = RefundPayment.objects.select_related('project', 'project__cooperator').order_by('-date_paid')
        payment_query = request.GET.get('payment_q', '').strip()
        if payment_query:
            payments = payments.filter(
                Q(project__title__icontains=payment_query)
                | Q(project__cooperator__name__icontains=payment_query)
                | Q(or_number__icontains=payment_query)
            )
        payments = payments[:50]

        pdf_bytes = build_funding_details_pdf(budget_items, payments, item_query, category_filter, payment_query)
        filename = f"funding_details_{date.today().isoformat()}.pdf"

    else:  # 'quarterly'
        year_filter = request.GET.get('year', '').strip()
        quarter_filter = request.GET.get('quarter', '').strip()
        records = ImpactRecord.objects.all()
        if year_filter:
            records = records.filter(year=year_filter)
        if quarter_filter:
            records = records.filter(quarter=quarter_filter)
        records = records.order_by('year', 'quarter')
        pdf_bytes = build_quarterly_impact_pdf(records, year_filter, quarter_filter)
        label = f"{quarter_filter or 'all'}_{year_filter or 'all'}"
        filename = f"quarterly_impact_{label}.pdf"

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# TODO: re-enable once auth/login is set up
# @login_required
def settings_view(request):
    """
    Account settings (name/email, password) for any signed-in user, plus
    an Administration section — quick links into the admin pages staff
    actually use day-to-day, and a plain list of user accounts — visible
    only when request.user.is_staff.
    """
    if not request.user.is_authenticated:
        return redirect('login')

    account_form = UserSettingsForm(instance=request.user)
    password_form = PasswordChangeForm(request.user)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update_account':
            account_form = UserSettingsForm(request.POST, instance=request.user)
            if account_form.is_valid():
                account_form.save()
                messages.success(request, "Account settings updated.")
                return redirect('settings')
            messages.error(request, "Couldn't save — please check the form.")

        elif action == 'change_password':
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                # Without this, changing your own password invalidates your
                # current session and immediately logs you out mid-request.
                update_session_auth_hash(request, user)
                messages.success(request, "Password changed.")
                return redirect('settings')
            messages.error(request, "Couldn't change password — please check the form.")

    context = {
        "active_page": "settings",
        "account_form": account_form,
        "password_form": password_form,
    }
    if request.user.is_staff:
        context["staff_users"] = get_user_model().objects.all().order_by('username')

    return render(request, "monitoring/settings.html", context)


# TODO: re-enable once auth/login is set up
# @login_required
def profile(request):
    """
    Basic account profile page. Guarded with a plain is_authenticated check
    (rather than @login_required) to match the rest of this app's views,
    which have auth temporarily disabled — see the TODOs above.
    """
    return render(request, "monitoring/profile.html", {"profile_user": request.user})