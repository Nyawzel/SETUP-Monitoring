"""
monitoring/signals.py

Auto-generates the RefundInstallment schedule the moment a Project's status
changes to phase_2 — this is what makes the refund schedule "automatic"
instead of something an admin has to type in row by row.

Manual entry is still possible and still matters: RefundInstallment rows
stay directly editable in the admin so staff can hand-adjust individual
installments after a RefundRestructure (Part III, Section 5(d)) changes the
schedule, or to correct/import historical data. This signal only handles
the common case — a clean schedule generated the moment Phase II starts.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .models import Project, RefundInstallment, ProjectRequirement


def add_months(d, months):
    """Add N months to a date, keeping day=1 (used for the 1st-of-month
    installment schedule). No external dependency needed for this."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return d.replace(year=year, month=month, day=1)


@receiver(pre_save, sender=Project)
def _cache_previous_status(sender, instance, **kwargs):
    """Stash the pre-save status on the instance so post_save can tell
    whether this save is the transition INTO phase_2, vs. just re-saving
    a project that's already in phase_2."""
    if instance.pk:
        try:
            instance._previous_status = Project.objects.get(pk=instance.pk).status
        except Project.DoesNotExist:
            instance._previous_status = None
    else:
        instance._previous_status = None


@receiver(post_save, sender=Project)
def _generate_refund_schedule(sender, instance, created, **kwargs):
    previous_status = getattr(instance, '_previous_status', None)
    entering_phase2 = (
        instance.status == Project.Status.PHASE_2
        and previous_status != Project.Status.PHASE_2
    )
    if not entering_phase2:
        return

    # Don't regenerate if a schedule already exists (e.g. status got bounced
    # back and forth, or this project was imported with installments already
    # in place) — avoids creating duplicate installment rows.
    if instance.refund_installments.exists():
        return

    if not (instance.refund_period_years and instance.refund_start_date and instance.total_ifund_amount):
        # Missing one of the required fields — nothing to generate from yet.
        # (Admin will need to fill these in and re-save to trigger this again,
        # or add installments manually.)
        return

    months = instance.refund_period_years * 12
    raw_monthly = instance.total_ifund_amount / months
    monthly_amount = raw_monthly.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    installments = []
    running_total = Decimal('0.00')
    for i in range(months):
        due_date = add_months(instance.refund_start_date, i)
        if i < months - 1:
            amount = monthly_amount
        else:
            # Last installment absorbs whatever rounding remainder is left,
            # so the schedule sums to exactly total_ifund_amount instead of
            # drifting by a few centavos over a 60-month schedule.
            amount = instance.total_ifund_amount - running_total
        running_total += amount
        installments.append(RefundInstallment(project=instance, due_date=due_date, amount_due=amount))

    RefundInstallment.objects.bulk_create(installments)


@receiver(post_save, sender=Project)
def create_default_requirements(sender, instance, created, **kwargs):
    """
    The moment a new Project is saved for the first time, populate its
    requirements checklist with the Phase I gate — the application/
    eligibility documents (Letter of Intent, TNA forms, DTI/SEC/CDA
    registration, financial statements, etc., per Part II Section 4) that
    must be submitted before the project can move from "pending" into
    Phase I. The Phase II and Completion gates get created later, by
    create_next_phase_requirements below, once they actually become
    relevant.
    """
    if not created:
        return

    ProjectRequirement.objects.bulk_create([
        ProjectRequirement(project=instance, requirement_type=req_type, phase=Project.Status.PHASE_1)
        for req_type in ProjectRequirement.REQUIRED_FOR[Project.Status.PHASE_1]
    ])


# Entering this status -> pre-create the checklist that gates the NEXT one.
# e.g. the moment a project enters Phase I, the Phase II gate (completion +
# financial report) gets created, so it's already there and ready to be
# filled in well before anyone needs to check it.
NEXT_GATE_ON_ENTERING = {
    Project.Status.PHASE_1: Project.Status.PHASE_2,
    Project.Status.PHASE_2: Project.Status.COMPLETED,
}


@receiver(post_save, sender=Project)
def create_next_phase_requirements(sender, instance, created, **kwargs):
    previous_status = getattr(instance, '_previous_status', None)
    if instance.status == previous_status:
        return  # not a status change at all — nothing to do

    target_phase = NEXT_GATE_ON_ENTERING.get(instance.status)
    if not target_phase:
        return

    # Only create rows that don't already exist for this gate — keeps this
    # safe to fire more than once (status bouncing back and forth, re-saves,
    # imported data that already has some rows in place, etc.).
    existing_types = set(
        instance.requirements.filter(phase=target_phase).values_list('requirement_type', flat=True)
    )
    needed_types = ProjectRequirement.REQUIRED_FOR.get(target_phase, [])
    to_create = [t for t in needed_types if t not in existing_types]

    if to_create:
        ProjectRequirement.objects.bulk_create([
            ProjectRequirement(project=instance, requirement_type=t, phase=target_phase)
            for t in to_create
        ])