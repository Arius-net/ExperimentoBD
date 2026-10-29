#!/usr/bin/env python3
"""
synthetic_data_generator.py - Generador de datos sintéticos para el benchmark
MongoDB vs PostgreSQL JSONB.

- Genera N órdenes (500.000 por defecto) de forma determinista (semilla fija),
  de modo que cargar Mongo y PostgreSQL por separado produce documentos idénticos.
- Sin ORMs: pymongo (insert_many) y psycopg2 (COPY).
- Al final crea los índices y deja la BD lista (VACUUM (ANALYZE) en PostgreSQL).
- Reporta por separado el tiempo de generación en Python y el de inserción en la BD.

Uso (antes: `set -a; source .env; set +a`):
    python synthetic_data_generator.py --target mongo
    python synthetic_data_generator.py --target postgres
"""
import argparse
import csv
import hashlib
import io
import json
import os
import random
import sys
import uuid
from datetime import datetime, timezone
from time import perf_counter
from typing import NamedTuple

import psycopg2
from faker import Faker
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

# ===========================================================================
# BLOQUE 1: Parámetros del dataset y CLI
# ===========================================================================

# ---------------------------------------------------------------------------
# Parámetros del dataset (cardinalidades aprobadas, D-19)
# ---------------------------------------------------------------------------
DEFAULT_N_ORDERS = 500_000
DEFAULT_SEED = 42
DEFAULT_BATCH_SIZE = 5_000

N_CUSTOMERS = 50_000        # ~10 órdenes por cliente
N_LOCATIONS = 500           # combinaciones país/estado/ciudad/zip/coordenadas
N_SKUS = 5_000              # cada SKU con categoría, marca y precio fijos
N_CATEGORIES = 20
N_BRANDS = 100

# Ítems por orden: valores 1..6 con pesos que dan media 3.01
ITEMS_PER_ORDER_VALUES = (1, 2, 3, 4, 5, 6)
ITEMS_PER_ORDER_WEIGHTS = (12, 26, 30, 18, 9, 5)

PENDING_PROBABILITY = 0.15  # payment.status: 85 % completed / 15 % pending

# Fechas: 365 días con base FIJA (reproducible), UTC (D-17)
BASE_TIMESTAMP_ISO = "2025-01-01T00:00:00.000Z"
DATE_RANGE_DAYS = 365

# Destino de los datos
MONGO_COLLECTION = "orders"
PG_TABLE = "orders"


def parse_args(argv=None) -> argparse.Namespace:
    """Lee la configuración de la línea de comandos. Los valores por defecto de conexión
    provienen de las variables del .env (MONGO_PORT, POSTGRES_USER, ...)."""
    env = os.environ.get

    default_mongo_uri = f"mongodb://localhost:{env('MONGO_PORT', '27017')}"
    default_pg_dsn = (
        f"host=localhost port={env('POSTGRES_PORT', '5432')} "
        f"user={env('POSTGRES_USER', 'bench')} "
        f"password={env('POSTGRES_PASSWORD', 'bench_pass')} "
        f"dbname={env('POSTGRES_DB', 'benchdb')}"
    )

    p = argparse.ArgumentParser(
        description="Genera y carga órdenes sintéticas en MongoDB o PostgreSQL (JSONB)."
    )
    p.add_argument("--target", required=True, choices=("mongo", "postgres"),
                   help="Base de datos de destino (solo una activa por celda, D-07).")
    p.add_argument("--n", type=int, default=DEFAULT_N_ORDERS,
                   help=f"Número de órdenes (por defecto {DEFAULT_N_ORDERS}).")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help=f"Semilla del generador (por defecto {DEFAULT_SEED}).")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help=f"Documentos por lote (por defecto {DEFAULT_BATCH_SIZE}).")
    p.add_argument("--drop", action=argparse.BooleanOptionalAction, default=True,
                   help="Borra y recrea la colección/tabla antes de cargar (por defecto SÍ; "
                        "usar --no-drop para desactivarlo). Con --no-drop y la misma --seed "
                        "los datos se DUPLICAN: en PostgreSQL fallará al crear la PK, "
                        "en Mongo no habrá error.")
    p.add_argument("--skip-indexes", action="store_true",
                   help="No crear índices ni ejecutar VACUUM/ANALYZE al final. Modo de "
                        "depuración: en PostgreSQL la tabla queda sin PK.")
    p.add_argument("--mongo-uri", default=default_mongo_uri)
    p.add_argument("--mongo-db", default=env("POSTGRES_DB", "benchdb"),
                   help="Nombre de la base de datos en MongoDB.")
    p.add_argument("--pg-dsn", default=default_pg_dsn)

    args = p.parse_args(argv)
    if args.n <= 0:
        p.error("--n debe ser > 0")
    if args.batch_size <= 0:
        p.error("--batch-size debe ser > 0")
    return args


