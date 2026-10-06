"""Default/CLI/GUI configuration must select the same effective H2 route."""
import asyncio
import contextlib
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from proxy.config import ProxyConfig
from proxy import tg_ws_proxy
from utils import tray_common
from utils.default_config import default_tray_config
from ui.settings import prepare_settings


class H2SettingsTest(unittest.TestCase):
    def test_cli_default_opt_out_and_existing_launch_script(self):
        cases = [([], True), (['--no-h2'], False),
                 (['--cfproxy-h2-media'], True), (['--no-cfproxy'], False),
                 (['--no-secure'], False), (['--force-test-dc'], False)]
        for flags, enabled in cases:
            with self.subTest(flags=flags):
                config = ProxyConfig()
                observed = []

                async def run():
                    observed.append(config.h2_enabled)

                root = logging.getLogger()
                previous_handlers, previous_level = root.handlers[:], root.level
                try:
                    with patch.object(tg_ws_proxy, 'proxy_config', config), \
                            patch.object(tg_ws_proxy, '_run', run), \
                            patch('sys.argv', ['proxy', '--secret', 'ab' * 16] + flags):
                        tg_ws_proxy.main()
                    self.assertEqual(observed, [enabled])
                finally:
                    for handler in root.handlers[:]:
                        if handler not in previous_handlers:
                            root.removeHandler(handler)
                            handler.close()
                    root.setLevel(previous_level)

    def test_help_advertises_opt_out_only(self):
        output = io.StringIO()
        with patch('sys.argv', ['proxy', '--help']), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                tg_ws_proxy.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertIn('--no-h2', output.getvalue())
        self.assertNotIn('--cfproxy-h2-media', output.getvalue())

    def test_saved_setting_migration_and_runtime_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'port': 1444}), encoding='utf-8')
            with patch.object(tray_common, 'CONFIG_FILE', path), \
                    patch.object(tray_common, 'ensure_dirs'), \
                    patch.object(tray_common, '_apply_ui_language'):
                config = tray_common.load_config()
                self.assertTrue(config['h2'])
                change = prepare_settings(config, {'h2': False}, default_tray_config())
                self.assertTrue(change.requires_restart)
                tray_common.save_config(change.config)
                saved = tray_common.load_config()
                self.assertFalse(saved['h2'])
                runtime = ProxyConfig()
                with patch.object(tray_common, 'proxy_config', runtime):
                    self.assertTrue(tray_common.apply_proxy_config(saved))
                    self.assertFalse(runtime.h2_enabled)
                    saved['h2'] = True
                    self.assertTrue(tray_common.apply_proxy_config(saved))
                    self.assertTrue(runtime.h2_enabled)
                    for key, value in [('cfproxy', False), ('no_secure', True),
                                       ('force_test_dc', True)]:
                        self.assertTrue(tray_common.apply_proxy_config({**saved, key: value}))
                        self.assertFalse(runtime.h2_enabled)
                        self.assertTrue(runtime.cfproxy_h2_media)


class H2ServerLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_and_cancellation_close_default_h2_and_listener_tasks(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                config = ProxyConfig(port=0, dc_redirects={}, pool_size=0)
                stop = asyncio.Event()
                before = asyncio.all_tasks()
                with patch.object(tg_ws_proxy, 'proxy_config', config), \
                        patch.object(tg_ws_proxy, 'start_cfproxy_domain_refresh'), \
                        patch.object(tg_ws_proxy.ws_pool, 'warmup', AsyncMock()), \
                        patch.object(tg_ws_proxy.cf_worker_pool, 'warmup', AsyncMock()):
                    server = asyncio.create_task(tg_ws_proxy._run(stop))
                    writer = None
                    try:
                        async def listening():
                            while tg_ws_proxy._server_instance is None:
                                await asyncio.sleep(.005)
                        await asyncio.wait_for(listening(), 1)
                        pool = tg_ws_proxy.cf_h2_pool
                        self.assertIsNotNone(pool)
                        port = tg_ws_proxy._server_instance.sockets[0].getsockname()[1]
                        _, writer = await asyncio.open_connection('127.0.0.1', port)
                        if cancel:
                            server.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(server, 2)
                        else:
                            stop.set()
                            await asyncio.wait_for(server, 2)
                        self.assertTrue(pool.closed)
                        self.assertIsNone(tg_ws_proxy.cf_h2_pool)
                        self.assertIsNone(tg_ws_proxy._server_instance)
                        self.assertFalse(tg_ws_proxy._client_tasks)
                        self.assertFalse(asyncio.all_tasks() - before)
                    finally:
                        server.cancel()
                        await asyncio.gather(server, return_exceptions=True)
                        if writer is not None:
                            writer.close()
                            await writer.wait_closed()


if __name__ == '__main__':
    unittest.main()
