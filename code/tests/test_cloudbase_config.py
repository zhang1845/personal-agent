"""Cloud JSON settings regressions; all documents and credentials are fake."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from personal_agent.api_model import APISettings
from personal_agent.caldav_sync import CalDAVSync
from personal_agent.cloudbase_store import SnapshotConflict
from personal_agent.errors import AgentError
from personal_agent.private_config import PrivateConfig


SECRET = 'unit-test-fake-secret-never-a-real-key'


class FakeDocument:
    def __init__(self):
        self.revision = 0
        self.value = None
        self.reads = 0
        self.writes = []
        self.read_error = False
        self.write_error = False
        self.unknown_write = False
        self.before_compare_swap = None

    def read(self):
        self.reads += 1
        if self.read_error:
            raise OSError(SECRET)
        return self.revision, copy.deepcopy(self.value)

    def compare_swap(self, revision, value):
        self.writes.append(revision)
        # Deterministically interleave another store after this store's read.
        # Both clients can see the same business version but different remote
        # revisions, especially when the other writer only records a test.
        callback, self.before_compare_swap = self.before_compare_swap, None
        if callback:
            callback()
        if self.write_error:
            raise OSError(SECRET)
        if revision != self.revision:
            raise SnapshotConflict()
        self.value = copy.deepcopy(value)
        self.revision += 1
        if self.unknown_write:
            raise OSError(SECRET)
        return self.revision


class CloudConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = {name: FakeDocument() for name in (
            'model-api.json', 'notifications.json', 'caldav.json')}
        for module in ('personal_agent.api_model', 'personal_agent.private_config'):
            context = patch(module + '.remote_document', side_effect=self.document)
            context.start()
            self.addCleanup(context.stop)

    def document(self, path):
        return self.docs[Path(path).name]

    def api(self):
        return APISettings(self.root / 'model-api.json')

    def notifications(self):
        return PrivateConfig(self.root / 'notifications.json',
                             {'urls': '', 'enabled': False}, {'urls'})

    def caldav(self):
        return PrivateConfig(self.root / 'caldav.json', {
            'url': '', 'username': '', 'password': '', 'calendar_url': '',
            'enabled': False, 'writes_enabled': False}, {'password'})

    def save_api(self, store, **changes):
        body = {'base_url': 'https://provider.example/v1', 'model': 'test-model',
                'api_key': SECRET, 'enabled': False, 'expected_version': 1}
        body.update(changes)
        return store.save(body)

    def assert_error(self, code, operation):
        with self.assertRaises(AgentError) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn(SECRET, str(caught.exception.as_dict()))

    def test_three_documents_reopen_without_local_files_and_redact_secrets(self):
        api = self.api()
        self.assertEqual(1, api.get()['version'])
        self.assertFalse(api.get()['enabled'])
        public_api = self.save_api(api)
        notification = self.notifications()
        public_notification = notification.save({
            'urls': SECRET, 'enabled': False, 'expected_version': 1}, lambda *args: None)
        caldav = self.caldav()
        public_caldav = caldav.save({
            'url': 'https://calendar.example/', 'username': '测试用户',
            'password': SECRET, 'expected_version': 1}, lambda *args: None)

        for public in (public_api, public_notification, public_caldav):
            self.assertNotIn(SECRET, json.dumps(public))
        self.assertTrue(public_api['has_key'])
        self.assertTrue(public_notification['has_urls'])
        self.assertTrue(public_caldav['has_password'])
        self.assertEqual(public_api, self.api().get())
        self.assertEqual(public_notification, self.notifications().public())
        self.assertEqual(public_caldav, self.caldav().public())
        self.assertEqual(SECRET, self.api().snapshot()['api_key'])
        self.assertEqual(SECRET, self.notifications().read()['urls'])
        self.assertEqual(SECRET, self.caldav().read()['password'])
        self.assertEqual([], list(self.root.iterdir()))

    def test_every_read_uses_remote_and_missing_remote_ignores_native_file(self):
        path = self.root / 'model-api.json'
        path.write_text(json.dumps({'base_url': 'https://local.example/v1',
            'model': 'local', 'api_key': SECRET, 'enabled': True, 'version': 99}), encoding='utf-8')
        store = self.api()
        self.assertEqual(1, store.get()['version'])
        self.assertFalse(store.get()['has_key'])
        doc = self.docs[path.name]
        doc.value = {'base_url': '', 'model': 'remote', 'api_key': '',
                     'enabled': False, 'version': 7, 'last_test': None}
        doc.revision = 23
        self.assertEqual('remote', store.get()['model'])
        self.assertEqual(7, store.get()['version'])
        self.assertEqual(4, doc.reads)

    def test_record_test_wins_remote_cas_without_changing_business_version(self):
        saving, testing = self.api(), self.api()
        saved = self.save_api(saving)
        doc = self.docs['model-api.json']
        doc.before_compare_swap = lambda: testing.record_test(saved['version'], True, 'ok')

        self.assert_error('version_conflict', lambda: self.save_api(
            saving, expected_version=saved['version'], model='stale-edit'))
        current = self.api().get()
        self.assertEqual(saved['version'], current['version'])
        self.assertEqual('test-model', current['model'])
        self.assertTrue(current['last_test']['ok'])
        self.assertEqual(2, doc.revision)
        self.assertEqual([0, 1, 1], doc.writes)

    def test_save_wins_remote_cas_and_stale_test_cannot_restore_old_settings(self):
        testing, saving = self.api(), self.api()
        saved = self.save_api(testing)
        doc = self.docs['model-api.json']
        doc.before_compare_swap = lambda: self.save_api(
            saving, expected_version=saved['version'], model='new-model')

        self.assert_error('version_conflict', lambda: testing.record_test(
            saved['version'], True, 'ok'))
        current = self.api().get()
        self.assertEqual(saved['version'] + 1, current['version'])
        self.assertEqual('new-model', current['model'])
        self.assertIsNone(current['last_test'])
        self.assertEqual(2, doc.revision)
        self.assertEqual([0, 1, 1], doc.writes)

    def test_private_config_conflict_does_not_overwrite_other_process(self):
        first, second = self.notifications(), self.notifications()
        doc = self.docs['notifications.json']
        doc.before_compare_swap = lambda: second.save({
            'urls': 'other-fake-url', 'expected_version': 1}, lambda *args: None)
        self.assert_error('version_conflict', lambda: first.save({
            'urls': SECRET, 'expected_version': 1}, lambda *args: None))
        self.assertEqual('other-fake-url', self.notifications().read()['urls'])

    def test_remote_outage_never_returns_defaults_or_native_saved_secrets(self):
        for name, factory, read in (
            ('model-api.json', self.api, lambda store: store.get()),
            ('notifications.json', self.notifications, lambda store: store.public()),
            ('caldav.json', self.caldav, lambda store: store.read())):
            with self.subTest(name=name):
                (self.root / name).write_text('{"version":99,"api_key":"' + SECRET + '"}', encoding='utf-8')
                store = factory()
                self.docs[name].read_error = True
                self.assert_error('storage_unavailable', lambda: read(store))
                self.assertEqual([], self.docs[name].writes)

    def test_failed_remote_save_is_redacted_and_never_writes_native_file(self):
        self.docs['model-api.json'].write_error = True
        self.assert_error('storage_unavailable', lambda: self.save_api(self.api()))
        for name, factory, body in (
            ('notifications.json', self.notifications, {'urls': SECRET}),
            ('caldav.json', self.caldav, {'password': SECRET})):
            with self.subTest(name=name):
                self.docs[name].write_error = True
                self.assert_error('storage_unavailable', lambda: factory().save(
                    dict(body, expected_version=1), lambda *args: None))
        self.assertEqual([], list(self.root.iterdir()))

    def test_unknown_write_is_not_retried_and_baseline_is_consumed(self):
        store = self.api()
        doc = self.docs['model-api.json']
        doc.unknown_write = True
        self.assert_error('storage_unavailable', lambda: self.save_api(store))
        self.assertEqual([0], doc.writes)
        self.assertEqual(1, doc.revision)
        self.assert_error('storage_unavailable', lambda: store._write(doc.value))
        self.assertEqual([0], doc.writes)
        self.assertTrue(store.get()['has_key'])

    def test_write_requires_successful_read_and_failed_read_clears_baseline(self):
        for name, factory in (
            ('model-api.json', self.api), ('notifications.json', self.notifications),
            ('caldav.json', self.caldav)):
            with self.subTest(name=name):
                store, doc = factory(), self.docs[name]
                self.assert_error('storage_unavailable', lambda: store._write({}))
                if isinstance(store, APISettings):
                    store.snapshot()
                else:
                    store.read()
                doc.read_error = True
                self.assert_error('storage_unavailable',
                    store.snapshot if isinstance(store, APISettings) else store.read)
                self.assert_error('storage_unavailable', lambda: store._write({}))
                self.assertEqual([], doc.writes)

    def test_api_address_change_and_explicit_clear_rules_still_apply(self):
        store = self.api()
        saved = self.save_api(store)
        self.assert_error('validation_error', lambda: self.save_api(store,
            api_key='', expected_version=saved['version'],
            base_url='https://another.example/v1'))
        saved = self.save_api(store, api_key='', clear_key=True,
            expected_version=saved['version'], base_url='https://another.example/v1')
        self.assertFalse(saved['has_key'])
        self.assertEqual('', self.api().snapshot()['api_key'])

    def test_caldav_address_change_requires_password_again(self):
        store = self.caldav()
        holder = SimpleNamespace(config=store)
        saved = CalDAVSync.save_settings(holder, {'url': 'https://calendar.example/',
            'username': 'owner', 'password': SECRET, 'expected_version': 1})
        self.assert_error('validation_error', lambda: CalDAVSync.save_settings(holder, {
            'url': 'https://other.example/', 'expected_version': saved['version']}))
        self.assertEqual('https://calendar.example/', store.read()['url'])

    def test_remote_corruption_uses_the_existing_integrity_checks(self):
        store = self.api()
        doc = self.docs['model-api.json']
        for value in ({}, {'version': True, 'enabled': False},
                      {'version': 1, 'enabled': True, 'base_url': '', 'model': '', 'api_key': ''},
                      {'version': 1, 'enabled': False, 'api_key': SECRET + '\ninvalid'}):
            with self.subTest(value=value):
                doc.value = value
                self.assert_error('storage_unavailable', store.get)
        self.docs['notifications.json'].value = {'version': 'bad', 'urls': SECRET}
        self.assert_error('storage_unavailable', self.notifications().public)
        self.assertEqual([], doc.writes)

    def test_factory_failure_is_safe_instead_of_native_fallback(self):
        for module, factory in (
            ('personal_agent.api_model', self.api),
            ('personal_agent.private_config', self.notifications)):
            with self.subTest(module=module), patch(module + '.remote_document', side_effect=OSError(SECRET)):
                self.assert_error('storage_unavailable', factory)


if __name__ == '__main__':
    unittest.main()
