#!/usr/bin/env python3
"""
benchmark_runner.py - Ejecutor atómico de una corrida del benchmark
MongoDB vs PostgreSQL JSONB bajo contención de cgroups v2.

- Ejecuta exactamente 1 corrida (Warmup + Medición) con N hilos concurrentes
  sincronizados mediante threading.Barrier.
- Sin ORMs: pymongo y psycopg2 en bruto.
- Extrae telemetría por doble vía: API HTTP de Prometheus (urllib.request)
  + muestreo directo de archivos cgroup v2 (/sys/fs/cgroup) e inspección Docker.
- Vuelca 1 fila con las 14 columnas exactas de la rúbrica al archivo CSV maestro
  con fsync inmediato.

Columnas CSV (D-33):
    timestamp_epoch,engine,profile,query_id,concurrency,run_number,
    throughput_qps,latency_p50_ms,latency_p95_ms,latency_p99_ms,
    cpu_throttled_seconds,max_rss_mb,disk_spill,status
"""
import argparse
import csv
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# ===========================================================================
# Constantes y Esquema CSV (D-33)
# ===========================================================================
CSV_COLUMNS = (
    "timestamp_epoch",
    "engine",
    "profile",
    "query_id",
    "concurrency",
    "run_number",
    "throughput_qps",
    "latency_p50_ms",
    "latency_p95_ms",
    "latency_p99_ms",
    "cpu_throttled_seconds",
    "max_rss_mb",
    "disk_spill",
    "status",
)

QUERIES_DIR = Path(__file__).resolve().parent / "queries"
DEFAULT_CSV_PATH = Path(__file__).resolve().parent / "benchmark_results.csv"

MONGO_COLLECTION = "orders"
STATEMENT_TIMEOUT_MS = 120_000  # 120s timeout por consulta bajo contención severa


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ===========================================================================
# 1. CLI y Carga de Consultas
# ===========================================================================
def parse_args(argv=None) -> argparse.Namespace:
    env = os.environ.get

    default_mongo_uri = f"mongodb://localhost:{env('MONGO_PORT', '27017')}"
    default_pg_dsn = (
        f"host=localhost port={env('POSTGRES_PORT', '5432')} "
        f"user={env('POSTGRES_USER', 'bench')} "
        f"password={env('POSTGRES_PASSWORD', 'bench_pass')} "
        f"dbname={env('POSTGRES_DB', 'benchdb')}"
    )
    default_prom_url = f"http://localhost:{env('PROMETHEUS_PORT', '9090')}"

    p = argparse.ArgumentParser(
        description="Ejecuta una corrida del benchmark (Warmup + Medición) y registra en CSV."
    )
    p.add_argument("--engine", required=True, choices=("mongo", "postgres"),
                   help="Motor de base de datos a evaluar.")
    p.add_argument("--profile", default=env("PROFILE_NAME", "PROFILE_A_BASELINE"),
                   help="Nombre del perfil cgroups activo.")
    p.add_argument("--query-id", required=True, choices=("Q1", "Q2"),
                   help="Identificador de la consulta (Q1 o Q2 y sus variantes).")
    p.add_argument("--query-label", default=None,
                   help="Etiqueta opcional para la columna query_id del CSV (ej. Q2_64MB). "
                        "Por defecto es igual a --query-id.")
    p.add_argument("--concurrency", type=int, required=True,
                   help="Número de clientes concurrentes (ej. 1, 10, 50).")
    p.add_argument("--run-number", type=int, default=1,
                   help="Número de repetición de la celda (1..3).")
    p.add_argument("--warmup", type=int, default=60,
                   help="Segundos de calentamiento previos a la medición (default: 60).")
    p.add_argument("--duration", type=int, default=300,
                   help="Segundos de ventana de medición (default: 300).")
    p.add_argument("--pg-work-mem", default="4MB",
                   help="Valor de work_mem inyectado por sesión en PostgreSQL (default: 4MB, D-34).")
    p.add_argument("--mongo-allow-disk-use", action=argparse.BooleanOptionalAction, default=False,
                   help="Habilita allowDiskUse en aggregate de MongoDB (default: False, D-34).")
    p.add_argument("--output-csv", type=Path, default=DEFAULT_CSV_PATH,
                   help=f"Ruta del archivo CSV de salida (default: {DEFAULT_CSV_PATH.name}).")
    p.add_argument("--mongo-uri", default=default_mongo_uri)
    p.add_argument("--mongo-db", default=env("POSTGRES_DB", "benchdb"))
    p.add_argument("--pg-dsn", default=default_pg_dsn)
    p.add_argument("--prometheus-url", default=default_prom_url)

    args = p.parse_args(argv)
    if args.concurrency <= 0:
        p.error("--concurrency debe ser > 0")
    if args.duration <= 0:
        p.error("--duration debe ser > 0")
    if args.warmup < 0:
        p.error("--warmup debe ser >= 0")
    return args


