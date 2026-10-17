"""Explicit OS vaults only. Never use keyring's plugin/backend auto-discovery."""
from __future__ import annotations

import hashlib
import sys


def enabled(cfg):
    return cfg.get('distribution', {}).get('credential_storage') == 'system'


def identity(cfg):
    from .model_config import endpoint
    address = endpoint(cfg['llm']['base_url'])
    return 'TME Multiomics API', hashlib.sha256(address.encode('utf-8')).hexdigest()


def backend():
    if sys.platform == 'darwin':
        from keyring.backends.macOS import Keyring
    elif sys.platform == 'win32':
        from keyring.backends.Windows import WinVaultKeyring as Keyring
    elif sys.platform == 'linux':
        from keyring.backends.SecretService import Keyring
    else:
        raise RuntimeError('此系统没有支持的安全凭据库。')
    value = Keyring()
    if value.priority <= 0:
        raise RuntimeError('系统凭据库不可用。')
    return value


def _operation(cfg, operation, key=None):
    service, account = identity(cfg)
    try:
        vault = backend()
        if operation == 'read':
            return vault.get_password(service, account)
        if operation == 'write':
            vault.set_password(service, account, key)
            if vault.get_password(service, account) != key:
                raise RuntimeError('凭据回读不一致')
        elif vault.get_password(service, account) is not None:
            vault.delete_password(service, account)
    except Exception as exc:
        # Backend errors can contain sensitive input; report only their type.
        raise RuntimeError('系统密钥' + {'read':'读取', 'write':'保存', 'delete':'删除'}[operation]
                           + '失败（' + type(exc).__name__ + '）。请解锁系统凭据库并检查权限；'
                           '不会改用明文文件。') from None


def read(cfg):
    return _operation(cfg, 'read')


def write(cfg, key):
    _operation(cfg, 'write', key)


def delete(cfg):
    _operation(cfg, 'delete')
