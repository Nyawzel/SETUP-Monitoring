"""
monitoring/management/commands/backfill_requirements.py

One-time fix-up for projects that predate the phase-gating requirements
system, or that have moved through statuses without ever getting a given
gate's checklist created for them. Safe to run more than once — it only
ever adds rows that don't already exist.

    python manage.py backfill_requirements
"""

from django.core.management.base import BaseCommand
from monitoring.models import Project, ProjectRequirement


# Every status a project has already reached (or passed through) implies
# that gate's checklist should exist — e.g. a project sitting in phase_2
# should have BOTH its phase_1 gate (already cleared, presumably) and its
# phase_2 gate present, since it needed to clear phase_1's to get there.
GATES_IMPLIED_BY_STATUS = {
    Project.Status.PENDING: [Project.Status.PHASE_1],
    Project.Status.PHASE_1: [Project.Status.PHASE_1, Project.Status.PHASE_2],
    Project.Status.PHASE_2: [Project.Status.PHASE_1, Project.Status.PHASE_2, Project.Status.COMPLETED],
    Project.Status.COMPLETED: [Project.Status.PHASE_1, Project.Status.PHASE_2, Project.Status.COMPLETED],
    Project.Status.TERMINATED: [Project.Status.PHASE_1, Project.Status.PHASE_2],
    Project.Status.WITHDRAWN: [Project.Status.PHASE_1, Project.Status.PHASE_2],
}


class Command(BaseCommand):
    help = "Backfills the phase-gated requirements checklist onto existing projects."

    def handle(self, *args, **options):
        updated = 0

        for project in Project.objects.all():
            gates_needed = GATES_IMPLIED_BY_STATUS.get(project.status, [Project.Status.PHASE_1])
            existing_types = set(project.requirements.values_list('requirement_type', flat=True))

            to_create = []
            for gate in gates_needed:
                for req_type in ProjectRequirement.REQUIRED_FOR.get(gate, []):
                    if req_type not in existing_types:
                        to_create.append(ProjectRequirement(project=project, requirement_type=req_type, phase=gate))
                        existing_types.add(req_type)  # avoid double-adding if a type somehow appears in 2 gates

            if to_create:
                ProjectRequirement.objects.bulk_create(to_create)
                updated += 1
                self.stdout.write(f"  + {project} — added {len(to_create)} requirement row(s)")

        if updated:
            self.stdout.write(self.style.SUCCESS(f"Done — updated {updated} project(s)."))
        else:
            self.stdout.write(self.style.SUCCESS("Every project already has the checklists it needs. Nothing to do."))