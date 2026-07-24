from django.contrib import admin

from .models import WmsLocation, WmsRole, WmsSettings, WmsUserAccess

admin.site.register(WmsLocation)
admin.site.register(WmsSettings)
admin.site.register(WmsRole)
admin.site.register(WmsUserAccess)
