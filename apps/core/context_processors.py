from django.conf import settings
from django.urls import NoReverseMatch, reverse

LOGICAL_BACK_PARENTS = {
    "reports:view": "reports:index",
    "catalog:product_create": "catalog:product_list",
    "catalog:product_import": "catalog:product_list",
    "catalog:product_detail": "catalog:product_list",
    "catalog:product_edit": "catalog:product_list",
    "catalog:product_labels": "catalog:product_list",
    "catalog:variant_create": "catalog:product_list",
    "catalog:variant_edit": "catalog:product_list",
    "catalog:brand_list": "catalog:category_list",
    "catalog:unit_list": "catalog:category_list",
    "catalog:tax_list": "catalog:category_list",
    "customers:create": "customers:list",
    "customers:import": "customers:list",
    "customers:detail": "customers:list",
    "customers:edit": "customers:list",
    "customers:payment": "customers:list",
    "customers:statement": "customers:list",
    "suppliers:create": "suppliers:list",
    "suppliers:detail": "suppliers:list",
    "suppliers:edit": "suppliers:list",
    "purchases:create": "purchases:list",
    "purchases:detail": "purchases:list",
    "purchases:share": "purchases:list",
    "purchases:receive": "purchases:list",
    "purchases:pay": "purchases:list",
    "purchases:return": "purchases:list",
    "sales:detail": "sales:list",
    "sales:return_create": "sales:return_list",
    "expenses:create": "expenses:list",
    "expenses:edit": "expenses:list",
    "expenses:categories": "expenses:list",
    "expenses:recurring_list": "expenses:list",
    "expenses:recurring_create": "expenses:list",
    "expenses:recurring_edit": "expenses:list",
    "registers:shift_open": "registers:shift_list",
    "registers:register_create": "registers:shift_list",
    "registers:register_edit": "registers:shift_list",
    "registers:shift_detail": "registers:shift_list",
    "registers:shift_close": "registers:shift_list",
    "branches:branch_create": "branches:list",
    "branches:branch_edit": "branches:list",
    "branches:warehouse_create": "branches:list",
    "branches:warehouse_edit": "branches:list",
    "inventory:import": "inventory:stock_list",
    "inventory:movement_list": "inventory:stock_list",
    "inventory:transfer_list": "inventory:stock_list",
    "inventory:transfer_create": "inventory:stock_list",
    "inventory:adjustment_list": "inventory:stock_list",
    "inventory:adjustment_create": "inventory:stock_list",
    "inventory:count_list": "inventory:stock_list",
    "inventory:count_detail": "inventory:stock_list",
    "platformadmin:business_create": "platformadmin:business_list",
    "platformadmin:business_detail": "platformadmin:business_list",
    "platformadmin:plan_create": "platformadmin:plan_list",
    "platformadmin:plan_edit": "platformadmin:plan_list",
    # WMS: forms and detail pages return to their parent list screens.
    "wms:order_create_batch": "wms:order_list",
    "wms:order_finish_batch": "wms:order_list",
    "wms:order_detail": "wms:order_list",
    "wms:alteration_create": "wms:alteration_list",
    "wms:alteration_detail": "wms:alteration_list",
    "wms:alteration_edit": "wms:alteration_list",
    "wms:attendance_create": "wms:attendance_list",
    "wms:attendance_detail": "wms:attendance_list",
    "wms:attendance_correct": "wms:attendance_list",
    "wms:production_entry_create": "wms:production_entry_list",
    "wms:production_entry_detail": "wms:production_entry_list",
    "wms:production_entry_correct": "wms:production_entry_list",
    "wms:salary_calculate": "wms:salary_list",
    "wms:salary_detail": "wms:salary_list",
    "wms:employee_create": "wms:employee_list",
    "wms:employee_detail": "wms:employee_list",
    "wms:employee_edit": "wms:employee_list",
    "wms:category_create": "wms:category_list",
    "wms:category_edit": "wms:category_list",
    "wms:location_create": "wms:settings",
    "wms:location_edit": "wms:settings",
    "wms:user_access_create": "wms:user_list",
    "wms:user_access_edit": "wms:user_list",
    "wms:user_create": "wms:user_list",
    "wms:user_edit": "wms:user_list",
    "wms:role_create": "wms:user_list",
    "wms:role_edit": "wms:user_list",
    "wms:report_daily_production": "wms:report_index",
    "wms:report_daily_finished": "wms:report_index",
    "wms:report_monthly_production": "wms:report_index",
    "wms:report_attendance_summary": "wms:report_index",
    "wms:report_individual_attendance": "wms:report_index",
    "wms:report_salary": "wms:report_index",
    "wms:report_daily_orders": "wms:report_index",
    "wms:report_monthly_orders": "wms:report_index",
    "wms:report_daily_alterations": "wms:report_index",
    "wms:report_monthly_alterations": "wms:report_index",
}


def logical_back_navigation(request):
    resolver_match = getattr(request, "resolver_match", None)
    view_name = resolver_match.view_name if resolver_match else ""
    parent_view_name = LOGICAL_BACK_PARENTS.get(view_name)
    if not parent_view_name:
        return {"back_enabled": False, "back_url": ""}

    try:
        back_url = reverse(parent_view_name)
    except NoReverseMatch:
        return {"back_enabled": False, "back_url": ""}
    return {"back_enabled": True, "back_url": back_url}


def platform_branding(request):
    return {
        "PRODUCT_NAME": settings.PRODUCT_NAME,
        "PLATFORM_SUPPORT_EMAIL": settings.PLATFORM_SUPPORT_EMAIL,
        "PLATFORM_WEBSITE": settings.PLATFORM_WEBSITE,
        "PLATFORM_TERMS_URL": settings.PLATFORM_TERMS_URL,
        "PLATFORM_PRIVACY_URL": settings.PLATFORM_PRIVACY_URL,
        "PLATFORM_PRIMARY_COLOR": settings.PLATFORM_PRIMARY_COLOR,
    }


def business_context(request):
    business = getattr(request, "business", None)
    membership = getattr(request, "membership", None)
    perms = membership.permission_set if membership else set()
    unread = 0
    if business and membership:
        from apps.notifications.models import Notification

        unread = Notification.objects.for_business(business).filter(
            recipient=request.user, is_read=False
        ).count()
    return {
        "current_business": business,
        "current_membership": membership,
        "business_perms": perms,
        "unread_notifications": unread,
    }
