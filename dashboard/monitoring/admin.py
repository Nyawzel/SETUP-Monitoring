from django.contrib import admin
from .models import (
    Cooperator, Project, BudgetLineItem, BudgetRealignment,
    RefundInstallment, RefundPayment, RefundRestructure,
    Equipment, MonitoringReport, ProjectRequirement, ImpactRecord,
    ProjectQuarterlyImpact,
)


@admin.register(Cooperator)
class CooperatorAdmin(admin.ModelAdmin):
    list_display = ['name', 'business_type', 'enterprise_category', 'is_eligible_for_new_ifund']
    search_fields = ['name']


class BudgetLineItemInline(admin.TabularInline):
    model = BudgetLineItem
    extra = 1


class RefundInstallmentInline(admin.TabularInline):
    model = RefundInstallment
    extra = 1


class EquipmentInline(admin.TabularInline):
    model = Equipment
    extra = 1


class ProjectRequirementInline(admin.TabularInline):
    model = ProjectRequirement
    extra = 1


class MonitoringReportInline(admin.TabularInline):
    model = MonitoringReport
    extra = 1


class ProjectQuarterlyImpactInline(admin.TabularInline):
    model = ProjectQuarterlyImpact
    extra = 1


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'cooperator', 'status', 'total_ifund_amount',
        'remaining_balance', 'refund_progress_percent', 'is_refund_delinquent',
    ]
    list_filter = ['status']
    search_fields = ['title', 'project_code', 'cooperator__name']
    # Searchable dropdown instead of a giant <select> — same reason as
    # RefundPaymentAdmin below. Requires CooperatorAdmin.search_fields,
    # which already exists above.
    autocomplete_fields = ['cooperator']
    inlines = [
        BudgetLineItemInline, RefundInstallmentInline,
        EquipmentInline, ProjectRequirementInline, MonitoringReportInline,
        ProjectQuarterlyImpactInline,
    ]


@admin.register(RefundPayment)
class RefundPaymentAdmin(admin.ModelAdmin):
    list_display = ['project', 'amount_paid', 'date_paid', 'or_number', 'installment', 'has_receipt']
    list_filter = ['date_paid']
    # Searchable dropdown (type-to-filter) instead of a plain <select> —
    # this is what was hard to use once there are many projects. Requires
    # ProjectAdmin.search_fields, which already exists above.
    autocomplete_fields = ['project', 'installment']

    @admin.display(boolean=True, description="Receipt on file")
    def has_receipt(self, obj):
        return bool(obj.receipt_file)


@admin.register(RefundRestructure)
class RefundRestructureAdmin(admin.ModelAdmin):
    list_display = ['project', 'ground', 'requested_date', 'approved', 'has_document']
    autocomplete_fields = ['project']

    @admin.display(boolean=True, description="Document on file")
    def has_document(self, obj):
        return bool(obj.supporting_document)


@admin.register(RefundInstallment)
class RefundInstallmentAdmin(admin.ModelAdmin):
    list_display = ['project', 'due_date', 'amount_due', 'status']
    list_filter = ['status']
    # search_fields here isn't just for this page's own search box — it's
    # what makes RefundPaymentAdmin.autocomplete_fields = [..., 'installment']
    # possible at all; Django requires it on the target model's admin.
    search_fields = ['project__title', 'project__project_code', 'status']
    # This page's OWN "project" field was still a plain <select> until now —
    # search_fields above only serves autocomplete for OTHER admins pointing
    # at RefundInstallment; it doesn't make this page's own FK fields
    # searchable. That's what autocomplete_fields does.
    autocomplete_fields = ['project']


@admin.register(BudgetLineItem)
class BudgetLineItemAdmin(admin.ModelAdmin):
    list_display = ['project', 'description', 'category', 'approved_amount', 'disbursed_amount']
    list_filter = ['category']
    autocomplete_fields = ['project']


@admin.register(BudgetRealignment)
class BudgetRealignmentAdmin(admin.ModelAdmin):
    list_display = ['project', 'requested_date', 'approved', 'approved_date', 'has_document']
    list_filter = ['approved']
    autocomplete_fields = ['project']

    @admin.display(boolean=True, description="Document on file")
    def has_document(self, obj):
        return bool(obj.supporting_document)


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ['name', 'project', 'status', 'acquisition_cost']
    list_filter = ['status']
    search_fields = ['name']
    autocomplete_fields = ['project']


@admin.register(MonitoringReport)
class MonitoringReportAdmin(admin.ModelAdmin):
    list_display = ['project', 'report_type', 'due_date', 'date_submitted']
    list_filter = ['report_type']
    autocomplete_fields = ['project']


@admin.register(ProjectRequirement)
class ProjectRequirementAdmin(admin.ModelAdmin):
    list_display = ['project', 'requirement_type', 'is_submitted', 'date_submitted']
    list_filter = ['requirement_type', 'is_submitted']
    autocomplete_fields = ['project']


@admin.register(ImpactRecord)
class ImpactRecordAdmin(admin.ModelAdmin):
    list_display = [
        'quarter', 'year', 'entities_assisted', 'jobs_created',
        'technology_interventions', 'export_firms_assisted', 'gross_sales',
    ]
    list_filter = ['year', 'quarter']


@admin.register(ProjectQuarterlyImpact)
class ProjectQuarterlyImpactAdmin(admin.ModelAdmin):
    list_display = ['project', 'quarter', 'year', 'jobs_created', 'gross_sales']
    list_filter = ['year', 'quarter']
    autocomplete_fields = ['project']