import base64
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).parents[1]))
from src.bridge_access import require_application_user,BridgeAccessError


def token(**claims):
    value=dict(role='authenticated',sub='ordinary-user',exp=time.time()+60,**{})
    value.update(claims)
    return 'header.'+base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')+'.signature'


class BridgeAccessTests(unittest.TestCase):
    def test_ordinary_application_user_is_verified_by_auth_not_admin_claim(self):
        with patch('src.bridge_access.get_resolved_supabase_url',return_value='https://fixture.invalid'),patch('src.bridge_access.get_resolved_supabase_key',return_value='test-key'),patch('src.bridge_access.urllib.request.urlopen',return_value=io.BytesIO(b'{"id":"ordinary-user","is_anonymous":false}')) as opener:
            self.assertEqual(require_application_user('Bearer '+token()),'ordinary-user')
            self.assertEqual(opener.call_args.args[0].full_url,'https://fixture.invalid/auth/v1/user')

    def test_missing_expired_anonymous_and_server_keys_rejected_before_network(self):
        with patch('src.bridge_access.urllib.request.urlopen') as opener:
            for value in [None,'','Bearer sb_secret_fixture','Bearer '+token(exp=1),'Bearer '+token(role='anon'),'Bearer '+token(role='service_role')]:
                with self.subTest(value=bool(value)),self.assertRaises(BridgeAccessError) as error:
                    require_application_user(value)
                self.assertEqual(error.exception.status,401)
            opener.assert_not_called()

    def test_tampered_or_anonymous_user_identity_rejected(self):
        for user in [{'id':'different-user'},{'id':'ordinary-user','is_anonymous':True}]:
            with patch('src.bridge_access.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(user).encode())):
                with self.assertRaises(BridgeAccessError) as error:require_application_user('Bearer '+token())
                self.assertEqual(error.exception.status,403)

    def test_invalid_session_and_verification_outage_fail_closed(self):
        for failure,status in [(HTTPError('https://fixture.invalid',401,'secret-body',None,None),401),(OSError('secret connection details'),503)]:
            with patch('src.bridge_access.urllib.request.urlopen',side_effect=failure):
                with self.assertRaises(BridgeAccessError) as error:require_application_user('Bearer '+token())
                self.assertEqual(error.exception.status,status)
                self.assertNotIn('secret',str(error.exception))


if __name__=='__main__':unittest.main()
