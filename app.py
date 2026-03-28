from flask import Flask, jsonify, request, send_from_directory
import threading
import queue
import time
import random
import collections
import os
from dataclasses import dataclass, field
from typing import List, Dict

app = Flask(__name__, static_folder=".")

# ─────────────────────────────────────────────
# TRAFFIC CLASSES
# ─────────────────────────────────────────────
TRAFFIC_CLASSES = {
    "Video Call":    {"priority": 1, "base_size": 200,  "base_delay": 5,   "dscp": "EF",   "icon": "◈"},
    "VoIP":          {"priority": 2, "base_size": 80,   "base_delay": 8,   "dscp": "EF",   "icon": "◉"},
    "Gaming":        {"priority": 3, "base_size": 150,  "base_delay": 12,  "dscp": "AF41", "icon": "◆"},
    "Web Browsing":  {"priority": 4, "base_size": 500,  "base_delay": 25,  "dscp": "AF21", "icon": "◇"},
    "Email":         {"priority": 5, "base_size": 800,  "base_delay": 60,  "dscp": "CS1",  "icon": "○"},
    "File Download": {"priority": 6, "base_size": 3000, "base_delay": 120, "dscp": "BE",   "icon": "□"},
}

# ─────────────────────────────────────────────
# PACKET MODEL
# ─────────────────────────────────────────────
@dataclass
class Packet:
    id: int
    traffic_class: str
    priority: int
    size_bytes: int
    arrival_time: float
    deadline_ms: float
    enqueue_time: float = 0.0
    dequeue_time: float = 0.0
    latency_ms: float = 0.0
    dropped: bool = False
    sequence: int = 0

    def __lt__(self, other):
        return self.priority < other.priority

# ─────────────────────────────────────────────
# QoS ENGINE
# ─────────────────────────────────────────────
class QoSEngine:
    def __init__(self, bandwidth_mbps: float, queue_size: int, algorithm: str):
        self.bandwidth_mbps = bandwidth_mbps
        self.queue_size = queue_size
        self.algorithm = algorithm
        self.pq = queue.PriorityQueue(maxsize=queue_size)
        self.sequence_counter = 0
        self.processed: List[Packet] = []
        self.dropped_pkts: List[Packet] = []
        self.stats = collections.defaultdict(lambda: {
            "sent": 0, "dropped": 0, "total_latency": 0.0,
            "min_latency": float("inf"), "max_latency": 0.0, "bytes_sent": 0,
        })
        self.running = False
        self.lock = threading.Lock()
        self.timeline: List[Dict] = []
        self.packet_id = 0
        self._weights = {cls: max(1, 7 - info["priority"]) for cls, info in TRAFFIC_CLASSES.items()}
        self._start_time = 0.0

    def _tx_delay_ms(self, size_bytes: int) -> float:
        return (size_bytes * 8 / (self.bandwidth_mbps * 1_000_000)) * 1000

    def _make_packet(self, traffic_class: str) -> Packet:
        cfg = TRAFFIC_CLASSES[traffic_class]
        size = max(40, int(random.gauss(cfg["base_size"], cfg["base_size"] * 0.25)))
        deadline = cfg["base_delay"] * random.uniform(0.85, 1.30)
        with self.lock:
            self.packet_id += 1
            pid = self.packet_id
        return Packet(id=pid, traffic_class=traffic_class, priority=cfg["priority"],
                      size_bytes=size, arrival_time=time.time(), deadline_ms=deadline)

    def _enqueue(self, pkt: Packet) -> bool:
        with self.lock:
            self.sequence_counter += 1
            seq = self.sequence_counter
        pkt.sequence = seq
        pkt.enqueue_time = time.time()
        alg = self.algorithm
        if alg == "FIFO":
            key = (seq,)
        elif alg == "WFQ":
            w = self._weights.get(pkt.traffic_class, 1)
            key = (seq / w,)
        else:  # PQ or DRR
            key = (pkt.priority, seq)
        try:
            self.pq.put_nowait((*key, pkt))
            return True
        except queue.Full:
            pkt.dropped = True
            with self.lock:
                self.dropped_pkts.append(pkt)
                self.stats[pkt.traffic_class]["dropped"] += 1
            return False

    def _dequeue(self):
        try:
            item = self.pq.get_nowait()
            pkt: Packet = item[-1]
            pkt.dequeue_time = time.time()
            delay_s = self._tx_delay_ms(pkt.size_bytes) / 1000.0
            time.sleep(delay_s)
            pkt.latency_ms = (time.time() - pkt.arrival_time) * 1000
            with self.lock:
                self.processed.append(pkt)
                s = self.stats[pkt.traffic_class]
                s["sent"] += 1
                s["total_latency"] += pkt.latency_ms
                s["min_latency"] = min(s["min_latency"], pkt.latency_ms)
                s["max_latency"] = max(s["max_latency"], pkt.latency_ms)
                s["bytes_sent"] += pkt.size_bytes
                self.timeline.append({
                    "time": round(pkt.dequeue_time - self._start_time, 3),
                    "class": pkt.traffic_class,
                    "latency_ms": round(pkt.latency_ms, 2),
                    "size": pkt.size_bytes,
                    "priority": pkt.priority,
                })
            return pkt
        except queue.Empty:
            return None

    def run(self, duration_s: float, pps: int, active_classes: List[str]):
        self.running = True
        self._start_time = time.time()
        end_time = self._start_time + duration_s
        interval = 1.0 / max(pps, 1)

        def producer():
            t = time.time()
            while time.time() < end_time and self.running:
                cls = random.choice(active_classes)
                pkt = self._make_packet(cls)
                self._enqueue(pkt)
                t += interval + random.uniform(-interval * 0.2, interval * 0.2)
                sleep_t = t - time.time()
                if sleep_t > 0:
                    time.sleep(sleep_t)

        def consumer():
            while (time.time() < end_time or not self.pq.empty()) and self.running:
                if not self._dequeue():
                    time.sleep(0.001)

        c = threading.Thread(target=consumer, daemon=True)
        p = threading.Thread(target=producer, daemon=True)
        c.start(); p.start()
        p.join(); c.join()
        self.running = False

