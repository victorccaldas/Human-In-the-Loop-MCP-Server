"""Integration tests for PersistentMiniAppServer and the MiniApp pool."""

import json
import queue
import sys
import threading
import time
import urllib.request
import urllib.error

sys.path.insert(0, ".")

from _miniapp_server import MiniAppHTTPServer, PersistentMiniAppServer


# ── PersistentMiniAppServer basics ────────────────────────────────────────


class TestPersistentMiniAppServer:
    """Verify the persistent server serves HTML and accepts submissions."""

    def _make_server(self):
        srv = PersistentMiniAppServer()
        port = srv.start(tunnel_base_url="http://127.0.0.1:9999")
        assert port > 0
        return srv, port

    def test_start_and_port(self):
        srv, port = self._make_server()
        try:
            assert srv.port == port
            assert port > 0
        finally:
            srv.stop()

    def test_get_no_token_returns_403(self):
        srv, port = self._make_server()
        try:
            url = f"http://127.0.0.1:{port}/"
            req = urllib.request.Request(url)
            try:
                urllib.request.urlopen(req, timeout=5)
                assert False, "Expected 403"
            except urllib.error.HTTPError as e:
                assert e.code == 403, f"Expected 403, got {e.code}"
        finally:
            srv.stop()

    def test_get_invalid_token_returns_403(self):
        srv, port = self._make_server()
        try:
            url = f"http://127.0.0.1:{port}/?t=invalid"
            req = urllib.request.Request(url)
            try:
                urllib.request.urlopen(req, timeout=5)
                assert False, "Expected 403"
            except urllib.error.HTTPError as e:
                assert e.code == 403
        finally:
            srv.stop()

    def test_get_valid_token_returns_html(self):
        srv, port = self._make_server()
        try:
            q = srv.register_session(
                token="tok123",
                title="Test Title",
                prompt="Hello!",
                prompts=[{"text": "prompt1", "checked": True}],
                name_or_role="tester",
            )
            url = f"http://127.0.0.1:{port}/?t=tok123"
            resp = urllib.request.urlopen(url, timeout=5)
            body = resp.read().decode()
            assert resp.status == 200
            assert "Test Title" in body
            assert "Hello!" in body
            assert "tok123" in body
            assert "text/html" in resp.headers["Content-Type"]
        finally:
            srv.stop()

    def test_submit_valid_answer(self):
        srv, port = self._make_server()
        try:
            q = srv.register_session(
                token="tok456",
                title="T",
                prompt="P",
                prompts=[],
                name_or_role="",
            )
            submit_url = f"http://127.0.0.1:{port}/submit"
            data = json.dumps({"answer": "my answer"}).encode()
            req = urllib.request.Request(
                submit_url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "X-Token": "tok456",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.status == 200
            result = json.loads(resp.read())
            assert result["ok"] is True

            # Answer should be in the queue
            msg = q.get(timeout=2)
            assert msg["text"] == "my answer"
            assert msg["source"] == "telegram_miniapp"
        finally:
            srv.stop()

    def test_double_submit_returns_409(self):
        srv, port = self._make_server()
        try:
            q = srv.register_session(
                token="tok789", title="T", prompt="P", prompts=[], name_or_role=""
            )
            submit_url = f"http://127.0.0.1:{port}/submit"
            data = json.dumps({"answer": "first"}).encode()
            req = urllib.request.Request(
                submit_url,
                data=data,
                headers={"Content-Type": "application/json", "X-Token": "tok789"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            # Second submit
            req2 = urllib.request.Request(
                submit_url,
                data=json.dumps({"answer": "second"}).encode(),
                headers={"Content-Type": "application/json", "X-Token": "tok789"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req2, timeout=5)
                assert False, "Expected 409"
            except urllib.error.HTTPError as e:
                assert e.code == 409
        finally:
            srv.stop()

    def test_unregister_session(self):
        srv, port = self._make_server()
        try:
            srv.register_session(
                token="tokdel", title="T", prompt="P", prompts=[], name_or_role=""
            )
            # Works before unregister
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/?t=tokdel", timeout=5
            )
            assert resp.status == 200

            srv.unregister_session("tokdel")
            # Should be 403 after unregister
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/?t=tokdel", timeout=5
                )
                assert False, "Expected 403"
            except urllib.error.HTTPError as e:
                assert e.code == 403
        finally:
            srv.stop()

    def test_update_tunnel_url_changes_submit_url(self):
        srv, port = self._make_server()
        try:
            srv.register_session(
                token="tokurl", title="T", prompt="P", prompts=[], name_or_role=""
            )
            # Initial submit URL uses initial tunnel_base_url
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/?t=tokurl", timeout=5
            )
            body1 = resp.read().decode()
            assert "http://127.0.0.1:9999/submit" in body1

            srv.update_tunnel_url("https://newtunnel.example.com")
            resp2 = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/?t=tokurl", timeout=5
            )
            body2 = resp2.read().decode()
            assert "https://newtunnel.example.com/submit" in body2
        finally:
            srv.stop()

    def test_concurrent_sessions(self):
        """Multiple tokens served simultaneously."""
        srv, port = self._make_server()
        try:
            tokens = [f"concurrent_{i}" for i in range(5)]
            queues = {}
            for tok in tokens:
                queues[tok] = srv.register_session(
                    token=tok,
                    title=f"Title {tok}",
                    prompt=f"Prompt {tok}",
                    prompts=[],
                    name_or_role="",
                )

            # GET each session
            for tok in tokens:
                resp = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/?t={tok}", timeout=5
                )
                body = resp.read().decode()
                assert f"Title {tok}" in body
                assert resp.status == 200

            # Submit to each session in parallel
            errors = []

            def submit(token):
                try:
                    data = json.dumps({"answer": f"answer_{token}"}).encode()
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/submit",
                        data=data,
                        headers={
                            "Content-Type": "application/json",
                            "X-Token": token,
                        },
                        method="POST",
                    )
                    resp = urllib.request.urlopen(req, timeout=5)
                    assert resp.status == 200
                except Exception as e:
                    errors.append(f"{token}: {e}")

            threads = [threading.Thread(target=submit, args=(t,)) for t in tokens]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert len(errors) == 0, f"Concurrent submit errors: {errors}"

            # Verify all answers arrived
            for tok in tokens:
                msg = queues[tok].get(timeout=2)
                assert msg["text"] == f"answer_{tok}"
        finally:
            srv.stop()

    def test_get_after_submit_returns_410(self):
        """After answering, GET should return 410 Gone."""
        srv, port = self._make_server()
        try:
            srv.register_session(
                token="tokgone", title="T", prompt="P", prompts=[], name_or_role=""
            )
            # Submit answer
            data = json.dumps({"answer": "done"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/submit",
                data=data,
                headers={"Content-Type": "application/json", "X-Token": "tokgone"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)

            # GET should now return 410
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/?t=tokgone", timeout=5
                )
                assert False, "Expected 410"
            except urllib.error.HTTPError as e:
                assert e.code == 410, f"Expected 410, got {e.code}"
        finally:
            srv.stop()

    def test_nonroot_path_returns_404(self):
        srv, port = self._make_server()
        try:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/other", timeout=5
                )
                assert False, "Expected 404"
            except urllib.error.HTTPError as e:
                assert e.code == 404
        finally:
            srv.stop()

    def test_parallel_get_and_submit(self):
        """Simulate parallel get_remote_input: register, GET, POST concurrently."""
        srv, port = self._make_server()
        try:
            NUM_SESSIONS = 10
            tokens = [f"parallel_{i}" for i in range(NUM_SESSIONS)]
            queues = {}
            for tok in tokens:
                queues[tok] = srv.register_session(
                    token=tok,
                    title=f"Parallel {tok}",
                    prompt=f"Prompt {tok}",
                    prompts=[{"text": f"opt_{tok}", "checked": True}],
                    name_or_role="test-agent",
                )

            errors = []
            results = {}

            def full_session(token):
                """Simulate a complete MiniApp session: GET page, POST answer."""
                try:
                    # 1. GET the HTML page
                    url = f"http://127.0.0.1:{port}/?t={token}"
                    resp = urllib.request.urlopen(url, timeout=5)
                    html = resp.read().decode()
                    assert resp.status == 200, f"GET {token}: expected 200, got {resp.status}"
                    assert token in html, f"GET {token}: token not in HTML"

                    # 2. POST the answer
                    data = json.dumps({"answer": f"answer_{token}"}).encode()
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/submit",
                        data=data,
                        headers={
                            "Content-Type": "application/json",
                            "X-Token": token,
                        },
                        method="POST",
                    )
                    resp2 = urllib.request.urlopen(req, timeout=5)
                    assert resp2.status == 200

                    # 3. Verify GET now returns 410 (already answered)
                    try:
                        urllib.request.urlopen(url, timeout=5)
                        errors.append(f"{token}: expected 410 after submit")
                    except urllib.error.HTTPError as e:
                        assert e.code == 410, f"{token}: expected 410, got {e.code}"

                    results[token] = True
                except Exception as e:
                    errors.append(f"{token}: {e}")

            # Run all sessions in parallel
            threads = [threading.Thread(target=full_session, args=(t,)) for t in tokens]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert len(errors) == 0, f"Parallel errors: {errors}"
            assert len(results) == NUM_SESSIONS, f"Only {len(results)}/{NUM_SESSIONS} completed"

            # Verify all answers arrived in queues
            for tok in tokens:
                msg = queues[tok].get(timeout=2)
                assert msg["text"] == f"answer_{tok}", f"{tok}: wrong answer"
                assert msg["source"] == "telegram_miniapp"
        finally:
            srv.stop()


