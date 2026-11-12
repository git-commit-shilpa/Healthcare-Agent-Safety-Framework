"""Real-PostgreSQL checks for database/database_setup.sql.

Builds an empty throwaway database from the setup script, then runs the behaviour checks in
smoke_test.sql and three concurrency checks. It never touches Supabase and is skipped unless you point
it at a LOCAL disposable server:

    set HASF_PG_TEST_URI=postgresql://postgres@127.0.0.1:5432/postgres
    python -m pytest backend/tests/pg

Needs the `psql` client on PATH (or set HASF_PSQL to its full path) and the btree_gist extension.
"""

import os
import subprocess
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
SETUP_SQL = HERE.parents[2] / "database" / "database_setup.sql"
URI = os.environ.get("HASF_PG_TEST_URI", "")
PSQL = os.environ.get("HASF_PSQL", "psql")

FIXTURES = """
insert into auth.users (id, email, email_confirmed_at) values
  ('00000000-0000-0000-0000-000000000001', 'patient.a@example.test', now()),
  ('00000000-0000-0000-0000-000000000002', 'patient.b@example.test', now()),
  ('00000000-0000-0000-0000-000000000003', 'tech@example.test', now()),
  ('00000000-0000-0000-0000-000000000004', 'physician@example.test', now());
insert into public.users (user_id, name, email, role, auth_user_id) overriding system value values
  (1, 'Patient A', 'patient.a@example.test', 'Patient',    '00000000-0000-0000-0000-000000000001'),
  (2, 'Patient B', 'patient.b@example.test', 'Patient',    '00000000-0000-0000-0000-000000000002'),
  (3, 'Tech T',    'tech@example.test',      'Technician', '00000000-0000-0000-0000-000000000003'),
  (4, 'Dr P',      'physician@example.test', 'Physician',  '00000000-0000-0000-0000-000000000004');
insert into public.patients (patient_id, user_id, age, medical_condition, treatment) overriding system value values
  (1, 1, 40, 'Fictional condition A', 'Fictional treatment A'),
  (2, 2, 50, 'Fictional condition B', 'Fictional treatment B');
insert into public.patient_assignments values (1, 3), (1, 4), (2, 3), (2, 4);
"""


def _local(uri):
    host = urlsplit(uri).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("/")


def _with_db(uri, name):
    parts = urlsplit(uri)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, "", ""))


def psql(uri, sql=None, file=None):
    cmd = [PSQL, "--dbname", uri, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]
    cmd += ["-f", str(file)] if file else ["-c", sql]
    return subprocess.run(cmd, capture_output=True, text=True)


@unittest.skipUnless(URI, "set HASF_PG_TEST_URI to a local throwaway PostgreSQL to run these tests")
class DatabaseSetupTests(unittest.TestCase):
    created = []

    @classmethod
    def setUpClass(cls):
        if not _local(URI):
            raise RuntimeError("HASF_PG_TEST_URI must point at a local server; refusing to run.")
        cls.created = []

    @classmethod
    def tearDownClass(cls):
        for name in cls.created:
            psql(URI, f'drop database if exists "{name}" with (force)')

    def new_database(self):
        name = "hasf_test_" + uuid.uuid4().hex[:10]
        self.created.append(name)
        self.assertEqual(psql(URI, f'create database "{name}"').returncode, 0)
        uri = _with_db(URI, name)
        for step in (HERE / "supabase_stub.sql", SETUP_SQL):
            result = psql(uri, file=step)
            self.assertEqual(result.returncode, 0, result.stderr)
        return uri

    def test_behaviour_checks_pass(self):
        uri = self.new_database()
        result = psql(uri, file=HERE / "smoke_test.sql")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith(("FAIL|", "TOTAL|"))]
        failures = [line for line in lines if line.startswith("FAIL|")]
        self.assertEqual(failures, [], "\n".join(failures))
        total = next(line for line in lines if line.startswith("TOTAL|")).split("|")
        self.assertEqual(total[1], total[2])
        self.assertGreaterEqual(int(total[1]), 30)

    def test_setup_script_can_be_applied_only_once(self):
        uri = self.new_database()
        self.assertNotEqual(psql(uri, file=SETUP_SQL).returncode, 0)

    def test_two_concurrent_confirmations_of_one_slot_have_exactly_one_winner(self):
        uri = self.new_database()
        setup = FIXTURES + """
        select public.upsert_schedule_rule(4, null, 1, 540, 720, 30, 'UTC', null, null);
        select set_config('t.slot', to_char(date_trunc('week', now() + interval '21 days') + interval '10 hours',
                          'YYYY-MM-DD"T"HH24:MI:SS"+00"'), false);
        select public.submit_appointment_request(1, 4, current_setting('t.slot')::timestamptz, 'Fictional visit', null);
        select public.submit_appointment_request(2, 4, current_setting('t.slot')::timestamptz, 'Fictional visit', null);
        select public.start_technician_task_review(task_id, 3) from public.technician_tasks
         where category = 'appointment_scheduling';
        """
        self.assertEqual(psql(uri, setup).returncode, 0)
        tasks = psql(uri, "select task_id from public.technician_tasks where category = 'appointment_scheduling' order by 1")
        task_ids = tasks.stdout.split()
        self.assertEqual(len(task_ids), 2)

        def confirm(task_id):
            return psql(uri, f"select public.record_technician_task_decision({task_id}, 3, 'ALLOW', null, null, "
                             "'Your appointment is confirmed for that time.', 'Allow', 'low_risk_permitted')")

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(confirm, task_ids))
        self.assertEqual(sorted(r.returncode == 0 for r in results), [False, True])
        loser = next(r for r in results if r.returncode != 0)
        self.assertIn("slot_unavailable", loser.stderr)
        confirmed = psql(uri, "select count(*) from public.appointments where status = 'Confirmed'")
        self.assertEqual(confirmed.stdout.strip(), "1")

    def test_two_concurrent_escalations_create_exactly_one_approval(self):
        uri = self.new_database()
        setup = FIXTURES + """
        select public.submit_patient_treatment_correction(1, 'Fictional new treatment A');
        select public.start_technician_task_review(task_id, 3) from public.technician_tasks
         where category = 'record_correction';
        """
        self.assertEqual(psql(uri, setup).returncode, 0)
        task_id = psql(uri, "select task_id from public.technician_tasks where category = 'record_correction'").stdout.strip()

        def escalate(_):
            return psql(uri, f"select public.coordinate_correction_task({task_id}, 3, 'request_physician_authorization', 'ready', null)")

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(escalate, range(2)))
        self.assertTrue(any(r.returncode == 0 for r in results), [r.stderr for r in results])
        approvals = psql(uri, "select count(*) from public.approvals where status = 'Pending'")
        self.assertEqual(approvals.stdout.strip(), "1")

    def test_identical_concurrent_chat_saves_store_the_turn_once(self):
        uri = self.new_database()
        self.assertEqual(psql(uri, FIXTURES).returncode, 0)
        chat, turn = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"

        def save(_):
            return psql(uri, f"select public.save_patient_chat_turn(1, '{chat}', true, '{turn}', "
                             "'fictional question', 'fictional answer', 'answer')")

        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(save, range(8)))
        self.assertTrue(any(r.returncode == 0 for r in results), [r.stderr for r in results])
        counts = psql(uri, "select (select count(*) from public.patient_chats), (select count(*) from public.patient_chat_messages)")
        self.assertEqual(counts.stdout.strip(), "1|2")


if __name__ == "__main__":
    unittest.main()
