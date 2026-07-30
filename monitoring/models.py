"""
monitoring/models.py

Models built around DOST AO No. 008 s.2024 (SETUP Guidelines Rev. 3.0),
specifically Part II (iFund) and Part III (Program Implementation and
Management) — i.e. the funding/loan supervision side of the program.

Core idea: a Project has two phases.
  Phase I  = implementation (equipment acquired/fabricated, trainings, etc.)
  Phase II = refund period (the "loan" — cooperator pays back the iFund)

Everything here is built so an admin employee can, at a glance, tell:
  - how much was disbursed
  - how much has been refunded vs. is still owed
  - whether a cooperator is behind on payments
  - what stage a project is in and what's overdue (reports, refunds, extensions)
"""

import calendar
from datetime import date
from decimal import Decimal
from django.db import models
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator
from dateutil.relativedelta import relativedelta


def _current_year():
    return timezone.now().year


# ---------------------------------------------------------------------------
# Cooperator (the MSME / industry association receiving assistance)
# ---------------------------------------------------------------------------

class Cooperator(models.Model):
    """The enterprise, industry association, or entity availing SETUP assistance."""

    BUSINESS_TYPE_CHOICES = [
        ('sole_proprietorship', 'Sole Proprietorship'),
        ('partnership', 'Partnership'),
        ('corporation', 'Corporation'),
        ('cooperative', 'Cooperative'),
        ('industry_association', 'Industry Association'),
        ('lgu', 'Local Government Unit'),
        ('suc', 'State University/College'),
        ('other', 'Other'),
    ]

    ENTERPRISE_CATEGORY_CHOICES = [
        ('micro', 'Micro'),
        ('small', 'Small'),
        ('medium', 'Medium'),
    ]

    DEVELOPMENT_STAGE_CHOICES = [
        ('developing', 'Developing Enterprise'),
        ('growing', 'Growing Enterprise'),
        ('expanding', 'Expanding and Innovating Enterprise'),
    ]

    name = models.CharField(max_length=255)
    business_type = models.CharField(max_length=30, choices=BUSINESS_TYPE_CHOICES)
    enterprise_category = models.CharField(max_length=10, choices=ENTERPRISE_CATEGORY_CHOICES, blank=True)
    development_stage = models.CharField(max_length=15, choices=DEVELOPMENT_STAGE_CHOICES, blank=True)

    priority_sector = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=255, blank=True)
    contact_person = models.CharField(max_length=255, blank=True)
    contact_number = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)

    # Registration / eligibility documentation
    dti_sec_cda_registration_no = models.CharField(
        "DTI/SEC/CDA Registration No.", max_length=100, blank=True
    )
    filipino_ownership_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("100.00"),
        help_text="Must be at least 60% per eligibility requirements."
    )

    # Eligibility flag — AO requires "no previous accountabilities with DOST-RO"
    has_outstanding_accountabilities = models.BooleanField(default=False)

    # Feeds the Impact KPI "export firms assisted" estimate (see
    # ImpactRecord.compute_project_estimate below) — set this once per
    # MSME, not per quarter, since whether a firm exports doesn't reset
    # every reporting period.
    is_export_firm = models.BooleanField(
        "Export Firm", default=False,
        help_text="Whether this MSME is engaged in exporting.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Model/table/field names stay "Cooperator" under the hood (renaming
        # the Python identifier would mean a much bigger migration touching
        # every FK across the app) — this is what actually changes the label
        # everywhere it's displayed: admin site, admin URLs section header,
        # and any {{ form.field.label }} rendering in templates.
        verbose_name = "MSME"
        verbose_name_plural = "MSMEs"

    def __str__(self):
        return self.name

    @property
    def is_eligible_for_new_ifund(self):
        """Quick eligibility check per Part II, Section 3(c)."""
        return (
            not self.has_outstanding_accountabilities
            and self.filipino_ownership_percent >= Decimal("60.00")
        )

    @property
    def active_projects(self):
        return self.projects.filter(
            status__in=[Project.Status.PHASE_1, Project.Status.PHASE_2]
        )

    @property
    def total_outstanding_balance(self):
        """Sum of remaining balances across all this cooperator's projects."""
        return sum((p.remaining_balance for p in self.projects.all()), Decimal("0"))


# ---------------------------------------------------------------------------
# Project — the central model
# ---------------------------------------------------------------------------

class Project(models.Model):
    """A single SETUP/iFund-assisted project for a cooperator."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending Approval'
        PHASE_1 = 'phase_1', 'Phase I - Implementation'
        PHASE_2 = 'phase_2', 'Phase II - Refund Period'
        COMPLETED = 'completed', 'Completed'
        TERMINATED = 'terminated', 'Terminated'
        WITHDRAWN = 'withdrawn', 'Withdrawn'

    class TerminationReason(models.TextChoices):
        FORCE_MAJEURE = 'force_majeure', 'Force Majeure / Fortuitous Event'
        COOPERATOR_DEFAULT = 'default', "Cooperator's Failure/Neglect"
        DEATH = 'death', 'Death of Cooperator'
        OTHER = 'other', 'Other'

    cooperator = models.ForeignKey(
        Cooperator, on_delete=models.PROTECT, related_name='projects', verbose_name="MSME"
    )
    title = models.CharField(max_length=255)
    project_code = models.CharField(max_length=50, unique=True, blank=True, null=True)

    status = models.CharField(max_length=15, choices=Status.choices, default=Status.PENDING)

    # --- Funding ---
    total_ifund_amount = models.DecimalField(
        "Total iFund Support (₱)", max_digits=14, decimal_places=2,
        help_text="Total amount of funding assistance approved (the principal to be refunded)."
    )
    fund_source = models.CharField(
        max_length=20,
        choices=[('setup_budget', 'SETUP Budget (refundable)'), ('lgia', 'Local GIA (non-refundable)')],
        default='setup_budget',
    )

    # --- Phase I: implementation ---
    phase1_start_date = models.DateField(null=True, blank=True, help_text="Date SETUP funds were received.")
    phase1_expected_end_date = models.DateField(
        null=True, blank=True,
        help_text="6–12 months from phase1_start_date per AO guidelines."
    )
    phase1_actual_end_date = models.DateField(null=True, blank=True)

    # --- Phase II: refund ---
    refund_period_years = models.PositiveSmallIntegerField(
        choices=[(3, '3 years'), (5, '5 years')], null=True, blank=True
    )
    refund_start_date = models.DateField(null=True, blank=True, help_text="Commences after Phase I completion.")
    refund_extension_years = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(2)],
        help_text="Max 2 additional years allowed."
    )

    # --- Realignment / extension tracking (caps enforced in clean()/save() logic) ---
    budget_realignment_count = models.PositiveSmallIntegerField(default=0)
    implementation_date_changed = models.BooleanField(default=False)

    # --- Termination / withdrawal ---
    termination_reason = models.CharField(
        max_length=20, choices=TerminationReason.choices, blank=True
    )
    termination_date = models.DateField(null=True, blank=True)
    final_obligation_amount = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="Computed per Annex B for terminated/withdrawn projects."
    )

    notes = models.TextField(blank=True)

    # Feeds the Impact KPI "jobs created" estimate (see
    # ImpactRecord.compute_project_estimate below). Left at 0 until staff
    # actually know the number — typically once Phase I implementation is
    # done — rather than guessed at project creation time.
    jobs_created = models.PositiveIntegerField(
        "Jobs Created", default=0,
        help_text="Jobs created as a direct result of this project (warm bodies).",
    )

    # A single project profile photo (not a gallery) — shown as a portrait
    # next to the project header on the detail page.
    photo = models.ImageField(upload_to='project_photos/%Y/%m/', blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.project_code or 'N/A'} — {self.title}"

    # -- Funding / loan supervision helpers -----------------------------------

    @property
    def total_refunded(self):
        return self.refund_payments.aggregate(
            total=models.Sum('amount_paid')
        )['total'] or Decimal("0")

    @property
    def remaining_balance(self):
        return self.total_ifund_amount - self.total_refunded

    @property
    def refund_progress_percent(self):
        if not self.total_ifund_amount:
            return Decimal("0")
        return round((self.total_refunded / self.total_ifund_amount) * 100, 2)

    @property
    def is_phase1_delayed(self):
        """Phase I is running past its expected end date without completion."""
        if self.status != self.Status.PHASE_1 or not self.phase1_expected_end_date:
            return False
        return timezone.now().date() > self.phase1_expected_end_date

    @property
    def is_refund_delinquent(self):
        """
        Flags a cooperator as delinquent if any scheduled installment is
        overdue and still unpaid — this is your early-warning signal for
        the six-month non-remittance rule (AO Part III, referenced re:
        DOST-RO collection action).
        """
        return self.refund_installments.filter(
            due_date__lt=timezone.now().date(),
            status=RefundInstallment.Status.UNPAID,
        ).exists()

    @property
    def is_pending_delinquent(self):
        """
        Flags a project that's been sitting in Pending Approval for over a
        year without moving into Phase I — a stalled-application signal,
        distinct from is_refund_delinquent (which is about missed refund
        payments in Phase II). Computed the same way as refund delinquency
        — live on each read, not a stored status — since there's no
        scheduled task in this app that would otherwise flip an actual
        status field the moment a year passes.
        """
        if self.status != self.Status.PENDING:
            return False
        return timezone.now().date() >= (self.created_at.date() + relativedelta(years=1))

    @property
    def is_delinquent(self):
        """Either flavor of delinquency — convenience for badges/filters
        that just want to know "is something wrong here", regardless of
        which stage the project is in."""
        return self.is_refund_delinquent or self.is_pending_delinquent

    @property
    def consecutive_missed_months(self):
        """
        Count of consecutive unpaid, overdue installments — used to flag
        cooperators approaching the 6-month non-remittance threshold that
        can trigger a demand letter / collection action.
        """
        overdue = self.refund_installments.filter(
            due_date__lt=timezone.now().date(),
            status=RefundInstallment.Status.UNPAID,
        ).order_by('due_date')
        return overdue.count()

    @property
    def can_request_realignment(self):
        """Realignment capped at 3 times during Phase I."""
        return self.status == self.Status.PHASE_1 and self.budget_realignment_count < 3

    @property
    def can_extend_refund(self):
        return self.refund_extension_years < 2

    # -- Phase-gating: which requirements block moving to the next status ----

    @property
    def next_status(self):
        """The next status in the normal forward path (pending -> phase_1 ->
        phase_2 -> completed). Returns None once completed, and doesn't
        apply to terminated/withdrawn since those are exits, not progress."""
        order = [self.Status.PENDING, self.Status.PHASE_1, self.Status.PHASE_2, self.Status.COMPLETED]
        if self.status not in order:
            return None
        idx = order.index(self.status)
        return order[idx + 1] if idx + 1 < len(order) else None

    def missing_requirements_for(self, target_status):
        """Requirements tagged for target_status that haven't been submitted
        yet — these are what's blocking the project from entering that
        status. See ProjectRequirement.REQUIRED_FOR for how requirements get
        tagged with a target phase in the first place."""
        return self.requirements.filter(phase=target_status, is_submitted=False)

    def can_advance_to(self, target_status):
        """
        Whether the project is allowed to move to target_status, per its
        phase-gating requirements checklist. Only forward progress (into
        phase_1, phase_2, or completed) is gated this way — moving to
        terminated/withdrawn is an exit, not progress, so it isn't blocked
        by an incomplete checklist for the phase being left.
        """
        if target_status not in (self.Status.PHASE_1, self.Status.PHASE_2, self.Status.COMPLETED):
            return True
        return not self.missing_requirements_for(target_status).exists()

    @property
    def requirements_completion_percent(self):
        """
        Overall completion % across EVERY requirement row currently
        attached to this project — however many phase-gates have been
        created so far (Phase I's application docs, plus Phase II's and/or
        Completed's gate once the project reaches those phases), out of
        however many are marked submitted.

        This used to be scoped to only next_status's gate, which looked
        "correct" in theory (only shows what's blocking the NEXT status)
        but had a real bug in practice: the moment a project advances into
        a new phase, its now-fully-submitted PREVIOUS gate stops being
        counted at all, and the percentage silently resets toward 0% for
        the brand-new (and therefore still-empty) next gate — so real,
        submitted requirements were quietly excluded from the number. See
        next_gate_completion_percent below if you specifically want "how
        close is this project to its NEXT milestone" instead of this
        overall total.
        """
        total = self.requirements.count()
        if not total:
            return 0
        submitted = self.requirements.filter(is_submitted=True).count()
        return round((submitted / total) * 100)

    @property
    def next_gate_completion_percent(self):
        """
        Completion % scoped to ONLY the gate blocking the next status —
        e.g. for a Phase I project, this is just the Phase II gate
        (Completion + Financial Report), ignoring the Phase I paperwork
        that's already done. Useful for "how close is this project to
        advancing" views; requirements_completion_percent above is the
        all-time total instead.
        """
        target = self.next_status
        if not target:
            return 100
        relevant = self.requirements.filter(phase=target)
        total = relevant.count()
        if not total:
            return 100
        submitted = relevant.filter(is_submitted=True).count()
        return round((submitted / total) * 100)

    @property
    def overdue_reports_count(self):
        return self.monitoring_reports.filter(
            date_submitted__isnull=True, due_date__lt=timezone.now().date()
        ).count()


# ---------------------------------------------------------------------------
# Line-Item Budget — what the iFund actually pays for
# ---------------------------------------------------------------------------

class BudgetLineItem(models.Model):
    """A single item in the project's approved Line-Item Budget (LIB)."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='budget_items')
    description = models.CharField(max_length=255)
    category = models.CharField(
        max_length=30,
        choices=[
            ('equipment', 'Equipment'),
            ('training', 'Training'),
            ('lab_analysis', 'Laboratory Analysis'),
            ('packaging_labeling', 'Packaging/Labeling'),
            ('consultancy', 'Consultancy'),
            ('other', 'Other'),
        ],
        default='equipment',
    )
    approved_amount = models.DecimalField(max_digits=14, decimal_places=2)
    disbursed_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))

    def __str__(self):
        return f"{self.project} — {self.description}"

    @property
    def unexpended_balance(self):
        return self.approved_amount - self.disbursed_amount


class BudgetRealignment(models.Model):
    """History log each time a project's LIB is realigned (max 3x in Phase I)."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='realignments')
    requested_date = models.DateField(default=timezone.now)
    reason = models.TextField()
    approved = models.BooleanField(default=False)
    approved_date = models.DateField(null=True, blank=True)
    # The realignment request letter / RPMO approval memo — same reasoning
    # as RefundPayment.receipt_file: a paper trail that can be opened later,
    # not just a checkbox saying it happened.
    supporting_document = models.FileField(
        upload_to='budget_realignments/%Y/%m/', blank=True, null=True
    )

    def __str__(self):
        return f"Realignment #{self.pk} — {self.project}"


# ---------------------------------------------------------------------------
# Refund tracking — the "loan" side
# ---------------------------------------------------------------------------

class RefundInstallment(models.Model):
    """
    A scheduled monthly (or periodic) refund installment. Generate these
    up front from refund_start_date + refund_period_years so overdue/unpaid
    installments can be queried directly instead of computed on the fly.
    """

    class Status(models.TextChoices):
        UNPAID = 'unpaid', 'Unpaid'
        PAID = 'paid', 'Paid'
        WAIVED = 'waived', 'Waived'
        RESTRUCTURED = 'restructured', 'Restructured'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='refund_installments')
    due_date = models.DateField()
    amount_due = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.UNPAID)

    class Meta:
        ordering = ['due_date']

    def __str__(self):
        return f"{self.project} due {self.due_date} — {self.status}"

    @property
    def is_overdue(self):
        return self.status == self.Status.UNPAID and self.due_date < timezone.now().date()


class RefundPayment(models.Model):
    """An actual payment received against one or more installments."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='refund_payments')
    installment = models.ForeignKey(
        RefundInstallment, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments'
    )
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2)
    date_paid = models.DateField(default=timezone.now)
    or_number = models.CharField("O.R. / Reference No.", max_length=100, blank=True)
    # The or_number field above is just the reference number for searching/
    # sorting — this is the actual scanned receipt image/PDF, so an OR can be
    # pulled up as proof rather than trusting the typed-in number alone.
    receipt_file = models.FileField(
        "Scanned O.R.", upload_to='refund_receipts/%Y/%m/', blank=True, null=True
    )
    remarks = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.project} — ₱{self.amount_paid} on {self.date_paid}"