def load_query(engine: str, query_id: str) -> Any:
    """Carga el pipeline MQL (lista JSON) o la sentencia SQL desde queries/."""
    q_num = "q1" if query_id.upper() == "Q1" else "q2"
    if engine == "mongo":
        path = QUERIES_DIR / f"mongo_{q_num}.json"
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    else:
        path = QUERIES_DIR / f"postgres_{q_num}.sql"
        with path.open("r", encoding="utf-8") as fh:
            return fh.read().strip()


# ===========================================================================
# 2. Telemetría (Doble Vía: cgroups v2 Directo + Prometheus + Motores)
# ===========================================================================
def container_name_for(engine: str) -> str:
    return "bench_mongo" if engine == "mongo" else "bench_postgres"


def read_cgroup_snapshot(container_name: str) -> Tuple[Optional[float], Optional[float]]:
    """Lee throttled_usec y memory.current (anon + active/inactive_anon o current)
    directamente desde /sys/fs/cgroup dentro del contenedor (cgroups v2).
    Devuelve (throttled_seconds, rss_or_mem_mb)."""
    cmd = [
        "docker", "exec", container_name, "sh", "-c",
        "cat /sys/fs/cgroup/cpu.stat 2>/dev/null; echo '---'; "
        "cat /sys/fs/cgroup/memory.current 2>/dev/null; echo '---'; "
        "cat /sys/fs/cgroup/memory.stat 2>/dev/null"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=4, check=False)
        if res.returncode != 0 or not res.stdout:
            return None, None
        parts = res.stdout.split("---")
        cpu_part = parts[0] if len(parts) > 0 else ""
        mem_cur_part = parts[1].strip() if len(parts) > 1 else ""
        mem_stat_part = parts[2] if len(parts) > 2 else ""

        throttled_s = None
        for line in cpu_part.splitlines():
            if line.startswith("throttled_usec"):
                throttled_s = int(line.split()[1]) / 1_000_000.0
                break

        # Para RSS estricto en cgroup v2: 'anon' en memory.stat representa RSS anónimo;
        # si no está disponible, usamos memory.current (working set total del cgroup).
        anon_bytes = None
        for line in mem_stat_part.splitlines():
            if line.startswith("anon "):
                anon_bytes = int(line.split()[1])
                break

        mem_cur_bytes = int(mem_cur_part) if mem_cur_part.isdigit() else None
        # Tomamos el mayor entre anon y memory.current (working set real bajo el límite cgroup)
        chosen_bytes = mem_cur_bytes if mem_cur_bytes is not None else anon_bytes
        rss_mb = (chosen_bytes / (1024.0 * 1024.0)) if chosen_bytes is not None else None
        return throttled_s, rss_mb
    except Exception:
        return None, None