# ─────────────────────────────────────────────
# SIMULATION STATE
# ─────────────────────────────────────────────
sim_lock = threading.Lock()
active_sim: Dict = {"running": False, "result": None}

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/traffic-classes", methods=["GET"])
def traffic_classes():
    return jsonify({
        cls: {**info} for cls, info in TRAFFIC_CLASSES.items()
    })

@app.route("/api/simulate", methods=["POST"])
def simulate():
    body = request.get_json(force=True)
    algorithm      = body.get("algorithm", "PQ")
    bandwidth_mbps = float(body.get("bandwidth", 10.0))
    duration_s     = float(body.get("duration", 8.0))
    pps            = int(body.get("pps", 60))
    queue_size     = int(body.get("queue_size", 100))
    active_classes = body.get("active_classes", list(TRAFFIC_CLASSES.keys()))

    if not active_classes:
        return jsonify({"error": "No active classes selected"}), 400

    with sim_lock:
        if active_sim["running"]:
            return jsonify({"error": "Simulation already running"}), 409
        active_sim["running"] = True
        active_sim["result"] = None

    def run_sim():
        engine = QoSEngine(bandwidth_mbps, queue_size, algorithm)
        engine.run(duration_s, pps, active_classes)

        stats_out = {}
        for cls, s in engine.stats.items():
            avg_lat = s["total_latency"] / s["sent"] if s["sent"] > 0 else 0
            sla_target = TRAFFIC_CLASSES[cls]["base_delay"] * 1.5
            stats_out[cls] = {
                "sent":        s["sent"],
                "dropped":     s["dropped"],
                "avg_latency": round(avg_lat, 2),
                "min_latency": round(s["min_latency"], 2) if s["min_latency"] < float("inf") else 0,
                "max_latency": round(s["max_latency"], 2),
                "bytes_sent":  s["bytes_sent"],
                "sla_target":  sla_target,
                "sla_ok":      avg_lat < sla_target,
                "priority":    TRAFFIC_CLASSES[cls]["priority"],
                "dscp":        TRAFFIC_CLASSES[cls]["dscp"],
            }

        total_sent    = sum(s["sent"] for s in engine.stats.values())
        total_dropped = sum(s["dropped"] for s in engine.stats.values())
        all_latencies = [t["latency_ms"] for t in engine.timeline]
        avg_lat_all   = sum(all_latencies) / len(all_latencies) if all_latencies else 0
        total_bytes   = sum(s["bytes_sent"] for s in engine.stats.values())
        throughput    = total_bytes * 8 / 1e6 / duration_s

        with sim_lock:
            active_sim["result"] = {
                "config": {
                    "algorithm": algorithm,
                    "bandwidth": bandwidth_mbps,
                    "duration":  duration_s,
                    "pps":       pps,
                    "queue_size": queue_size,
                },
                "summary": {
                    "total_sent":    total_sent,
                    "total_dropped": total_dropped,
                    "drop_rate":     round(total_dropped / max(total_sent + total_dropped, 1) * 100, 2),
                    "avg_latency":   round(avg_lat_all, 2),
                    "throughput_mbps": round(throughput, 3),
                },
                "stats":    stats_out,
                "timeline": engine.timeline[-400:],  # cap for transfer size
            }
            active_sim["running"] = False

    t = threading.Thread(target=run_sim, daemon=True)
    t.start()

    # Block until done (synchronous for simplicity; could SSE for streaming)
    t.join()

    with sim_lock:
        result = active_sim["result"]

    return jsonify(result)

@app.route("/api/status", methods=["GET"])
def status():
    with sim_lock:
        return jsonify({"running": active_sim["running"]})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