class RefundRestructure(models.Model):
    """A restructuring event per AO Part III, Section 5(d)."""

    class Ground(models.TextChoices):
        IMPLEMENTATION_DELAY = 'delay', 'Delay in Project Implementation'
        CALAMITY = 'calamity', 'Natural Calamity'
        TECHNICAL = 'technical', 'Technical Difficulties with Equipment'
        NEW_REGULATION = 'regulation', 'New Rules/Regulations'
        INDUSTRY_ISSUE = 'industry', 'Industry-related Problem'
        OTHER = 'other', 'Other (RTEC-recommended)'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='restructures')
    ground = models.CharField(max_length=15, choices=Ground.choices)
    requested_date = models.DateField(default=timezone.now)
    new_schedule_notes = models.TextField(blank=True)
    approved = models.BooleanField(default=False)
    approved_date = models.DateField(null=True, blank=True)
    # Evidence for the ground cited (calamity declaration, RTEC recommendation
    # letter, etc.) — the AO requires restructuring to be justified, not just
    # asserted.
    supporting_document = models.FileField(
        upload_to='refund_restructures/%Y/%m/', blank=True, null=True
    )

    def __str__(self):
        return f"Restructure — {self.project} ({self.get_ground_display()})"


# ---------------------------------------------------------------------------
# Equipment — acquired under the project, including pulled-out tracking
# ---------------------------------------------------------------------------

