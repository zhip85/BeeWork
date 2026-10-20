import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from beework.config import settings


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.model_credentials_secret.encode("utf-8")).digest()
    )
    return Fernet(key)


def encrypt_model_api_key(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_model_api_key(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as error:
        raise ValueError("模型凭证无法解密，请检查 MODEL_CREDENTIALS_SECRET") from error
