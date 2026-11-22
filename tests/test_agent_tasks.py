import tempfile
import unittest
from pathlib import Path

import agent_tasks


class AgentTaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = agent_tasks.DB_PATH
        agent_tasks.DB_PATH = str(Path(self.temp.name) / "agent-tasks.sqlite3")
        agent_tasks.init_agent_tasks_db()

    def tearDown(self):
        agent_tasks.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_create_and_read_task_is_owner_scoped(self):
        task_id = agent_tasks.create_agent_task("owner:1", "Research a topic and prepare a report.")
        task = agent_tasks.get_agent_task("owner:1", task_id)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["progress"], 0)
        self.assertEqual(task["request"], "Research a topic and prepare a report.")
        self.assertIsNone(agent_tasks.get_agent_task("owner:2", task_id))

    def test_queued_task_can_be_cancelled(self):
        task_id = agent_tasks.create_agent_task("owner:1", "Do a multi-step task.")
        self.assertEqual(agent_tasks.cancel_agent_task("owner:1", task_id), "cancelled")
        task = agent_tasks.get_agent_task("owner:1", task_id)
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["events"][-1]["event_type"], "cancelled")

    def test_running_task_receives_cancellation_request(self):
        task_id = agent_tasks.create_agent_task("owner:1", "Do a multi-step task.")
        claimed = agent_tasks._claim_next_task()
        self.assertEqual(claimed, task_id)
        self.assertEqual(agent_tasks.cancel_agent_task("owner:1", task_id), "cancellation_requested")
        self.assertTrue(agent_tasks._is_cancelled(task_id))

    def test_task_progress_and_results_are_persisted(self):
        task_id = agent_tasks.create_agent_task("owner:1", "Prepare a report.")
        agent_tasks._claim_next_task()
        plan = [
            {"title": "Research", "goal": "Find reliable information."},
            {"title": "Write", "goal": "Write a report."},
        ]
        agent_tasks._save_plan(task_id, plan, [])
        agent_tasks._progress(task_id, 1, [{"step": "Research", "output": "Finding A"}], "Research complete")
        task = agent_tasks.get_agent_task("owner:1", task_id)
        self.assertEqual(task["total_steps"], 2)
        self.assertEqual(task["current_step"], 1)
        self.assertEqual(task["progress"], 50)
        self.assertEqual(task["step_results"][0]["output"], "Finding A")

    def test_plan_parser_bounds_steps_and_rejects_malformed_output(self):
        raw = '{"steps":[{"title":"One","goal":"Do one"},{"title":"Two","goal":"Do two"}]}'
        self.assertEqual(len(agent_tasks._extract_plan(raw)), 2)
        self.assertEqual(len(agent_tasks._extract_plan("not JSON")), 1)
        oversized = '{"steps":[' + ",".join(
            '{"title":"S%d","goal":"G"}' % index for index in range(20)
        ) + ']}'
        self.assertEqual(len(agent_tasks._extract_plan(oversized)), agent_tasks.MAX_STEPS)

    def test_empty_and_oversized_tasks_are_rejected(self):
        with self.assertRaises(ValueError):
            agent_tasks.create_agent_task("owner:1", " ")
        with self.assertRaises(ValueError):
            agent_tasks.create_agent_task("owner:1", "x" * 10001)

    def test_task_lists_do_not_include_full_step_outputs(self):
        task_id = agent_tasks.create_agent_task("owner:1", "A long task.")
        agent_tasks._claim_next_task()
        agent_tasks._save_plan(task_id, [{"title": "Step", "goal": "Goal"}], [])
        listing = agent_tasks.list_agent_tasks("owner:1")
        self.assertEqual(len(listing), 1)
        self.assertNotIn("step_results", listing[0])
        self.assertEqual(listing[0]["task_id"], task_id)


if __name__ == "__main__":
    unittest.main()
