"""AgentClient must survive read timeouts: bounded event waits (watch) time out by design."""
import json
import socket
import threading

from droidctl import device as dev


def test_a_read_timeout_does_not_break_the_connection():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        f = c.makefile("rb")
        for line in f:                      # answer every request, push nothing
            req = json.loads(line)
            c.sendall(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"ok": 1}}).encode() + b"\n")
    threading.Thread(target=serve, daemon=True).start()

    c = dev.AgentClient(port)
    assert list(c.notifications(timeout=0.1)) == []       # times out: no pushed events
    assert list(c.notifications(timeout=0.1)) == []       # and again
    assert c.call("ping") == {"ok": 1}                     # the connection still works
    c.close()
    srv.close()
