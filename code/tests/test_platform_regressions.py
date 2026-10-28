"""Exercise OS-specific failures without weakening the portable integration tests."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import subprocess
import os
import runpy
import unittest
from unittest.mock import Mock, patch

from personal_agent.errors import AgentError
from personal_agent.ics import calendar_bytes
from personal_agent.timeutils import zone
from test_windows_compat import manager


class TimezonePlatformTests(unittest.TestCase):
    def test_control_characters_are_rejected_before_the_os_timezone_loader(self):
        with patch('personal_agent.timeutils.ZoneInfo') as loader:
            for name in ('bad\nTZ', 'Asia/Shanghai\r', 'bad\0TZ', 'bad\x7fTZ'):
                with self.subTest(name=repr(name)), self.assertRaises(AgentError) as caught:
                    zone(name)
                self.assertEqual('validation_error', caught.exception.code)
            loader.assert_not_called()

    def test_windows_timezone_oserror_becomes_a_validation_error(self):
        with patch('personal_agent.timeutils.ZoneInfo', side_effect=OSError(22, 'invalid path')):
            with self.assertRaises(AgentError) as caught:
                zone('Unknown/Zone')
            self.assertEqual('validation_error', caught.exception.code)

    def test_all_day_export_turns_bad_or_os_rejected_timezone_into_business_error(self):
        event = {'id':'one', 'calendar_id':'local', 'title':'全天安排',
                 'start':'2030-01-01T00:00:00Z', 'end':'2030-01-02T00:00:00Z',
                 'all_day':True, 'timezone':'bad\nTZ'}
        with self.assertRaises(AgentError) as caught:
            calendar_bytes([event])
        self.assertEqual('export_invalid_event', caught.exception.code)
        event['timezone'] = 'Unknown/Zone'
        with patch('personal_agent.timeutils.ZoneInfo', side_effect=OSError(22, 'invalid path')):
            with self.assertRaises(AgentError) as caught:
                calendar_bytes([event])
        self.assertEqual('export_invalid_event', caught.exception.code)


class ServiceDeadlineTests(unittest.TestCase):
    def test_slow_probes_are_included_in_the_startup_deadline(self):
        elapsed = [0.0]
        timeouts = []
        process = SimpleNamespace(pid=123, poll=lambda:None)
        def probe(port, data, timeout):
            timeouts.append(timeout)
            elapsed[0] += timeout
            return None
        def sleep(duration):
            elapsed[0] += duration
        with patch.object(manager.time, 'monotonic', side_effect=lambda:elapsed[0]), \
             patch.object(manager.time, 'sleep', side_effect=sleep), \
             patch.object(manager, 'probe', side_effect=probe):
            with self.assertRaises(SystemExit) as caught:
                manager.wait_started(process, 1234, Path('data'), Path('service.log'), timeout=1.2)
        self.assertIn('启动未就绪', str(caught.exception))
        self.assertAlmostEqual(1.2, elapsed[0])
        self.assertTrue(all(0 < timeout <= manager.PROBE_TIMEOUT for timeout in timeouts))
        self.assertLess(len(timeouts), 10)

    def test_ready_probe_must_belong_to_the_new_child(self):
        process = SimpleNamespace(pid=123, poll=lambda:None)
        with patch.object(manager, 'probe', return_value={'service_pid':456}):
            with self.assertRaises(SystemExit) as caught:
                manager.wait_started(process, 1234, Path('data'), Path('service.log'))
        self.assertIn('另一个服务', str(caught.exception))

    def test_failed_start_reaps_only_its_own_child(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('child',5), 0]
        manager.reap_failed_start(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual([{'timeout':5},{'timeout':2}], [call.kwargs for call in process.wait.call_args_list])

    def test_child_exit_reports_its_log_path_and_exit_code(self):
        process = SimpleNamespace(pid=123, returncode=7, poll=lambda:7)
        with self.assertRaises(SystemExit) as caught:
            manager.wait_started(process, 1234, Path('data'), Path('failed-start.log'))
        self.assertIn('退出码 7', str(caught.exception))
        self.assertIn('failed-start.log', str(caught.exception))


class StartupDiagnosticsTests(unittest.TestCase):
    def test_normal_launch_does_not_schedule_diagnostics(self):
        with patch.dict(os.environ, {}, clear=True), patch('faulthandler.dump_traceback_later') as timer:
            env = manager.service_environment('unit-test-token')
            self.assertNotIn('PERSONAL_AGENT_STARTUP_DIAGNOSTICS', env)
            runpy.run_path(str(manager.ROOT/'code/run.py'), run_name='diagnostics_test')
            timer.assert_not_called()

    def test_managed_ci_enables_diagnostics_but_explicit_disable_wins(self):
        with patch.dict(os.environ, {'CI':'true'}, clear=True):
            self.assertEqual('1', manager.service_environment('unit-test-token')['PERSONAL_AGENT_STARTUP_DIAGNOSTICS'])
        with patch.dict(os.environ, {'GITHUB_ACTIONS':'true'}, clear=True):
            self.assertEqual('1', manager.service_environment('unit-test-token')['PERSONAL_AGENT_STARTUP_DIAGNOSTICS'])
        with patch.dict(os.environ, {'CI':'true','PERSONAL_AGENT_STARTUP_DIAGNOSTICS':'0'}, clear=True):
            self.assertEqual('0', manager.service_environment('unit-test-token')['PERSONAL_AGENT_STARTUP_DIAGNOSTICS'])

    def test_explicit_diagnostics_are_scheduled_before_cli_import(self):
        with patch.dict(os.environ, {'PERSONAL_AGENT_STARTUP_DIAGNOSTICS':'1'}, clear=True), \
             patch('faulthandler.dump_traceback_later') as timer:
            runpy.run_path(str(manager.ROOT/'code/run.py'), run_name='diagnostics_test')
            timer.assert_called_once_with(10, repeat=True)


if __name__ == '__main__':
    unittest.main()
