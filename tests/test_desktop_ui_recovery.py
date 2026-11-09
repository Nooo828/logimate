"""Stopping an unknown process must fail before any mutation."""
import pytest
from scripts.restart_desktop_ui import verified_processes, INSTALL


@pytest.mark.parametrize('part',['Docker Desktop.exe','frontend/Docker Desktop.exe','resources/com.docker.backend.exe'])
def test_only_installed_docker_components_are_accepted(part):
    rows=[{'Id':123,'Path':str(INSTALL/part)}]
    assert verified_processes(rows)==rows


@pytest.mark.parametrize('row',[{'Id':123,'Path':None},{'Id':123,'Path':r'C:\Other\Docker Desktop.exe'},
    {'Id':'123','Path':str(INSTALL/'Docker Desktop.exe')},{'Id':True,'Path':str(INSTALL/'Docker Desktop.exe')}])
def test_unknown_path_or_unverified_pid_prevents_forced_stop(row):
    with pytest.raises(ValueError):verified_processes([row])
