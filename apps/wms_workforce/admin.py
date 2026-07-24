from django.contrib import admin

from .models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
    WmsProductionCategory,
)

admin.site.register(WmsEmployee)
admin.site.register(WmsProductionCategory)
admin.site.register(WmsEmployeeCategoryAssignment)
