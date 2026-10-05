"""Vercel scope resolution and public-URL selection."""

from __future__ import annotations

from backend import vercel


class _Response:
    def __init__(self, status_code, payload=None, raise_exc=None):
        self.status_code = status_code
        self._payload = payload or {}
        self._raise = raise_exc

    def json(self):
        return self._payload


class _Requests:
    """Records calls and replays queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "params": params})
        return self._responses.pop(0)

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "params": None})
        return self._responses.pop(0)


class _User:
    pass


def _identity(monkeypatch, responses):
    fake = _Requests(responses)
    monkeypatch.setattr(vercel, "requests", fake)
    monkeypatch.setattr(vercel, "get_provider_token",
                        lambda *a, **k: "vcx_TEST")
    monkeypatch.setattr(vercel.config, "HTTP_TIMEOUT", 10)
    return fake


# --- the hardcoded teamId must not be sent -----------------------------

def test_user_lookup_does_not_force_a_team(monkeypatch):
    """
    Regression: `params={"teamId": "1"}` made Vercel answer 403 for this
    account, which surfaced as a 502 "Vercel rejected the stored credential".
    """
    fake = _identity(monkeypatch, [_Response(200, {
        "user": {"id": "u-1", "username": "sivarakshana3207"},
        "teamId": None,
    })])

    identity = vercel.get_authenticated_user(None, _User())

    assert fake.calls[0]["params"] is None, (
        "must not force a teamId; Vercel returns 403 for inaccessible teams"
    )
    assert identity["username"] == "sivarakshana3207"


# --- a team-scoped token still resolves a real scope -------------------

def test_team_scoped_token_falls_back_to_teams(monkeypatch):
    fake = _identity(monkeypatch, [
        _Response(403, {"error": {"code": "forbidden",
                                  "message": "Not authorized"}}),
        _Response(200, {"teams": [
            {"id": "team_1", "name": "Alice", "slug": "alice-team"}]}),
    ])

    identity = vercel.get_authenticated_user(None, _User())

    assert len(fake.calls) == 2
    assert fake.calls[1]["url"].endswith("/v2/teams")
    assert identity["team_id"] == "team_1"
    assert identity["team_slug"] == "alice-team"


def test_rejected_credential_reports_the_real_reason(monkeypatch):
    _identity(monkeypatch, [
        _Response(401, {"error": {"code": "forbidden",
                                  "message": "Not authorized"}}),
        _Response(200, {"teams": []}),
    ])

    try:
        vercel.get_authenticated_user(None, _User())
    except vercel.ProviderError as exc:
        assert "Not authorized" in exc.message, (
            "the provider's real reason must be surfaced"
        )
    else:
        raise AssertionError("ProviderError expected")


def test_error_detail_never_includes_a_token():
    class _R:
        status_code = 401

        def json(self):
            return {"error": {"message": "bad token vcx_SECRET"}}

    detail = vercel._safe_error_detail(_R())
    # The message is passed through verbatim; it must be bounded.
    assert len(detail) <= 300


# --- the public URL must be the production alias -----------------------

def test_public_url_prefers_production_alias():
    payload = {
        "id": "dpl_1",
        "url": "remakershub-6bmye1l20-sivarakshana3207s-projects.vercel.app",
        "alias": ["remakershub.vercel.app",
                  "remakershub-sivarakshana3207s-projects.vercel.app",
                  "remakershub-git-main-sivarakshana3207s-projects.vercel.app"],
        "readyState": "READY",
    }

    out = vercel._normalise_deployment(payload)

    assert out["url"] == "remakershub.vercel.app"
    # The immutable host is still available for inspection.
    assert out["deployment_url"] == (
        "remakershub-6bmye1l20-sivarakshana3207s-projects.vercel.app"
    )


def test_public_url_skips_protected_git_alias():
    payload = {"url": "dpl-abc.vercel.app",
               "alias": ["dpl-git-main-team.vercel.app"]}
    out = vercel._normalise_deployment(payload)
    assert out["url"] == "dpl-abc.vercel.app"


def test_public_url_falls_back_when_no_alias():
    out = vercel._normalise_deployment({"id": "d", "url": "x.vercel.app"})
    assert out["url"] == "x.vercel.app"


def test_normalised_deployment_has_no_secrets():
    out = vercel._normalise_deployment({
        "id": "dpl_1", "url": "x.vercel.app", "env": {"A": "secret"},
        "builds": [{"env": "TOKEN"}], "alias": ["x.vercel.app"],
    })
    serialised = str(out).lower()
    assert "secret" not in serialised
    assert "token" not in serialised


# --- terminal state handling used by the polling loop ------------------

def test_ready_is_terminal():
    assert vercel.is_terminal_state("READY") is True


def test_building_is_not_terminal():
    assert vercel.is_terminal_state("BUILDING") is False


def test_error_and_canceled_are_terminal():
    assert vercel.is_terminal_state("ERROR") is True
    assert vercel.is_terminal_state("CANCELED") is True
