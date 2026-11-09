"""No provider or Docker requests. Verify credential failure gates and secret handling."""
import json
from types import SimpleNamespace

from scripts.probe_shared_model import probe, continue_verification, classify_host


def test_provider_401_is_preserved_but_never_starts_acceptance():
    calls = []
    def runner(argv, **kw):
        assert 'explicit-key-value' not in argv
        assert kw['env']['DEEPSEEK_API_KEY'] == 'explicit-key-value'
        return SimpleNamespace(returncode=0, stdout=json.dumps({'ok':False,'http_status':401,'error_kind':'provider_http'}))
    row = probe('explicit-key-value', {}, runner=runner)
    assert row['http_status'] == 401 and row['ok'] is False
    assert classify_host(row) == 'credential_rejected'
    assert continue_verification(row, True, {}, runner=lambda *a,**k:calls.append(a)) is None
    assert calls == []


def test_probe_success_only_continues_when_explicitly_requested_and_key_stays_in_env():
    calls = []
    row = {'ok':True,'http_status':200,'response_id':'explicit-probe-id','usage':{'total_tokens':9}}
    result = probe('explicit-key-value', {}, runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(row)))
    assert result == row
    assert classify_host(row) == 'host_probe_passed_not_yet_container_acceptance'
    assert continue_verification(row, False, {}, runner=lambda *a,**k:calls.append(a)) is None
    def verify(argv, **kw):
        assert 'explicit-key-value' not in argv and kw['env']['DEEPSEEK_API_KEY']=='explicit-key-value'
        assert '--local-parent' in argv and 'scripts.verify_shared_graph_docker' in argv
        calls.append(argv); return 7
    assert continue_verification(row, True, {'DEEPSEEK_API_KEY':'explicit-key-value'}, runner=verify)==7
    assert len(calls)==1


def test_unexpected_secret_echo_is_withheld():
    row = probe('explicit-key-value', {}, runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps({'ok':False,'data':'explicit-key-value'})))
    assert row == {'ok':False,'error_kind':'sensitive_output_withheld'}


def test_200_without_response_id_is_not_an_auth_success():
    row = probe('explicit-key-value', {}, runner=lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps({'ok':True,'http_status':200})))
    assert row['ok'] is False and row['error_kind']=='invalid_provider_success'