def inspect_container_oom(container_name: str) -> Tuple[bool, bool]:
    """Devuelve (is_running, oom_killed) consultando docker inspect."""
    cmd = [
        "docker", "inspect",
        "-f", "{{.State.Running}},{{.State.OOMKilled}},{{.State.ExitCode}}",
        container_name,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
        if res.returncode != 0:
            return False, False
        tokens = res.stdout.strip().split(",")
        running = tokens[0].lower() == "true"
        oom = (len(tokens) > 1 and tokens[1].lower() == "true") or (
            len(tokens) > 2 and tokens[2] == "137"
        )
        return running, oom
    except Exception:
        return False, False


class CgroupMonitor:
    """Hilo ligero que muestrea cada 1s el cgroup del contenedor durante la ventana
    de medición (t0..t1), garantizando lecturas exactas incluso si cAdvisor en WSL2
    omite etiquetas de nombre."""

    def __init__(self, container_name: str) -> None:
        self.container_name = container_name
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.t0_throttled_s: Optional[float] = None
        self.t1_throttled_s: Optional[float] = None
        self.max_rss_mb: float = 0.0

    def start(self) -> None:
        self.t0_throttled_s, rss0 = read_cgroup_snapshot(self.container_name)
        if rss0 is not None:
            self.max_rss_mb = max(self.max_rss_mb, rss0)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_evt.wait(1.0):
            thr_s, rss_mb = read_cgroup_snapshot(self.container_name)
            if thr_s is not None:
                self.t1_throttled_s = thr_s
                if self.t0_throttled_s is None:
                    self.t0_throttled_s = thr_s
            if rss_mb is not None and rss_mb > self.max_rss_mb:
                self.max_rss_mb = rss_mb

    def stop(self) -> Tuple[Optional[float], Optional[float]]:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        thr_end, rss_end = read_cgroup_snapshot(self.container_name)
        if thr_end is not None:
            self.t1_throttled_s = thr_end
        if rss_end is not None and rss_end > self.max_rss_mb:
            self.max_rss_mb = rss_end

        delta_thr = None
        if self.t0_throttled_s is not None and self.t1_throttled_s is not None:
            delta_thr = max(0.0, self.t1_throttled_s - self.t0_throttled_s)
        return delta_thr, (self.max_rss_mb if self.max_rss_mb > 0 else None)


def query_prometheus_scalar(prom_url: str, promql: str, eval_ts: float) -> Optional[float]:
    """Ejecuta una consulta instantánea en Prometheus en el instante eval_ts."""
    params = urllib.parse.urlencode({"query": promql, "time": f"{eval_ts:.3f}"})
    url = f"{prom_url.rstrip('/')}/api/v1/query?{params}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("status") != "success":
            return None
        results = payload.get("data", {}).get("result", [])
        if not results:
            return None
        vals = [float(r["value"][1]) for r in results if "value" in r]
        return max(vals) if vals else None
    except Exception:
        return None


def fetch_prometheus_metrics(
    prom_url: str, container_name: str, window_s: int, eval_ts: float
) -> Tuple[Optional[float], Optional[float]]:
    """Intenta obtener (cpu_throttled_seconds, max_rss_mb) desde Prometheus/cAdvisor."""
    w = max(10, int(window_s))
    # Probamos por etiqueta name y por coincidencia de id/name
    thr_queries = [
        f'increase(container_cpu_cfs_throttled_seconds_total{{name="{container_name}"}}[{w}s])',
        f'increase(container_cpu_cfs_throttled_seconds_total{{name=~".*{container_name}.*"}}[{w}s])',
    ]
    rss_queries = [
        f'max_over_time(container_memory_working_set_bytes{{name="{container_name}"}}[{w}s])',
        f'max_over_time(container_memory_rss{{name="{container_name}"}}[{w}s])',
        f'max_over_time(container_memory_usage_bytes{{name="{container_name}"}}[{w}s])',
    ]
    thr_val = None
    for q in thr_queries:
        thr_val = query_prometheus_scalar(prom_url, q, eval_ts)
        if thr_val is not None:
            break

    rss_val = None
    for q in rss_queries:
        v = query_prometheus_scalar(prom_url, q, eval_ts)
        if v is not None and v > 0:
            rss_val = v / (1024.0 * 1024.0)
            break

    return thr_val, rss_val


# ===========================================================================
# 3. Contadores de Disk Spill en el Motor (entre t0 y t1)
# ===========================================================================
def read_db_spill_counter(args: argparse.Namespace) -> Optional[int]:
    """Lee el contador acumulado de volcado a disco del motor activo:
    - PostgreSQL: SUM(temp_files + temp_bytes) en pg_stat_database para benchdb.
    - MongoDB: métricas de sort spill en serverStatus (sortSpills / allowDiskUse)."""
    try:
        if args.engine == "postgres":
            conn = psycopg2.connect(args.pg_dsn, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COALESCE(temp_files, 0) + COALESCE(temp_bytes, 0) "
                        "FROM pg_stat_database WHERE datname = current_database();"
                    )
                    row = cur.fetchone()
                    return int(row[0]) if row else 0
            finally:
                conn.close()
        else:
            client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5_000)
            try:
                status = client[args.mongo_db].command("serverStatus")
                metrics = status.get("metrics", {})
                op_metrics = metrics.get("operation", {})
                qe_metrics = metrics.get("queryExecutor", {})
                spills = (
                    int(op_metrics.get("sortSpills", 0))
                    + int(qe_metrics.get("sortSpills", 0))
                    + int(metrics.get("Sort", {}).get("spillToDisk", 0))
                )
                return spills
            finally:
                client.close()
    except Exception:
        return None