# ===========================================================================
# BLOQUE 2: Catálogos (Faker)
# ===========================================================================

# ---------------------------------------------------------------------------
# Dominios de valores categóricos
# ---------------------------------------------------------------------------
TIERS = ("bronze", "silver", "gold", "platinum")
TIER_WEIGHTS = (50, 30, 15, 5)

SEGMENTS = ("retail", "wholesale", "enterprise", "online_only", "vip")
SEGMENT_WEIGHTS = (40, 15, 10, 25, 10)

COUNTRIES = ("Mexico", "United States", "Canada", "Spain", "Colombia",
             "Argentina", "Chile", "Brazil", "Germany", "France")

CATEGORIES = ("electronics", "home_kitchen", "books", "toys", "sports",
              "beauty", "grocery", "fashion", "automotive", "garden",
              "pets", "office", "music", "movies", "health",
              "tools", "jewelry", "baby", "video_games", "furniture")

PAYMENT_METHODS = ("credit_card", "debit_card", "paypal", "bank_transfer")
CHANNELS = ("web", "mobile_app", "store", "marketplace", "call_center")
FLAGS = ("gift", "priority", "fraud_review", "promo_applied",
         "first_purchase", "international")
DISCOUNT_CODES = ("WELCOME10", "SPRING15", "LOYALTY5", "BULK20", "FLASH25")

N_USER_AGENTS = 50


class Pools(NamedTuple):
    """Catálogos reutilizados por todas las órdenes. Se construyen una sola vez."""
    customers: list    # dicts {id, tier, segment, geo{...}}; el geo es fijo por cliente
    skus: list         # dicts {sku, category, brand, price}
    user_agents: list  # strings


def build_pools(seed: int) -> Pools:
    """Construye los catálogos de forma determinista a partir de la semilla.

    Faker (lento) solo se usa aquí, una vez. La instancia propia se siembra con
    seed_instance, y el resto del azar usa un random.Random independiente.
    El resultado es reproducible mientras se mantengan fijas las versiones
    de Faker y de Python (requirements.txt las fija).
    """
    assert len(CATEGORIES) == N_CATEGORIES, "CATEGORIES debe tener N_CATEGORIES elementos"

    fake = Faker("en_US")
    fake.seed_instance(seed)
    rng = random.Random(f"pools-{seed}")

    # Marcas: N_BRANDS nombres distintos
    brands = []
    seen = set()
    while len(brands) < N_BRANDS:
        name = fake.company()
        if name not in seen:
            seen.add(name)
            brands.append(name)

    # Ubicaciones (geo completo, con coordenadas [lat, lon])
    locations = [
        {
            "country": rng.choice(COUNTRIES),
            "state": fake.state(),
            "city": fake.city(),
            "zip": fake.postcode(),
            "coordinates": [round(float(fake.latitude()), 6),
                            round(float(fake.longitude()), 6)],
        }
        for _ in range(N_LOCATIONS)
    ]

    # Clientes: cada uno con tier, segmento y ubicación fijos
    customers = []
    for i in range(1, N_CUSTOMERS + 1):
        customers.append({
            "id": f"CUST-{i:06d}",
            "tier": rng.choices(TIERS, weights=TIER_WEIGHTS)[0],
            "segment": rng.choices(SEGMENTS, weights=SEGMENT_WEIGHTS)[0],
            "geo": rng.choice(locations),
        })

    # SKUs: categoría rotativa (todas las categorías con el mismo número de SKUs),
    # marca aleatoria y precio log-normal acotado a [1.00, 2000.00]
    skus = []
    for i in range(1, N_SKUS + 1):
        price = min(max(rng.lognormvariate(3.8, 0.9), 1.0), 2000.0)
        skus.append({
            "sku": f"SKU-{i:05d}",
            "category": CATEGORIES[(i - 1) % N_CATEGORIES],
            "brand": rng.choice(brands),
            "price": round(price, 2),
        })

    user_agents = [fake.user_agent() for _ in range(N_USER_AGENTS)]

    return Pools(customers=customers, skus=skus, user_agents=user_agents)


# ===========================================================================
# BLOQUE 3: generate_order
# ===========================================================================

