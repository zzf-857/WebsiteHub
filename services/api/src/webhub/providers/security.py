from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from webhub.config import Settings

SECRET_MASK = "********"


class ProviderSecretUnavailableError(RuntimeError):
    pass


class ProviderSecretInvalidError(RuntimeError):
    pass


def _aad(*, user_id: str, config_id: str, kind: str, provider: str) -> bytes:
    values = ("webhub-provider-secret-v1", user_id, config_id, kind, provider)
    return "\x00".join(values).encode("utf-8")


def encrypt_secret(
    settings: Settings,
    plaintext: str,
    *,
    user_id: str,
    config_id: str,
    kind: str,
    provider: str,
) -> tuple[bytes, bytes, int]:
    key = settings.provider_master_key
    if key is None:
        raise ProviderSecretUnavailableError("Provider 主密钥未配置")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        plaintext.encode("utf-8"),
        _aad(
            user_id=user_id,
            config_id=config_id,
            kind=kind,
            provider=provider,
        ),
    )
    return ciphertext, nonce, settings.provider_master_key_version


def decrypt_secret(
    settings: Settings,
    ciphertext: bytes,
    nonce: bytes,
    key_version: int,
    *,
    user_id: str,
    config_id: str,
    kind: str,
    provider: str,
) -> str:
    key = settings.provider_master_key
    if key is None or key_version != settings.provider_master_key_version:
        raise ProviderSecretUnavailableError("Provider 主密钥版本不可用")
    try:
        plaintext = AESGCM(key).decrypt(
            nonce,
            ciphertext,
            _aad(
                user_id=user_id,
                config_id=config_id,
                kind=kind,
                provider=provider,
            ),
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as error:
        raise ProviderSecretInvalidError("Provider 凭据无法解密") from error
