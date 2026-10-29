# Contexto del Experimento: MongoDB vs PostgreSQL JSONB

## 1. Objetivo Principal
Medir latencia, rendimiento y huella de recursos al ejecutar agregaciones JSON anidadas bajo límites estrictos de CPU (CFS throttling) y memoria con Docker cgroups v2, comparando MongoDB (motor documental) con PostgreSQL (almacenando documentos en JSONB).

## 2. Stack Tecnológico
*   **Orquestación e Infraestructura:** Docker Engine con soporte para `cgroups v2` y Docker Compose.
*   **Bases de Datos:**
    *   MongoDB (latest-stable), motor WiredTiger.
    *   PostgreSQL (latest-stable).
*   **Monitorización (Telemetría):** cAdvisor, Node Exporter, Prometheus (scrape_interval: 1s).
*   **Lenguaje de Desarrollo:** Python 3.10+ (Generador de datos y Benchmark Runner).
*   **Librerías de Conexión (SIN ORM para evitar overhead):**
    *   PostgreSQL: `psycopg2-binary` (o `asyncpg` si se opta por runner asíncrono).
    *   MongoDB: `pymongo` (o `motor` para asíncrono).
*   **Automatización:** Bash Scripts (Linux/Unix).

## 3. Estructura de las Bases de Datos

### MongoDB
*   **Colección:** `orders` (sin sharding).
*   **Índices:** Índice simple compuesto o individuales en `timestamp` y `order_id`.

### PostgreSQL
*   **Tabla:** `orders`
    *   `id` UUID PRIMARY KEY
    *   `data` JSONB
*   **Índices:** B-Tree sobre la clave primaria `id` y B-Tree extraído sobre la propiedad `timestamp` del JSONB (ej. `CREATE INDEX idx_orders_timestamp ON orders ((data->>'timestamp')::timestamptz);`).

### Estructura del Documento JSON (500,000 registros)
```json
{
  "order_id": "uuid",
  "timestamp": "ISO-8601",
  "customer": {
    "id": "string", "tier": "string", "segment": "string",
    "geo": { "country": "string", "state": "string", "city": "string", "zip": "string", "coordinates": [0.0, 0.0] }
  },
  "items": [
    { "sku": "string", "category": "string", "brand": "string", "qty": 1, "price": 100.0, "tax_pct": 0.16, "discounts": [] },
    // Promedio 3 items por registro
  ],
  "payment": {
    "method": "string", "status": "completed|pending", "transaction_id": "string", "gateway_latency_ms": 10,
    "totals": { "subtotal": 0, "tax": 0, "final_total": 0 }
  },
  "metadata": { "channel": "string", "user_agent": "string", "session_id": "string", "flags": [] }
}
```

## 4. Perfiles de Contención (Docker cgroups v2)
1.  **PROFILE_A_BASELINE:** 4.0 CPUs, 4096 MB Memoria, 0 Swap.
2.  **PROFILE_B_MEDIUM:** 2.0 CPUs, 2048 MB Memoria, 0 Swap.
3.  **PROFILE_C_SEVERE:** 1.0 CPU, 1024 MB Memoria, 0 Swap.

## 5. Matriz de Ejecución
*   **Queries:** Q1 (Unwind & Group - CPU Bound), Q2 (Massive Sort & Top-K - Memory/Buffer Bound).
*   **Concurrencias:** 1, 10, 50 clientes simultáneos.
*   **Ciclo de vida por ejecución:** Drop Caches OS -> 60s Warmup -> 300s Medición -> 60s Cooldown.
*   **Repeticiones:** 3 por celda (36 celdas en total).