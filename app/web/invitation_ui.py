"""Helpere UI comune pentru afisarea o singura data a URL-ului de invitatie
(issue #149) -- folosite atat de autoservire (`/organizations/{id}`,
organization_admin) cat si de backoffice (`/admin/organizations/{id}`,
platform_admin), ca sa nu diverga cele doua fluxuri."""
from __future__ import annotations

from app.config import Settings, get_settings
from app.models.user import Invitation


def invitation_reveal(invitation: Invitation, raw_token: str) -> dict:
    """URL-ul complet, afisat o singura data -- niciodata reconstituibil din
    DB (doar hash-ul e persistat, vezi `auth_service.create_invitation`)."""
    settings = get_settings()
    return {
        "url": f"{settings.base_url}/accept-invitation?token={raw_token}",
        "email": invitation.email,
        "role": invitation.role,
        "expires_at": invitation.expires_at,
    }


def should_reveal_invitation_link(settings: Settings) -> bool:
    """In modul 'manual_link' linkul e SINGURA cale de livrare, deci trebuie
    afisat mereu (inclusiv in productie). In modul 'email' comportamentul
    istoric ramane: afisat doar in medii non-productie, ca ajutor de
    dezvoltare/testare -- in productie emailul e singura livrare."""
    if settings.invitation_manual_link:
        return True
    return not settings.is_production
