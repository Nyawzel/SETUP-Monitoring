"""
monitoring/report_exports.py

Builds the three PDF exports available from the Impact KPI tab's Export
panel:
  - Quarterly Impact Report   (ImpactRecord rows, office-wide)
  - Project Details Report    (one Project, everything about it)
  - Dashboard KPI Snapshot    (the same numbers shown on the Dashboard)

pandas does the aggregating/tabulating (sums, per-row formatting);
reportlab (platypus) lays out the actual PDF. Kept in its own module so
views.py doesn't get crowded with layout code.

Requires: pip install pandas reportlab
"""

import io
from datetime import date

import pandas as pd
from dateutil.relativedelta import relativedelta
from django.contrib.staticfiles import finders
from django.db.models import Sum
from django.utils import timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)

from .models import Project, RefundPayment, RefundInstallment, BudgetLineItem, ImpactRecord


# ---------------------------------------------------------------------------
# Corporate letterhead — EDIT THESE to match your PSTO exactly.
# ---------------------------------------------------------------------------

# Centered office-identity block at the top of every page. Add/remove lines
# as needed — each string in the list is its own centered line.
PSTO_OFFICE_LINES = [
    "Republic of the Philippines",
    "Department of Science and Technology",
    "Region XII (SOCCSKSARGEN)",
    "Provincial Science and Technology Office – Sarangani Province and General Santos City",
]

# Single line in the footer — address / phone / email, as used on the
# PSTO SarGen office's own letterhead/Facebook page. Verify/update if the
# office has since moved or changed contact details.
PSTO_CONTACT_LINE = (
    "Barangay Hall Compound, Calumpang, General Santos City 9500  |  "
    "Tel/Fax: (083) 554-7997  |  Email: pstc_sargen@region12.dost.gov.ph"
)

# These match what base.html already uses for the top nav (DOST logo /
# partner logo) — update the filenames here if your actual DOST and
# ONEDOST/ONEPilipinas logo files are named differently.
DOST_LOGO_STATIC_PATH = 'monitoring/images/image-30.png'
PARTNER_LOGO_STATIC_PATH = 'monitoring/images/image-40.png'


def _find_static(path):
    """Resolve a Django static file to an absolute filesystem path so
    reportlab can draw it directly with drawImage(). Returns None (rather
    than raising) if the file can't be found, so a missing/renamed logo
    just quietly skips that image instead of crashing the whole PDF."""
    try:
        return finders.find(path)
    except Exception:
        return None


_DOST_LOGO_PATH = _find_static(DOST_LOGO_STATIC_PATH)
_PARTNER_LOGO_PATH = _find_static(PARTNER_LOGO_STATIC_PATH)


def _draw_letterhead(canvas_obj, doc):
    """Draws the header (logos + centered office block + rule) and footer
    (rule + contact line) on every page. Registered with SimpleDocTemplate
    as onFirstPage/onLaterPages in _build() below. Does NOT draw the page
    number itself — that's handled by _NumberedCanvas, which only knows
    the true total page count once the whole document has been built."""
    canvas_obj.saveState()
    width, height = letter

    # --- Header logos ---
    logo_h = 0.5 * inch
    if _DOST_LOGO_PATH:
        canvas_obj.drawImage(
            _DOST_LOGO_PATH, 0.6 * inch, height - 0.95 * inch,
            height=logo_h, preserveAspectRatio=True, mask='auto',
        )
    if _PARTNER_LOGO_PATH:
        logo_w = 0.9 * inch
        canvas_obj.drawImage(
            _PARTNER_LOGO_PATH, width - 0.6 * inch - logo_w, height - 0.95 * inch,
            width=logo_w, height=logo_h, preserveAspectRatio=True, mask='auto',
        )

    # --- Header text (centered office block) ---
    canvas_obj.setFont('Helvetica-Bold', 9)
    canvas_obj.setFillColor(colors.HexColor('#1e3466'))
    text_y = height - 0.45 * inch
    for line in PSTO_OFFICE_LINES:
        canvas_obj.drawCentredString(width / 2, text_y, line)
        text_y -= 11

    # --- Header rule ---
    canvas_obj.setStrokeColor(colors.HexColor('#09ACED'))
    canvas_obj.setLineWidth(1.2)
    canvas_obj.line(0.6 * inch, height - 1.1 * inch, width - 0.6 * inch, height - 1.1 * inch)

    # --- Footer rule + contact line ---
    canvas_obj.setStrokeColor(colors.HexColor('#dcdcdc'))
    canvas_obj.setLineWidth(0.5)
    canvas_obj.line(0.6 * inch, 0.65 * inch, width - 0.6 * inch, 0.65 * inch)

    canvas_obj.setFont('Helvetica', 7)
    canvas_obj.setFillColor(colors.HexColor('#8a8a8a'))
    canvas_obj.drawString(0.6 * inch, 0.5 * inch, PSTO_CONTACT_LINE)

    canvas_obj.restoreState()


