from django.urls import path

from . import views

app_name = "bulkregister"

urlpatterns = [
    path("", views.index, name="index"),
    path("register/", views.register_all, name="register_all"),
    path("login-both/", views.login_both, name="login_both"),
]
