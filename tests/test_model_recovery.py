import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from evaluation.recovery import recovery_case


class ModelRecoveryTest(TestCase):
    def test_retries_and_checkpoint_resume_preserve_confirmed_step(self):
        with TemporaryDirectory() as temp, patch("app.observability.TRACE_DIR", Path(temp)):
            for fault, count in (("timeout", 1), ("rate_limit", 1), ("length", 1), ("timeout", 2), ("auth", 1)):
                with self.subTest(fault=fault, injections=count):
                    result = recovery_case(fault, count)
                    self.assertTrue(result["passed"], result)
                    path = Path(temp) / f"{result['thread_id']}.jsonl"
                    self.assertEqual([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()],
                                     result["trace"])
                    self.assertNotIn("清洁费用", path.read_text(encoding="utf-8"))