class _NumberedCanvas(Canvas):
    """Prints 'Page X of Y' in the footer. A plain Canvas only knows the
    CURRENT page number as each page is drawn — it can't know the total
    until every page has been built. This buffers every page's drawing
    commands via showPage(), then replays them at save() time once the
    real total is known, stamping the page number in on each pass."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_page_number(total_pages)
            super().showPage()
        super().save()

    def _draw_page_number(self, total_pages):
        width, _ = letter
        self.setFont('Helvetica', 7)
        self.setFillColor(colors.HexColor('#8a8a8a'))
        self.drawRightString(width - 0.6 * inch, 0.5 * inch, f"Page {self._pageNumber} of {total_pages}")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _php(amount):
    """Format a Decimal/None/float as 'PHP 1,234.56'. Deliberately NOT the
    unicode peso sign (₱) — reportlab's built-in base14 fonts don't include
    that glyph, so it would render as a solid black box instead of text."""
    if amount is None:
        amount = 0
    return f"PHP {float(amount):,.2f}"


def _date(d):
    return d.strftime("%b %d, %Y") if d else "—"


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='DostTitle', parent=styles['Title'], textColor=colors.HexColor('#1e3466')))
    styles.add(ParagraphStyle(name='DostHeading', parent=styles['Heading2'], spaceBefore=14, spaceAfter=6, textColor=colors.HexColor('#1e3466')))
    styles.add(ParagraphStyle(name='DostSmall', parent=styles['Normal'], fontSize=8, textColor=colors.HexColor('#8a8a8a')))
    return styles


def _table(rows, col_widths=None, header=True):
    style_cmds = [
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dcdcdc')),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]
    if header:
        style_cmds += [
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F8FAFB')),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ]
    t = Table(rows, colWidths=col_widths, repeatRows=1 if header else 0)
    t.setStyle(TableStyle(style_cmds))
    return t


def _footer_note(styles):
    return Paragraph(
        f"Generated {timezone.now().strftime('%b %d, %Y %H:%M')} — DOST SETUP Projects Management System",
        styles['DostSmall'],
    )


def _build(story):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        # Top/bottom margins are wider than the side margins on purpose —
        # they need to clear the letterhead band drawn by _draw_letterhead
        # (header rule sits at height-1.1in, footer rule at 0.65in) so the
        # story content never overlaps the logos/office block/contact line.
        topMargin=1.3 * inch, bottomMargin=0.9 * inch,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
    )
    # Without onFirstPage/onLaterPages, _draw_letterhead is never actually
    # invoked by reportlab — SimpleDocTemplate only draws page decoration
    # you explicitly register here, so this is what puts the header/footer
    # on every page. canvasmaker=_NumberedCanvas is what adds "Page X of Y"
    # on top of that.
    doc.build(
        story,
        onFirstPage=_draw_letterhead,
        onLaterPages=_draw_letterhead,
        canvasmaker=_NumberedCanvas,
    )
    buffer.seek(0)
    return buffer.getvalue()


def _timeline_for_project(project):
    """Same event list as views._build_project_timeline — kept as its own
    small copy here (rather than importing from views) so this module has
    no dependency on views.py at all."""
    events = []
    events.append({"date": project.created_at.date(), "label": "Project record created"})
    if project.phase1_start_date:
        events.append({"date": project.phase1_start_date, "label": "Phase I (implementation) started"})
    if project.phase1_expected_end_date:
        events.append({"date": project.phase1_expected_end_date, "label": "Phase I expected end"})
    if project.phase1_actual_end_date:
        events.append({"date": project.phase1_actual_end_date, "label": "Phase I completed"})
    if project.refund_start_date:
        events.append({"date": project.refund_start_date, "label": "Phase II (refund period) started"})
    if project.termination_date:
        events.append({
            "date": project.termination_date,
            "label": f"Project {project.get_status_display()}"
                     + (f" — {project.get_termination_reason_display()}" if project.termination_reason else ""),
        })
    for report in project.monitoring_reports.all():
        if report.date_submitted:
            events.append({"date": report.date_submitted, "label": f"Submitted {report.get_report_type_display()}"})
    for payment in project.refund_payments.all():
        events.append({"date": payment.date_paid, "label": f"Refund payment received — {_php(payment.amount_paid)}"})
    for restructure in project.restructures.all():
        events.append({
            "date": restructure.requested_date,
            "label": f"Refund restructure requested ({restructure.get_ground_display()})"
                     + (" — approved" if restructure.approved else ""),
        })
    events.sort(key=lambda e: e["date"])
    return events


# ---------------------------------------------------------------------------
# 1. Quarterly Impact Report
# ---------------------------------------------------------------------------

def build_quarterly_impact_pdf(records_qs, year_filter, quarter_filter):
    styles = _styles()
    story = []
    quarter_labels = dict(ImpactRecord.Quarter.choices)

    scope = "All Quarters"
    if year_filter and quarter_filter:
        scope = f"{quarter_labels.get(quarter_filter, quarter_filter)} {year_filter}"
    elif year_filter:
        scope = f"Year {year_filter}"
    elif quarter_filter:
        scope = f"{quarter_labels.get(quarter_filter, quarter_filter)} (all years)"

    story.append(Paragraph("Quarterly Impact KPI Report", styles['DostTitle']))
    story.append(Paragraph(scope, styles['Heading2']))
    story.append(Spacer(1, 10))

    # pandas does the actual tabulating here: one DataFrame built straight
    # from the queryset, with a formatted "Quarter" column and a TOTAL row
    # computed via .sum() instead of accumulated by hand in a loop.
    records = list(records_qs.values(
        'year', 'quarter', 'entities_assisted', 'jobs_created',
        'technology_interventions', 'export_firms_assisted', 'gross_sales',
    ))

    if records:
        df = pd.DataFrame(records)
        df['gross_sales'] = df['gross_sales'].astype(float)
        df['Quarter'] = df.apply(lambda r: f"{quarter_labels.get(r['quarter'], r['quarter'])} {r['year']}", axis=1)

        rows = [["Quarter", "Entities Assisted", "Jobs Created", "Tech. Interventions", "Export Firms", "Gross Sales"]]
        for _, r in df.iterrows():
            rows.append([
                r['Quarter'], int(r['entities_assisted']), int(r['jobs_created']),
                int(r['technology_interventions']), int(r['export_firms_assisted']), _php(r['gross_sales']),
            ])

        totals = df[['entities_assisted', 'jobs_created', 'technology_interventions', 'export_firms_assisted', 'gross_sales']].sum()
        rows.append([
            "TOTAL", int(totals['entities_assisted']), int(totals['jobs_created']),
            int(totals['technology_interventions']), int(totals['export_firms_assisted']), _php(totals['gross_sales']),
        ])
        story.append(_table(rows, col_widths=[110, 90, 80, 100, 80, 90]))
    else:
        story.append(Paragraph("No Impact KPI entries match this selection.", styles['Normal']))

    story.append(Spacer(1, 16))
    story.append(_footer_note(styles))
    return _build(story)


# ---------------------------------------------------------------------------
# 2. Project Details Report
# ---------------------------------------------------------------------------

def build_project_detail_pdf(project):
    styles = _styles()
    story = []
    coop = project.cooperator

    story.append(Paragraph("SETUP iFund Project Report", styles['DostTitle']))
    story.append(Paragraph(project.title, styles['Heading2']))
    story.append(Paragraph(
        f"{coop.name} &middot; Code: {project.project_code or '—'} &middot; Status: {project.get_status_display()}",
        styles['Normal'],
    ))
    story.append(Spacer(1, 12))

    story.append(Paragraph("MSME Profile", styles['DostHeading']))
    story.append(_table([
        ["Business Type", coop.get_business_type_display()],
        ["Enterprise Category", coop.get_enterprise_category_display() or "—"],
        ["Development Stage", coop.get_development_stage_display() or "—"],
        ["Priority Sector", coop.priority_sector or "—"],
        ["Address", coop.address or "—"],
        ["Contact", f"{coop.contact_person or '—'} / {coop.contact_number or '—'} / {coop.email or '—'}"],
        ["DTI/SEC/CDA Reg. No.", coop.dti_sec_cda_registration_no or "—"],
        ["Filipino Ownership", f"{coop.filipino_ownership_percent}%"],
        ["Export Firm", "Yes" if coop.is_export_firm else "No"],
        ["Eligible for New iFund", "Yes" if coop.is_eligible_for_new_ifund else "No"],
    ], col_widths=[160, 340], header=False))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Funding Summary", styles['DostHeading']))
    story.append(_table([
        ["iFund Amount", _php(project.total_ifund_amount)],
        ["Total Refunded", _php(project.total_refunded)],
        ["Remaining Balance", _php(project.remaining_balance)],
        ["Refund Progress", f"{project.refund_progress_percent}%"],
        ["Refund Delinquent", "Yes" if project.is_refund_delinquent else "No"],
        ["Consecutive Missed Installments", str(project.consecutive_missed_months)],
        ["Jobs Created", str(project.jobs_created)],
    ], col_widths=[160, 340], header=False))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Phase Dates", styles['DostHeading']))
    story.append(_table([
        ["Phase I Start", _date(project.phase1_start_date)],
        ["Phase I Expected End", _date(project.phase1_expected_end_date)],
        ["Phase I Actual End", _date(project.phase1_actual_end_date)],
        ["Refund Period", f"{project.refund_period_years or '—'} years"],
        ["Refund Start", _date(project.refund_start_date)],
        ["Refund Extension", f"{project.refund_extension_years} years"],
    ], col_widths=[160, 340], header=False))
    story.append(Spacer(1, 10))

    # --- Budget line items — pandas computes the unexpended balance per
    # row and the subtotal row, rather than a hand-rolled running total. ---
    story.append(Paragraph("Approved Line-Item Budget", styles['DostHeading']))
    budget_rows = list(project.budget_items.all().values('description', 'category', 'approved_amount', 'disbursed_amount'))
    if budget_rows:
        bdf = pd.DataFrame(budget_rows)
        bdf['approved_amount'] = bdf['approved_amount'].astype(float)
        bdf['disbursed_amount'] = bdf['disbursed_amount'].astype(float)
        bdf['unexpended'] = bdf['approved_amount'] - bdf['disbursed_amount']
        category_labels = dict(BudgetLineItem._meta.get_field('category').choices)

        rows = [["Description", "Category", "Approved", "Disbursed", "Unexpended"]]
        for _, r in bdf.iterrows():
            rows.append([
                r['description'], category_labels.get(r['category'], r['category']),
                _php(r['approved_amount']), _php(r['disbursed_amount']), _php(r['unexpended']),
            ])
        totals = bdf[['approved_amount', 'disbursed_amount', 'unexpended']].sum()
        rows.append(["TOTAL", "", _php(totals['approved_amount']), _php(totals['disbursed_amount']), _php(totals['unexpended'])])
        story.append(_table(rows, col_widths=[150, 90, 90, 90, 90]))
    else:
        story.append(Paragraph("No budget line items recorded.", styles['Normal']))
    story.append(Spacer(1, 10))

    # --- Refund payments ---
    story.append(Paragraph("Refund Payments Received", styles['DostHeading']))
    payment_rows = list(
        project.refund_payments.all().order_by('date_paid').values('date_paid', 'amount_paid', 'or_number', 'remarks')
    )
    if payment_rows:
        pdf_df = pd.DataFrame(payment_rows)
        rows = [["Date Paid", "Amount", "O.R. Number", "Remarks"]]
        for _, r in pdf_df.iterrows():
            rows.append([_date(r['date_paid']), _php(r['amount_paid']), r['or_number'] or "—", r['remarks'] or "—"])
        rows.append(["TOTAL", _php(pdf_df['amount_paid'].astype(float).sum()), "", ""])
        story.append(_table(rows, col_widths=[90, 90, 110, 130]))
    else:
        story.append(Paragraph("No refund payments recorded.", styles['Normal']))
    story.append(Spacer(1, 10))

    # --- Requirements checklist ---
    story.append(Paragraph("Application Requirements", styles['DostHeading']))
    req_rows = [["Requirement", "Submitted", "Date Submitted"]]
    for req in project.requirements.all():
        req_rows.append([req.get_requirement_type_display(), "Yes" if req.is_submitted else "No", _date(req.date_submitted)])
    if len(req_rows) == 1:
        req_rows.append(["No requirements set up.", "", ""])
    story.append(_table(req_rows, col_widths=[300, 100, 120]))
    story.append(Spacer(1, 10))

    # --- Monitoring reports ---
    story.append(Paragraph("Monitoring Reports", styles['DostHeading']))
    rep_rows = [["Report", "Due Date", "Date Submitted", "Overdue"]]
    for rep in project.monitoring_reports.all():
        rep_rows.append([rep.get_report_type_display(), _date(rep.due_date), _date(rep.date_submitted), "Yes" if rep.is_overdue else "No"])
    if len(rep_rows) == 1:
        rep_rows.append(["No monitoring reports scheduled.", "", "", ""])
    story.append(_table(rep_rows, col_widths=[260, 90, 90, 80]))
    story.append(Spacer(1, 10))

    # --- Equipment ---
    story.append(Paragraph("Equipment", styles['DostHeading']))
    eq_rows = [["Item", "Supplier", "Cost", "Status"]]
    for item in project.equipment.all():
        eq_rows.append([item.name, item.supplier_fabricator or "—", _php(item.acquisition_cost), item.get_status_display()])
    if len(eq_rows) == 1:
        eq_rows.append(["No equipment recorded.", "", "", ""])
    story.append(_table(eq_rows, col_widths=[200, 150, 90, 80]))
    story.append(Spacer(1, 10))

    if project.restructures.exists():
        story.append(Paragraph("Refund Restructures", styles['DostHeading']))
        rs_rows = [["Ground", "Requested", "Approved"]]
        for rs in project.restructures.all():
            rs_rows.append([rs.get_ground_display(), _date(rs.requested_date), "Yes" if rs.approved else "No"])
        story.append(_table(rs_rows, col_widths=[260, 130, 130]))
        story.append(Spacer(1, 10))

    story.append(PageBreak())
    story.append(Paragraph("Timeline", styles['DostHeading']))
    tl_rows = [["Date", "Event"]]
    for event in _timeline_for_project(project):
        tl_rows.append([_date(event["date"]), event["label"]])
    if len(tl_rows) == 1:
        tl_rows.append(["No timeline events.", ""])
    story.append(_table(tl_rows, col_widths=[100, 420]))

    story.append(Spacer(1, 16))
    story.append(_footer_note(styles))
    return _build(story)


# ---------------------------------------------------------------------------
# 3. Funding Details Report
# ---------------------------------------------------------------------------

def build_funding_details_pdf(budget_items_qs, payments_qs, item_query='', category_filter='', payment_query=''):
    """
    Mirrors the Funding page: program-wide Fund Supervision totals (always
    unfiltered, same numbers as the page's summary cards), followed by the
    Approved Line-Item Budget and Refund Payment ledgers — each honoring
    whatever search/category filter is currently applied on that page
    (the view passes in the already-filtered querysets).
    """
    styles = _styles()
    story = []

    story.append(Paragraph("Funding Details Report", styles['DostTitle']))
    story.append(Paragraph(date.today().strftime("%B %d, %Y"), styles['Heading2']))
    story.append(Spacer(1, 12))

    # --- Fund Supervision summary — program-wide, same as the Funding
    # page's KPI cards (not affected by the ledger filters below). ---
    disbursed_projects = Project.objects.exclude(status=Project.Status.PENDING)
    total_disbursed = disbursed_projects.aggregate(total=Sum('total_ifund_amount'))['total'] or 0
    total_refunded = RefundPayment.objects.filter(
        project__in=disbursed_projects
    ).aggregate(total=Sum('amount_paid'))['total'] or 0
    outstanding_balance = total_disbursed - total_refunded
    collection_rate = round((total_refunded / total_disbursed) * 100, 1) if total_disbursed else 0
    total_approved_budget = BudgetLineItem.objects.aggregate(total=Sum('approved_amount'))['total'] or 0
    total_disbursed_budget = BudgetLineItem.objects.aggregate(total=Sum('disbursed_amount'))['total'] or 0

    story.append(Paragraph("Fund Supervision", styles['DostHeading']))
    story.append(_table([
        ["Metric", "Amount"],
        ["Total Disbursed", _php(total_disbursed)],
        ["Total Refunded", _php(total_refunded)],
        ["Outstanding Balance", _php(outstanding_balance)],
        ["Collection Rate", f"{collection_rate}%"],
        ["Approved Line-Item Budget", _php(total_approved_budget)],
        ["Budget Disbursed", _php(total_disbursed_budget)],
    ], col_widths=[300, 200]))
    story.append(Spacer(1, 10))

    # --- Approved Line-Item Budget (respects item_q / category filters) ---
    category_labels = dict(BudgetLineItem._meta.get_field('category').choices)
    scope_bits = []
    if item_query:
        scope_bits.append(f'Search: "{item_query}"')
    if category_filter:
        scope_bits.append(f'Category: {category_labels.get(category_filter, category_filter)}')
    budget_scope = " · ".join(scope_bits) if scope_bits else "All budget items"

    story.append(Paragraph("Approved Line-Item Budget", styles['DostHeading']))
    story.append(Paragraph(budget_scope, styles['DostSmall']))
    story.append(Spacer(1, 4))

    budget_rows = list(budget_items_qs.values(
        'project__title', 'description', 'category', 'approved_amount', 'disbursed_amount',
    ))
    if budget_rows:
        bdf = pd.DataFrame(budget_rows)
        bdf['approved_amount'] = bdf['approved_amount'].astype(float)
        bdf['disbursed_amount'] = bdf['disbursed_amount'].astype(float)
        bdf['unexpended'] = bdf['approved_amount'] - bdf['disbursed_amount']

        rows = [["Project", "Description", "Category", "Approved", "Disbursed", "Unexpended"]]
        for _, r in bdf.iterrows():
            rows.append([
                r['project__title'], r['description'], category_labels.get(r['category'], r['category']),
                _php(r['approved_amount']), _php(r['disbursed_amount']), _php(r['unexpended']),
            ])
        totals = bdf[['approved_amount', 'disbursed_amount', 'unexpended']].sum()
        rows.append([
            "TOTAL", "", "", _php(totals['approved_amount']), _php(totals['disbursed_amount']), _php(totals['unexpended']),
        ])
        story.append(_table(rows, col_widths=[100, 130, 75, 75, 75, 75]))
    else:
        story.append(Paragraph("No budget line items match this selection.", styles['Normal']))
    story.append(Spacer(1, 10))

    # --- Refund Payment ledger (respects payment_q filter; page itself
    # only shows the most recent 50, so the export matches what's shown) ---
    story.append(Paragraph("Refund Payment Ledger", styles['DostHeading']))
    story.append(Paragraph(
        f'Search: "{payment_query}"' if payment_query else "Most recent payments",
        styles['DostSmall'],
    ))
    story.append(Spacer(1, 4))

    payment_rows = list(payments_qs.values(
        'date_paid', 'project__title', 'project__cooperator__name', 'amount_paid', 'or_number',
    ))
    if payment_rows:
        pdf_df = pd.DataFrame(payment_rows)
        rows = [["Date Paid", "MSME", "Project", "Amount", "O.R. Number"]]
        for _, r in pdf_df.iterrows():
            rows.append([
                _date(r['date_paid']), r['project__cooperator__name'], r['project__title'],
                _php(r['amount_paid']), r['or_number'] or "—",
            ])
        rows.append(["TOTAL", "", "", _php(pdf_df['amount_paid'].astype(float).sum()), ""])
        story.append(_table(rows, col_widths=[70, 110, 150, 90, 80]))
    else:
        story.append(Paragraph("No refund payments match this selection.", styles['Normal']))

    story.append(Spacer(1, 16))
    story.append(_footer_note(styles))
    return _build(story)


# ---------------------------------------------------------------------------
# 4. Dashboard KPI Snapshot
# ---------------------------------------------------------------------------

def build_dashboard_kpi_pdf():
    styles = _styles()
    story = []

    story.append(Paragraph("Dashboard KPI Snapshot", styles['DostTitle']))
    story.append(Paragraph(date.today().strftime("%B %d, %Y"), styles['Heading2']))
    story.append(Spacer(1, 12))

    projects = Project.objects.all()
    total_projects = projects.count()
    phase1_count = projects.filter(status=Project.Status.PHASE_1).count()
    phase2_projects = projects.filter(status=Project.Status.PHASE_2)
    phase2_count = phase2_projects.count()
    completed_count = projects.filter(status=Project.Status.COMPLETED).count()
    pending_count = projects.filter(status=Project.Status.PENDING).count()
    terminated_withdrawn_count = projects.filter(
        status__in=[Project.Status.TERMINATED, Project.Status.WITHDRAWN]
    ).count()
    delinquent_projects = [p for p in phase2_projects if p.is_refund_delinquent]
    delinquent_count = len(delinquent_projects)

    story.append(Paragraph("Project Status", styles['DostHeading']))
    story.append(_table([
        ["Status", "Count"],
        ["Phase I", phase1_count],
        ["Phase II", phase2_count],
        ["Completed", completed_count],
        ["Pending", pending_count],
        ["Delinquent", delinquent_count],
        ["Terminated/Withdrawn", terminated_withdrawn_count],
        ["TOTAL", total_projects],
    ], col_widths=[300, 200]))
    story.append(Spacer(1, 10))

    disbursed_projects = projects.exclude(status=Project.Status.PENDING)
    total_disbursed = disbursed_projects.aggregate(total=Sum('total_ifund_amount'))['total'] or 0
    total_refunded = RefundPayment.objects.filter(project__in=disbursed_projects).aggregate(total=Sum('amount_paid'))['total'] or 0
    outstanding_balance = total_disbursed - total_refunded
    collection_rate = round((total_refunded / total_disbursed) * 100, 1) if total_disbursed else 0
    total_approved_budget = BudgetLineItem.objects.aggregate(total=Sum('approved_amount'))['total'] or 0
    total_disbursed_budget = BudgetLineItem.objects.aggregate(total=Sum('disbursed_amount'))['total'] or 0

    story.append(Paragraph("Fund Supervision", styles['DostHeading']))
    story.append(_table([
        ["Metric", "Amount"],
        ["Total Disbursed", _php(total_disbursed)],
        ["Total Refunded", _php(total_refunded)],
        ["Outstanding Balance", _php(outstanding_balance)],
        ["Collection Rate", f"{collection_rate}%"],
        ["Approved Line-Item Budget", _php(total_approved_budget)],
        ["Budget Disbursed", _php(total_disbursed_budget)],
    ], col_widths=[300, 200]))
    story.append(Spacer(1, 10))

    # --- Refund collection, due vs. collected, last 12 months — same
    # window as the dashboard's bar chart, built as a DataFrame so the
    # per-month figures and the TOTAL row come from one .sum() call
    # instead of two hand-accumulated running totals. ---
    story.append(Paragraph("Refund Collection — Due vs. Collected (Last 12 Months)", styles['DostHeading']))
    today = date.today()
    months = [(today.replace(day=1) - relativedelta(months=i)) for i in range(11, -1, -1)]
    month_rows = []
    for month_start in months:
        month_end = month_start + relativedelta(months=1)
        due = RefundInstallment.objects.filter(
            due_date__gte=month_start, due_date__lt=month_end
        ).aggregate(total=Sum('amount_due'))['total'] or 0
        collected = RefundPayment.objects.filter(
            date_paid__gte=month_start, date_paid__lt=month_end
        ).aggregate(total=Sum('amount_paid'))['total'] or 0
        month_rows.append({"Month": month_start.strftime("%b %Y"), "Due": float(due), "Collected": float(collected)})

    mdf = pd.DataFrame(month_rows)
    rows = [["Month", "Due", "Collected"]]
    for _, r in mdf.iterrows():
        rows.append([r['Month'], _php(r['Due']), _php(r['Collected'])])
    rows.append(["TOTAL", _php(mdf['Due'].sum()), _php(mdf['Collected'].sum())])
    story.append(_table(rows, col_widths=[150, 175, 175]))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Watchlist — Top Delinquent MSMEs", styles['DostHeading']))
    watchlist = sorted(delinquent_projects, key=lambda p: p.remaining_balance, reverse=True)[:10]
    wl_rows = [["MSME", "Project", "Months Overdue", "Outstanding Balance"]]
    for p in watchlist:
        wl_rows.append([p.cooperator.name, p.title, str(p.consecutive_missed_months), _php(p.remaining_balance)])
    if len(wl_rows) == 1:
        wl_rows.append(["No delinquent MSMEs right now.", "", "", ""])
    story.append(_table(wl_rows, col_widths=[150, 190, 100, 110]))

    story.append(Spacer(1, 16))
    story.append(_footer_note(styles))
    return _build(story)