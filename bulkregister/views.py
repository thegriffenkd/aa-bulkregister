"""Bulk-register characters that already have valid ESI tokens.

Mirrors what Member Audit's `add_character` and CorpTools' `add_char` views do
after the "Select Character" page, but for all eligible characters at once.
"""

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from allianceauth.services.hooks import get_extension_logger
from esi.models import Token

logger = get_extension_logger(__name__)

# Seconds between queued update tasks, so 70 characters don't hit ESI at once.
STAGGER_SECONDS = 2


# ---------------------------------------------------------------- targets


class Target:
    key = ""
    label = ""
    app_label = ""
    permission = None  # None = login only (same as the app's own add view)

    @classmethod
    def installed(cls):
        return apps.is_installed(cls.app_label)

    @classmethod
    def allowed(cls, user):
        return cls.installed() and (cls.permission is None or user.has_perm(cls.permission))

    @staticmethod
    def scopes():
        raise NotImplementedError

    @staticmethod
    def registered_ids(character_ids):
        raise NotImplementedError

    @staticmethod
    def register(eve_character, countdown):
        raise NotImplementedError

    @staticmethod
    def after_all(user):
        pass


class MemberAuditTarget(Target):
    key = "memberaudit"
    label = "Member Audit"
    app_label = "memberaudit"
    permission = "memberaudit.basic_access"

    @staticmethod
    def scopes():
        from memberaudit.models import Character

        return Character.esi_scopes()

    @staticmethod
    def registered_ids(character_ids):
        """Registered AND healthy. Disabled or token-error characters are
        treated as not registered, so they get re-registered (same as the
        Member Audit menu badge logic)."""
        from django.db.models import Q

        from memberaudit.models import Character

        enabled_sections = list(Character.UpdateSection.enabled_sections())
        broken = set(
            Character.objects.filter(eve_character__character_id__in=character_ids)
            .filter(
                Q(
                    update_status_set__section__in=enabled_sections,
                    update_status_set__has_token_error=True,
                )
                | Q(is_disabled=True)
            )
            .values_list("eve_character__character_id", flat=True)
        )
        registered = set(
            Character.objects.filter(
                eve_character__character_id__in=character_ids
            ).values_list("eve_character__character_id", flat=True)
        )
        return registered - broken

    @staticmethod
    def register(eve_character, countdown):
        from memberaudit import tasks
        from memberaudit.app_settings import MEMBERAUDIT_TASKS_NORMAL_PRIORITY
        from memberaudit.models import Character

        with transaction.atomic():
            character = Character.objects.update_or_create(
                eve_character=eve_character, defaults={"is_disabled": False}
            )[0]
        tasks.update_character.apply_async(
            kwargs={
                "character_pk": character.pk,
                "force_update": True,
                "ignore_stale": True,
            },
            priority=MEMBERAUDIT_TASKS_NORMAL_PRIORITY,
            countdown=countdown,
        )

    @staticmethod
    def after_all(user):
        from memberaudit import tasks
        from memberaudit.app_settings import MEMBERAUDIT_TASKS_NORMAL_PRIORITY
        from memberaudit.models import ComplianceGroupDesignation

        if ComplianceGroupDesignation.objects.exists():
            tasks.update_compliance_groups_for_user.apply_async(
                args=[user.pk], priority=MEMBERAUDIT_TASKS_NORMAL_PRIORITY
            )


class CorpToolsTarget(Target):
    key = "corptools"
    label = "CorpTools"
    app_label = "corptools"
    permission = None

    @staticmethod
    def scopes():
        from corptools import app_settings

        return app_settings.get_character_scopes()

    @staticmethod
    def registered_ids(character_ids):
        from corptools.models import CharacterAudit

        return set(
            CharacterAudit.objects.filter(
                character__character_id__in=character_ids
            ).values_list("character__character_id", flat=True)
        )

    @staticmethod
    def register(eve_character, countdown):
        from corptools.models import CharacterAudit
        from corptools.tasks import update_character

        CharacterAudit.objects.update_or_create(character=eve_character)
        update_character.apply_async(
            args=[eve_character.character_id],
            kwargs={"force_refresh": True},
            priority=6,
            countdown=countdown,
        )


TARGETS = {t.key: t for t in (MemberAuditTarget, CorpToolsTarget)}


def available_targets(user):
    return [t for t in TARGETS.values() if t.allowed(user)]


# ---------------------------------------------------------------- helpers


def build_status(user, target):
    """Split the user's owned characters into ready / registered / missing token."""
    owned = list(
        EveCharacter.objects.filter(
            character_ownership__in=CharacterOwnership.objects.filter(user=user)
        ).order_by("character_name")
    )
    owned_ids = [c.character_id for c in owned]

    with_token = set(
        Token.objects.filter(user=user, character_id__in=owned_ids)
        .require_scopes(target.scopes())
        .values_list("character_id", flat=True)
    )
    registered = target.registered_ids(owned_ids)

    ready, done, missing = [], [], []
    for char in owned:
        if char.character_id in registered:
            done.append(char)
        elif char.character_id in with_token:
            ready.append(char)
        else:
            missing.append(char)
    return {"ready": ready, "registered": done, "missing": missing}


# ---------------------------------------------------------------- views


@login_required
def index(request):
    targets = available_targets(request.user)
    if not targets:
        messages.error(request, "You don't have access to Member Audit or CorpTools.")
        return redirect("authentication:dashboard")

    key = request.GET.get("target", targets[0].key)
    target = TARGETS.get(key)
    if target not in targets:
        target = targets[0]

    context = {
        "page_title": "Bulk Register",
        "targets": targets,
        "target": target,
        **build_status(request.user, target),
    }
    return render(request, "bulkregister/index.html", context)


@login_required
@require_POST
def register_all(request):
    target = TARGETS.get(request.POST.get("target", ""))
    if target is None or not target.allowed(request.user):
        return HttpResponseBadRequest("Invalid target")

    selected = {int(x) for x in request.POST.getlist("character_id") if x.isdigit()}
    # Re-check server side: only characters the user owns AND has a valid-scope token for.
    ready = build_status(request.user, target)["ready"]
    to_add = [c for c in ready if c.character_id in selected]

    added, failed = [], []
    for i, char in enumerate(to_add):
        try:
            target.register(char, countdown=i * STAGGER_SECONDS)
            added.append(char.character_name)
        except Exception:  # keep going with the rest
            logger.exception("Bulk register failed for %s (%s)", char, target.key)
            failed.append(char.character_name)

    if added:
        target.after_all(request.user)
        messages.success(
            request,
            f"{len(added)} character(s) registered in {target.label}. "
            "Updates are queued and may take a few minutes.",
        )
    if failed:
        messages.error(request, f"Failed: {', '.join(failed)}")
    if not added and not failed:
        messages.info(request, "Nothing to register.")

    logger.info(
        "%s bulk-registered %d character(s) in %s", request.user, len(added), target.key
    )
    return redirect(f"{reverse('bulkregister:index')}?target={target.key}")
