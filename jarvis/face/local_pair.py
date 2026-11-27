"""FACELOCAL1: the window on the service's own PC pairs itself, with no human step.

At service start ensure() leaves a valid device key for 127.0.0.1:<port> in that user's keystore.
Pure logic over an injected devices.Registry and keystore.KeyStore; no network, no ctypes.
* Local identity is the device id stored here, never the name: every other live device named
  LOCAL_NAME (an old local pairing, or a remote one that took the name) is revoked.
* Never two live local devices, never a live local device without a stored key.
* Every outcome is a LocalDevice or a Refusal; the code and the key never leave this module.
"""
from __future__ import annotations

from dataclasses import dataclass

from jarvis.face.devices import MAX_DEVICES, Paired, Refusal
from jarvis.face.keystore import PORT_MAX, PORT_MIN, DeviceKey
from jarvis.face.keystore import Refusal as StoreRefusal

LOCAL_NAME = "local window"
LOCAL_IP = "127.0.0.1"
_FAILED = object()  # what a call that raised returns: never None (save's success), never a valid value


@dataclass(frozen=True)
class LocalDevice:
    device_id: str


def _call(fn, *args):
    """fn(*args), or _FAILED when it raises; the exception (which may carry a secret) is dropped."""
    try:
        return fn(*args)
    except Exception:
        return _FAILED


def _live_local(listed):
    return [d for d in listed if not d.revoked and d.name == LOCAL_NAME]


def _revoke_local(devices, keep=None) -> bool:
    """Revoke every live LOCAL_NAME device except keep; False when that cannot be proven done."""
    listed = _call(devices.list)
    if type(listed) is not tuple:
        return False
    for device in _live_local(listed):
        if device.id != keep and getattr(_call(devices.revoke, device.id), "revoked", False) is not True:
            return False
    return True


def _stored_id(devices, store, port, now, listed):
    """The stored key's device id when it is a live local device for 127.0.0.1:port, else None."""
    if _call(store.load_address) != (LOCAL_IP, port):
        return None
    key = _call(store.load)
    if not isinstance(key, DeviceKey):
        return None
    found = _call(devices.check, key.reveal(), now)
    return found if type(found) is str and found in {d.id for d in _live_local(listed)} else None


def ensure(devices, store, port, now):
    """LocalDevice(device_id) for the window on this PC, or Refusal; never raises."""
    if type(port) is not int or not PORT_MIN <= port <= PORT_MAX:
        return Refusal("bad_port")
    if store is None or isinstance(store, StoreRefusal):  # keystore.default() refused: no DPAPI, no LOCALAPPDATA
        return Refusal("keystore_unavailable")
    listed = _call(devices.list)
    if type(listed) is not tuple:
        return Refusal("registry_unreadable")
    kept = _stored_id(devices, store, port, now, listed)
    if kept is not None:
        return LocalDevice(kept) if _revoke_local(devices, keep=kept) else Refusal("revoke_failed")
    if sum(not d.revoked for d in listed) >= MAX_DEVICES and not _live_local(listed):
        return Refusal("device_cap")
    if not _revoke_local(devices):
        return Refusal("revoke_failed")
    code = _call(devices.issue_code, now)
    paired = _call(devices.pair, code, LOCAL_NAME, now) if type(code) is str else None
    if not isinstance(paired, Paired):
        _revoke_local(devices)  # a pair that raised after writing must not leave a keyless device
        return Refusal("device_cap" if getattr(paired, "reason", "") == "too_many_devices" else "pair_failed")
    if _call(store.save, paired.key) is not None or _call(store.save_address, LOCAL_IP, port) is not None:
        _revoke_local(devices)
        return Refusal("keystore_save_failed")
    return LocalDevice(paired.device.id)
