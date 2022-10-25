import json
import os
import re
import subprocess
import time

import pytest
import requests
from click.testing import CliRunner
from yaml import safe_load

from gravity.cli import galaxyctl
from gravity.state import CELERY_BEAT_DB_FILENAME

STARTUP_TIMEOUT = 30
CELERY_BEAT_TIMEOUT = 10
# celery.beat.PersistentScheduler uses shelve, which can append a suffix based on which db backend is used
CELERY_BEAT_DB_FILENAMES = list(map(lambda ext: CELERY_BEAT_DB_FILENAME + ext, ('', '.db', '.dat', '.bak', '.dir')))


def log_for_service(state_dir, service_name, process_manager_name, start_time):
    if process_manager_name == "systemd":
        cmd = f"journalctl --user --no-pager --since=@{start_time} --unit=galaxy-{service_name}.service".split()
        return subprocess.check_output(cmd, text=True)
    else:
        path = state_dir / "log" / f"{service_name}.log"
        with open(path) as fh:
            return fh.read()


def wait_for_startup(state_dir, free_port, prefix="/", path="/api/version", service_name="gunicorn",
                     process_manager_name="supervisor", start_time=None):
    for _ in range(STARTUP_TIMEOUT * 4):
        try:
            requests.get(f"http://localhost:{free_port}{prefix.rstrip('/')}{path}").raise_for_status()
            return True, ""
        except Exception:
            time.sleep(0.25)
    return False, log_for_service(state_dir, service_name, process_manager_name, start_time)


def wait_for_gxit_proxy(state_dir, process_manager_name, start_time):
    for _ in range(STARTUP_TIMEOUT * 4):
        startup_logs = log_for_service(state_dir, "gx-it-proxy", process_manager_name, start_time)
        if 'Watching path' in startup_logs:
            return True, ""
        time.sleep(0.25)
    return False, startup_logs


def wait_for_any_path(paths, timeout):
    for _ in range(timeout * 4):
        try:
            assert any(map(lambda x: x.exists(), paths))
            return True
        except AssertionError:
            time.sleep(0.25)
    return False


def start_instance(state_dir, galaxy_yml, free_port, process_manager_name="supervisor"):
    runner = CliRunner()
    start_time = time.time()
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'start'])
    assert result.exit_code == 0, result.output
    if process_manager_name == "systemd":
        result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'status'])
        output = subprocess.check_output("systemctl --user status galaxy-gunicorn.service".split(), text=True)
        assert "● galaxy-gunicorn.service" in output
    else:
        assert re.search(r"gunicorn\s*STARTING", result.output)
    startup_done, startup_logs = wait_for_startup(state_dir, free_port, process_manager_name=process_manager_name,
                                                  start_time=start_time)
    assert startup_done is True, f"Startup failed. Application startup logs:\n {startup_logs}"


@pytest.mark.parametrize('process_manager_name', ['supervisor', 'systemd'])
def test_cmd_start(state_dir, galaxy_yml, startup_config, free_port, process_manager_name):
    # TODO: test service_command_style = gravity, doesn't work when you're using CliRunner, which just imports the cli
    # rather than the entry point existing on the filesystem somewhere.
    # TODO: systemd tests should use a randomly generated instance name so that running tests locally does not result in
    # service unit name collisions
    startup_config["gravity"]["process_manager"] = process_manager_name
    galaxy_yml.write(json.dumps(startup_config))
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'update'])
    assert result.exit_code == 0, result.output
    start_instance(state_dir, galaxy_yml, free_port, process_manager_name)
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'status'])
    celery_beat_db_paths = list(map(lambda f: state_dir / f, CELERY_BEAT_DB_FILENAMES))
    celery_beat_db_exists = wait_for_any_path(celery_beat_db_paths, CELERY_BEAT_TIMEOUT)
    assert celery_beat_db_exists is True, "celery-beat failed to write db. State dir contents:\n" \
        f"{os.listdir(state_dir)}"
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'stop'])
    assert result.exit_code == 0, result.output
    if process_manager_name == "supervisor":
        assert "All processes stopped, supervisord will exit" in result.output
    else:
        assert "" == result.output


def test_cmd_start_reports(state_dir, galaxy_yml, reports_config, free_port):
    galaxy_yml.write(json.dumps(reports_config))
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'update'])
    assert result.exit_code == 0, result.output
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'start'])
    assert re.search(r"reports\s*STARTING", result.output)
    assert result.exit_code == 0, result.output
    startup_done, startup_logs = wait_for_startup(state_dir, free_port, path="/", service_name="reports")
    assert startup_done is True, f"Startup failed. Application startup logs:\n {startup_logs}"
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'stop'])
    assert result.exit_code == 0, result.output
    assert "All processes stopped, supervisord will exit" in result.output


@pytest.mark.parametrize('process_manager_name', ['supervisor', 'systemd'])
def test_cmd_start_with_gxit(state_dir, galaxy_yml, gxit_startup_config, free_port, process_manager_name):
    galaxy_yml.write(json.dumps(gxit_startup_config))
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'update'])
    assert result.exit_code == 0, result.output
    start_time = time.time()
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'start'])
    assert result.exit_code == 0, result.output
    start_instance(state_dir, galaxy_yml, free_port, process_manager_name)
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'status'])
    assert result.exit_code == 0, result.output
    startup_done, startup_logs = wait_for_gxit_proxy(state_dir, process_manager_name, start_time)
    assert startup_done is True, f"gx-it-proxy startup failed. gx-it-proxy startup logs:\n {startup_logs}"


def test_cmd_restart_with_update(state_dir, galaxy_yml, startup_config, free_port):
    galaxy_yml.write(json.dumps(startup_config))
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'update'])
    assert result.exit_code == 0, result.output
    start_instance(state_dir, galaxy_yml, free_port)
    # change prefix
    prefix = '/galaxypf/'
    startup_config['galaxy']['galaxy_url_prefix'] = prefix
    galaxy_yml.write(json.dumps(startup_config))
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'restart'])
    assert result.exit_code == 0, result.output
    startup_done, startup_logs = wait_for_startup(state_dir=state_dir, free_port=free_port, prefix=prefix)
    assert startup_done is True, f"Startup failed. Application startup logs:\n {startup_logs}"


def test_cmd_show(state_dir, galaxy_yml):
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'show'])
    assert result.exit_code == 0, result.output
    details = safe_load(result.output)
    assert details['config_type'] == 'galaxy'


def test_cmd_list(state_dir, galaxy_yml):
    runner = CliRunner()
    result = runner.invoke(galaxyctl, ['--config-file', str(galaxy_yml), 'list'])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("TYPE")
    assert str(galaxy_yml) in result.output
