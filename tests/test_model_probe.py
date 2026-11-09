from io import BytesIO
import json
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from scripts.probe_model_network import PROBE_CODE, classify


def test_http_credentials_and_container_paths_are_distinguished():
    assert classify({'ok':False,'http_status':401},{'ok':False})=='credential_rejected'
    assert classify({'ok':False,'http_status':402},{'ok':False})=='balance_or_billing_rejected'
    assert classify({'ok':True},{'ok':False,'error_kind':'tls'})=='container_path_failure_or_runtime_environment'
    assert classify({'ok':True},{'ok':True})=='both_probes_passed_not_yet_full_acceptance'


def test_provider_error_body_is_never_read(monkeypatch,capsys):
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_CREDENTIAL_SHOULD_NOT_APPEAR')
    class SensitiveBody(BytesIO):
        def read(self,*a,**kw):
            raise AssertionError('Never read provider error body')
    error=HTTPError('https://api.deepseek.com/chat/completions',401,'auth',{},SensitiveBody(b'secret'))
    with patch('urllib.request.urlopen',side_effect=error):
        exec(PROBE_CODE,{})
    row=json.loads(capsys.readouterr().out)
    assert row['http_status']==401 and not row['ok']
    assert 'TEST_CREDENTIAL' not in json.dumps(row)


def test_network_error_does_not_expose_proxy_url(monkeypatch,capsys):
    monkeypatch.setenv('DEEPSEEK_API_KEY','TEST_CREDENTIAL_SHOULD_NOT_APPEAR')
    with patch('urllib.request.urlopen',side_effect=URLError('http://user:password@proxy.invalid')):
        exec(PROBE_CODE,{})
    row=json.loads(capsys.readouterr().out)
    assert row['error_kind']=='network'
    assert 'password' not in json.dumps(row)
