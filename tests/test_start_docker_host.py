"""Explicit fake command responses; not actual Docker acceptance."""
import json
from types import SimpleNamespace
import pytest
from scripts.start_docker_host import DESKTOP_PIPE, decode, linux_server, run_acceptance, wait_for_engine


@pytest.mark.parametrize("code,output", [(1, '{"Version":"29", "Os":"linux"}'),
    (0, 'null'), (0, '{}'), (0, 'not json'), (0, '{"Version":"29", "Os":"windows"}')])
def test_linux_server_gate(code, output):
    assert linux_server(code, output) is None


def test_wsl_unicode():
    text = "拒绝访问\r\nWsl/E_ACCESSDENIED"
    assert decode(text.encode("utf-16-le")) == text
    assert decode("Stopped".encode("utf-16")) == "Stopped"


def test_launch_success_is_not_readiness():
    now, attempts = [0], []
    def sleep(seconds):
        now[0] += seconds
    def probe():
        attempts.append(True)
        return 0, "null"
    assert wait_for_engine(probe, 10, clock=lambda: now[0], sleep=sleep) is None
    assert len(attempts) == 3


def test_retry_until_server_ready():
    replies = iter([(1, "missing pipe"), (0, json.dumps({"Version": "29", "Os": "linux"}))])
    server = wait_for_engine(lambda: next(replies), 10, clock=lambda: 0, sleep=lambda _: None)
    assert server["Os"] == "linux"


def test_no_acceptance_without_engine_and_option():
    def forbidden(*args, **kwargs):
        pytest.fail("Must not launch acceptance")
    assert run_acceptance(None, True, {}, forbidden) is None
    assert run_acceptance({"Os": "linux"}, False, {}, forbidden) is None


def test_checked_engine_and_failure_propagation():
    env = {"DOCKER_CONTEXT": "unrelated", "OTHER": "keep"}
    def runner(argv, **kwargs):
        assert argv[-1] == "scripts.verify_milvus"
        assert kwargs["env"]["DOCKER_HOST"] == DESKTOP_PIPE
        assert "DOCKER_CONTEXT" not in kwargs["env"]
        assert kwargs["env"]["OTHER"] == "keep"
        return SimpleNamespace(returncode=1)
    assert run_acceptance({"Os": "linux"}, True, env, runner) == 1
    assert env["DOCKER_CONTEXT"] == "unrelated"
