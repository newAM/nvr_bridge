"""OIDC (SSO) authentication for the events web UI.

The browser is redirected to the identity provider, and on callback a
server-side session is established from the verified token's claims.
Access to the events pages requires the configured user role, extracted
from the claims at ``ROLES_PATH``.
"""

from typing import Any, NotRequired, TypedDict

from authlib.integrations.base_client import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint,
    Flask,
    current_app,
    redirect,
    render_template,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue


class UserSession(TypedDict):
    sub: str
    email: NotRequired[str | None]
    name: NotRequired[str | None]
    roles: NotRequired[list[str]]


auth_bp = Blueprint("auth", __name__)


def value_at_path(data: dict[str, Any], path: list[str]) -> list[str]:
    try:
        current: Any = data
        for key in path:
            current = current[key]
    except (KeyError, TypeError, IndexError):
        return []
    if isinstance(current, list) and all(isinstance(item, str) for item in current):
        return current
    if isinstance(current, str):
        return [current]
    return []


def _extract_roles(token_data: dict[str, Any], userinfo: dict[str, Any]) -> list[str]:
    roles_path = current_app.config["ROLES_PATH"]
    roles = value_at_path(token_data, roles_path)
    if not roles:
        roles = value_at_path(userinfo, roles_path)
    return roles


def init_oauth(app: Flask) -> OAuth:
    """Create this app's OAuth client registry (one per app instance).

    authlib caches clients and fetched server metadata per ``OAuth``
    instance, so apps created in one process (the test suite) each get
    their own registry.
    """
    oauth = OAuth(app)
    oauth.register(
        name="oidc",
        client_id=app.config["OIDC_CLIENT_ID"],
        client_secret=app.config["OIDC_CLIENT_SECRET"],
        server_metadata_url=app.config["OIDC_DISCOVERY_URL"],
        client_kwargs={
            "scope": "openid email profile",
            "code_challenge_method": "S256",
        },
    )
    app.extensions["oauth"] = oauth
    return oauth


def oauth_client() -> OAuth:
    return current_app.extensions["oauth"]


@auth_bp.route("/login")
def login() -> ResponseReturnValue:
    callback_uri = current_app.config["PUBLIC_URL"].rstrip("/") + "/auth/callback"
    return oauth_client().oidc.authorize_redirect(callback_uri)


def _safe_next(next_url: str | None) -> str:
    """The post-login redirect target: only relative paths are honoured so
    the IdP roundtrip cannot be turned into an open redirect."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/events"


@auth_bp.route("/auth/callback")
def callback() -> ResponseReturnValue:
    try:
        token = oauth_client().oidc.authorize_access_token()
        userinfo = token.get("userinfo") or oauth_client().oidc.userinfo()
        roles = _extract_roles(token, dict(userinfo))
    except OAuthError as exc:
        session.clear()
        current_app.logger.warning("event=auth_failed error=%r", exc)
        return (
            render_template("error.html", code=401, message="Authentication failed"),
            401,
        )
    current_app.logger.info("event=login user_sub=%r roles=%r", userinfo["sub"], roles)
    # Pop before session.clear() wipes it.
    next_url = session.get("login_next")
    session.clear()
    session["id_token"] = token.get("id_token")
    session["user"] = {
        "sub": userinfo["sub"],
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "roles": roles,
    }
    return redirect(_safe_next(next_url))


@auth_bp.route("/logout", methods=["POST"])
def logout() -> ResponseReturnValue:
    id_token = session.pop("id_token", None)
    user = session.pop("user", None)
    session.clear()

    if user:
        current_app.logger.info("event=logout user_sub=%r", user["sub"])

    client = oauth_client().oidc
    end_session_endpoint = client.load_server_metadata().get("end_session_endpoint")
    if end_session_endpoint:
        post_logout_redirect_uri = current_app.config["PUBLIC_URL"].rstrip("/") + "/logged-out"
        return client.logout_redirect(
            post_logout_redirect_uri=post_logout_redirect_uri,
            id_token_hint=id_token,
        )

    return redirect(url_for("web.logged_out"))


def current_user() -> UserSession | None:
    """Return the session user dict, or None if not logged in."""
    return session.get("user")