# ---------------------------------------------------------------------------
# Parámetros de las líneas de pedido, descuentos y metadatos
# ---------------------------------------------------------------------------
TAX_RATES = (0.0, 0.08, 0.16)
QTY_MIN, QTY_MAX = 1, 5

DISCOUNT_PROBABILITY = 0.20          # ~80 % de los ítems sin descuentos
DISCOUNT_PCTS = (0.05, 0.10, 0.15, 0.20, 0.25)

FLAG_COUNT_VALUES = (0, 1, 2, 3)
FLAG_COUNT_WEIGHTS = (55, 30, 10, 5)

# Base temporal fija (D-17): ms desde epoch de BASE_TIMESTAMP_ISO y amplitud en ms
_BASE_EPOCH_MS = int(
    datetime.strptime(BASE_TIMESTAMP_ISO, "%Y-%m-%dT%H:%M:%S.%fZ")
    .replace(tzinfo=timezone.utc)
    .timestamp()
) * 1000
_RANGE_MS = DATE_RANGE_DAYS * 86_400_000


def orders_rng(seed: int) -> random.Random:
    """Generador de las órdenes, independiente del de los catálogos (build_pools)."""
    return random.Random(f"orders-{seed}")


def _format_timestamp(epoch_ms: int) -> str:
    """ISO-8601 UTC de ancho fijo: YYYY-MM-DDTHH:MM:SS.mmmZ (D-17).
    El ancho fijo garantiza que el orden alfabético sea el cronológico."""
    seconds, millis = divmod(epoch_ms, 1000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{millis:03d}Z"


def generate_order(rng: random.Random, pools: Pools) -> dict:
    """Genera UN documento de orden con la estructura de contexto_del_experimento.md.

    Pura y determinista: el resultado depende solo del estado de `rng` y de `pools`.
    El orden de las llamadas a `rng` es fijo, no reordenar sin regenerar los datos.

    Totales: `subtotal` es la suma de qty * price con descuentos aplicados,
    `tax` aplica el tax_pct de cada ítem sobre su neto y
    `final_total` = subtotal + tax, todo redondeado a 2 decimales.
    El cliente (con su `geo`) y los catálogos se reutilizan por referencia: es seguro
    porque nada los modifica después de generada la orden.
    """
    order_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
    timestamp = _format_timestamp(_BASE_EPOCH_MS + rng.randrange(_RANGE_MS))
    customer = rng.choice(pools.customers)

    # --- ítems ---
    n_items = rng.choices(ITEMS_PER_ORDER_VALUES, weights=ITEMS_PER_ORDER_WEIGHTS)[0]
    items = []
    subtotal = 0.0
    tax = 0.0
    for _ in range(n_items):
        sku = rng.choice(pools.skus)
        qty = rng.randint(QTY_MIN, QTY_MAX)
        tax_pct = rng.choice(TAX_RATES)

        discounts = []
        if rng.random() < DISCOUNT_PROBABILITY:
            for _ in range(rng.randint(1, 2)):
                discounts.append({
                    "code": rng.choice(DISCOUNT_CODES),
                    "pct": rng.choice(DISCOUNT_PCTS),
                })

        line_net = qty * sku["price"] * (1.0 - sum(d["pct"] for d in discounts))
        subtotal += line_net
        tax += line_net * tax_pct

        items.append({
            "sku": sku["sku"],
            "category": sku["category"],
            "brand": sku["brand"],
            "qty": qty,
            "price": sku["price"],
            "tax_pct": tax_pct,
            "discounts": discounts,
        })

    subtotal = round(subtotal, 2)
    tax = round(tax, 2)

    # --- pago ---
    payment = {
        "method": rng.choice(PAYMENT_METHODS),
        "status": "pending" if rng.random() < PENDING_PROBABILITY else "completed",
        "transaction_id": f"TXN-{rng.getrandbits(48):012x}",
        "gateway_latency_ms": int(min(max(rng.lognormvariate(4.0, 0.7), 10.0), 500.0)),
        "totals": {
            "subtotal": subtotal,
            "tax": tax,
            "final_total": round(subtotal + tax, 2),
        },
    }

    # --- metadatos ---
    n_flags = rng.choices(FLAG_COUNT_VALUES, weights=FLAG_COUNT_WEIGHTS)[0]
    metadata = {
        "channel": rng.choice(CHANNELS),
        "user_agent": rng.choice(pools.user_agents),
        "session_id": f"{rng.getrandbits(128):032x}",
        "flags": rng.sample(FLAGS, k=n_flags),
    }

    return {
        "order_id": order_id,
        "timestamp": timestamp,
        "customer": customer,
        "items": items,
        "payment": payment,
        "metadata": metadata,
    }


# ===========================================================================
# BLOQUE 4: Lotes y cargadores (con tiempos separados, D-26)
# ===========================================================================

PROGRESS_EVERY_BATCHES = 10   # informa cada 10 lotes (50.000 docs con lotes de 5.000)


class LoadResult(NamedTuple):
    inserted: int
    gen_seconds: float        # generate_order (Python/CPU), medido dentro del generador
    serialize_seconds: float  # JSON -> CSV para COPY (solo PostgreSQL; 0.0 en Mongo)
    insert_seconds: float     # driver + red + motor (+ commit en PostgreSQL)
    wall_seconds: float       # tiempo total del bucle de carga


class TimedBatches:
    """Itera los lotes midiendo SOLO el tiempo dentro del generador (Python Gen)."""

    def __init__(self, batches):
        self._it = iter(batches)
        self.gen_seconds = 0.0

    def __iter__(self):
        return self

    def __next__(self):
        t = perf_counter()
        try:
            return next(self._it)
        finally:
            self.gen_seconds += perf_counter() - t


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def generate_batches(n: int, batch_size: int, rng: random.Random, pools: Pools):
    """Genera `n` órdenes en lotes de `batch_size`, sin guardarlas todas en memoria."""
    produced = 0
    while produced < n:
        size = min(batch_size, n - produced)
        yield [generate_order(rng, pools) for _ in range(size)]
        produced += size


def _report(batch_no: int, inserted: int, total: int, t0: float) -> None:
    if batch_no % PROGRESS_EVERY_BATCHES == 0 or inserted == total:
        elapsed = perf_counter() - t0
        log(f"{inserted:>9,}/{total:,} docs  ({inserted / elapsed:,.0f} docs/s)")


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------
def load_mongo(args: argparse.Namespace, batches) -> LoadResult:
    """Inserta los lotes con insert_many (sin ORM). Con --drop borra la colección antes."""
    timed = TimedBatches(batches)
    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=10_000)
    try:
        client.admin.command("ping")               # falla pronto si Mongo no está arriba
        coll = client[args.mongo_db][MONGO_COLLECTION]
        if args.drop:
            coll.drop()
            log(f"Colección {args.mongo_db}.{MONGO_COLLECTION} eliminada (--drop).")

        inserted = 0
        insert_s = 0.0
        t0 = perf_counter()
        for batch_no, batch in enumerate(timed, start=1):
            t = perf_counter()
            coll.insert_many(batch, ordered=False)
            insert_s += perf_counter() - t
            inserted += len(batch)
            _report(batch_no, inserted, args.n, t0)
        return LoadResult(inserted, timed.gen_seconds, 0.0, insert_s, perf_counter() - t0)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
