// Receives focus scores from the local Python pipeline over a WebSocket.
//
// Replaces the original project's HTTP polling loop, which requested a value
// that changes 4 times a second at a rate of 100 requests per second -- one
// every 10 ms, each awaited. The socket pushes instead, so the request rate
// matches the data rate by construction.
//
// Lives outside Assets/Logitech deliberately. That directory is Logitech's MX
// Ink SDK sample; the previous version put all of this project's logic inside
// their MxInkHandler.cs, where an SDK update would overwrite it.
//
// Every failure path in this file fails *open* -- neutral, no wobble. A
// student must never have their pen shake because a Python process is not
// running or a socket dropped, since that is a fault they can neither
// diagnose nor fix.

using System;
using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace MXFocus
{
    /// <summary>
    /// One score as it arrives on the wire. Field names must match the JSON
    /// exactly -- JsonUtility matches by name and silently leaves anything it
    /// cannot find at its default value.
    ///
    /// Note that the default of `value` is 0f, which this pipeline reads as
    /// *maximum* wobble. The server never sends null precisely so a parse
    /// failure cannot produce a violently shaking pen. Do not make this
    /// nullable without re-reading that reasoning.
    /// </summary>
    [Serializable]
    public class FocusMessage
    {
        public float t;
        public float value;
        public float z;
        public bool calibrating;
        public bool artifact;
        public bool held;
    }

    public class FocusClient : MonoBehaviour
    {
        /// <summary>No wobble either way.</summary>
        public const float Neutral = 0.5f;

        [SerializeField] private string _url = "ws://127.0.0.1:8766";

        [Tooltip("Silence longer than this counts as stale and falls back to " +
                 "neutral. Eight missed messages at 4 Hz, so it will not trip " +
                 "on a hiccup.")]
        [SerializeField] private float _staleAfterSeconds = 2f;

        [SerializeField] private float _reconnectDelaySeconds = 1f;

        // Messages arrive on a background thread; Unity APIs may only be
        // touched on the main thread. This queue is the crossing point,
        // drained in Update(). Raw strings cross, not parsed objects, because
        // JsonUtility is main-thread only.
        private readonly ConcurrentQueue<string> _inbox
            = new ConcurrentQueue<string>();

        private CancellationTokenSource _cancellation;
        private volatile bool _connected;

        private FocusMessage _latest;
        private float _lastMessageTime = float.NegativeInfinity;

        public bool Connected { get { return _connected; } }

        public bool Calibrating
        {
            get { return _latest != null && _latest.calibrating; }
        }

        public bool Stale
        {
            get { return Time.time - _lastMessageTime > _staleAfterSeconds; }
        }

        /// <summary>
        /// Latest focus score, 0-1. Safe to read from Update().
        ///
        /// Returns neutral before the first message, after a disconnect, once
        /// stale, and throughout calibration -- during which the student is
        /// establishing the baseline, so shaking the pen would corrupt the
        /// very measurement being taken.
        /// </summary>
        public float CurrentValue
        {
            get
            {
                if (_latest == null || Stale || _latest.calibrating)
                {
                    return Neutral;
                }
                return Mathf.Clamp01(_latest.value);
            }
        }

        private void OnEnable()
        {
            _cancellation = new CancellationTokenSource();
            // Fire and forget: RunAsync owns its own error handling, and
            // awaiting it here would block the frame.
            _ = RunAsync(_cancellation.Token);
        }

        private void OnDisable()
        {
            // Cancelling unblocks ReceiveAsync, which lets RunAsync unwind and
            // dispose the socket. Without this an editor domain reload leaks
            // the receive task.
            if (_cancellation != null)
            {
                _cancellation.Cancel();
                _cancellation.Dispose();
                _cancellation = null;
            }
            _connected = false;
        }

        private void Update()
        {
            // Drain to the newest, discarding any backlog. A queue of stale
            // scores would make the pen act out history -- the same reasoning
            // that makes the server drop rather than buffer.
            string json = null;
            string next;
            while (_inbox.TryDequeue(out next))
            {
                json = next;
            }

            if (json == null)
            {
                return;
            }

            try
            {
                FocusMessage message = JsonUtility.FromJson<FocusMessage>(json);
                if (message != null)
                {
                    _latest = message;
                    _lastMessageTime = Time.time;
                }
            }
            catch (Exception error)
            {
                // A malformed payload must not kill the loop; the next one may
                // be fine, and staleness will fall back to neutral meanwhile.
                Debug.LogWarning("FocusClient: bad payload: " + error.Message);
            }
        }

        private async Task RunAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    using (ClientWebSocket socket = new ClientWebSocket())
                    {
                        await socket.ConnectAsync(new Uri(_url), token);
                        _connected = true;
                        await ReceiveLoop(socket, token);
                    }
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (Exception error)
                {
                    // The server legitimately may not be up yet when the scene
                    // loads, so this is expected rather than exceptional.
                    Debug.Log("FocusClient: not connected (" + error.Message + ")");
                }
                finally
                {
                    _connected = false;
                }

                try
                {
                    await Task.Delay(
                        TimeSpan.FromSeconds(_reconnectDelaySeconds), token);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
            }
        }

        private async Task ReceiveLoop(ClientWebSocket socket,
                                       CancellationToken token)
        {
            byte[] buffer = new byte[4096];
            StringBuilder message = new StringBuilder();

            while (socket.State == WebSocketState.Open
                   && !token.IsCancellationRequested)
            {
                WebSocketReceiveResult result = await socket.ReceiveAsync(
                    new ArraySegment<byte>(buffer), token);

                if (result.MessageType == WebSocketMessageType.Close)
                {
                    return;
                }

                message.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));

                // Payloads this small will not fragment in practice, but
                // reassembling costs three lines and removes the word
                // "practice" from that sentence.
                if (result.EndOfMessage)
                {
                    _inbox.Enqueue(message.ToString());
                    message.Length = 0;
                }
            }
        }
    }
}
