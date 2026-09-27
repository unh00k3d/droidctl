package dev.droidctl.agent

import android.net.LocalServerSocket
import android.net.LocalSocket
import android.system.Os
import android.system.OsConstants
import android.util.Log
import org.json.JSONException
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.Collections

/** JSON-RPC error codes (see PROTOCOL.md). */
object Codes {
    const val PARSE = -32700
    const val INVALID_REQUEST = -32600
    const val METHOD_NOT_FOUND = -32601
    const val INVALID_PARAMS = -32602
    const val INTERNAL = -32603
    const val UNAUTHORIZED = -32001
    const val STALE = -32002
}

class RpcError(val code: Int, message: String) : Exception(message)

/**
 * NDJSON JSON-RPC 2.0 over the abstract unix socket "droidctl".
 *
 * One thread accepts; each connection gets its own thread and is served in
 * order. Only peers with uid 2000 (shell, i.e. adb) or 0 (root) are accepted.
 */
class Server(private val rpc: Rpc) {
    companion object {
        const val SOCKET_NAME = "droidctl"
        const val MAX_LINE = 1 shl 20
        val ALLOWED_UIDS = setOf(2000, 0)
    }

    @Volatile private var stopping = false
    @Volatile private var listener: LocalServerSocket? = null
    private val clients: MutableSet<LocalSocket> = Collections.synchronizedSet(HashSet())

    fun start() {
        Thread({ acceptLoop() }, "droidctl-accept").apply { isDaemon = true }.start()
    }

    fun stop() {
        stopping = true
        listener?.let { s ->
            // close() alone does not wake a blocked accept(); shutting the fd down does
            try { Os.shutdown(s.fileDescriptor, OsConstants.SHUT_RDWR) } catch (_: Exception) {}
            try { s.close() } catch (_: Exception) {}
        }
        listener = null
        synchronized(clients) {
            for (c in clients) try { c.close() } catch (_: Exception) {}
            clients.clear()
        }
    }

    private fun bind(): LocalServerSocket? {
        // a previous instance of the service may still hold the name for a moment
        for (attempt in 1..5) {
            if (stopping) return null
            try {
                return LocalServerSocket(SOCKET_NAME)
            } catch (e: IOException) {
                Log.w(TAG, "bind @$SOCKET_NAME failed (attempt $attempt): ${e.message}")
                try { Thread.sleep(200L * attempt) } catch (_: InterruptedException) { return null }
            }
        }
        Log.e(TAG, "giving up binding @$SOCKET_NAME")
        return null
    }

    private fun acceptLoop() {
        val s = bind() ?: return
        listener = s
        if (stopping) { stop(); return }
        Log.i(TAG, "listening on @$SOCKET_NAME")
        while (!stopping) {
            val sock = try { s.accept() } catch (e: IOException) {
                if (!stopping) Log.w(TAG, "accept failed: ${e.message}")
                break
            }
            Thread({ serve(sock) }, "droidctl-conn").apply { isDaemon = true }.start()
        }
    }

    private fun serve(sock: LocalSocket) {
        clients.add(sock)
        try {
            val uid = sock.peerCredentials.uid
            val out = sock.outputStream
            if (uid !in ALLOWED_UIDS) {
                Log.w(TAG, "rejected connection from uid $uid")
                write(out, error(null, Codes.UNAUTHORIZED, "unauthorized uid $uid"))
                return
            }
            val input = sock.inputStream.buffered()
            while (!stopping) {
                val line = try { readLine(input) } catch (e: LineTooLong) {
                    write(out, error(null, Codes.INVALID_REQUEST, "request line exceeds $MAX_LINE bytes"))
                    continue
                } ?: break
                if (line.isBlank()) continue
                val reply = handle(line, uid) ?: continue
                write(out, reply)
            }
        } catch (e: IOException) {
            // peer went away
        } catch (e: Exception) {
            Log.e(TAG, "connection crashed", e)
        } finally {
            clients.remove(sock)
            try { sock.close() } catch (_: Exception) {}
        }
    }

    /** Returns the reply line, or null for a notification (no id). */
    private fun handle(line: String, uid: Int): JSONObject? {
        val req = try { JSONObject(line) } catch (e: JSONException) {
            return error(null, Codes.PARSE, "parse error: ${e.message}")
        }
        val hasId = req.has("id")
        val id: Any? = if (hasId) req.get("id") else null
        val method = req.optString("method", "")
        if (method.isEmpty() || req.optString("jsonrpc") != "2.0") {
            return if (hasId) error(id, Codes.INVALID_REQUEST, "expected jsonrpc \"2.0\" and a method") else null
        }
        val params = req.opt("params")
        if (params != null && params != JSONObject.NULL && params !is JSONObject) {
            return if (hasId) error(id, Codes.INVALID_PARAMS, "params must be an object") else null
        }
        val result = try {
            rpc.call(method, params as? JSONObject ?: JSONObject(), Rpc.Ctx(uid))
        } catch (e: RpcError) {
            return if (hasId) error(id, e.code, e.message ?: "") else null
        } catch (e: Throwable) {
            Log.e(TAG, "method $method failed", e)
            return if (hasId) error(id, Codes.INTERNAL, e.toString()) else null
        }
        if (!hasId) return null
        return JSONObject().put("jsonrpc", "2.0").put("id", id ?: JSONObject.NULL).put("result", result)
    }

    private fun error(id: Any?, code: Int, message: String): JSONObject =
        JSONObject().put("jsonrpc", "2.0").put("id", id ?: JSONObject.NULL)
            .put("error", JSONObject().put("code", code).put("message", message))

    private fun write(out: OutputStream, obj: JSONObject) {
        val bytes = (obj.toString() + "\n").toByteArray(Charsets.UTF_8)
        // No flush(): LocalSocket's stream is unbuffered, and its flush() polls the
        // socket's send queue (SIOCOUTQ) with ~10 ms sleeps until adbd drains it.
        // Measured on the SM-N950F: +10.6 ms per reply (bench/results/m1-rtt.json).
        synchronized(out) {
            out.write(bytes)
        }
    }

    private class LineTooLong : IOException()

    /** Reads one '\n'-terminated UTF-8 line; null at EOF. An over-long line is skipped whole. */
    private fun readLine(input: InputStream): String? {
        val buf = ByteArrayOutputStream()
        var tooLong = false
        while (true) {
            val b = input.read()
            if (b < 0) return if (buf.size() > 0 && !tooLong) buf.toString("UTF-8") else null
            if (b == '\n'.code) {
                if (tooLong) throw LineTooLong()
                return buf.toString("UTF-8")
            }
            if (!tooLong) {
                if (buf.size() >= MAX_LINE) { tooLong = true; buf.reset() } else buf.write(b)
            }
        }
    }
}
