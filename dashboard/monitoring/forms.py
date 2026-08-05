from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from .models import (
    Project, ProjectRequirement, MonitoringReport, Cooperator,
    BudgetLineItem, RefundPayment, RefundInstallment, ImpactRecord,
    ProjectQuarterlyImpact, Equipment,
)


class UserSettingsForm(forms.ModelForm):
    """Basic account details — the Settings page's 'Account' section."""

    class Meta:
        model = get_user_model()
        fields = ['first_name', 'last_name', 'email']


class CooperatorForm(forms.ModelForm):
    """Add/edit an MSME profile — used by the MSME panel's 'Add MSME' page."""

    class Meta:
        model = Cooperator
        fields = [
            'name', 'business_type', 'enterprise_category', 'development_stage',
            'priority_sector', 'address', 'contact_person', 'contact_number', 'email',
            'dti_sec_cda_registration_no', 'filipino_ownership_percent',
            'has_outstanding_accountabilities', 'is_export_firm',
        ]


class ProjectForm(forms.ModelForm):
    """Create (or fully edit) a project — used by the 'Add Project' page."""

    class Meta:
        model = Project
        fields = [
            'cooperator', 'title', 'project_code', 'status',
            'total_ifund_amount', 'fund_source',
            'phase1_start_date', 'phase1_expected_end_date',
            'refund_period_years', 'refund_start_date',
            'notes',
        ]
        widgets = {
            'phase1_start_date': forms.DateInput(attrs={'type': 'date'}),
            'phase1_expected_end_date': forms.DateInput(attrs={'type': 'date'}),
            'refund_start_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 3}),
            # Without an explicit step, <input type="number"> defaults to
            # step="1" — browsers then reject decimal values like 45000.50
            # on submit as "not a valid value", not just displaying them
            # wrong. step="0.01" allows centavos; found this was missing
            # everywhere decimal money fields are edited, not just here.
            'total_ifund_amount': forms.NumberInput(attrs={'step': '0.01'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Without this, the dropdown falls back to whatever order Cooperator
        # rows happen to be in (usually creation order) — alphabetical by
        # name is what actually helps someone scanning/typing to find one.
        self.fields['cooperator'].queryset = Cooperator.objects.order_by('name')

    def clean_total_ifund_amount(self):
        """
        total_ifund_amount is blank=True on the model (so this field isn't
        required in the form), but it's NOT null=True at the DB level —
        remaining_balance/refund_progress_percent do arithmetic on it, and
        a real None would crash both. Left blank, Django's form gives back
        None as the cleaned value; this converts that to Decimal("0")
        before it ever reaches .save(), matching the model field's own
        default and avoiding a NOT NULL error at the database.
        """
        return self.cleaned_data.get('total_ifund_amount') or Decimal("0")


class ProjectPhotoForm(forms.ModelForm):
    """Set/replace the project's single profile photo."""

    class Meta:
        model = Project
        fields = ['photo']


class ExcelImportForm(forms.Form):
    """
    Bulk-create projects (and their MSMEs, if new) from an .xlsx file.
    Expected columns, in order, starting row 2 (row 1 = headers):
    MSME Name | Project Title | Project Code | Status | iFund Amount | Fund Source
    """
    file = forms.FileField(
        label="Excel file (.xlsx)",
        help_text=(
            "Columns in order: MSME Name, Project Title, Project Code, "
            "Status (pending/phase_1/phase_2/completed/terminated/withdrawn), "
            "iFund Amount, Fund Source (setup_budget/lgia). First row is treated as a header."
        ),
    )

    def clean_file(self):
        f = self.cleaned_data['file']
        if not f.name.lower().endswith('.xlsx'):
            raise forms.ValidationError("Please upload a .xlsx file.")
        return f


class ProjectStatusForm(forms.ModelForm):
    """Admin-only control to change a project's status."""

    class Meta:
        model = Project
        fields = ['status', 'termination_reason', 'termination_date', 'notes']
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def clean_status(self):
        """
        Blocks moving into Phase I, Phase II, or Completed while that gate's
        requirements checklist still has unsubmitted items — per the DOST
        guidelines, a project shouldn't advance past a stage until the
        paperwork for that stage is actually in hand. Moving to terminated/
        withdrawn is never blocked this way, since those are exits, not
        forward progress (see Project.can_advance_to).

        Completed has a second gate on top of the checklist: the refund
        must be fully paid off. That check has no matching
        ProjectRequirement row to list by name, so it needs its own
        message rather than falling through to the generic "still
        missing: ..." wording below.
        """
        new_status = self.cleaned_data['status']

        # self.instance still holds the ORIGINAL (pre-save) values here —
        # ModelForm doesn't apply cleaned_data onto the instance until save(),
        # so self.instance.status is the OLD status, which is exactly what
        # can_advance_to()/missing_requirements_for() need to check against.
        if new_status != self.instance.status and not self.instance.can_advance_to(new_status):
            if new_status == Project.Status.COMPLETED and self.instance.remaining_balance > 0:
                raise forms.ValidationError(
                    f'Can\'t mark this Completed yet — ₱{self.instance.remaining_balance:,.2f} '
                    f'of the refund is still outstanding.'
                )
            missing = self.instance.missing_requirements_for(new_status)
            missing_names = ", ".join(r.get_requirement_type_display() for r in missing)
            status_label = dict(Project.Status.choices).get(new_status, new_status)
            raise forms.ValidationError(
                f'Can\'t move to "{status_label}" yet — still missing: {missing_names}'
            )

        return new_status


class RequirementUploadForm(forms.ModelForm):
    """
    Upload/replace the file for a single requirement row. is_submitted and
    date_submitted are set automatically in the view once a file is attached,
    so the admin doesn't have to fill those in by hand.
    """

    class Meta:
        model = ProjectRequirement
        fields = ['file', 'remarks']
        widgets = {
            # data-dz-placeholder is picked up by site-dropzone.js so the
            # drag-and-drop zone shows requirement-specific wording instead
            # of the generic default.
            'file': forms.ClearableFileInput(attrs={'data-dz-placeholder': 'Drag file here or click to browse'}),
        }


class ReportUploadForm(forms.ModelForm):
    """Upload/replace the file for a single monitoring report row."""

    class Meta:
        model = MonitoringReport
        fields = ['file', 'date_submitted']
        widgets = {
            'date_submitted': forms.DateInput(attrs={'type': 'date'}),
            'file': forms.ClearableFileInput(attrs={'data-dz-placeholder': 'Drag report file here or click to browse'}),
        }


class BudgetLineItemForm(forms.ModelForm):
    """Edit a single approved Line-Item Budget entry — the Funding page's
    equivalent of the admin's BudgetLineItem edit form."""

    class Meta:
        model = BudgetLineItem
        fields = ['description', 'category', 'approved_amount', 'disbursed_amount']
        widgets = {
            'approved_amount': forms.NumberInput(attrs={'step': '0.01'}),
            'disbursed_amount': forms.NumberInput(attrs={'step': '0.01'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Nothing may have been disbursed yet at the time this line item
        # is entered/edited — not required in the form, same reasoning as
        # clean_total_ifund_amount on ProjectForm below.
        self.fields['disbursed_amount'].required = False

    def clean_disbursed_amount(self):
        """
        disbursed_amount has default=Decimal("0") on the model but is NOT
        null=True — left blank, Django's form gives back None as the
        cleaned value, which would hit a NOT NULL error at the database
        rather than a form-validation error. Converts blank to
        Decimal("0") first, matching the model field's own default.
        """
        return self.cleaned_data.get('disbursed_amount') or Decimal("0")


class BudgetLineItemCreateForm(forms.ModelForm):
    """
    Add a new Line-Item Budget entry — used both from the Funding page
    (any project) and from a project's own detail page (project
    pre-locked). Same project-lock pattern as RefundPaymentCreateForm
    below: pass project=<Project instance> to disable and pre-fill the
    project field instead of showing it as a dropdown.
    """

    class Meta:
        model = BudgetLineItem
        fields = ['project', 'description', 'category', 'approved_amount', 'disbursed_amount']
        widgets = {
            'approved_amount': forms.NumberInput(attrs={'step': '0.01'}),
            'disbursed_amount': forms.NumberInput(attrs={'step': '0.01'}),
        }

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['project'].queryset = Project.objects.order_by('title')
        if project is not None:
            self.fields['project'].initial = project
            self.fields['project'].disabled = True
        # Nothing may have been disbursed yet when a line item is first
        # created — not required in the form; see clean_disbursed_amount.
        self.fields['disbursed_amount'].required = False

    def clean_disbursed_amount(self):
        """Blank in the form → Decimal("0"), matching the model field's
        own default (see BudgetLineItemForm.clean_disbursed_amount for
        why this can't just be left as Django's default None)."""
        return self.cleaned_data.get('disbursed_amount') or Decimal("0")


class ImpactRecordForm(forms.ModelForm):
    """Log/update the office's Impact KPI figures for one quarter."""

    class Meta:
        model = ImpactRecord
        fields = [
            'year', 'quarter', 'entities_assisted', 'jobs_created',
            'technology_interventions', 'export_firms_assisted', 'gross_sales', 'remarks',
        ]
        widgets = {
            'gross_sales': forms.NumberInput(attrs={'step': '0.01'}),
        }


class RefundPaymentForm(forms.ModelForm):
    """Edit a single refund payment entry — the Funding page's equivalent
    of the admin's RefundPayment edit form."""

    class Meta:
        model = RefundPayment
        fields = ['installment', 'amount_paid', 'date_paid', 'or_number', 'receipt_file', 'remarks']
        widgets = {
            'date_paid': forms.DateInput(attrs={'type': 'date'}),
            'receipt_file': forms.ClearableFileInput(attrs={'data-dz-placeholder': 'Drag receipt here or click to browse'}),
            'amount_paid': forms.NumberInput(attrs={'step': '0.01'}),
        }

    def __init__(self, *args, **kwargs):
        # Only offer installments belonging to this payment's own project,
        # not every installment in the system.
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['installment'].queryset = self.instance.project.refund_installments.all()


class RefundPaymentCreateForm(forms.ModelForm):
    """
    Add a new refund payment — used both from the Funding page (any
    project) and from a project's own detail page (project pre-locked).

    Pass project=<Project instance> to lock the project (hides the field
    and scopes `installment` to that project only, same as
    RefundPaymentForm does for edits). Without it, `project` is a normal
    dropdown and `installment` starts out unfiltered/empty — the
    installment choices only make sense once a project is picked, which
    needs JS on the page to re-filter live; until then staff can just
    leave installment blank (it's optional on the model) or pick from
    the full list, which is labelled with its own project via
    RefundInstallment.__str__.
    """

    class Meta:
        model = RefundPayment
        fields = ['project', 'installment', 'amount_paid', 'date_paid', 'or_number', 'receipt_file', 'remarks']
        widgets = {
            'date_paid': forms.DateInput(attrs={'type': 'date'}),
            'receipt_file': forms.ClearableFileInput(attrs={'data-dz-placeholder': 'Drag receipt here or click to browse'}),
            'amount_paid': forms.NumberInput(attrs={'step': '0.01'}),
        }

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None:
            self.fields['project'].initial = project
            self.fields['project'].disabled = True
            self.fields['installment'].queryset = project.refund_installments.filter(
                status=RefundInstallment.Status.UNPAID
            )
        else:
            # Unpaid-only by default, across every project, sorted so the
            # dropdown groups by project without needing JS filtering.
            self.fields['installment'].queryset = RefundInstallment.objects.filter(
                status=RefundInstallment.Status.UNPAID
            ).select_related('project').order_by('project__title', 'due_date')

class ProjectImpactDataForm(forms.ModelForm):
    """Quick inline update for the two project-level numbers that feed
    the Impact KPI page's project-derived estimate (see
    ImpactRecord.compute_project_estimate) — jobs created and gross
    sales, typically filled in once Phase I implementation is done."""
    class Meta:
        model = Project
        fields = ['jobs_created', 'gross_sales']
        widgets = {
            'gross_sales': forms.NumberInput(attrs={'step': '0.01'}),
        }


class ProjectQuarterlyImpactForm(forms.ModelForm):
    """
    Log THIS quarter's Jobs Created / Gross Sales for one project — the
    actual per-quarter increment that ImpactRecord.compute_project_estimate
    now sums, as opposed to ProjectImpactDataForm above (which edits
    Project.jobs_created/gross_sales, a single lifetime/baseline figure
    with no notion of which quarter it belongs to).

    Re-submitting the same (project, year, quarter) updates that entry
    instead of creating a duplicate — same pattern as ImpactRecordForm on
    the office-wide Impact KPI page.
    """

    class Meta:
        model = ProjectQuarterlyImpact
        fields = ['year', 'quarter', 'jobs_created', 'gross_sales', 'remarks']
        widgets = {
            'gross_sales': forms.NumberInput(attrs={'step': '0.01'}),
        }


class EquipmentCreateForm(forms.ModelForm):
    """
    Add a new Equipment record — the project detail page's "+ Add
    Equipment" button. Unlike Budget Line Items / Refund Payments,
    equipment has no office-wide "any project" entry point, so this only
    ever runs with the project pre-locked (project=<Project instance>),
    same locking pattern as BudgetLineItemCreateForm/RefundPaymentCreateForm.
    """

    class Meta:
        model = Equipment
        fields = ['project', 'name', 'specification', 'supplier_fabricator', 'acquisition_cost', 'status']
        widgets = {
            'specification': forms.Textarea(attrs={'rows': 2}),
            'acquisition_cost': forms.NumberInput(attrs={'step': '0.01'}),
        }

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None:
            self.fields['project'].initial = project
            self.fields['project'].disabled = True