# ── Compare old vs new server output ──────────────────────────────────────


class TestOldVsNewServerParity:
    """Verify PersistentMiniAppServer produces identical output to MiniAppHTTPServer."""

    def test_html_output_matches(self):
        """Both servers should produce the same HTML for the same input."""
        title = "Parity Test"
        prompt = "Please enter your answer"
        prompts = [{"text": "Option A", "checked": True}]
        token = "parity_token_123"
        name_or_role = "developer"
        tunnel_url = "https://test-tunnel.trycloudflare.com"

        # Old server
        old_srv = MiniAppHTTPServer(
            title=title,
            prompt=prompt,
            prompts=prompts,
            token=token,
            tunnel_base_url=tunnel_url,
            name_or_role=name_or_role,
        )
        old_port = old_srv.start()

        # New server
        new_srv = PersistentMiniAppServer()
        new_port = new_srv.start(tunnel_base_url=tunnel_url)
        new_srv.register_session(
            token=token,
            title=title,
            prompt=prompt,
            prompts=prompts,
            name_or_role=name_or_role,
        )

        try:
            old_resp = urllib.request.urlopen(
                f"http://127.0.0.1:{old_port}/?t={token}", timeout=5
            )
            old_html = old_resp.read().decode()

            new_resp = urllib.request.urlopen(
                f"http://127.0.0.1:{new_port}/?t={token}", timeout=5
            )
            new_html = new_resp.read().decode()

            # Parse the session JSON from both
            import re

            old_match = re.search(
                r"const SESSION = ({.*?});", old_html, re.DOTALL
            )
            new_match = re.search(
                r"const SESSION = ({.*?});", new_html, re.DOTALL
            )
            assert old_match, "Old server HTML missing SESSION JSON"
            assert new_match, "New server HTML missing SESSION JSON"

            old_session = json.loads(old_match.group(1))
            new_session = json.loads(new_match.group(1))

            # Keys and values should match
            assert old_session.keys() == new_session.keys(), (
                f"Key mismatch: old={set(old_session.keys())}, new={set(new_session.keys())}"
            )
            for key in old_session:
                assert old_session[key] == new_session[key], (
                    f"Value mismatch for '{key}': old={old_session[key]!r}, new={new_session[key]!r}"
                )

            # Full HTML should be identical (same template, same data)
            assert old_html == new_html, "Full HTML output differs between old and new server"
        finally:
            old_srv.stop()
            new_srv.stop()