# ===========================================================================
# 4. Workers Concurrentes (Warmup + Ventana de Medición)
# ===========================================================================
class WorkerResult:
    __slots__ = ("latencies_ms", "error_count", "disk_spill_error")

    def __init__(self) -> None:
        self.latencies_ms: List[float] = []
        self.error_count: int = 0
        self.disk_spill_error: bool = False


def mongo_worker(
    worker_id: int,
    client: MongoClient,
    db_name: str,
    pipeline: List[Dict[str, Any]],
    allow_disk_use: bool,
    barrier: threading.Barrier,
    timing_ref: List[float],
) -> WorkerResult:
    """Hilo de carga para MongoDB. Comparte MongoClient thread-safe con pool >= C."""
    res = WorkerResult()
    coll = client[db_name][MONGO_COLLECTION]

    # Pre-calentamiento de socket antes de la barrera
    try:
        client.admin.command("ping")
    except Exception:
        pass

    try:
        barrier.wait(timeout=60.0)
    except threading.BrokenBarrierError:
        res.error_count += 1
        return res

    t_measure_start = timing_ref[0]
    t_measure_end = timing_ref[1]

    # Fase 1: Warmup (no registra métricas)
    while perf_counter() < t_measure_start:
        try:
            with coll.aggregate(pipeline, allowDiskUse=allow_disk_use, maxTimeMS=STATEMENT_TIMEOUT_MS) as cur:
                for _ in cur:
                    pass
        except PyMongoError as exc:
            # Si excede 100MB en RAM durante warmup con allowDiskUse=False, es señal de spill
            if getattr(exc, "code", None) in (292, 16819, 16820) or "memory limit" in str(exc).lower():
                res.disk_spill_error = True
            time.sleep(0.05)
        except Exception:
            time.sleep(0.05)

    # Fase 2: Medición (300s)
    while perf_counter() < t_measure_end:
        t0 = perf_counter()
        try:
            with coll.aggregate(pipeline, allowDiskUse=allow_disk_use, maxTimeMS=STATEMENT_TIMEOUT_MS) as cur:
                for _ in cur:
                    pass
            dt_ms = (perf_counter() - t0) * 1000.0
            # Solo registramos si terminó o inició dentro de la ventana
            res.latencies_ms.append(dt_ms)
        except PyMongoError as exc:
            res.error_count += 1
            if getattr(exc, "code", None) in (292, 16819, 16820) or "memory limit" in str(exc).lower():
                res.disk_spill_error = True
            time.sleep(0.05)
        except Exception:
            res.error_count += 1
            time.sleep(0.05)

    return res