class Equipment(models.Model):
    class Status(models.TextChoices):
        IN_USE = 'in_use', 'In Use at Cooperator'
        PULLED_OUT = 'pulled_out', 'Pulled Out'
        TRANSFERRED = 'transferred', 'Transferred to New Cooperator'
        DONATED = 'donated', 'Donated'
        DISPOSED = 'disposed', 'Disposed'
        OWNED_BY_COOPERATOR = 'owned', 'Ownership Transferred (Fully Refunded)'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='equipment')
    name = models.CharField(max_length=255)
    specification = models.TextField(blank=True)
    supplier_fabricator = models.CharField(max_length=255, blank=True)
    acquisition_cost = models.DecimalField(max_digits=14, decimal_places=2)
    net_book_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_USE)
    pulled_out_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.project})"


# ---------------------------------------------------------------------------
# Monitoring reports — the periodic reportorial requirements (Part III §2)
# ---------------------------------------------------------------------------

class MonitoringReport(models.Model):
    """
    Tracks each required report per the AO's reportorial schedule, so the
    admin dashboard can flag what's due/overdue per project.
    """

    class ReportType(models.TextChoices):
        SEMESTRAL_STATUS = 'form_003', 'Form 003 - Semestral Status Report'
        PROJECT_INFO_ONGOING = 'form_009', 'Form 009 - Project Info Sheet (Ongoing)'
        COMPLETION = 'form_010', 'Form 010 - Completion Report'
        FINANCIAL = 'form_004', 'Form 004 - Audited Financial Report'
        REFUND_PERFORMANCE = 'form_012', 'Form 012 - Annual Refund Performance Report'
        TERMINAL = 'form_013', 'Form 013 - Terminal Report'
        TERMINATION_WITHDRAWAL = 'form_011', 'Form 011 - Termination/Withdrawal Report'

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='monitoring_reports')
    report_type = models.CharField(max_length=15, choices=ReportType.choices)
    period_covered = models.CharField(max_length=100, blank=True, help_text="e.g. 'H1 2026', 'FY 2026'")
    due_date = models.DateField()
    date_submitted = models.DateField(null=True, blank=True)
    file = models.FileField(upload_to='monitoring_reports/', blank=True, null=True)

    class Meta:
        ordering = ['due_date']

    def __str__(self):
        return f"{self.get_report_type_display()} — {self.project}"

    @property
    def is_overdue(self):
        return self.date_submitted is None and self.due_date < timezone.now().date()


