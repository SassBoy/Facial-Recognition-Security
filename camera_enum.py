"""
Camera enumeration using Windows DirectShow COM interfaces (ctypes).

Enumerates video-capture devices by their DirectShow / Media Foundation names,
detects virtual cameras, and maps them to OpenCV-compatible indices.

No external dependencies — uses only ctypes + the Windows SDK COM layer.
"""

import ctypes
import ctypes.wintypes as wt
import re
import uuid as _uuid
from ctypes import POINTER, byref, c_ulong, c_void_p, c_wchar_p, HRESULT

# ---------------------------------------------------------------------------
# COM basics
# ---------------------------------------------------------------------------

_ole32 = ctypes.windll.ole32
_ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
_ole32.CoInitializeEx.restype = HRESULT
_ole32.CoCreateInstance.argtypes = [c_void_p, c_void_p, c_ulong, c_void_p,
                                    POINTER(c_void_p)]
_ole32.CoCreateInstance.restype = HRESULT
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None

_oleaut32 = ctypes.windll.oleaut32
_oleaut32.SysFreeString.argtypes = [c_void_p]
_oleaut32.SysFreeString.restype = None

CLSCTX_INPROC_SERVER = 0x1
COINIT_MULTITHREADED = 0x0
S_OK = 0
S_FALSE = 1

GUID = ctypes.c_byte * 16
_GUID_TYPE = type(GUID)


def _guid(s: str):
    """'{xxxxxxxx-xxxx-...}' → 16-byte GUID in Windows little-endian layout."""
    return GUID(*_uuid.UUID(s).bytes_le)


# GUIDs for DirectShow device enumeration
_CLSID_SystemDeviceEnum        = _guid("{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}")
_IID_ICreateDevEnum            = _guid("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
_CLSID_VideoInputDeviceCategory = _guid("{860BB310-5D01-11D0-BD3B-00A0C911CE86}")
_IID_IPropertyBag              = _guid("{55272A00-42CB-11CE-8135-00AA004BB851}")


# ---------------------------------------------------------------------------
# Low-level COM vtable helpers (no class wrapper — avoids Release issues)
# ---------------------------------------------------------------------------

def _vtbl(com_ptr):
    """Dereference a COM object pointer to get its vtable pointer array."""
    return ctypes.cast(
        ctypes.cast(com_ptr, POINTER(c_void_p))[0],
        POINTER(c_void_p),
    )


def _release(com_ptr):
    """IUnknown::Release — vtable slot 2."""
    if com_ptr:
        fn = ctypes.CFUNCTYPE(c_ulong, c_void_p)(_vtbl(com_ptr)[2])
        fn(com_ptr)


def _create_class_enumerator(dev_enum, clsid):
    """ICreateDevEnum::CreateClassEnumerator — vtable slot 3."""
    out = c_void_p()
    fn = ctypes.CFUNCTYPE(HRESULT, c_void_p, c_void_p,
                          POINTER(c_void_p), c_ulong)(
        _vtbl(dev_enum)[3]
    )
    hr = fn(dev_enum, ctypes.addressof(clsid), byref(out), 0)
    return out if hr == S_OK and out else None


def _enum_next(enum_mon):
    """IEnumMoniker::Next(1, ...) — vtable slot 3."""
    mon = c_void_p()
    fetched = c_ulong(0)
    fn = ctypes.CFUNCTYPE(HRESULT, c_void_p, c_ulong,
                          POINTER(c_void_p), POINTER(c_ulong))(
        _vtbl(enum_mon)[3]
    )
    hr = fn(enum_mon, 1, byref(mon), byref(fetched))
    return mon if hr == S_OK and mon else None


def _bind_to_storage(moniker, iid):
    """IMoniker::BindToStorage — vtable slot 9.

    Slot layout: IUnknown(0-2), IPersist(3), IPersistStream(4-7),
    BindToObject(8), BindToStorage(9).
    """
    bag = c_void_p()
    fn = ctypes.CFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p,
                          c_void_p, POINTER(c_void_p))(
        _vtbl(moniker)[9]
    )
    hr = fn(moniker, None, None, ctypes.addressof(iid), byref(bag))
    return bag if hr == S_OK and bag else None


