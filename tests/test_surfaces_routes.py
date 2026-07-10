"""GET /surfaces — the dashboard's read-only view of gateway composition, and
the /ui SPA serving (ui-shell.md §6)."""

from __future__ import annotations

from starlette.testclient import TestClient

from snowline_platform.app import create_app
from snowline_platform.manifest import PluginManifest
from snowline_platform.registry import PluginRegistry, PluginStatus
from snowline_platform.trust import Principal, TrustResolver


class _AlwaysTrust:
    def resolve(self, peer_ip, headers):
        return Principal(id="test-owner", source="test")


def _app(registry=None):
    return create_app(
        resolver=TrustResolver([_AlwaysTrust()]),
        registry=registry,
        migrate_on_startup=False,
    )


def test_surfaces_reports_mounts_allowlists_and_composed_plugins(monkeypatch):
    monkeypatch.setenv("SNOWLINE_SURFACES", "main,core")
    monkeypatch.setenv("SNOWLINE_SURFACE_PLUGINS", "core=governance")
    reg = PluginRegistry()
    reg.upsert(
        PluginManifest(
            name="governance", base_url="http://g:1", surfaces={"/mcp": "main"}
        )
    )
    reg.upsert(
        PluginManifest(name="pm", base_url="http://p:1", surfaces={"/mcp": "main"})
    )

    body = TestClient(_app(reg)).get("/surfaces").json()
    by_name = {s["name"]: s for s in body["surfaces"]}
    assert by_name["main"]["route"] == "/mcp"
    assert by_name["main"]["allowlist"] == "*"
    assert by_name["main"]["plugins"] == ["governance", "pm"]
    # core allowlists governance only; the ROOT_SURFACE projection (#38) maps
    # governance's main mapping onto it, pm is filtered out.
    assert by_name["core"]["route"] == "/core/mcp"
    assert by_name["core"]["allowlist"] == ["governance"]
    assert by_name["core"]["plugins"] == ["governance"]


def test_surfaces_skips_down_plugins(monkeypatch):
    monkeypatch.delenv("SNOWLINE_SURFACES", raising=False)
    monkeypatch.delenv("SNOWLINE_SURFACE_PLUGINS", raising=False)
    reg = PluginRegistry()
    reg.upsert(
        PluginManifest(
            name="governance", base_url="http://g:1", surfaces={"/mcp": "main"}
        )
    )
    reg.set_status("governance", PluginStatus.DOWN)
    body = TestClient(_app(reg)).get("/surfaces").json()
    main = next(s for s in body["surfaces"] if s["name"] == "main")
    assert main["plugins"] == []  # composed view mirrors gateway route-around


def test_ui_serves_spa_with_fallback_and_traversal_guard(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>shell</html>")
    (dist / "assets" / "app.js").write_text("//js")
    (tmp_path / "secret.txt").write_text("outside")
    monkeypatch.setenv("SNOWLINE_DASHBOARD_DIST", str(dist))

    client = TestClient(_app())
    assert client.get("/ui").text == "<html>shell</html>"
    assert client.get("/ui/assets/app.js").text == "//js"
    # A client-side route falls back to the shell.
    assert client.get("/ui/plugins").text == "<html>shell</html>"
    # Traversal attempts never escape the dist dir. (Plain `/ui/../` is
    # normalized away client-side before it ever reaches the app; the
    # percent-encoded form survives to the route and must hit the guard.)
    assert client.get("/ui/%2e%2e/secret.txt").text == "<html>shell</html>"


def test_ui_absent_without_a_built_bundle(monkeypatch):
    monkeypatch.setenv("SNOWLINE_DASHBOARD_DIST", "/nonexistent-dist")
    assert TestClient(_app()).get("/ui").status_code == 404


def test_ui_appears_after_a_late_first_build(monkeypatch, tmp_path):
    # First-deploy ordering: the platform can boot BEFORE the dashboard's
    # first build; the dist is resolved per-request, so /ui starts serving
    # without a restart the moment the bundle lands.
    dist = tmp_path / "dist"
    monkeypatch.setenv("SNOWLINE_DASHBOARD_DIST", str(dist))
    client = TestClient(_app())
    assert client.get("/ui").status_code == 404
    dist.mkdir()
    (dist / "index.html").write_text("<html>late</html>")
    assert client.get("/ui").text == "<html>late</html>"


def test_ui_half_built_dist_is_404_not_500(monkeypatch, tmp_path):
    # vite mid-rebuild / interrupted build: dist exists, index.html doesn't.
    dist = tmp_path / "dist"
    dist.mkdir()
    monkeypatch.setenv("SNOWLINE_DASHBOARD_DIST", str(dist))
    resp = TestClient(_app()).get("/ui/plugins")
    assert resp.status_code == 404
    assert "rebuild" in resp.json()["detail"]


def test_reserved_surface_names_fail_boot(monkeypatch):
    import pytest

    from snowline_platform.config import ConfigError

    monkeypatch.setenv("SNOWLINE_SURFACES", "main,ui")
    with pytest.raises(ConfigError, match="reserved"):
        _app()


def test_ui_cache_headers_split_immutable_hashed_from_revalidating_shell(
    monkeypatch, tmp_path
):
    # index.html (direct and via SPA fallback) must carry Cache-Control:
    # no-cache — with NO Cache-Control, browsers apply heuristic freshness
    # and a phone keeps rendering a stale shell pointing at a bundle that no
    # longer exists after a redeploy. Only files whose NAME carries vite's
    # content hash are immutable (private: /ui is trust-gated, so a shared
    # cache must not re-serve it past the gate); a stable-named file that
    # lands under assets/ (vite copies public/ verbatim) revalidates like
    # the shell. 404s carry no-cache too (404 is heuristically cacheable),
    # and a dead assets/ reference 404s instead of SPA-falling-back — an
    # index.html served as a .js module trips the browser MIME check and
    # blanks the dashboard.
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>shell</html>")
    (dist / "assets" / "app-Belvg2xW.js").write_text("//js")
    (dist / "assets" / "logo.svg").write_text("<svg/>")
    (dist / "favicon.svg").write_text("<svg/>")
    monkeypatch.setenv("SNOWLINE_DASHBOARD_DIST", str(dist))

    client = TestClient(_app())
    assert client.get("/ui").headers["cache-control"] == "no-cache"
    assert client.get("/ui/plugins").headers["cache-control"] == "no-cache"
    assert client.get("/ui/favicon.svg").headers["cache-control"] == "no-cache"
    assert client.get("/ui/assets/logo.svg").headers["cache-control"] == "no-cache"
    immutable = client.get("/ui/assets/app-Belvg2xW.js")
    assert immutable.headers["cache-control"] == "private, max-age=31536000, immutable"

    # Dead bundle reference: 404 + no-cache, never the SPA shell.
    dead = client.get("/ui/assets/app-00000000.js")
    assert dead.status_code == 404
    assert dead.headers["cache-control"] == "no-cache"

    # Bundle-missing 404s are no-cache as well, so a pre-first-build visit
    # can't stick (test_ui_appears_after_a_late_first_build's guarantee).
    monkeypatch.setenv("SNOWLINE_DASHBOARD_DIST", str(tmp_path / "never-built"))
    absent = TestClient(_app()).get("/ui")
    assert absent.status_code == 404
    assert absent.headers["cache-control"] == "no-cache"