# ---------------------------------------------------------------------------
# Application requirements checklist (Part II, Section 4 of the AO) — lets
# admin staff see at a glance which submission documents are in hand and
# upload/replace the actual files.
# ---------------------------------------------------------------------------

class ProjectRequirement(models.Model):
    class RequirementType(models.TextChoices):
        # --- Gate: required before the project can enter Phase I ---
        # (Part II, Section 4 — application/eligibility documents)
        LETTER_OF_INTENT = 'letter_of_intent', 'Letter of Intent'
        TNA_FORM_01 = 'tna_form_01', 'DOST TNA Form 01 - Application for TNA'
        TNA_FORM_02 = 'tna_form_02', 'DOST TNA Form 02 - TNA Report'
        PROJECT_PROPOSAL = 'proposal', 'SETUP Form 001 - Project Proposal'
        BUSINESS_PERMIT = 'business_permit', 'Business Permits/Licenses'
        REGISTRATION_CERT = 'registration_cert', 'DTI/SEC/CDA Registration'
        OFFICIAL_RECEIPT = 'official_receipt', 'Official Receipt (photocopy)'
        ARTICLES_OF_INCORPORATION = 'articles_of_incorporation', 'Articles of Incorporation'
        BOARD_RESOLUTION = 'board_resolution', 'Board/Council Resolution'
        FINANCIAL_STATEMENTS = 'financial_statements', 'Financial Statements (past 1-3 yrs)'
        SWORN_AFFIDAVIT = 'sworn_affidavit', 'Sworn Affidavit (no conflict of interest)'
        PROJECTED_FINANCIALS = 'projected_financials', 'Projected Financial Statements'
        TECHNICAL_SPECS = 'technical_specs', 'Technical Specifications/Drawings'
        SUPPLIER_QUOTATIONS = 'supplier_quotations', 'Supplier Quotations (min. 3)'

        # --- Gate: required before the project can enter Phase II ---
        # (Part III — close out Phase I implementation before refund starts)
        PHASE1_COMPLETION_REPORT = 'phase1_completion_report', 'SETUP Form 010 - Completion Report'
        PHASE1_FINANCIAL_REPORT = 'phase1_financial_report', 'SETUP Form 004 - Audited Financial Report (Phase I)'

        # --- Gate: required before the project can be marked Completed ---
        # (Part III — close out Phase II after the refund is fully paid)
        TERMINAL_REPORT = 'terminal_report', 'SETUP Form 013 - Terminal Report'

        OTHER = 'other', 'Other'

    # Which status this requirement must be satisfied BEFORE the project can
    # enter — this is what turns the checklist into an actual gate instead of
    # just a flat to-do list. See Project.missing_requirements_for() and
    # Project.can_advance_to() below, and ProjectStatusForm.clean() in
    # forms.py, which is what actually blocks the status change.
    PHASE_CHOICES = [
        (Project.Status.PHASE_1, 'Required before Phase I'),
        (Project.Status.PHASE_2, 'Required before Phase II'),
        (Project.Status.COMPLETED, 'Required before Completion'),
    ]

    # Single source of truth for "which requirement types gate which target
    # status" — used both by the signals that auto-create these rows and by
    # Project.missing_requirements_for() when checking whether a status
    # change is allowed. Keeping this in one place means the checklist and
    # the enforcement logic can never drift out of sync with each other.
    REQUIRED_FOR = {
        Project.Status.PHASE_1: [
            RequirementType.LETTER_OF_INTENT, RequirementType.TNA_FORM_01, RequirementType.TNA_FORM_02,
            RequirementType.PROJECT_PROPOSAL, RequirementType.BUSINESS_PERMIT, RequirementType.REGISTRATION_CERT,
            RequirementType.OFFICIAL_RECEIPT, RequirementType.ARTICLES_OF_INCORPORATION,
            RequirementType.BOARD_RESOLUTION, RequirementType.FINANCIAL_STATEMENTS,
            RequirementType.SWORN_AFFIDAVIT, RequirementType.PROJECTED_FINANCIALS,
            RequirementType.TECHNICAL_SPECS, RequirementType.SUPPLIER_QUOTATIONS,
        ],
        Project.Status.PHASE_2: [
            RequirementType.PHASE1_COMPLETION_REPORT, RequirementType.PHASE1_FINANCIAL_REPORT,
        ],
        Project.Status.COMPLETED: [
            RequirementType.TERMINAL_REPORT,
        ],
    }

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='requirements')
    requirement_type = models.CharField(max_length=30, choices=RequirementType.choices)
    phase = models.CharField(
        max_length=15, choices=PHASE_CHOICES, default=Project.Status.PHASE_1,
        help_text="Which status this requirement must be completed before the project can enter.",
    )
    is_submitted = models.BooleanField(default=False)
    file = models.FileField(upload_to='project_requirements/%Y/%m/', blank=True, null=True)
    date_submitted = models.DateField(null=True, blank=True)
    remarks = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = [('project', 'requirement_type')]
        ordering = ['phase', 'requirement_type']

    def __str__(self):
        return f"{self.get_requirement_type_display()} — {self.project}"


