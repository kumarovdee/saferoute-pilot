import json
import re
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pilot import DecisionError, Pilot
from server import make_server


class PilotTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = 1000
        self.pilot = Pilot(Path(tmp.name) / "pilot.db", 30, lambda: self.now)

    def submit(self, transfer_id="test", amount=250000, new=True, unusual=True,
               urgent=False, blocked=False):
        return self.pilot.submit(transfer_id, amount, new, unusual, urgent, blocked)

    def decision(self, action="approve"):
        return self.pilot.resolve("test", action, "Сценарий проверен.")

    def test_hold_and_same_session_reconfirmation(self):
        self.assertEqual("held", self.submit())
        self.assertEqual("held", self.pilot.resolve("test", "reconfirm"))
        self.assertEqual([], self.pilot.snapshot()["payments"])

    def test_ordinary_payment(self):
        self.assertEqual("sent", self.submit(new=False))
        self.assertEqual(1, len(self.pilot.snapshot()["payments"]))

    def test_small_transfer_to_new_recipient(self):
        self.assertEqual("sent", self.submit(unusual=False))

    def test_urgent_signal(self):
        self.assertEqual("held", self.submit(unusual=False, urgent=True))

    def test_operator_needs_contact_and_reason(self):
        self.submit()
        with self.assertRaises(DecisionError):
            self.decision()
        self.pilot.resolve("test", "contact")
        with self.assertRaises(DecisionError):
            self.pilot.resolve("test", "approve", " ")
        self.assertEqual([], self.pilot.snapshot()["payments"])

    def test_cancelled_transfer_never_sends(self):
        self.submit()
        self.pilot.resolve("test", "contact")
        self.assertEqual("cancelled", self.pilot.resolve("test", "cancel"))
        self.assertEqual("cancelled", self.decision())
        self.assertEqual([], self.pilot.snapshot()["payments"])

    def test_operator_can_reject(self):
        self.submit()
        self.pilot.resolve("test", "contact")
        self.assertEqual("declined", self.decision("reject"))
        self.assertEqual("declined", self.decision())
        self.assertEqual([], self.pilot.snapshot()["payments"])

    def test_approved_transfer_sends_once_under_concurrency(self):
        self.submit()
        self.pilot.resolve("test", "contact")
        barrier = threading.Barrier(2, timeout=5)

        def approve():
            barrier.wait()
            return self.decision()

        with ThreadPoolExecutor(2) as executor:
            self.assertEqual(["sent", "sent"], list(executor.map(lambda _: approve(), range(2))))
        self.assertEqual(1, len(self.pilot.snapshot()["payments"]))

    def test_race_between_cancellation_and_approval(self):
        self.submit()
        self.pilot.resolve("test", "contact")
        barrier = threading.Barrier(2, timeout=5)

        def act(action):
            barrier.wait()
            return self.decision(action)

        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(act, ["cancel", "approve"]))
        self.assertEqual(results[0], results[1])
        self.assertIn(results[0], ("cancelled", "sent"))
        self.assertEqual(int(results[0] == "sent"), len(self.pilot.snapshot()["payments"]))

    def test_deadline_wins_even_if_no_poll_has_run(self):
        self.submit()
        self.pilot.resolve("test", "contact")
        self.now += 30
        self.assertEqual("expired", self.decision())
        self.assertEqual([], self.pilot.snapshot()["payments"])

    def test_retry_keeps_original_terms_and_does_not_send_again(self):
        self.assertEqual("held", self.submit())
        self.pilot.resolve("test", "contact")
        self.decision()
        self.assertEqual("sent", self.submit())
        with self.assertRaises(DecisionError):
            self.submit(amount=300000)
        self.assertEqual(1, len(self.pilot.snapshot()["payments"]))

    def test_switch_to_shadow_leaves_existing_holds_in_place(self):
        self.submit()
        self.pilot.set_mode("shadow")
        self.assertEqual("held", self.submit())
        self.assertEqual("sent", self.submit("shadow"))
        self.assertEqual("declined", self.submit("blocked", blocked=True))
        snapshot = self.pilot.snapshot()
        self.assertEqual(["shadow"], [row["transfer_id"] for row in snapshot["payments"]])
        self.assertTrue(any(
            event["kind"] == "would_hold" and event["transfer_id"] == "shadow"
            for event in snapshot["events"]
        ))

    def test_state_survives_server_restart(self):
        self.submit()
        self.pilot = Pilot(self.pilot.database, 30, lambda: self.now)
        self.assertEqual("held", self.submit())
        self.assertEqual([], self.pilot.snapshot()["payments"])

    def test_http_demo_path(self):
        with make_server(self.pilot) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(base, timeout=5) as response:
                    page = response.read().decode()
                token = re.search(r'const token = "([^"]+)"', page).group(1)
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/api/state", timeout=5)
                self.assertEqual(403, error.exception.code)
                error.exception.close()

                def call(path, data=None):
                    body = None if data is None else json.dumps(data).encode()
                    req = Request(base + "/api/" + path, data=body,
                                  headers={"X-Demo-Token": token, "Content-Type": "application/json"})
                    with urlopen(req, timeout=5) as response:
                        return json.load(response)

                self.assertEqual("held", call("submit", {
                    "transfer_id": "http", "amount": 250000, "new_payee": True,
                    "unusual_amount": True, "urgent": False, "blocked": False,
                })["state"])
                call("resolve", {"transfer_id": "http", "action": "contact"})
                call("resolve", {"transfer_id": "http", "action": "approve", "note": "Проверено."})
                self.assertEqual("http", call("state")["payments"][0]["transfer_id"])
            finally:
                server.shutdown()
                worker.join()


if __name__ == "__main__":
    unittest.main()
