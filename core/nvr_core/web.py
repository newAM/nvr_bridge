"""The SSO-protected events web UI."""

import functools
import json
from collections.abc import Callable
from datetime import UTC, date, datetime

from authlib.integrations.base_client import OAuthError
from flask import (
    Blueprint,
    Flask,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue

from nvr_core.auth import UserSession, current_user, oauth_client
from nvr_core.models import (
    adjacent_event_days,
    events_for_day,
    events_for_group,
    get_event,
    latest_event_day,
)


def _has_required_role(user: UserSession) -> bool:
    return current_app.config["USER_ROLE"] in user.get("roles", [])


def _role_denied_response() -> tuple[str, int]:
    user_role = current_app.config["USER_ROLE"]
    message = (
        f"Permission denied. Contact your system administrator and request the {user_role!r} role."
    )
    return render_template("error.html", code=403, message=message), 403


def _requested_day() -> date | None:
    """The ``?day=YYYY-MM-DD`` query parameter, if present and valid."""
    raw = request.args.get("day")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def require_web_auth(
    f: Callable[..., ResponseReturnValue],
) -> Callable[..., ResponseReturnValue]:
    """Decorator that injects the authenticated user as the first argument.

    Unauthenticated requests are redirected to the SSO login, remembering
    the requested path so the callback can return the user to it; users
    without the required role get a 403 page.
    """

    @functools.wraps(f)
    def wrapper(*args: object, **kwargs: object) -> ResponseReturnValue:
        user = current_user()
        if user is None:
            # Remember the requested path so the SSO roundtrip returns
            # the user to it.
            next_url = request.path
            if request.query_string:
                next_url += "?" + request.query_string.decode()
            session["login_next"] = next_url
            return redirect(url_for("auth.login"))
        if not _has_required_role(user):
            return _role_denied_response()
        return f(user, *args, **kwargs)

    return wrapper


def create_web_blueprint(app: Flask) -> None:
    # A fresh blueprint per app: the routes close over nothing app-specific,
    # but the test suite creates many apps in one process.
    web_bp = Blueprint("web", __name__)

    @web_bp.context_processor
    def inject_template_context() -> dict[str, Callable[..., str]]:
        def format_received_at(received_at: datetime) -> str:
            # Naive values are UTC (SQLite round-trip); render in the
            # host's local timezone -- event times are home-local by
            # nature, wherever the page is viewed from.
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=UTC)
            return received_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")

        def to_isoformat(dt: datetime) -> str:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.isoformat()

        def pretty_payload(payload: str) -> str:
            # payloads are stored compact; pretty-print for display,
            # preserving the original key order
            try:
                return json.dumps(json.loads(payload), indent=2)
            except ValueError:
                return payload

        return {
            "format_received_at": format_received_at,
            "to_isoformat": to_isoformat,
            "pretty_payload": pretty_payload,
        }

    @web_bp.route("/")
    def index() -> ResponseReturnValue:
        user = current_user()
        if user is None:
            return redirect(url_for("auth.login"))
        if not _has_required_role(user):
            return _role_denied_response()
        return redirect(url_for("web.events"))

    @web_bp.route("/events")
    @require_web_auth
    def events(user: UserSession) -> ResponseReturnValue:
        # One day per page: listing every event did not scale.  The
        # default day is the newest one holding events (today when there
        # are none); a malformed ?day= falls back to it too.
        day = _requested_day()
        if day is None:
            day = latest_event_day() or date.today()
        prev_day, next_day = adjacent_event_days(day)
        return render_template(
            "events.html",
            user=user,
            events=events_for_day(day),
            day=day,
            prev_day=prev_day,
            next_day=next_day,
        )

    @web_bp.route("/events/group/<token>")
    @require_web_auth
    def event_group(user: UserSession, token: str) -> ResponseReturnValue:
        # The notification's click target: every image of one event group
        # (the instant go2rtc captures taken at alarm time plus the AI
        # snapshots that followed), from the first event onwards.
        group_events = events_for_group(token)
        if not group_events:
            return render_template("error.html", code=404, message="Not found"), 404
        return render_template("event_group.html", user=user, group=token, events=group_events)

    @web_bp.route("/events/<int:event_id>")
    @require_web_auth
    def event_detail(user: UserSession, event_id: int) -> ResponseReturnValue:
        event = get_event(event_id)
        if event is None:
            return render_template("error.html", code=404, message="Not found"), 404
        # Stills are captured at alarm time on the alarm's event; events
        # without their own fall back to the group's.
        stills = event.live_snaps_parsed
        if not stills and event.group_token:
            for other in events_for_group(event.group_token):
                if other.live_snaps_parsed:
                    stills = other.live_snaps_parsed
                    break
        return render_template("event_detail.html", user=user, event=event, stills=stills)

    @web_bp.route("/logged-out")
    def logged_out() -> ResponseReturnValue:
        if request.args.get("state"):
            try:
                oauth_client().oidc.validate_logout_response()
            except OAuthError:
                current_app.logger.warning("event=logout_validation_failed", exc_info=True)
                return render_template(
                    "error.html", code=400, message="Logout validation failed"
                ), 400
        return render_template("logged_out.html")

    @web_bp.errorhandler(403)
    def forbidden(_e: Exception) -> ResponseReturnValue:
        return render_template("error.html", code=403, message="Forbidden"), 403

    @web_bp.errorhandler(404)
    def not_found(_e: Exception) -> ResponseReturnValue:
        return render_template("error.html", code=404, message="Not found"), 404

    @web_bp.errorhandler(500)
    def server_error(_e: Exception) -> ResponseReturnValue:
        return render_template("error.html", code=500, message="Internal server error"), 500

    app.register_blueprint(web_bp)
