"""Atomic local state and optional encrypted credentials in the Docker volume."""
import base64
import json
import os
from pathlib import Path
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes


class Storage:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def read(self, name, default):
        path = self.directory / (name + '.json')
        if not path.exists(): return default
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict): raise ValueError()
            return value
        except (ValueError, OSError):
            raise ValueError('저장된 ' + name + ' 정보를 읽지 못했습니다. 저장 파일을 확인하세요.') from None

    def write(self, name, value):
        path = self.directory / (name + '.json')
        temporary = path.with_suffix('.tmp')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)

    def _key(self, create=False):
        path = self.directory / 'credentials.key'
        if not path.exists():
            if not create: raise ValueError('로그인 정보 암호화 키가 없습니다.')
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
                output.write(get_random_bytes(32))
        key = path.read_bytes()
        if len(key) != 32: raise ValueError('로그인 정보 암호화 키를 읽지 못했습니다.')
        return key

    def credentials(self):
        saved = self.read('credentials', {})
        if not saved: return {'username': '', 'password': ''}
        try:
            cipher = AES.new(self._key(), AES.MODE_GCM, nonce=base64.b64decode(saved['nonce'], validate=True))
            plaintext = cipher.decrypt_and_verify(base64.b64decode(saved['ciphertext'], validate=True), base64.b64decode(saved['tag'], validate=True))
            value = json.loads(plaintext)
            if not all(isinstance(value.get(k), str) for k in ('username', 'password')): raise ValueError()
            return value
        except (ValueError, KeyError, TypeError):
            raise ValueError('저장된 로그인 정보를 열지 못했습니다. 저장 정보 삭제 후 다시 입력하세요.') from None

    def save_credentials(self, username, password, remember_id, remember_password):
        if not (remember_id or remember_password):
            self.write('credentials', {})
            return
        value = {'username': username, 'password': password if remember_password else ''}
        cipher = AES.new(self._key(create=True), AES.MODE_GCM)
        ciphertext, tag = cipher.encrypt_and_digest(json.dumps(value).encode('utf-8'))
        self.write('credentials', {key: base64.b64encode(data).decode('ascii') for key, data in
            [('nonce', cipher.nonce), ('ciphertext', ciphertext), ('tag', tag)]})
