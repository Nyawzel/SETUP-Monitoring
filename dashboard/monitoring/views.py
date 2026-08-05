from decimal import Decimal, InvalidOperation
from datetime import date
from dateutil.relativedelta import relativedelta
from itertools import groupby

import openpyxl

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.paginator import Paginator
from django.db.models import (
    Sum, Count, Q, F, ExpressionWrapper, DecimalField,
    Case, When, Value, BooleanField,
)
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from .models import (
    Project, RefundPayment, RefundInstallment, BudgetLineItem,
    ProjectRequirement, MonitoringReport, Cooperator, ImpactRecord,
    ProjectQuarterlyImpact, Equipment,
)
from .forms import (
    ProjectForm, ProjectStatusForm, RequirementUploadForm, ReportUploadForm,
    ProjectPhotoForm, ProjectImpactDataForm, ExcelImportForm, CooperatorForm,
    BudgetLineItemForm, BudgetLineItemCreateForm, RefundPaymentForm, RefundPaymentCreateForm,
    UserSettingsForm, ImpactRecordForm, ProjectQuarterlyImpactForm, EquipmentCreateForm,
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

    # Delinquent = in Phase II AND has missed DELINQUENCY_THRESHOLD_MONTHS+
    # consecutive installments. Past Due = in Phase II with at least one
    # overdue unpaid installment, but not yet at the delinquent threshold.
    # Doing this in Python via the model properties keeps the logic in one
    # place (Project.is_refund_delinquent / is_refund_past_due) rather than
    # duplicating it as a query.
    phase2_projects = projects.filter(status=Project.Status.PHASE_2)
    delinquent_projects = [p for p in phase2_projects if p.is_refund_delinquent]
    past_due_projects = [p for p in phase2_projects if p.is_refund_past_due]
    delinquent_count = len(delinquent_projects)
    past_due_count = len(past_due_projects)

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
        "labels": ["Phase I", "Phase II", "Completed", "Pending", "Past Due", "Delinquent", "Terminated/Withdrawn"],
        "values": [
            phase1_count, phase2_count, completed_count,
            pending_count, past_due_count, delinquent_count, terminated_withdrawn_count,
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
    # Watchlist — top cooperators behind on refunds, past due AND fully
    # delinquent alike (delinquent ones first, since that sort key is
    # boolean-then-months). Sortable by column (same ?sort=/-sort
    # convention as project_list/funding_overview), but kept as a plain
    # Python sort of the already-materialized at_risk_projects list rather
    # than a queryset .order_by() — remaining_balance and
    # consecutive_missed_months are Python @properties on Project, not
    # database columns, so they can't be sorted at the DB level anyway.
    # Whatever column is currently sorted on determines which 8 projects
    # show (sort first, then take the top 8), not just how the same
    # fixed 8 are reordered.
    # ---------------------------------------------------------------
    at_risk_projects = delinquent_projects + past_due_projects
    WATCHLIST_SORT_KEYS = {
        "msme": lambda p: p.cooperator.name.lower(),
        "project": lambda p: p.title.lower(),
        "overdue": lambda p: p.consecutive_missed_months,
        "balance": lambda p: p.remaining_balance,
    }
    watchlist_sort = request.GET.get('watchlist_sort', '-balance')
    watchlist_sort_field = watchlist_sort[1:] if watchlist_sort.startswith('-') else watchlist_sort
    watchlist_reverse = watchlist_sort.startswith('-')
    watchlist_key = WATCHLIST_SORT_KEYS.get(watchlist_sort_field, WATCHLIST_SORT_KEYS['balance'])
    watchlist = sorted(at_risk_projects, key=watchlist_key, reverse=watchlist_reverse)[:8]

    context = {
        "active_page": "dashboard",
        # status row
        "total_projects": total_projects,
        "phase1_count": phase1_count,
        "phase2_count": phase2_count,
        "completed_count": completed_count,
        "pending_count": pending_count,
        "delinquent_count": delinquent_count,
        "past_due_count": past_due_count,
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
        "watchlist_sort": watchlist_sort,
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

    # Neither "past due" nor "delinquent" is a stored `status` value — both
    # are derived from how many consecutive overdue, still-unpaid
    # installments a project has (same rule as Project.consecutive_missed_
    # months / is_refund_past_due / is_refund_delinquent). Rather than loop
    # over every project in Python and call those properties per-row (one
    # query each), this annotates the queryset with a single count of
    # overdue+unpaid installments per project, then buckets that count into
    # the two severities at the DB level, so Django does it all in one SQL
    # query regardless of how many projects there are. Named *_annotated
    # (not is_refund_delinquent/is_refund_past_due) specifically to avoid
    # colliding with the same-named @property already defined on the
    # Project model — annotate() sets this as a plain instance attribute,
    # and Python won't let you assign over a read-only property under that
    # name.
    overdue_installments_count = Count(
        'refund_installments',
        filter=Q(
            refund_installments__due_date__lt=date.today(),
            refund_installments__status=RefundInstallment.Status.UNPAID,
        ),
        distinct=True,
    )
    projects = projects.annotate(overdue_installments_count=overdue_installments_count).annotate(
        is_past_due_annotated=Case(
            When(
                overdue_installments_count__gt=0,
                overdue_installments_count__lt=Project.DELINQUENCY_THRESHOLD_MONTHS,
                then=Value(True),
            ),
            default=Value(False), output_field=BooleanField(),
        ),
        is_delinquent_annotated=Case(
            When(overdue_installments_count__gte=Project.DELINQUENCY_THRESHOLD_MONTHS, then=Value(True)),
            default=Value(False), output_field=BooleanField(),
        ),
    )

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

    # "Delinquent only" still means the severe, >=DELINQUENCY_THRESHOLD_
    # MONTHS bucket specifically — "Past due only" is the separate, milder
    # bucket. Checking both boxes shows anything flagged either way.
    delinquent_filter = request.GET.get('delinquent', '') == '1'
    past_due_filter = request.GET.get('past_due', '') == '1'
    if delinquent_filter and past_due_filter:
        projects = projects.filter(Q(is_delinquent_annotated=True) | Q(is_past_due_annotated=True))
    elif delinquent_filter:
        projects = projects.filter(is_delinquent_annotated=True)
    elif past_due_filter:
        projects = projects.filter(is_past_due_annotated=True)

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
        "past_due_filter": past_due_filter,
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

    # Column sorting is done on the assembled `rows` list rather than the
    # queryset above — active_projects/outstanding_balance come from
    # Python @properties on Cooperator (active_projects,
    # total_outstanding_balance), not database columns, so there's
    # nothing for .order_by() to sort on for those two. Doing it here
    # keeps name/business_type/category on the same mechanism instead of
    # splitting sorting across two different approaches.
    MSME_SORT_KEYS = {
        "name": lambda r: r["msme"].name.lower(),
        "business_type": lambda r: r["msme"].get_business_type_display().lower(),
        "category": lambda r: (r["msme"].get_enterprise_category_display() or "").lower(),
        "projects": lambda r: r["total_projects"],
        "balance": lambda r: r["outstanding_balance"],
    }
    sort = request.GET.get('sort', 'name')
    sort_field = sort[1:] if sort.startswith('-') else sort
    sort_reverse = sort.startswith('-')
    sort_key = MSME_SORT_KEYS.get(sort_field, MSME_SORT_KEYS['name'])
    rows = sorted(rows, key=sort_key, reverse=sort_reverse)

    context = {
        "active_page": "msmes",
        "rows": rows,
        "query": query,
        "category_filter": category_filter,
        "category_choices": Cooperator.ENTERPRISE_CATEGORY_CHOICES,
        "sort": sort,
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

    impact_year, impact_quarter, current_year = _resolve_impact_quarter_params(request)
    msme_quarter_estimate = ImpactRecord.compute_project_estimate(
        impact_year, impact_quarter, projects_qs=projects,
    )

    context = {
        "active_page": "msmes",
        "msme": msme,
        "projects": projects,
        "total_outstanding_balance": msme.total_outstanding_balance,
        "msme_quarter_estimate": msme_quarter_estimate,
        "impact_year": impact_year,
        "impact_quarter": impact_quarter,
        "impact_quarter_choices": ImpactRecord.Quarter.choices,
        "impact_year_choices": range(current_year - 3, current_year + 1),
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

    # --- Upcoming / Overdue loan installments — the "is this MSME about to
    # miss a payment, or has it already" tracker. Both are unpaid
    # installments; the only difference is which side of today's date the
    # due_date falls on. Ordered oldest-due-first in both cases, since
    # that's "most urgent" either way (soonest upcoming, longest overdue).
    today = date.today()
    UPCOMING_WINDOW_DAYS = 30  # "upcoming" = due within the next 30 days

    upcoming_installments = RefundInstallment.objects.filter(
        status=RefundInstallment.Status.UNPAID,
        due_date__gte=today,
        due_date__lte=today + relativedelta(days=UPCOMING_WINDOW_DAYS),
    ).select_related('project', 'project__cooperator').order_by('project__title', 'due_date')

    overdue_installments = RefundInstallment.objects.filter(
        status=RefundInstallment.Status.UNPAID,
        due_date__lt=today,
    ).select_related('project', 'project__cooperator').order_by('project__title', 'due_date')

    upcoming_total = upcoming_installments.aggregate(total=Sum('amount_due'))['total'] or 0
    overdue_total = overdue_installments.aggregate(total=Sum('amount_due'))['total'] or 0

    # Grouped by project — the Funding page shows one row per project
    # ("Project X — 3 payments overdue"), expandable to the individual
    # installments, rather than one flat row per installment. groupby
    # needs its input pre-sorted by the grouping key, which the
    # order_by('project__title', ...) above already guarantees.
    def _group_by_project(installments_qs, sort_key):
        rows = []
        for project_id, group in groupby(installments_qs, key=lambda i: i.project_id):
            group_installments = list(group)
            rows.append({
                "project": group_installments[0].project,
                "installments": group_installments,
                "count": len(group_installments),
                "total_amount": sum(i.amount_due for i in group_installments),
                # Installments within each group are already ordered by
                # due_date (oldest/soonest first), so the first one is
                # always the most urgent — oldest overdue, or soonest due.
                "urgent_installment": group_installments[0],
            })
        rows.sort(key=sort_key)
        return rows

    overdue_by_project = _group_by_project(
        overdue_installments, sort_key=lambda row: row["urgent_installment"].due_date,
    )
    upcoming_by_project = _group_by_project(
        upcoming_installments, sort_key=lambda row: row["urgent_installment"].due_date,
    )

    # --- Approved Line-Item Budget, across all projects ---
    BUDGET_ITEM_SORT_FIELDS = {
        "project": "project__title", "-project": "-project__title",
        "msme": "project__cooperator__name", "-msme": "-project__cooperator__name",
        "description": "description", "-description": "-description",
        "category": "category", "-category": "-category",
        "approved": "approved_amount", "-approved": "-approved_amount",
        "disbursed": "disbursed_amount", "-disbursed": "-disbursed_amount",
        # unexpended_balance is a Python @property on the model (approved -
        # disbursed), not a real column, so it can't be sorted directly —
        # this annotates the same subtraction as a DB-level expression
        # under a different name so ordering can happen in SQL instead of
        # pulling every row into Python to sort. Display still uses the
        # property as before; this annotation exists purely for ordering.
        "unexpended": "unexpended_calc", "-unexpended": "-unexpended_calc",
    }
    budget_items = BudgetLineItem.objects.select_related('project', 'project__cooperator').annotate(
        unexpended_calc=ExpressionWrapper(
            F('approved_amount') - F('disbursed_amount'),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        )
    )

    item_query = request.GET.get('item_q', '').strip()
    if item_query:
        budget_items = budget_items.filter(
            Q(description__icontains=item_query) | Q(project__title__icontains=item_query)
        )

    category_filter = request.GET.get('category', '')
    if category_filter:
        budget_items = budget_items.filter(category=category_filter)

    item_sort = request.GET.get('item_sort', 'project')
    budget_items = budget_items.order_by(BUDGET_ITEM_SORT_FIELDS.get(item_sort, 'project__title'))

    # --- Refund payment ledger ---
    PAYMENT_SORT_FIELDS = {
        "date": "date_paid", "-date": "-date_paid",
        "amount": "amount_paid", "-amount": "-amount_paid",
        "project": "project__title", "-project": "-project__title",
        "msme": "project__cooperator__name", "-msme": "-project__cooperator__name",
        "or_number": "or_number", "-or_number": "-or_number",
    }
    payments = RefundPayment.objects.select_related('project', 'project__cooperator')

    payment_query = request.GET.get('payment_q', '').strip()
    if payment_query:
        payments = payments.filter(
            Q(project__title__icontains=payment_query)
            | Q(project__cooperator__name__icontains=payment_query)
            | Q(or_number__icontains=payment_query)
        )

    # Date range — either end optional, so "From" alone means "on or after",
    # "To" alone means "on or before". Guarded against malformed manual URL
    # edits (a normal <input type="date"> always submits YYYY-MM-DD, but a
    # hand-edited query string might not) so a bad value is just ignored
    # rather than 500ing the whole page.
    payment_date_from = request.GET.get('payment_date_from', '').strip()
    if payment_date_from:
        try:
            payments = payments.filter(date_paid__gte=date.fromisoformat(payment_date_from))
        except ValueError:
            payment_date_from = ''

    payment_date_to = request.GET.get('payment_date_to', '').strip()
    if payment_date_to:
        try:
            payments = payments.filter(date_paid__lte=date.fromisoformat(payment_date_to))
        except ValueError:
            payment_date_to = ''

    payment_sort = request.GET.get('payment_sort', '-date')
    payments = payments.order_by(PAYMENT_SORT_FIELDS.get(payment_sort, '-date_paid'))

    # Paginated rather than capped at a fixed recent-N — sorting ascending
    # by date (oldest first) plus paging is how staff reach older years;
    # nothing is hidden, it's just spread across pages instead of one
    # unbounded table that gets slower to load the longer this office runs.
    paginator = Paginator(payments, 25)
    payments_page = paginator.get_page(request.GET.get('payment_page', 1))

    context = {
        "active_page": "funding",
        "total_disbursed": total_disbursed,
        "total_refunded": total_refunded,
        "outstanding_balance": outstanding_balance,
        "collection_rate": collection_rate,
        "total_approved_budget": total_approved_budget,
        "total_disbursed_budget": total_disbursed_budget,
        "upcoming_by_project": upcoming_by_project,
        "upcoming_installments_count": sum(row["count"] for row in upcoming_by_project),
        "upcoming_total": upcoming_total,
        "upcoming_window_days": UPCOMING_WINDOW_DAYS,
        "overdue_by_project": overdue_by_project,
        "overdue_installments_count": sum(row["count"] for row in overdue_by_project),
        "overdue_total": overdue_total,
        "budget_items": budget_items,
        "item_query": item_query,
        "category_filter": category_filter,
        "item_sort": item_sort,
        "category_choices": BudgetLineItem._meta.get_field('category').choices,
        "payments": payments_page,
        "payment_query": payment_query,
        "payment_sort": payment_sort,
        "payment_date_from": payment_date_from,
        "payment_date_to": payment_date_to,
    }
    return render(request, "monitoring/funding.html", context)


# TODO: re-enable once auth/login is set up
# @login_required
def budget_item_create(request, project_pk=None):
    """
    Add a new Line-Item Budget entry.

    Reached two ways, same split as refund_payment_create:
      - /funding/budget-item/new/          — project_pk is None; staff pick
        the project from a dropdown.
      - /projects/<pk>/budget-item/new/    — project_pk is set; the project
        field is locked. This is the "+ Add Budget Item" button on the
        project detail page's Line-Item Budget section.

    Where the user is sent back to on success/cancel follows the same
    split, so adding a budget item from a project's own page doesn't
    bounce them over to the Funding page.
    """
    project = get_object_or_404(Project, pk=project_pk) if project_pk else None

    if request.method == 'POST':
        form = BudgetLineItemCreateForm(request.POST, project=project)
        if form.is_valid():
            item = form.save(commit=False)
            if project is not None:
                # The project field is disabled (locked) in the form, so
                # Django ignores whatever was posted for it — set it
                # explicitly here rather than trusting cleaned_data.
                item.project = project
            item.save()
            messages.success(request, f"Added budget line item: {item.description}")
            return redirect('project_detail', pk=item.project.pk) if project else redirect('funding_overview')
        messages.error(request, "Couldn't save — please check the form.")
    else:
        form = BudgetLineItemCreateForm(project=project)

    context = {
        "form": form,
        "project": project,
        "active_page": "projects" if project else "funding",
        "cancel_url": reverse('project_detail', args=[project.pk]) if project else reverse('funding_overview'),
    }
    return render(request, "monitoring/budget_item_create_form.html", context)


# TODO: re-enable once auth/login is set up
# @login_required
def equipment_create(request, project_pk):
    """
    Add a new Equipment record — the "+ Add Equipment" button on the
    project detail page's Equipment section. Unlike budget items/refund
    payments, equipment has no office-wide "any project" entry point (no
    equipment list page to add one from), so project_pk is always
    required and the project field is always locked.
    """
    project = get_object_or_404(Project, pk=project_pk)

    if request.method == 'POST':
        form = EquipmentCreateForm(request.POST, project=project)
        if form.is_valid():
            item = form.save(commit=False)
            # The project field is disabled (locked) in the form, so
            # Django ignores whatever was posted for it — set it
            # explicitly here rather than trusting cleaned_data.
            item.project = project
            item.save()
            messages.success(request, f"Added equipment: {item.name}")
            return redirect('project_detail', pk=project.pk)
        messages.error(request, "Couldn't save — please check the form.")
    else:
        form = EquipmentCreateForm(project=project)

    context = {
        "form": form,
        "project": project,
        "active_page": "projects",
        "cancel_url": reverse('project_detail', args=[project.pk]),
    }
    return render(request, "monitoring/equipment_create_form.html", context)


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
            'restructures', 'quarterly_impacts',
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

        # --- Remove a requirement's uploaded file. Also reverts is_submitted/
        # date_submitted — with the file gone there's no evidence left, so
        # this puts the row back to "not yet submitted" rather than leaving
        # it checked off with nothing behind it. Undoes what upload_requirement_
        # async (or the older upload_requirement) did; picking a new file and
        # hitting "Upload All" again handles replacement, so no separate
        # "edit" action is needed alongside this one.
        elif action == 'delete_requirement_file':
            requirement = get_object_or_404(ProjectRequirement, pk=request.POST.get('requirement_id'), project=project)
            if requirement.file:
                requirement.file.delete(save=False)
                requirement.file = None
            requirement.is_submitted = False
            requirement.date_submitted = None
            requirement.save(update_fields=['file', 'is_submitted', 'date_submitted'])
            messages.success(request, f"Removed file for: {requirement.get_requirement_type_display()}")
            return redirect('project_detail', pk=pk)

        # --- Remove a monitoring report's uploaded file. Also clears
        # date_submitted, the same reasoning as delete_requirement_file above
        # — no file on hand means it goes back to not-submitted (and shows up
        # as overdue again if its due date has passed). Replacing a report's
        # file is already handled by re-submitting the existing upload_report
        # form with a new file, so there's no separate "edit" action here either.
        elif action == 'delete_report_file':
            report = get_object_or_404(MonitoringReport, pk=request.POST.get('report_id'), project=project)
            if report.file:
                report.file.delete(save=False)
                report.file = None
            report.date_submitted = None
            report.save(update_fields=['file', 'date_submitted'])
            messages.success(request, f"Removed file for: {report.get_report_type_display()}")
            return redirect('project_detail', pk=pk)

        # --- Async, per-row upload — this is what the "Upload All" button's
        # JS (bottom of project_detail.html) actually calls, once per staged
        # file. Same validation/save as upload_requirement above, just JSON
        # instead of a redirect, and explicitly targeted by requirement_id
        # rather than "whichever requirement is next" (see bulk_upload_next
        # below, which is a different, older design and isn't called from
        # this template anymore).
        elif action == 'upload_requirement_async':
            requirement_id = request.POST.get('requirement_id')
            uploaded_file = request.FILES.get('file')
            if not uploaded_file:
                return JsonResponse({'ok': False, 'error': 'No file received.'}, status=400)

            requirement = get_object_or_404(ProjectRequirement, pk=requirement_id, project=project)
            # The per-row staging UI only collects a file, not remarks — bind
            # remarks to its current value rather than request.POST, since an
            # absent 'remarks' key on a bound form would otherwise blank out
            # whatever was already there.
            form = RequirementUploadForm(
                {'remarks': requirement.remarks}, {'file': uploaded_file}, instance=requirement,
            )
            if not form.is_valid():
                error_text = " ".join(form.errors.get('file', ["Invalid file — please try again."]))
                return JsonResponse({'ok': False, 'error': error_text}, status=400)

            req = form.save(commit=False)
            req.is_submitted = True
            req.date_submitted = date.today()
            req.save()

            return JsonResponse({
                'ok': True,
                'requirement_id': req.pk,
                'requirement_label': req.get_requirement_type_display(),
            })

        # --- Bulk upload: multiple files at once, matched IN ORDER to this
        # project's still-unsubmitted requirements (ordered by phase, then
        # requirement type — a stable, predictable order so staff can
        # predict the matching by selecting/dropping files in that same
        # sequence). Any extra files past the last open requirement are
        # skipped, not silently discarded without saying so.
        elif action == 'bulk_upload_requirements':
            files = request.FILES.getlist('files')
            if not files:
                messages.error(request, "No files were selected.")
                return redirect('project_detail', pk=pk)

            open_requirements = list(
                project.requirements.filter(is_submitted=False).order_by('phase', 'requirement_type')
            )
            if not open_requirements:
                messages.error(request, "Every requirement for this project is already submitted — nothing to match these files to.")
                return redirect('project_detail', pk=pk)

            matched = 0
            for uploaded_file, requirement in zip(files, open_requirements):
                requirement.file = uploaded_file
                requirement.is_submitted = True
                requirement.date_submitted = date.today()
                requirement.save()
                matched += 1

            leftover = len(files) - matched
            if leftover > 0:
                messages.success(
                    request,
                    f"Matched {matched} file(s) to open requirements. {leftover} extra "
                    f"file(s) had no open requirement left and were skipped."
                )
            else:
                messages.success(request, f"Matched {matched} file(s) to open requirements.")
            return redirect('project_detail', pk=pk)

        # --- Queue-based bulk upload: ONE file per request instead of the
        # whole batch in a single multipart POST (see bulk_upload_requirements
        # above, which this doesn't replace — it's kept as a no-JS fallback,
        # since the <form>'s default action is still bulk_upload_requirements).
        # This is what the bulk-upload-form's JS actually calls: it fires one
        # of these per staged file, in sequence, so a big batch shows live
        # per-file progress instead of living or dying as one all-or-nothing
        # request, and one bad file doesn't sink the rest of the batch. It
        # always re-queries "whatever requirement is still open right now"
        # fresh on each call rather than trusting a client-computed mapping,
        # so it self-corrects even if something else changed requirement
        # state mid-batch. Returns JSON — the page never reloads mid-queue.
        elif action == 'bulk_upload_next':
            uploaded_file = request.FILES.get('file')
            if not uploaded_file:
                return JsonResponse({'ok': False, 'error': 'No file received.'}, status=400)

            requirement = project.requirements.filter(
                is_submitted=False
            ).order_by('phase', 'requirement_type').first()
            if not requirement:
                return JsonResponse(
                    {'ok': False, 'error': 'No open requirements left to match this file to.'},
                    status=409,
                )

            requirement.file = uploaded_file
            requirement.is_submitted = True
            requirement.date_submitted = date.today()
            requirement.save()

            return JsonResponse({
                'ok': True,
                'requirement_id': requirement.pk,
                'requirement_label': requirement.get_requirement_type_display(),
            })

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

        # --- Update this project's Impact KPI contribution (jobs created /
        # gross sales) — these feed ImpactRecord.compute_project_estimate
        # on the Impact KPI page, and are typically 0/unset until Phase I
        # implementation is actually done, so they need to be editable
        # here rather than only at project-creation time. ---
        elif action == 'update_impact_data':
            impact_form = ProjectImpactDataForm(request.POST, instance=project)
            if impact_form.is_valid():
                impact_form.save()
                messages.success(request, "Impact data updated.")
            else:
                messages.error(request, "Couldn't update — please check the values.")
            return redirect('project_detail', pk=pk)

        # --- Log THIS project's Jobs Created / Gross Sales for one
        # specific quarter (ProjectQuarterlyImpact) — the per-quarter
        # increment that ImpactRecord.compute_project_estimate now sums,
        # as opposed to update_impact_data above (a single lifetime
        # figure). Upsert-by-(project, year, quarter), same
        # look-up-the-existing-row-first pattern impact_kpi's add_record
        # uses, and for the same reason: a ModelForm with no instance
        # would otherwise fail Django's own unique_together validation
        # the moment staff re-submit a correction for a quarter that
        # already has an entry. ---
        elif action == 'log_quarterly_impact':
            existing_entry = ProjectQuarterlyImpact.objects.filter(
                project=project,
                year=request.POST.get('year'),
                quarter=request.POST.get('quarter'),
            ).first()
            quarterly_form = ProjectQuarterlyImpactForm(request.POST, instance=existing_entry)
            if quarterly_form.is_valid():
                entry = quarterly_form.save(commit=False)
                entry.project = project
                entry.save()
                verb = "Updated" if existing_entry else "Logged"
                messages.success(request, f"{verb} {entry.get_quarter_display()} {entry.year} impact for {project.title}.")
            else:
                messages.error(request, "Couldn't save — please check the values.")
            return redirect('project_detail', pk=pk)

    # Build per-row upload forms for GET rendering, grouped by phase (Phase I
    # / Phase II / Completed) so the template can show each gate's checklist
    # as its own labeled section instead of one flat undifferentiated list.
    phase_order = [Project.Status.PHASE_1, Project.Status.PHASE_2, Project.Status.COMPLETED]
    phase_labels = dict(Project.Status.choices)
    requirement_groups = []
    for phase in phase_order:
        phase_reqs = project.requirements.filter(phase=phase)
        if not phase_reqs.exists():
            continue
        forms = [(req, RequirementUploadForm(instance=req)) for req in phase_reqs]
        total = len(forms)
        submitted = sum(1 for req, _ in forms if req.is_submitted)
        requirement_groups.append({
            "phase": phase,
            "label": phase_labels.get(phase, phase),
            "forms": forms,
            "percent": round((submitted / total) * 100) if total else 0,
        })

    report_forms = [
        (rep, ReportUploadForm(instance=rep)) for rep in project.monitoring_reports.all()
    ]

    impact_year, impact_quarter, current_year = _resolve_impact_quarter_params(request)

    # Pre-fill the "Log Quarterly Impact" form with whatever's already
    # logged for the currently-selected quarter, so re-opening the form to
    # correct a figure shows the existing value instead of a blank 0.
    existing_quarterly_entry = project.quarterly_impacts.filter(year=impact_year, quarter=impact_quarter).first()
    quarterly_impact_form = ProjectQuarterlyImpactForm(
        instance=existing_quarterly_entry,
        initial=None if existing_quarterly_entry else {'year': impact_year, 'quarter': impact_quarter},
    )

    context = {
        "active_page": "projects",
        "project": project,
        "status_form": status_form,
        "requirement_groups": requirement_groups,
        "report_forms": report_forms,
        "can_change_status": request.user.has_perm('monitoring.change_project'),
        "photo_form": ProjectPhotoForm(instance=project),
        "impact_form": ProjectImpactDataForm(instance=project),
        "summary": _project_summary(project),
        "timeline": _build_project_timeline(project),
        "quarterly_impact_form": quarterly_impact_form,
        "quarterly_impact_entries": project.quarterly_impacts.all(),
    }
    return render(request, "monitoring/project_detail.html", context)


def _resolve_impact_quarter_params(request):
    """
    Reads ?impact_year=&impact_quarter= off the request (defaulting to the
    current quarter, and falling back to it on anything malformed), for the
    read-only "Auto-computed Impact KPI Contribution" box on the Project and
    MSME detail pages. Returns (year:int, quarter:str, current_year:int) —
    current_year is handed back too so callers can build a year-choices
    range off it without recomputing today's date themselves.
    """
    current_year, current_quarter = ImpactRecord.current_year_quarter()

    try:
        impact_year = int(request.GET.get('impact_year', current_year))
    except (TypeError, ValueError):
        impact_year = current_year

    impact_quarter = request.GET.get('impact_quarter', current_quarter)
    if impact_quarter not in dict(ImpactRecord.Quarter.choices):
        impact_quarter = current_quarter

    return impact_year, impact_quarter, current_year


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


def _recent_quarters(end_year, end_quarter, count):
    """The `count` (year, quarter) pairs ending at and including
    (end_year, end_quarter), oldest first — used to build the "Computed
    from Website Data" chart's x-axis the same way chart_data's own
    CHART_QUARTER_LIMIT windowing does, just without depending on any
    ImpactRecord rows existing for those quarters."""
    order = ['q1', 'q2', 'q3', 'q4']
    year, q_idx = end_year, order.index(end_quarter)
    quarters = []
    for _ in range(count):
        quarters.append((year, order[q_idx]))
        q_idx -= 1
        if q_idx < 0:
            q_idx = 3
            year -= 1
    quarters.reverse()
    return quarters


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

        # --- Inline edit of one project's Jobs Created / Gross Sales from
        # the Project & MSME Indicators table below — same form/fields as
        # the "Impact KPI Contribution" box on the project's own detail
        # page, just reachable without leaving this page. ---
        elif action == 'update_project_indicators':
            project = get_object_or_404(Project, pk=request.POST.get('project_id'))
            indicator_form = ProjectImpactDataForm(request.POST, instance=project)
            if indicator_form.is_valid():
                indicator_form.save()
                messages.success(request, f"Updated indicators for {project.title}.")
            else:
                messages.error(request, f"Couldn't update {project.title} — please check the values.")
            return redirect('reports')

        # --- Inline toggle of one MSME's Export Firm flag from the same
        # table — a single checkbox, submitted on change (no separate Save
        # button), same pattern as the project photo auto-submit above. ---
        elif action == 'update_msme_export':
            msme = get_object_or_404(Cooperator, pk=request.POST.get('msme_id'))
            msme.is_export_firm = request.POST.get('is_export_firm') == 'on'
            msme.save(update_fields=['is_export_firm'])
            messages.success(request, f"Updated export status for {msme.name}.")
            return redirect('reports')

    records = ImpactRecord.objects.all()

    year_filter = request.GET.get('year', '').strip()
    if year_filter:
        records = records.filter(year=year_filter)

    quarter_filter = request.GET.get('quarter', '').strip()
    if quarter_filter:
        records = records.filter(quarter=quarter_filter)

    # Project & MSME Indicators table — the underlying per-project/per-MSME
    # data that compute_project_estimate reads from, editable right on this
    # page instead of requiring a trip to each project's/MSME's own detail
    # page. Same optional search box narrows both tables at once.
    indicator_q = request.GET.get('indicator_q', '').strip()

    indicator_projects = Project.objects.select_related('cooperator').order_by('cooperator__name', 'title')
    indicator_msmes = Cooperator.objects.order_by('name')
    if indicator_q:
        indicator_projects = indicator_projects.filter(
            Q(title__icontains=indicator_q) | Q(cooperator__name__icontains=indicator_q)
        )
        indicator_msmes = indicator_msmes.filter(name__icontains=indicator_q)

    project_indicator_rows = [(p, ProjectImpactDataForm(instance=p)) for p in indicator_projects]

    # When both Year and Quarter are picked in the filter above, that's
    # specific enough to compute a project-derived baseline for the "Log a
    # Quarterly Entry" form — saves staff from re-counting projects by hand
    # every quarter. See ImpactRecord.compute_project_estimate for exactly
    # what is/isn't included (walk-ins are NOT — those stay fully manual;
    # Gross Sales IS included now, sourced from ProjectQuarterlyImpact
    # entries logged for this exact quarter — see compute_project_estimate).
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
            'gross_sales': computed_estimate['gross_sales'],
        }

    totals = records.aggregate(
        entities_assisted=Sum('entities_assisted'),
        jobs_created=Sum('jobs_created'),
        technology_interventions=Sum('technology_interventions'),
        export_firms_assisted=Sum('export_firms_assisted'),
        gross_sales=Sum('gross_sales'),
    )

    current_year, current_quarter = ImpactRecord.current_year_quarter()

    # --- One shared quarter window drives BOTH the Actual and Computed
    # series — this is what lets them be overlaid on the same two charts
    # (solid = Actual, dashed = Computed) instead of four separate ones,
    # and it's also what makes a quarter with nothing logged show up as a
    # visible gap (0 on the Actual line) rather than just not existing on
    # the x-axis. Same windowing rule as before: Year+Quarter -> just that
    # quarter; Year only -> that year's 4 quarters; no filter -> the most
    # recent CHART_QUARTER_LIMIT calendar quarters ending at the current
    # one. See the CHART_QUARTER_LIMIT comment above for why the cap
    # exists at all.
    if year_filter and quarter_filter:
        chart_quarters = [(int(year_filter), quarter_filter)]
    elif year_filter:
        chart_quarters = [(int(year_filter), q) for q, _ in ImpactRecord.Quarter.choices]
    else:
        chart_quarters = _recent_quarters(current_year, current_quarter, CHART_QUARTER_LIMIT)

    quarter_labels = dict(ImpactRecord.Quarter.choices)

    # One query for every quarter in the window, instead of one per quarter.
    actual_lookup = Q()
    for cy, cq in chart_quarters:
        actual_lookup |= Q(year=cy, quarter=cq)
    actual_by_quarter = {
        (r.year, r.quarter): r
        for r in (ImpactRecord.objects.filter(actual_lookup) if chart_quarters else ImpactRecord.objects.none())
    }

    chart_data = []
    computed_chart_data = []
    for cy, cq in chart_quarters:
        label = f"{quarter_labels[cq]} {cy}"
        actual_record = actual_by_quarter.get((cy, cq))
        chart_data.append({
            "label": label,
            "entities_assisted": actual_record.entities_assisted if actual_record else 0,
            "jobs_created": actual_record.jobs_created if actual_record else 0,
            "technology_interventions": actual_record.technology_interventions if actual_record else 0,
            "export_firms_assisted": actual_record.export_firms_assisted if actual_record else 0,
            "gross_sales": float(actual_record.gross_sales) if actual_record else 0.0,
            "logged": actual_record is not None,
        })

        est = ImpactRecord.compute_project_estimate(cy, cq)
        computed_chart_data.append({
            "label": label,
            "entities_assisted": est['entities_assisted'],
            "jobs_created": est['jobs_created'],
            "technology_interventions": est['technology_interventions'],
            "export_firms_assisted": est['export_firms_assisted'],
            "gross_sales": float(est['gross_sales']),
        })

    computed_totals = {
        "entities_assisted": sum(r['entities_assisted'] for r in computed_chart_data),
        "jobs_created": sum(r['jobs_created'] for r in computed_chart_data),
        "technology_interventions": sum(r['technology_interventions'] for r in computed_chart_data),
        "export_firms_assisted": sum(r['export_firms_assisted'] for r in computed_chart_data),
        "gross_sales": sum(r['gross_sales'] for r in computed_chart_data),
    }

    # --- Actual-vs-Computed comparison, scoped to the exact same window as
    # the charts (NOT the headline totals cards above, which can cover a
    # wider or narrower range depending on the filter — comparing those
    # against computed_totals would be apples-to-oranges). A negative delta
    # (Actual below Computed) is the case worth a second look: Computed is
    # a floor built only from what the site already knows, so Actual
    # dropping under it usually means a quarter hasn't been logged yet
    # rather than a real shortfall.
    comparison_totals = {
        "entities_assisted": sum(r['entities_assisted'] for r in chart_data),
        "jobs_created": sum(r['jobs_created'] for r in chart_data),
        "technology_interventions": sum(r['technology_interventions'] for r in chart_data),
        "export_firms_assisted": sum(r['export_firms_assisted'] for r in chart_data),
        "gross_sales": sum(r['gross_sales'] for r in chart_data),
    }
    kpi_deltas = {
        key: comparison_totals[key] - computed_totals[key] for key in comparison_totals
    }
    has_actual_for_comparison = any(r['logged'] for r in chart_data)

    # --- "X of Y quarters logged" — counts logged quarters within the same
    # window the charts show, so it's always consistent with what's on
    # screen (narrows to the filtered range when a filter's applied).
    logged_quarter_count = sum(1 for r in chart_data if r['logged'])
    expected_quarter_count = len(chart_data)

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
        "computed_chart_data": computed_chart_data,
        "computed_totals": computed_totals,
        "kpi_deltas": kpi_deltas,
        "has_actual_for_comparison": has_actual_for_comparison,
        "current_year": current_year,
        "current_quarter": current_quarter,
        "logged_quarter_count": logged_quarter_count,
        "expected_quarter_count": expected_quarter_count,
        "all_projects": Project.objects.select_related('cooperator').order_by('cooperator__name', 'title'),
        "project_indicator_rows": project_indicator_rows,
        "msme_indicator_rows": indicator_msmes,
        "indicator_q": indicator_q,
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