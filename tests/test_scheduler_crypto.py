import base64
import os
import unittest

from scheduler.crypto import encrypt_token,decrypt_token


class TokenEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.old=os.environ.get("TOKEN_ENCRYPTION_KEY")
        os.environ["TOKEN_ENCRYPTION_KEY"]=base64.urlsafe_b64encode(bytes(range(32))).decode()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TOKEN_ENCRYPTION_KEY",None)
        else:
            os.environ["TOKEN_ENCRYPTION_KEY"]=self.old

    def test_round_trip_and_randomized_ciphertext(self):
        token="sample-access-token-never-log"
        a=encrypt_token(token); b=encrypt_token(token)
        self.assertNotEqual(a,b)  # random nonce
        self.assertEqual(decrypt_token(a),token)
        self.assertEqual(decrypt_token(b),token)

    def test_ciphertext_tampering_fails_authentication(self):
        encrypted=bytearray(encrypt_token("token"))
        encrypted[-1]^=1
        with self.assertRaises(RuntimeError):
            decrypt_token(encrypted)

    def test_wrong_key_fails_closed(self):
        encrypted=encrypt_token("token")
        os.environ["TOKEN_ENCRYPTION_KEY"]=base64.urlsafe_b64encode(b"x"*32).decode()
        with self.assertRaises(RuntimeError):
            decrypt_token(encrypted)


if __name__=="__main__":
    unittest.main()
