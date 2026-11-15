"""qBittorrent login: every outcome is reported as what happened, and failures back off."""

from __future__ import annotations

import httpx
import pytest

from ai_server_monitor.arr import ArrError, QBittorrent, qbit_auth

URL = "https://qb.example.test"


class Answer:
    """A fake qBittorrent whose login answer can be changed; counts login attempts."""

    def __init__(self, status: int = 200, text: str = "Ok.", content_type: str = "text/plain"):
        self.status, self.text, self.content_type = status, text, content_type
        self.logins = 0
        self.seen: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            if request.url.path == "/api/v2/auth/login":
                self.logins += 1
                return httpx.Response(
                    self.status, text=self.text, headers={"content-type": self.content_type}
                )
            return httpx.Response(200, text="v5.0.1")

        return httpx.MockTransport(handle)


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(qbit_auth, "clock", fake)
    return fake


async def login(fake: Answer, password: str = "s3cret-pw") -> ArrError | None:
    client = QBittorrent(URL, "admin", password, transport=fake.transport())
    try:
        await client.version()
    except ArrError as exc:
        return exc
    finally:
        await client.close()
    return None


async def test_ok_logs_in_and_sends_referer_and_origin() -> None:
    fake = Answer()
    assert await login(fake) is None
    sent = fake.seen[0].headers
    assert sent["referer"] == URL and sent["origin"] == URL


async def test_fails_means_wrong_credentials() -> None:
    err = await login(Answer(200, "Fails."))
    assert err is not None and err.status == 401
    text = str(err)
    assert "answered 'Fails.'" in text and "user name or password" in text
    assert 'HTTP 200, body "Fails."' in text
    assert "Referer: https://qb.example.test" in text and "Origin:" in text and "Host:" in text
    assert "s3cret-pw" not in text


async def test_401_means_host_header_or_csrf_not_password() -> None:
    err = await login(Answer(401, "Unauthorized"))
    assert err is not None and err.status == 401
    text = str(err)
    assert "NOT a wrong password" in text and "Host-header validation" in text
    assert "http://<qbittorrent container>:8080" in text and "Server domains" in text
    assert "qb.example.test" in text and 'HTTP 401, body "Unauthorized"' in text


async def test_403_means_ip_ban_and_our_retries_extend_it() -> None:
    err = await login(Answer(403, "Forbidden"))
    assert err is not None and err.status == 403
    text = str(err)
    assert "banned" in text and "3600" in text and "extends the ban" in text
    assert "paused until" in text


async def test_html_means_a_proxy_page_in_front() -> None:
    err = await login(Answer(200, "<html><body>Please sign in</body></html>", "text/html"))
    assert err is not None
    assert "web page, not qBittorrent's API" in str(err) and "Please sign in" in str(err)
    assert "paused" not in str(err)  # nothing to back off from: a retry cannot hurt


async def test_body_excerpt_is_capped() -> None:
    err = await login(Answer(502, "x" * 1000))
    assert err is not None and "x" * 201 not in str(err) and "x" * 200 in str(err)


async def test_failed_login_backs_off_across_clients_and_grows(clock: Clock) -> None:
    fake = Answer(200, "Fails.")
    first = await login(fake)
    assert first is not None and "paused until" in str(first)
    assert fake.logins == 1
    # a second client (the collector vs the action layer) with the same credentials
    # does not even try
    again = await login(fake)
    assert again is not None and "not retried" in str(again) and fake.logins == 1
    clock.now += 601  # 10 minutes later: one more try, then a 30 minute pause
    assert await login(fake) is not None and fake.logins == 2
    clock.now += 601
    assert fake.logins == 2 and "not retried" in str(await login(fake))
    clock.now += 1200
    assert await login(fake) is not None and fake.logins == 3  # (30 min since the second)
    state = qbit_auth.state_of(qbit_auth.backoff_key(URL, "admin", "s3cret-pw"))
    assert state["failures_in_a_row"] == 3 and state["paused"] is True


async def test_other_credentials_or_a_success_end_the_pause(clock: Clock) -> None:
    fake = Answer(200, "Fails.")
    assert await login(fake) is not None
    assert await login(fake, password="another-pw") is not None  # new key: tried at once
    assert fake.logins == 2
    fake.status, fake.text = 200, "Ok."
    clock.now += 601
    assert await login(fake) is None
    assert qbit_auth.state_of(qbit_auth.backoff_key(URL, "admin", "s3cret-pw"))["paused"] is False
