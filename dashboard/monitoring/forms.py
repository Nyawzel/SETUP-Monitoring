from django import forms
from django.contrib.auth import get_user_model
from .models import (
    Project, ProjectRequirement, MonitoringReport, Cooperator,
    BudgetLineItem, RefundPayment, RefundInstallment, ImpactRecord,
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
            'jobs_created', 'notes',
        ]
        widgets = {
            'phase1_start_date': forms.DateInput(attrs={'type': 'date'}),
            'phase1_expected_end_date': forms.DateInput(attrs={'type': 'date'}),
            'refund_start_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }


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
        """
        new_status = self.cleaned_data['status']

        # self.instance still holds the ORIGINAL (pre-save) values here —
        # ModelForm doesn't apply cleaned_data onto the instance until save(),
        # so self.instance.status is the OLD status, which is exactly what
        # can_advance_to()/missing_requirements_for() need to check against.
        if new_status != self.instance.status and not self.instance.can_advance_to(new_status):
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


class ImpactRecordForm(forms.ModelForm):
    """Log/update the office's Impact KPI figures for one quarter."""

    class Meta:
        model = ImpactRecord
        fields = [
            'year', 'quarter', 'entities_assisted', 'jobs_created',
            'technology_interventions', 'export_firms_assisted', 'gross_sales', 'remarks',
        ]


class RefundPaymentForm(forms.ModelForm):
    """Edit a single refund payment entry — the Funding page's equivalent
    of the admin's RefundPayment edit form."""

    class Meta:
        model = RefundPayment
        fields = ['installment', 'amount_paid', 'date_paid', 'or_number', 'receipt_file', 'remarks']
        widgets = {
            'date_paid': forms.DateInput(attrs={'type': 'date'}),
            'receipt_file': forms.ClearableFileInput(attrs={'data-dz-placeholder': 'Drag receipt here or click to browse'}),
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