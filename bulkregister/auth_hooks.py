from django.utils.translation import gettext_lazy as _

from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

from . import urls
from .views import available_targets


class BulkRegisterMenu(MenuItemHook):
    def __init__(self):
        super().__init__(
            _("Bulk Register"),
            "fas fa-user-plus fa-fw",
            "bulkregister:index",
            navactive=["bulkregister:"],
        )

    def render(self, request):
        if request.user.is_authenticated and available_targets(request.user):
            return MenuItemHook.render(self, request)
        return ""


@hooks.register("menu_item_hook")
def register_menu():
    return BulkRegisterMenu()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "bulkregister", r"^bulkregister/")
