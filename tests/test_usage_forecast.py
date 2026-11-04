import json
from datetime import datetime, timedelta, timezone

import pytest

from rerunly import db
from rerunly.usage_forecast import observe, forecast


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def reading(at, percent, reset=None, plan="plus"):
    return {"agent": "Codex", "plan": plan, "live": False, "observed_at": at.isoformat(),
            "windows": [{"window_minutes": 300, "used_percent": percent,
                         "resets_at": reset or (NOW + timedelta(hours=5)).timestamp()}]}


def add_run(c, key, start, end, model="model-a", tokens=1000, harness="Codex"):
    with c:
        c.execute("INSERT INTO prompts(id,session_id,source,text,started_at,ended_at,harness,models,input_tokens) "
                  "VALUES(?,?,?,'example',?,?,?,?,?)", (key, key, 'test', start.isoformat(), end.isoformat(),
                                                       harness, json.dumps({model: 1}), tokens))


def collect(c):
    observe(c, reading(NOW, 10), now=NOW)
    for i in range(1, 4):
        start = NOW + timedelta(minutes=(i - 1) * 10 + 1)
        end = NOW + timedelta(minutes=i * 10)
        add_run(c, str(i), start, end)
        observe(c, reading(end, 10 + i * 2), now=end)
    return end


def test_model_specific_persistent_learning_and_projection(tmp_path):
    path = tmp_path / 'test.db'
    c = db.connect(path)
    end = collect(c)
    c.close()
    c = db.connect(path)
    estimate = {"tokens_by_model": {"model-a": 2000}, "duration_ms": 1000}
    w = forecast(c, reading(end, 16), estimate, now=end)["windows"][0]
    assert w["status"] == "estimated"
    assert w["additional_percent"] == pytest.approx(4)
    assert w["additional_percent_low"] == pytest.approx(2)
    assert w["additional_percent_high"] == pytest.approx(6)
    assert w["projected_percent"] == pytest.approx(20)
    assert w["models"][0]["model"] == "model-a"
    assert w["models"][0]["samples"] == 3
    assert w["models"][0]["tokens_per_percentage_point"] == 500
    assert forecast(c, reading(end, 16), {"tokens_by_model": {"other": 2000}}, now=end)["status"] == "learning"
    assert forecast(c, reading(end, 16, plan="pro"), estimate, now=end)["status"] == "learning"
    assert "projected_percent" not in forecast(c, reading(end, 16), estimate, now=end + timedelta(hours=2))["windows"][0]


@pytest.mark.parametrize('case', ['reset', 'decrease', 'plan', 'mixed', 'late', 'stale', 'saturated', 'unknown'])
def test_reject_unattributable_intervals(tmp_path, case):
    c = db.connect(tmp_path / 'test.db')
    observe(c, reading(NOW, 10), now=NOW)
    end = NOW + timedelta(minutes=10)
    add_run(c, 'one', NOW + timedelta(minutes=-1 if case == 'late' else 1), end)
    if case == 'mixed':
        add_run(c, 'two', NOW + timedelta(minutes=2), end, model='model-b')
    if case == 'unknown':
        with c:
            c.execute("UPDATE prompts SET models='{}'")
    r = reading(end, 9 if case == 'decrease' else 100 if case == 'saturated' else 12,
                reset=(NOW + timedelta(hours=6)).timestamp() if case == 'reset' else None,
                plan='pro' if case == 'plan' else 'plus')
    observe(c, r, now=end + timedelta(hours=2) if case == 'stale' else end)
    assert c.execute('SELECT COUNT(*) FROM usage_samples').fetchone()[0] == 0


def test_duplicate_reads_and_reimports_do_not_create_samples(tmp_path):
    c = db.connect(tmp_path / 'test.db')
    end = collect(c)
    observe(c, reading(end, 16), now=end)
    assert c.execute('SELECT COUNT(*) FROM usage_samples').fetchone()[0] == 3
    with c:
        c.execute('UPDATE prompts SET input_tokens=500 WHERE id="3"')
    observe(c, reading(end + timedelta(minutes=1), 17), now=end + timedelta(minutes=1))
    assert c.execute('SELECT COUNT(*) FROM usage_samples').fetchone()[0] == 3


def test_zero_increase_is_recorded_without_inventing_a_rate(tmp_path):
    c = db.connect(tmp_path / 'test.db')
    observe(c, reading(NOW, 10), now=NOW)
    for i in range(1, 4):
        end = NOW + timedelta(minutes=i * 10)
        add_run(c, str(i), end - timedelta(minutes=1), end)
        observe(c, reading(end, 10), now=end)
    result = forecast(c, reading(end, 10), {'tokens_by_model': {'model-a': 1000}}, now=end)
    assert result['status'] == 'learning'
    assert result['windows'][0]['models'][0]['samples'] == 3


def test_child_models_are_not_charged_to_parent(tmp_path):
    c = db.connect(tmp_path / 'test.db')
    observe(c, reading(NOW, 10), now=NOW)
    end = NOW + timedelta(minutes=10)
    add_run(c, 'parent', NOW + timedelta(minutes=1), end)
    with c:
        c.execute("INSERT INTO agents(id,prompt_id,session_id,started_at,ended_at,models,input_tokens) VALUES('child','parent','parent',?,?,?,500)",
                  ((NOW + timedelta(minutes=2)).isoformat(), end.isoformat(), json.dumps({'model-b': 1})))
    observe(c, reading(end, 12), now=end)
    assert c.execute('SELECT COUNT(*) FROM usage_samples').fetchone()[0] == 0