def _prepare_pg_table(cur, drop: bool) -> None:
    """Crea la tabla sin índices: la PK y el índice de timestamp se crean tras la carga."""
    if drop:
        cur.execute(f"DROP TABLE IF EXISTS {PG_TABLE}")
        log(f"Tabla {PG_TABLE} eliminada (--drop).")
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {PG_TABLE} (id UUID NOT NULL, data JSONB NOT NULL)"
    )


def _batch_to_csv(batch) -> io.StringIO:
    """Serializa un lote a CSV (id, json) para COPY. El JSON se escribe compacto y el
    csv.writer entrecomilla los campos, evitando los problemas de escapes de COPY texto."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for doc in batch:
        writer.writerow((doc["order_id"], json.dumps(doc, separators=(",", ":"))))
    buf.seek(0)
    return buf


def load_postgres(args: argparse.Namespace, batches) -> LoadResult:
    """Carga con COPY ... FROM STDIN (CSV) dentro de UNA transacción: si algo falla,
    no queda carga parcial. Sin ORM. El commit cuenta como tiempo de inserción."""
    timed = TimedBatches(batches)
    conn = psycopg2.connect(args.pg_dsn)
    try:
        with conn.cursor() as cur:
            _prepare_pg_table(cur, args.drop)

            inserted = 0
            serialize_s = 0.0
            insert_s = 0.0
            t0 = perf_counter()
            copy_sql = f"COPY {PG_TABLE} (id, data) FROM STDIN WITH (FORMAT csv)"
            for batch_no, batch in enumerate(timed, start=1):
                t = perf_counter()
                buf = _batch_to_csv(batch)
                serialize_s += perf_counter() - t

                t = perf_counter()
                cur.copy_expert(copy_sql, buf)
                insert_s += perf_counter() - t

                inserted += len(batch)
                _report(batch_no, inserted, args.n, t0)

            t = perf_counter()
            conn.commit()
            insert_s += perf_counter() - t
        return LoadResult(inserted, timed.gen_seconds, serialize_s, insert_s,
                          perf_counter() - t0)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ===========================================================================
# BLOQUE 5: Índices, VACUUM (ANALYZE), verificación y main
# ===========================================================================

# Rango de noviembre de 2025 (comparación de texto ISO-8601, D-17/D-18)
NOV_START = "2025-11-01T00:00:00.000Z"
DEC_START = "2025-12-01T00:00:00.000Z"


class Verification(NamedTuple):
    count: int
    items_total: int
    final_total_sum: float
    completed_nov: int      # órdenes 'completed' de noviembre de 2025 (calibrar OFFSET de Q2, D-25)
    ids_md5: str            # hash de todos los order_id ordenados (mismo dataset en ambas BD)
    data_bytes: int
    index_bytes: int


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:,.1f} MB"


# ---------------------------------------------------------------------------
# Índices post-carga (D-22) y verificación: MongoDB
# ---------------------------------------------------------------------------
def finalize_mongo(args: argparse.Namespace) -> float:
    """Crea los índices individuales (timestamp, order_id). Devuelve los segundos usados."""
    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=10_000)
    try:
        coll = client[args.mongo_db][MONGO_COLLECTION]
        t0 = perf_counter()
        for field in ("timestamp", "order_id"):
            t = perf_counter()
            coll.create_index([(field, ASCENDING)], name=f"idx_orders_{field}")
            log(f"Índice Mongo idx_orders_{field} creado ({perf_counter() - t:.1f} s).")
        return perf_counter() - t0
    finally:
        client.close()


def verify_mongo(args: argparse.Namespace) -> Verification:
    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=10_000)
    try:
        db = client[args.mongo_db]
        coll = db[MONGO_COLLECTION]
        agg = next(coll.aggregate([{"$group": {
            "_id": None,
            "n": {"$sum": 1},
            "items": {"$sum": {"$size": "$items"}},
            "total": {"$sum": "$payment.totals.final_total"},
        }}]), {"n": 0, "items": 0, "total": 0.0})
        completed_nov = coll.count_documents({
            "payment.status": "completed",
            "timestamp": {"$gte": NOV_START, "$lt": DEC_START},
        })
        ids = sorted(d["order_id"] for d in coll.find({}, {"order_id": 1, "_id": 0}))
        ids_md5 = hashlib.md5(",".join(ids).encode()).hexdigest()
        stats = db.command("collStats", MONGO_COLLECTION)
        return Verification(agg["n"], agg["items"], round(agg["total"], 2), completed_nov,
                            ids_md5, stats["storageSize"], stats["totalIndexSize"])
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Índices post-carga (D-22, D-27, D-18) y verificación: PostgreSQL
# ---------------------------------------------------------------------------
def finalize_postgres(args: argparse.Namespace) -> float:
    """PK sobre id, B-Tree sobre el TEXTO data->>'timestamp' (D-18) y VACUUM (ANALYZE).

    VACUUM fija los hint bits tras el COPY masivo (evita escrituras en las primeras
    lecturas) y construye el mapa de visibilidad; no puede ejecutarse dentro de una
    transacción, de ahí autocommit.
    """
    conn = psycopg2.connect(args.pg_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            t0 = perf_counter()

            t = perf_counter()
            cur.execute("SELECT 1 FROM pg_constraint "
                        f"WHERE conrelid = '{PG_TABLE}'::regclass AND contype = 'p'")
            if cur.fetchone() is None:
                cur.execute(f"ALTER TABLE {PG_TABLE} ADD CONSTRAINT {PG_TABLE}_pkey PRIMARY KEY (id)")
                log(f"PK de {PG_TABLE} creada ({perf_counter() - t:.1f} s).")
            else:
                log(f"PK de {PG_TABLE} ya existía; se conserva.")

            t = perf_counter()
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_orders_timestamp "
                        f"ON {PG_TABLE} ((data->>'timestamp'))")
            log(f"Índice PG idx_orders_timestamp creado ({perf_counter() - t:.1f} s).")

            t = perf_counter()
            cur.execute(f"VACUUM (ANALYZE) {PG_TABLE}")
            log(f"VACUUM (ANALYZE) ejecutado ({perf_counter() - t:.1f} s).")
            return perf_counter() - t0
    finally:
        conn.close()


def verify_postgres(args: argparse.Namespace) -> Verification:
    conn = psycopg2.connect(args.pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT count(*),
                       coalesce(sum(jsonb_array_length(data->'items')), 0),
                       coalesce(round(sum((data->'payment'->'totals'->>'final_total')::numeric), 2), 0),
                       count(*) FILTER (WHERE data->'payment'->>'status' = 'completed'
                                          AND data->>'timestamp' >= %s
                                          AND data->>'timestamp' <  %s),
                       md5(string_agg(id::text, ',' ORDER BY id)),
                       pg_table_size('{PG_TABLE}'),
                       pg_indexes_size('{PG_TABLE}')
                FROM {PG_TABLE}
                """,
                (NOV_START, DEC_START),
            )
            n, items, total, nov, md5, data_b, index_b = cur.fetchone()
        return Verification(int(n), int(items), float(total), int(nov), md5 or "",
                            int(data_b), int(index_b))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reporte y main
