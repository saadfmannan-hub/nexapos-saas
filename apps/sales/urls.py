from django.urls import path

from . import views

app_name = "sales"

urlpatterns = [
    path("pos/", views.pos_view, name="pos"),
    path("pos/products/", views.pos_products, name="pos_products"),
    path("pos/barcode/", views.pos_barcode, name="pos_barcode"),
    path("pos/customers/", views.pos_customers, name="pos_customers"),
    path("pos/customers/quick/", views.pos_quick_customer, name="pos_quick_customer"),
    path("pos/checkout/", views.pos_checkout, name="pos_checkout"),
    path("pos/hold/", views.pos_hold, name="pos_hold"),
    path("pos/held/", views.pos_held_list, name="pos_held_list"),
    path("pos/held/<int:pk>/delete/", views.pos_held_delete, name="pos_held_delete"),
    path("", views.sale_list, name="list"),
    path("returns/", views.return_list, name="return_list"),
    path("<uuid:public_id>/", views.sale_detail, name="detail"),
    path("<uuid:public_id>/invoice/", views.sale_invoice, name="invoice"),
    path("<uuid:public_id>/invoice.pdf", views.sale_invoice_pdf, name="invoice_pdf"),
    path("<uuid:public_id>/workshop-job-card.pdf", views.sale_workshop_job_card_pdf, name="workshop_job_card_pdf"),
    path("<uuid:public_id>/items/<int:item_id>/workshop-job-card.pdf", views.sale_item_workshop_job_card_pdf, name="sale_item_workshop_job_card_pdf"),
    path("<uuid:public_id>/receipt/", views.sale_receipt, name="receipt"),
    path("<uuid:public_id>/void/", views.sale_void, name="void"),
    path("<uuid:public_id>/payments/add/", views.sale_payment_add, name="payment_add"),
    path("<uuid:public_id>/delete/", views.sale_delete, name="delete"),
    path("<uuid:public_id>/delivery/", views.sale_set_delivery, name="set_delivery"),
    path("<uuid:public_id>/return/", views.return_create, name="return_create"),
]