def _prop_read(bag, name: str) -> str | None:
    """IPropertyBag::Read — vtable slot 3.  Returns VT_BSTR value or None."""
    PTR_SZ = ctypes.sizeof(c_void_p)
    VT_SZ = 24 if PTR_SZ == 8 else 16          # VARIANT size
    variant = (ctypes.c_ubyte * VT_SZ)()

    fn = ctypes.CFUNCTYPE(HRESULT, c_void_p, c_wchar_p,
                          c_void_p, c_void_p)(
        _vtbl(bag)[3]
    )
    try:
        hr = fn(bag, name, ctypes.addressof(variant), None)
    except OSError:
        return None
    if hr != S_OK:
        return None

    vt = int.from_bytes(bytes(variant[0:2]), "little")
    if vt != 8:                                  # VT_BSTR
        return None

    bstr_addr = int.from_bytes(bytes(variant[8:8 + PTR_SZ]), "little")
    if not bstr_addr:
        return None

    result = ctypes.wstring_at(bstr_addr)
    try:
        _oleaut32.SysFreeString(c_void_p(bstr_addr))
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Virtual-camera heuristics
# ---------------------------------------------------------------------------

_VIRTUAL_NAME_RE = re.compile(
    r"obs|virtual|v4l2?|snap\s*cam|xsplit|manycam|ndi|streamlabs|"
    r"droidcam|iriun|epoccam|mmhmm|prism|fake|screen\s*capture|"
    r"camtwist|splitcam|sparkocam|youcam|chromacam|avatarify|"
    r"vtuber|animaze|unreal|newtek|blackmagic.*virtual|"
    r"elgato.*virtual|nvidia\s*broadcast|"
    r"meta\s*quest|oculus",
    re.IGNORECASE,
)

_USB_PATH_RE     = re.compile(r"usb#vid_", re.IGNORECASE)
_VIRTUAL_PATH_RE = re.compile(
    r"sw#|root#|virtual|\\\\\\?\\sw|avstream\\",
    re.IGNORECASE,
)


def _classify(name: str, path: str | None) -> str:
    """Return ``'virtual'`` or ``'physical'``."""
    if _VIRTUAL_NAME_RE.search(name or ""):
        return "virtual"
    if path:
        if _VIRTUAL_PATH_RE.search(path):
            return "virtual"
        if _USB_PATH_RE.search(path):
            return "physical"
    # No device path and no USB indicator — likely a software / VR device
    if not path:
        return "virtual"
    return "physical"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enumerate_cameras() -> list[dict]:
    """
    Enumerate video-capture devices via DirectShow COM.

    Returns a list of dicts **in DirectShow index order** (matching the
    indices that ``cv2.VideoCapture(idx)`` uses):

    .. code-block:: python

        {
            "index":       int,        # OpenCV-compatible device index
            "name":        str,        # Friendly device name from the driver
            "device_path": str | None, # Win32 device path (if available)
            "type":        str,        # 'physical' or 'virtual'
            "label":       str,        # Human-readable label for UI
        }

    Errors are caught internally — the function returns an empty list
    if COM initialisation or enumeration fails.
    """
    devices: list[dict] = []
    com_inited = False

    try:
        hr = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        com_inited = hr in (S_OK, S_FALSE)

        dev_enum = c_void_p()
        hr = _ole32.CoCreateInstance(
            ctypes.addressof(_CLSID_SystemDeviceEnum),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.addressof(_IID_ICreateDevEnum),
            byref(dev_enum),
        )
        if hr != S_OK or not dev_enum:
            return devices

        enum_mon = _create_class_enumerator(dev_enum, _CLSID_VideoInputDeviceCategory)
        if not enum_mon:
            _release(dev_enum)
            return devices

        idx = 0
        while True:
            moniker = _enum_next(enum_mon)
            if not moniker:
                break

            name = None
            path = None
            bag = _bind_to_storage(moniker, _IID_IPropertyBag)
            if bag:
                name = _prop_read(bag, "FriendlyName")
                path = _prop_read(bag, "DevicePath")
                _release(bag)
            _release(moniker)

            if not name:
                name = f"Camera {idx}"

            cam_type = _classify(name, path)
            tag = "  [Virtual]" if cam_type == "virtual" else ""

            devices.append({
                "index":       idx,
                "name":        name,
                "device_path": path,
                "type":        cam_type,
                "label":       f"{name}{tag}",
            })
            idx += 1

        _release(enum_mon)
        _release(dev_enum)

    except Exception:
        # Fail silently — caller should fall back to index-based probing
        import traceback
        traceback.print_exc()

    finally:
        if com_inited:
            try:
                _ole32.CoUninitialize()
            except Exception:
                pass

    return devices


def get_camera_name(index: int) -> str | None:
    """Return the friendly name for the device at *index*, or ``None``."""
    for cam in enumerate_cameras():
        if cam["index"] == index:
            return cam["name"]
    return None


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Enumerating cameras via DirectShow COM …\n")
    cams = enumerate_cameras()
    if not cams:
        print("  (no cameras found)")
    for c in cams:
        virt = " (VIRTUAL)" if c["type"] == "virtual" else ""
        print(f"  [{c['index']}] {c['name']}{virt}")
        if c["device_path"]:
            print(f"       path: {c['device_path']}")
    print(f"\nTotal: {len(cams)} device(s)")
