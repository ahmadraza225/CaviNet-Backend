import fakeredis
from rq import SimpleWorker

from app.workers.jobs import ping
from app.workers.queue import get_analysis_queue


def test_queue_uses_configured_name(monkeypatch):
    monkeypatch.setenv("ANALYSIS_QUEUE", "analysis-test")
    queue = get_analysis_queue(connection=fakeredis.FakeStrictRedis())
    assert queue.name == "analysis-test"


def test_worker_runs_enqueued_job():
    connection = fakeredis.FakeStrictRedis()
    queue = get_analysis_queue(connection=connection)
    job = queue.enqueue(ping)
    SimpleWorker([queue], connection=connection).work(burst=True)
    job.refresh()
    assert job.is_finished
    assert job.return_value() == "pong"
