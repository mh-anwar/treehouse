"""Public auth has one identity source; private self-join keeps root's whois verification."""
import asyncio
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import tempfile
import unittest
from unittest.mock import patch

import sbtestenv
import sbcommon as sb


def load(name):
    loader = importlib.machinery.SourceFileLoader('test_modes_' + name, os.path.join(sbtestenv.LIB, name))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


admin = load('sharedbox-admin')
gate = load('sharedbox-gate')
web = load('sharedbox-web')


class PublicModes(unittest.TestCase):
    def test_legacy_config_disables_public_url_and_listener(self):
        for mode in ('funnel', '', 'unknown'):
            cfg = dict(sb.DEFAULTS, PUBLIC_URL='https://box.example.org', PUBLIC_MODE=mode)
            self.assertEqual(sb.public_url(cfg), '')
            self.assertEqual(sb.public_mode(cfg), '')
            with patch.object(gate, 'CF', False), patch.object(gate.asyncio, 'start_server') as listen:
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    asyncio.run(gate.main())
                listen.assert_not_called()
                self.assertIn('Cloudflare', err.getvalue())

    def test_join_defaults_and_explicit_open(self):
        for mode, explicit, expected in (('', '', 'open'), ('cloudflare', '', 'closed'),
                                         ('cloudflare', 'JOIN_MODE=open\n', 'open')):
            with tempfile.NamedTemporaryFile(mode='w') as f:
                f.write(f'PUBLIC_MODE={mode}\n' + explicit)
                f.flush()
                self.assertEqual(sb.load_config(f.name)['JOIN_MODE'], expected)

    def test_private_join_reverifies_whois_and_creates_immediately(self):
        cfg = dict(sb.DEFAULTS, JOIN_MODE='open')
        who = dict(id='friend-id', login='friend@example.com', node='laptop')
        req = dict(ip='100.64.0.2', ts_id='friend-id', username='alice', email='friend@example.com')
        with patch.object(sb, 'tailscale_whois', return_value=who), \
                patch.object(sb, 'tailscale_ip4', return_value='100.64.0.1'), \
                patch.object(sb, 'email_taken', return_value=None), \
                patch.object(sb, 'validate_username', return_value=None), \
                patch.object(admin, 'create_joined', return_value=({'uid': 2000}, None)) as create:
            self.assertTrue(admin.handle_join(cfg, 'id', req)['ok'])
            create.assert_called_once_with(cfg, 'alice', '', who['login'], who['id'], who['node'], req['email'])
            create.reset_mock()
            for changed in (dict(req, ts_id='forged'), dict(req, ip='100.64.0.1')):
                self.assertFalse(admin.handle_join(cfg, 'id', changed)['ok'])
            for mode in ('closed', 'approve', 'invalid'):
                self.assertFalse(admin.handle_join(dict(cfg, JOIN_MODE=mode), 'id', req)['ok'])
            create.assert_not_called()

    def test_public_invite_reverifies_jwt_and_bound_email(self):
        cfg = sb.load_config()
        inv = dict(email='friend@example.com', hash='a' * 64)
        req = dict(invite='x' * 43, username='alice', cf_jwt='signed', email='spoof@example.com')
        with patch.object(sb, 'gate_lookup', return_value=(inv, inv['hash'])), \
                patch.object(sb, 'validate_username', return_value=None), \
                patch.object(sb, 'email_taken', return_value=None), \
                patch.object(admin, 'cf_email') as email, \
                patch.object(admin, 'create_user', return_value=({'uid': 2000}, None)) as create, \
                patch.object(admin, 'gate_consume'), patch.object(sb, 'log_event'):
            for identity in (None, 'other@example.com'):
                email.return_value = identity
                self.assertFalse(admin.handle_invite_join(cfg, 'id', req)['ok'])
                create.assert_not_called()
            email.return_value = inv['email']
            self.assertTrue(admin.handle_invite_join(cfg, 'id', req)['ok'])
            email.assert_called_with(cfg, 'signed')
            self.assertEqual(create.call_args.kwargs['email'], inv['email'])

    def test_removed_login_routes_redirect_without_action(self):
        handler = object.__new__(web.GateHandler)
        handler.path = '/login/' + 'x' * 43
        handler.headers = {}
        with patch.object(handler, 'redirect') as redirect:
            handler.do_GET()
            redirect.assert_called_once_with('/')
        handler.path = '/_box/logout'
        handler.headers = {'Content-Length': '0'}
        handler.rfile = io.BytesIO()
        with patch.object(handler, 'redirect') as redirect, patch.object(web, 'spool_call') as spool:
            handler.do_POST()
            redirect.assert_called_once_with('/')
            spool.assert_not_called()

    def test_session_header_cannot_authenticate_portal(self):
        handler = object.__new__(web.GateHandler)
        handler.headers = {'X-Sharedbox-Session': 'old-secret'}
        self.assertIsNone(handler.who())
