"""Headere pentru raspunsuri care poarta un secret afisat o singura data
(link de invitatie, token de resetare parola): browserul/proxy-urile nu
trebuie sa le stocheze in cache, motorul de cautare nu trebuie sa le indexeze
si niciun `Referer` nu trebuie sa scurga token-ul din URL catre o resursa
externa la navigarea in continuare (issue #149)."""
from __future__ import annotations

from starlette.responses import Response


def apply_no_store_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response