# ---------------------------------------------------------------------------
# Impact KPI — quarterly office-wide accomplishment figures, matching the
# PSTO's "DATA TO BE SUBMITTED AT THE END OF EVERY QUARTER" form exactly:
# one row of five figures per quarter, submitted for the office as a whole
# (not broken down per project — e.g. "entities assisted" explicitly
# includes walk-in clients who were never attached to a project record).
# ---------------------------------------------------------------------------

class ImpactRecord(models.Model):
    """One quarter's Impact KPI submission for the whole PSTO."""

    class Quarter(models.TextChoices):
        Q1 = 'q1', '1st Quarter'
        Q2 = 'q2', '2nd Quarter'
        Q3 = 'q3', '3rd Quarter'
        Q4 = 'q4', '4th Quarter'

    year = models.PositiveIntegerField(default=_current_year)
    quarter = models.CharField(max_length=2, choices=Quarter.choices)

    entities_assisted = models.PositiveIntegerField(
        "No. of firms/other S&T entities provided with S&T assistance",
        default=0, help_text="Please include the walk-in clients.",
    )
    jobs_created = models.PositiveIntegerField(
        "No. of jobs created (in terms of warm bodies)", default=0,
    )
    technology_interventions = models.PositiveIntegerField(
        "No. of technology interventions", default=0,
    )
    export_firms_assisted = models.PositiveIntegerField(
        "Number of export firms assisted", default=0,
    )
    gross_sales = models.DecimalField(
        "Gross Sales (in Pesos)", max_digits=16, decimal_places=2, default=Decimal("0"),
    )

    remarks = models.CharField(max_length=255, blank=True)
    date_recorded = models.DateField(default=timezone.now)

    class Meta:
        unique_together = [('year', 'quarter')]
        ordering = ['-year', '-quarter']

    def __str__(self):
        return f"{self.get_quarter_display()} {self.year}"

    # Calendar-quarter month ranges (Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec).
    # Adjust here if the office's fiscal quarters don't line up with the
    # calendar year — everything else derives from this one mapping.
    QUARTER_MONTH_RANGES = {
        'q1': (1, 3),
        'q2': (4, 6),
        'q3': (7, 9),
        'q4': (10, 12),
    }

    @classmethod
    def quarter_date_range(cls, year, quarter):
        """First and last calendar date of the given (year, quarter)."""
        start_month, end_month = cls.QUARTER_MONTH_RANGES[quarter]
        start = date(year, start_month, 1)
        last_day = calendar.monthrange(year, end_month)[1]
        end = date(year, end_month, last_day)
        return start, end

    @classmethod
    def compute_project_estimate(cls, year, quarter):
        """
        A starting-point baseline for a quarter's Impact KPI figures,
        computed straight from Project/Cooperator data — so staff aren't
        re-counting projects by hand every quarter before they can log an
        entry. This is deliberately NOT the final number: it can't see
        walk-in clients or any other non-project S&T assistance, which is
        exactly why the quarterly entry form still needs a human to review
        and top the numbers up before saving.

        Assumptions baked in here (adjust if your office counts
        differently):
          - "Assisted this quarter" / "technology intervention" = a
            project whose Phase I actually started (phase1_start_date)
            within the quarter — i.e. iFund was released and
            implementation began.
          - "Jobs created" only counts once Phase I is actually finished
            (phase1_actual_end_date within the quarter), not merely
            started — a project with no actual end date yet contributes
            nothing until that's filled in.
          - "Export firms assisted" = the same "started this quarter"
            projects, filtered to cooperators flagged is_export_firm.

        Gross Sales has NO computed component — nothing in this schema
        tracks an MSME's actual business revenue, so it's intentionally
        left out here and stays 100% manually reported.
        """
        start, end = cls.quarter_date_range(year, quarter)

        started_this_quarter = Project.objects.filter(
            phase1_start_date__gte=start, phase1_start_date__lte=end,
        )

        entities_assisted = started_this_quarter.values('cooperator').distinct().count()
        technology_interventions = started_this_quarter.count()
        export_firms_assisted = started_this_quarter.filter(
            cooperator__is_export_firm=True
        ).values('cooperator').distinct().count()

        completed_this_quarter = Project.objects.filter(
            phase1_actual_end_date__gte=start, phase1_actual_end_date__lte=end,
        )
        jobs_created = completed_this_quarter.aggregate(
            total=models.Sum('jobs_created')
        )['total'] or 0

        return {
            'entities_assisted': entities_assisted,
            'jobs_created': jobs_created,
            'technology_interventions': technology_interventions,
            'export_firms_assisted': export_firms_assisted,
        }