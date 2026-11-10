"""The usbmuxd client against FakeUsbmux (the plist protocol, piping after Connect)."""
import socket
import threading

import pytest

from droidctl.core import UserError
from droidctl.ios import transport
from tests.fakes import FakeUsbmux

UDID = "00008101-00AAAAAAAAAAAAAA"


@pytest.fixture
def echo_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(4)

    def serve():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            data = c.recv(100)
            c.sendall(b"echo:" + data)
            c.close()
    threading.Thread(target=serve, daemon=True).start()
    yield s.getsockname()[1]
    s.close()


@pytest.fixture
def mux(monkeypatch, echo_port):
    m = FakeUsbmux({UDID: {18100: echo_port}}, paired=[UDID])
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", m.address)
    yield m
    m.close()


def test_socket_address_forms(monkeypatch):
    monkeypatch.delenv("USBMUXD_SOCKET_ADDRESS", raising=False)
    assert transport.socket_address() == "/var/run/usbmuxd"
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", "UNIX:/tmp/mux")
    assert transport.socket_address() == "/tmp/mux"
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", "127.0.0.1:27015")
    assert transport.socket_address() == ("127.0.0.1", 27015)


def test_lists_usb_iphones_by_udid(mux):
    assert transport.list_devices() == [{"udid": UDID, "device_id": 100, "product_id": 4776}]


def test_pair_record_means_paired(mux):
    assert transport.is_paired(UDID)
    assert not transport.is_paired("00008101-00BBBBBBBBBBBBBB")


def test_connect_becomes_a_pipe_to_the_device_port(mux):
    s = transport.connect(UDID, 18100)
    s.sendall(b"hi")
    assert s.recv(100) == b"echo:hi"
    s.close()


def test_a_closed_device_port_is_refused(mux):
    with pytest.raises(UserError) as e:
        transport.connect(UDID, 18101)
    assert e.value.kind == "connection" and e.value.data == {"usbmux_result": 3}


def test_an_unknown_phone_is_no_device(mux):
    with pytest.raises(UserError) as e:
        transport.connect("00008101-00BBBBBBBBBBBBBB", 18100)
    assert e.value.kind == "no-device"


def test_no_usbmuxd_is_a_connection_error(monkeypatch):
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", "127.0.0.1:1")
    with pytest.raises(UserError) as e:
        transport.list_devices()
    assert e.value.kind == "connection"
