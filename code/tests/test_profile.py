import tempfile
import unittest
from pathlib import Path

from personal_agent.errors import AgentError
from personal_agent.service import CalendarService


class ProfileBackend:
    kind = "external_test"
    capabilities = {"remote": True}

    def __init__(self, profile_id):
        self.profile_id = profile_id


class ProfileTests(unittest.TestCase):
    def test_local_database_cannot_be_reused_for_remote_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "calendar.sqlite3"
            CalendarService(db)
            with self.assertRaises(AgentError) as error:
                CalendarService(db, backend=ProfileBackend("personal"))
            self.assertEqual("backend_profile_mismatch", error.exception.code)

    def test_account_profile_binding_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "calendar.sqlite3"
            CalendarService(db, backend=ProfileBackend("personal"))
            CalendarService(db, backend=ProfileBackend("personal"))
            with self.assertRaises(AgentError) as error:
                CalendarService(db, backend=ProfileBackend("work"))
            self.assertEqual("backend_profile_mismatch", error.exception.code)


if __name__ == "__main__":
    unittest.main()