# ---------------------------------------------------------------------------
def print_summary(args, pools_seconds: float, load: LoadResult,
                  finalize_seconds: float, v: Verification) -> None:
    rate = load.inserted / load.insert_seconds if load.insert_seconds else 0.0
    print()
    print("=" * 72)
    print(f"RESUMEN  target={args.target}  n={args.n:,}  seed={args.seed}")
    print("=" * 72)
    print(f"Python Gen: {load.gen_seconds:.1f} s | DB Insert: {load.insert_seconds:.1f} s"
          f"  ({rate:,.0f} docs/s solo inserción)")
    print(f"  Catálogos (Faker, una vez):          {pools_seconds:.1f} s")
    print(f"  Serialización JSON/CSV (solo PG):    {load.serialize_seconds:.1f} s"
          f"  (Mongo: 0; la codificación BSON va dentro de DB Insert)")
    print(f"  Índices + VACUUM/ANALYZE (post):     {finalize_seconds:.1f} s")
    print(f"  Bucle de carga (total):              {load.wall_seconds:.1f} s")
    print("-" * 72)
    ok = "OK" if v.count == args.n else f"DISTINTO de --n={args.n:,} (¿--no-drop?)"
    print(f"Documentos:            {v.count:,}  [{ok}]")
    print(f"Ítems totales:         {v.items_total:,}  (media {v.items_total / max(v.count, 1):.2f})")
    print(f"Suma final_total:      {v.final_total_sum:,.2f}")
    print(f"MD5 de order_id:       {v.ids_md5}")
    print(f"Datos en disco:        {_mb(v.data_bytes)}   Índices: {_mb(v.index_bytes)}")
    print(f"Q2 -> 'completed' de noviembre de 2025: {v.completed_nov:,}  (usar OFFSET < este valor)")
    print("Compara 'Documentos', 'Ítems', 'Suma final_total' y 'MD5' entre Mongo y PostgreSQL:")
    print("deben ser idénticos si ambas cargas usaron la misma --seed y --n.")
    print("=" * 72)