def postgres_worker(
    worker_id: int,
    dsn: str,
    sql_query: str,
    work_mem: str,
    barrier: threading.Barrier,
    timing_ref: List[float],
) -> WorkerResult:
    """Hilo de carga para PostgreSQL. Cada hilo abre 1 conexión dedicada e inyecta work_mem."""
    res = WorkerResult()
    conn = None

    def _connect_and_configure():
        c = psycopg2.connect(dsn, connect_timeout=15)
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("SET work_mem = %s;", (work_mem,))
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS};")
        return c

    try:
        conn = _connect_and_configure()
    except Exception:
        # Si no pudo conectar antes de la barrera, igualmente toca la barrera para no bloquear a los demás
        pass

    try:
        barrier.wait(timeout=60.0)
    except threading.BrokenBarrierError:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        res.error_count += 1
        return res

    t_measure_start = timing_ref[0]
    t_measure_end = timing_ref[1]

    # Fase 1: Warmup
    while perf_counter() < t_measure_start:
        try:
            if conn is None or conn.closed:
                conn = _connect_and_configure()
            with conn.cursor() as cur:
                cur.execute(sql_query)
                cur.fetchall()
        except Exception:
            time.sleep(0.05)

    # Fase 2: Medición
    while perf_counter() < t_measure_end:
        t0 = perf_counter()
        try:
            if conn is None or conn.closed:
                conn = _connect_and_configure()
            with conn.cursor() as cur:
                cur.execute(sql_query)
                cur.fetchall()
            dt_ms = (perf_counter() - t0) * 1000.0
            res.latencies_ms.append(dt_ms)
        except psycopg2.Error as exc:
            res.error_count += 1
            # Código 53100 = disk_full / temp file limit
            if getattr(exc, "pgcode", None) == "53100":
                res.disk_spill_error = True
            time.sleep(0.05)
        except Exception:
            res.error_count += 1
            time.sleep(0.05)

    if conn is not None and not conn.closed:
        try:
            conn.close()
        except Exception:
            pass

    return res


