from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.profile_view, name="profile"),
    path("password/change/", views.change_password_view, name="change_password"),
    path("password/reset/", views.PasswordResetView.as_view(), name="password_reset"),
    path("password/reset/done/", views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("password/reset/<uidb64>/<token>/", views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("password/reset/complete/", views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("users/", views.user_list, name="user_list"),
    path("users/new/", views.user_create, name="user_create"),
    path("users/<uuid:public_id>/edit/", views.user_edit, name="user_edit"),
    path("roles/", views.role_list, name="role_list"),
    path("roles/new/", views.role_form, name="role_create"),
    path("roles/<uuid:public_id>/edit/", views.role_form, name="role_edit"),
]
