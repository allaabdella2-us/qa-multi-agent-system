"""The profile fields must actually be read, not just exist in the schema.

`TargetProfile` declared `environment.compose_file`, `environment.seed_sql`,
`layout.spec`, `auth.roles` and `ledger` since the schema was written, and
nothing read any of them. Every one was hardcoded to the bundled demo:
`docker-compose.yml`, `api/seed/fixtures.sql`, `openapi.yaml`, three
`@northwind.test` accounts and the literal password `password123`. The
"portable" seam was fiction, and the first thing anyone pointing qaas at their
own repository would hit.

The whole suite exercises the demo, so it stayed green throughout. These tests
deliberately use a profile that looks nothing like the demo -- that is the only
way this class of bug is visible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qaas.mcp import contract_diff, env_control
from qaas.target import Auth, Environment, Layout, Role, TargetProfile


def _profile(**over) -> TargetProfile:
    """A target that shares no path convention with the bundled demo."""
    base = dict(
        name="acme",
        root="/srv/acme",
        layout=Layout(backend=["services/api"], frontend=["ui/app"], spec="contracts/api.yml"),
        environment=Environment(
            mode="compose",
            compose_file="deploy/compose.prod.yml",
            seed_sql="db/fixtures/baseline.sql",
            api_url="http://localhost:9000",
        ),
        auth=Auth(
            mode="login",
            login_endpoint="POST /api/v2/session",
            username_field="login",
            password_field="secret",
            roles={
                "owner": Role(username="owner@acme.example", password_env="ACME_OWNER_PW"),
                "readonly": Role(username="ro@acme.example", password_env="ACME_RO_PW"),
            },
        ),
    )
    base.update(over)
    return TargetProfile(**base)


class _Ctx:
    """The two attributes these resolvers touch."""

    def __init__(self, profile, target_app=Path("/srv/acme")):
        self.config = type("Cfg", (), {"profile": profile, "target_app": str(target_app)})()
        self.target_app = target_app


# -- environment ------------------------------------------------------------


def test_the_compose_file_comes_from_the_profile():
    ctx = _Ctx(_profile())
    assert env_control._compose_path(ctx) == Path("/srv/acme/deploy/compose.prod.yml")


def test_the_fixture_directory_comes_from_the_profile():
    """A project keeping fixtures in db/fixtures/ must not be told about api/seed."""
    ctx = _Ctx(_profile())
    assert env_control._seed_dir(ctx) == Path("/srv/acme/db/fixtures")
    assert env_control._default_fixture(ctx) == "baseline.sql"


def test_the_demo_conventions_still_apply_when_the_profile_is_silent():
    """The fallbacks are fallbacks, not dead code -- the bundled demo relies on
    them and so does any profile that declares nothing.

    Note `mode: compose` cannot reach the compose fallback: the schema already
    refuses that combination (`environment.mode is 'compose' but no compose_file
    is set`). So the fallback matters for the other modes, and for a profile
    that never gets as far as declaring an environment.
    """
    ctx = _Ctx(_profile(environment=Environment(mode="none")))
    assert env_control._compose_path(ctx).name == "docker-compose.yml"
    assert env_control._seed_dir(ctx) == Path("/srv/acme/api/seed")
    assert env_control._default_fixture(ctx) == "fixtures.sql"


def test_no_profile_at_all_does_not_crash():
    ctx = _Ctx(None)
    assert env_control._compose_path(ctx).name == "docker-compose.yml"


# -- auth: the security half ------------------------------------------------


def test_roles_come_from_the_profile_not_the_demo_accounts(monkeypatch):
    monkeypatch.setenv("ACME_OWNER_PW", "s3cret")
    roles = env_control._roles(_Ctx(_profile()))
    assert set(roles) == {"owner", "readonly"}
    assert roles["owner"] == ("owner@acme.example", "s3cret")
    assert "admin" not in roles, "the demo's accounts leaked into a real target"


def test_a_password_is_read_from_the_environment_never_the_profile(monkeypatch):
    """A profile is committed to a repository. A password in one is a leak."""
    monkeypatch.delenv("ACME_OWNER_PW", raising=False)
    roles = env_control._roles(_Ctx(_profile()))
    assert roles["owner"][1] is None, "a password materialised from somewhere"


def test_the_demo_password_never_reaches_a_target_that_declares_roles(monkeypatch):
    """The sharp edge: before this, pointing qaas at a real app with
    `auth.mode: login` POSTed the literal string `password123` at its login
    endpoint."""
    monkeypatch.setenv("ACME_OWNER_PW", "s3cret")
    values = {pw for _, pw in env_control._roles(_Ctx(_profile())).values()}
    assert env_control.FALLBACK_PASSWORD not in values


def test_the_login_path_comes_from_the_profile():
    assert env_control._login_path(_Ctx(_profile())) == "/api/v2/session"


def test_the_login_path_falls_back_when_unset():
    ctx = _Ctx(_profile(auth=Auth(mode="none")))
    assert env_control._login_path(ctx) == "/v1/auth/login"


# -- contract -------------------------------------------------------------


def test_the_spec_path_comes_from_layout_spec(tmp_path):
    """contract_diff hardcoded `<target>/openapi.yaml` while `layout.spec`
    existed and was ignored."""
    src = (Path(contract_diff.__file__)).read_text()
    assert 'ctx.target_app / (_declared_spec or DEFAULT_SPEC_FILE)' in src, (
        "contract_diff no longer resolves its spec through the profile"
    )


@pytest.mark.parametrize("needle", ["target-app/openapi.yaml", "target-app/web/src"])
def test_no_tool_description_names_one_specific_application(needle):
    """CLAUDE.md: nothing in a prompt or task may name a specific application.
    A tool description is read by the model, so it is a prompt."""
    src = Path(contract_diff.__file__).read_text()
    assert needle not in src, f"a tool description still names the demo app: {needle}"