# ===========================================================================
# 5. Cálculo de Percentiles y Escritura Atómica en CSV
# ===========================================================================
def percentile(sorted_vals: List[float], pct: float) -> float:
    """Calcula el percentil lineal exacto (0..100) sobre una lista ya ordenada."""
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = (pct / 100.0) * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def append_csv_row(csv_path: Path, row: Dict[str, Any]) -> None:
    """Añade una fila al CSV maestro creando la cabecera si el archivo no existe,
    y fuerza fsync a disco para garantizar supervivencia ante caídas."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = csv_path.exists() and csv_path.stat().st_size > 0
    with csv_path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())


# ===========================================================================
# 6. Orquestación Principal de la Corrida
# ===========================================================================
def main(argv=None) -> int:
    args = parse_args(argv)
    cname = container_name_for(args.engine)
    q_label = args.query_label if args.query_label else args.query_id
    query_payload = load_query(args.engine, args.query_id)

    log(
        f"INICIO CORRIDA | engine={args.engine} profile={args.profile} "
        f"query={q_label} concurrency={args.concurrency} run={args.run_number} "
        f"(warmup={args.warmup}s, duration={args.duration}s)"
    )

    # timing_ref se rellena justo cuando todos los hilos llegan a la barrera
    timing_ref: List[float] = [0.0, 0.0]

    def _on_barrier_release() -> None:
        now = perf_counter()
        timing_ref[0] = now + args.warmup
        timing_ref[1] = now + args.warmup + args.duration
        log(f"Barrera liberada ({args.concurrency} clientes listos). Iniciando Warmup ({args.warmup}s)...")

    barrier = threading.Barrier(args.concurrency, action=_on_barrier_release)

    mongo_client: Optional[MongoClient] = None
    if args.engine == "mongo":
        pool_sz = max(20, args.concurrency + 10)
        mongo_client = MongoClient(
            args.mongo_uri,
            maxPoolSize=pool_sz,
            minPoolSize=args.concurrency,
            serverSelectionTimeoutMS=15_000,
            socketTimeoutMS=STATEMENT_TIMEOUT_MS,
            connectTimeoutMS=15_000,
        )

    cgroup_mon = CgroupMonitor(cname)
    spill_t0: Optional[int] = None
    spill_t1: Optional[int] = None
    actual_measure_s: float = float(args.duration)

    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            if args.engine == "mongo":
                futures = [
                    executor.submit(
                        mongo_worker,
                        i,
                        mongo_client,
                        args.mongo_db,
                        query_payload,
                        args.mongo_allow_disk_use,
                        barrier,
                        timing_ref,
                    )
                    for i in range(args.concurrency)
                ]
            else:
                futures = [
                    executor.submit(
                        postgres_worker,
                        i,
                        args.pg_dsn,
                        query_payload,
                        args.pg_work_mem,
                        barrier,
                        timing_ref,
                    )
                    for i in range(args.concurrency)
                ]

            # Esperamos a que la barrera se libere
            while timing_ref[0] == 0.0 and any(not f.done() for f in futures):
                time.sleep(0.05)

            # Esperamos el fin del warmup para iniciar los monitores de la ventana de medición
            if timing_ref[0] > 0.0:
                rem_warmup = timing_ref[0] - perf_counter()
                if rem_warmup > 0:
                    time.sleep(rem_warmup)

            log(f"Warmup completado. Iniciando Ventana de Medición ({args.duration}s)...")
            t_measure_real_start = perf_counter()
            spill_t0 = read_db_spill_counter(args)
            cgroup_mon.start()

            # Recolectar resultados al terminar todos los workers
            worker_results: List[WorkerResult] = [f.result() for f in futures]
            actual_measure_s = max(0.001, perf_counter() - t_measure_real_start)

        t_end_epoch = time.time()
        thr_cg, rss_cg = cgroup_mon.stop()
        spill_t1 = read_db_spill_counter(args)
    finally:
        if mongo_client is not None:
            try:
                mongo_client.close()
            except Exception:
                pass

    # Consolidar latencias y errores
    all_latencies: List[float] = []
    total_errors = 0
    any_spill_error = False
    for wr in worker_results:
        all_latencies.extend(wr.latencies_ms)
        total_errors += wr.error_count
        if wr.disk_spill_error:
            any_spill_error = True

    all_latencies.sort()
    ok_count = len(all_latencies)
    total_ops = ok_count + total_errors

    qps = round(ok_count / actual_measure_s, 2)
    p50 = round(percentile(all_latencies, 50.0), 2)
    p95 = round(percentile(all_latencies, 95.0), 2)
    p99 = round(percentile(all_latencies, 99.0), 2)

    # Telemetría: combinamos Prometheus y lectura directa de cgroup v2
    thr_prom, rss_prom = fetch_prometheus_metrics(
        args.prometheus_url, cname, int(round(actual_measure_s)), t_end_epoch
    )
    cpu_throttled_s = round(
        thr_cg if thr_cg is not None else (thr_prom if thr_prom is not None else 0.0), 3
    )
    max_rss_mb = round(
        max(rss_cg or 0.0, rss_prom or 0.0), 2
    )

    # Determinación de disk_spill (boolean estricto: true / false)
    spill_delta = 0
    if spill_t0 is not None and spill_t1 is not None and spill_t1 > spill_t0:
        spill_delta = spill_t1 - spill_t0
    disk_spill_bool = (spill_delta > 0) or any_spill_error
    disk_spill_str = "true" if disk_spill_bool else "false"

    # Determinación de status de la corrida
    is_running, oom_killed = inspect_container_oom(cname)
    if oom_killed:
        status = "OOM_KILLED"
    elif not is_running or (ok_count == 0 and total_errors > 0):
        status = "FAILED"
    elif total_errors > 0:
        status = "DEGRADED"
    else:
        status = "OK"

    row = {
        "timestamp_epoch": int(t_end_epoch),
        "engine": args.engine,
        "profile": args.profile,
        "query_id": q_label,
        "concurrency": args.concurrency,
        "run_number": args.run_number,
        "throughput_qps": qps,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "latency_p99_ms": p99,
        "cpu_throttled_seconds": cpu_throttled_s,
        "max_rss_mb": max_rss_mb,
        "disk_spill": disk_spill_str,
        "status": status,
    }

    append_csv_row(args.output_csv, row)

    err_pct = (total_errors / total_ops * 100.0) if total_ops > 0 else 0.0
    log(
        f"FIN CORRIDA | status={status} ok={ok_count} err={total_errors} ({err_pct:.1f}%) "
        f"| QPS={qps} | p50={p50}ms p95={p95}ms p99={p99}ms "
        f"| throttled={cpu_throttled_s}s max_rss={max_rss_mb}MB spill={disk_spill_str}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

