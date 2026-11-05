"""Dashboardet som app på hjemmeskærmen: manifest og ikoner er åbne, alt andet kræver stadig login."""

from __future__ import annotations

import struct

from test_auth import _client, _login


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_manifest_and_icons_load_without_login(tmp_path):
    with _client(tmp_path) as client:
        manifest = client.get("/manifest.webmanifest")
        assert manifest.status_code == 200
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        data = manifest.json()
        assert data["start_url"] == "/meme" and data["display"] == "standalone"
        assert {icon["purpose"] for icon in data["icons"]} == {"any", "maskable"}
        for icon in data["icons"]:
            response = client.get(icon["src"])
            assert response.status_code == 200, icon["src"]
            width, height = _png_size(response.content)
            assert f"{width}x{height}" == icon["sizes"]
        apple = client.get("/apple-touch-icon.png")
        assert apple.status_code == 200 and _png_size(apple.content) == (180, 180)
        assert client.get("/apple-touch-icon-precomposed.png").status_code == 200


def test_icon_route_serves_only_known_files(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/icons/nope.png").status_code == 404
        assert client.get("/icons/..%2Fmeme.html").status_code == 404
        sneaky = client.get("/icons/../app.py", follow_redirects=False)
        assert sneaky.status_code in {401, 303, 404}  # aldrig selve filen
        assert "import" not in sneaky.text
        # Alt andet kræver stadig login.
        assert client.get("/api/status").status_code == 401
        assert client.get("/meme", follow_redirects=False).status_code == 303


def test_pages_link_the_manifest(tmp_path):
    with _client(tmp_path) as client:
        login = client.get("/login")
        assert 'rel="manifest"' in login.text and "apple-touch-icon" in login.text
        assert _login(client).status_code == 303
        meme = client.get("/meme")
        assert 'rel="manifest"' in meme.text
        assert 'id="app-card"' in meme.text
        assert 'apple-mobile-web-app-title" content="Memebot"' in meme.text


def test_every_page_links_to_the_other_two(tmp_path):
    with _client(tmp_path) as client:
        assert _login(client).status_code == 303
        for path in ("/", "/meme", "/desk"):
            page = client.get(path).text
            assert 'class="appnav"' in page, path
            for target in ('href="/meme"', 'href="/desk"', 'href="/"'):
                assert target in page, (path, target)
            assert page.count('aria-current="page"') == 1, path
