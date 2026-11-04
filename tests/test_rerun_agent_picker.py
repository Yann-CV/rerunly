"""Keep the rerun picker aligned with model-based agent routing."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_rerun_model_agent():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend checks")
    script = (Path(__file__).parents[1] / "rerunly/static/app.js").read_text()
    helper = script.split('function rerunModelAgent(', 1)[1].split('\nconst EFFORTS', 1)[0]
    subprocess.run([node, "-e", "function rerunModelAgent(" + helper + """
const assert = require('node:assert/strict');
assert.equal(rerunModelAgent('codex:gpt-6.1-sol'), 'codex');
assert.equal(rerunModelAgent('gpt-6.1-sol'), 'codex');
assert.equal(rerunModelAgent('claude-sonnet-4-6'), 'claude');
assert.equal(rerunModelAgent('gpt-oss-120b'), 'claude');
assert.equal(rerunModelAgent('', 'codex'), 'codex');
assert.equal(rerunModelAgent('', 'claude'), 'claude');
"""], check=True)
