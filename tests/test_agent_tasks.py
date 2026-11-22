import os
import sqlite3
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
        self.assertNotIn("owner_key", task)
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
        self.assertEqual(task["progress"], 47)
        self.assertEqual(task["step_results"][0]["output"], "Finding A")

    def test_expired_worker_lease_allows_task_recovery(self):
        task_id = agent_tasks.create_agent_task("owner:1", "Recover after worker restart.")
        self.assertEqual(agent_tasks._claim_next_task(), task_id)
        with sqlite3.connect(agent_tasks.DB_PATH) as conn:
            conn.execute(
                "UPDATE agent_tasks SET lease_until=? WHERE task_id=?",
                ("2000-01-01T00:00:00+00:00", task_id),
            )
        self.assertEqual(agent_tasks._claim_next_task(), task_id)
        task = agent_tasks.get_agent_task("owner:1", task_id)
        self.assertEqual(task["attempt_count"], 2)

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

    def test_active_task_limit_is_enforced_per_owner(self):
        previous = agent_tasks.MAX_ACTIVE_TASKS_PER_OWNER
        agent_tasks.MAX_ACTIVE_TASKS_PER_OWNER = 1
        try:
            first = agent_tasks.create_agent_task("owner:1", "First task.")
            with self.assertRaises(ValueError):
                agent_tasks.create_agent_task("owner:1", "Second task.")
            agent_tasks.cancel_agent_task("owner:1", first)
            second = agent_tasks.create_agent_task("owner:1", "Second task.")
            self.assertNotEqual(first, second)
            other_owner = agent_tasks.create_agent_task("owner:2", "Another owner's task.")
            self.assertNotEqual(second, other_owner)
        finally:
            agent_tasks.MAX_ACTIVE_TASKS_PER_OWNER = previous

    def test_task_lists_do_not_include_full_step_outputs(self):
        task_id = agent_tasks.create_agent_task("owner:1", "A long task.")
        agent_tasks._claim_next_task()
        agent_tasks._save_plan(task_id, [{"title": "Step", "goal": "Goal"}], [])
        listing = agent_tasks.list_agent_tasks("owner:1")
        self.assertEqual(len(listing), 1)
        self.assertNotIn("step_results", listing[0])
        self.assertEqual(listing[0]["task_id"], task_id)



class AgentTaskWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = agent_tasks.DB_PATH
        agent_tasks.DB_PATH = str(Path(self.temp.name) / "agent-worker.sqlite3")
        agent_tasks.init_agent_tasks_db()

    def tearDown(self):
        agent_tasks.DB_PATH = self.old_path
        self.temp.cleanup()

    async def test_worker_completes_plan_steps_quality_review_and_final_answer(self):
        old_run_agent = agent_tasks.run_agent
        old_key = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "unit-test-provider-key"

        async def fake_run_agent(messages, headers):
            system = messages[0]["content"]
            if "Create a practical plan" in system:
                answer = '{"steps":[{"title":"Research","goal":"Gather facts"},{"title":"Synthesize","goal":"Combine findings"}]}'
            elif "independent quality reviewer" in system:
                answer = "PASS: Both planned steps are complete and the limitations are stated."
            elif "Produce the final deliverable" in system:
                answer = "Final report: the two steps were completed and reviewed."
            else:
                answer = "Step output: verified test finding."
            return {
                "assistant_message": {"content": answer},
                "model": "mock-agent-model",
                "usage": {"total_tokens": 12},
                "tool_steps": 0,
            }

        agent_tasks.run_agent = fake_run_agent
        try:
            task_id = agent_tasks.create_agent_task("owner:worker-test", "Prepare and review a short report.")
            self.assertEqual(agent_tasks._claim_next_task(), task_id)
            await agent_tasks._execute_task(task_id)
            task = agent_tasks.get_agent_task("owner:worker-test", task_id)
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["progress"], 100)
            self.assertEqual(task["total_steps"], 2)
            self.assertEqual(len(task["step_results"]), 2)
            self.assertIn("Final report", task["result"])
            self.assertTrue(any(event["event_type"] == "quality_review" for event in task["events"]))
        finally:
            agent_tasks.run_agent = old_run_agent
            if old_key is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            else:
                os.environ["OPENROUTER_API_KEY"] = old_key


if __name__ == "__main__":
    unittest.main()
