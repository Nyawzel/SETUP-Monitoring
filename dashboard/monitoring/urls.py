from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('projects/', views.project_list, name='project_list'),
    path('projects/new/', views.project_create, name='project_create'),
    path('projects/import/', views.import_excel, name='project_import'),
    path('projects/<int:pk>/', views.project_detail, name='project_detail'),
    path('msmes/', views.msme_list, name='msme_list'),
    path('msmes/new/', views.msme_create, name='msme_create'),
    path('msmes/<int:pk>/', views.msme_detail, name='msme_detail'),
    path('msmes/<int:pk>/edit/', views.msme_edit, name='msme_edit'),
    path('funding/', views.funding_overview, name='funding_overview'),
    path('funding/budget-item/new/', views.budget_item_create, name='budget_item_create'),
    path('funding/budget-item/<int:pk>/edit/', views.budget_item_edit, name='budget_item_edit'),
    path('projects/<int:project_pk>/budget-item/new/', views.budget_item_create, name='project_budget_item_create'),
    path('funding/payment/new/', views.refund_payment_create, name='refund_payment_create'),
    path('funding/payment/<int:pk>/edit/', views.refund_payment_edit, name='refund_payment_edit'),
    path('projects/<int:project_pk>/payment/new/', views.refund_payment_create, name='project_payment_create'),
    path('projects/<int:project_pk>/equipment/new/', views.equipment_create, name='equipment_create'),
    path('profile/', views.profile, name='profile'),
    path('reports/', views.impact_kpi, name='reports'),
    path('reports/export/', views.export_report_pdf, name='export_report_pdf'),
    path('settings/', views.settings_view, name='settings'),
]