def main(argv=None) -> int:
    args = parse_args(argv)
    log(f"Destino={args.target} n={args.n:,} seed={args.seed} batch={args.batch_size} "
        f"drop={args.drop} skip_indexes={args.skip_indexes}")

    if args.target == "mongo":
        load_fn, finalize_fn, verify_fn = load_mongo, finalize_mongo, verify_mongo
    else:
        load_fn, finalize_fn, verify_fn = load_postgres, finalize_postgres, verify_postgres

    try:
        t = perf_counter()
        pools = build_pools(args.seed)
        pools_seconds = perf_counter() - t
        log(f"Catálogos construidos en {pools_seconds:.1f} s "
            f"({len(pools.customers):,} clientes, {len(pools.skus):,} SKUs).")

        batches = generate_batches(args.n, args.batch_size, orders_rng(args.seed), pools)
        load = load_fn(args, batches)

        finalize_seconds = 0.0
        if args.skip_indexes:
            log("--skip-indexes: se omiten índices y VACUUM/ANALYZE.")
        else:
            finalize_seconds = finalize_fn(args)

        log("Verificando la carga...")
        v = verify_fn(args)
    except (PyMongoError, psycopg2.Error) as exc:
        log(f"ERROR de base de datos: {exc}")
        return 1

    print_summary(args, pools_seconds, load, finalize_seconds, v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
