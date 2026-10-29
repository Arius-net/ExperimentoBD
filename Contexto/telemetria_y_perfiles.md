# Telemetría, Perfiles cgroups v2 y Tuning

Documento de apoyo del **Paso 1** (infraestructura). Describe cómo está configurado lo que ya existe en `docker-compose.yml`, `.env` y `prometheus.yml`. Las decisiones están numeradas (D-xx) según `agente/registro_de_ejecuci_n_bit_cora.md`.

## 1. Arquitectura

```text
        ┌────────────────────────── host Linux (WSL2) ──────────────────────────┐
        │  bench_mongo  ──┐  (solo UNA BD activa por celda: --profile)           │
        │  bench_postgres ┘   límites cgroups v2 (CPU / memoria / 0 swap)        │
        │                                                                        │
        │  cadvisor (:8080)       métricas por contenedor (CPU, memoria, I/O)    │
        │  node-exporter (:9100)  métricas del host                              │
        │  prometheus (:9090)     scrape cada 1s de los dos anteriores           │
        └────────────────────────────────────────────────────────────────────────┘
```

- **Aislamiento (D-07):** `mongo` y `postgres` están en `profiles:` distintos. Se levanta solo uno por celda para que no compitan por CPU, I/O ni page cache del host. La telemetría no tiene perfil y siempre arranca.
- **Red:** `bench_net`, dedicada. Prometheus resuelve `cadvisor` y `node-exporter` por nombre de servicio.

## 2. Imágenes con versión fija (D-04, D-13)

| Servicio | Imagen |
| :--- | :--- |
| MongoDB | `mongo:7.0` |
| PostgreSQL | `postgres:16` |
| cAdvisor | `gcr.io/cadvisor/cadvisor:v0.49.1` |
| node-exporter | `prom/node-exporter:v1.8.2` |
| Prometheus | `prom/prometheus:v2.53.0` |

Las imágenes de BD se parametrizan en `.env` (`MONGO_IMAGE`, `POSTGRES_IMAGE`). Las de telemetría están escritas directamente en el compose.

## 3. Perfiles de contención y cgroups v2

Los límites se aplican con las claves de servicio `cpus`, `mem_limit` y `memswap_limit`, a partir de las variables del `.env`.

| Variable `.env` | Clave compose | Archivo cgroup v2 resultante |
| :--- | :--- | :--- |
| `CPU_LIMIT` | `cpus` | `cpu.max` (cuota/periodo CFS; 4.0 CPUs = `400000 100000`) |
| `MEM_LIMIT` | `mem_limit` | `memory.max` |
| `MEMSWAP_LIMIT` | `memswap_limit` | `memory.swap.max` = `memswap_limit` − `mem_limit` |

**Swap = 0 (D-10, D-12):** con `MEMSWAP_LIMIT` igual a `MEM_LIMIT`, `memory.swap.max` queda en 0. Así el SO no enmascara la falta de memoria con swap y se puede provocar un OOM o un volcado a disco del motor bajo estrés. Se omite `mem_swappiness`: en cgroups v2 Docker no lo soporta (lo ignora o falla al crear el contenedor) y es redundante con lo anterior.

### Perfiles

| Perfil | `CPU_LIMIT` | `MEM_LIMIT` = `MEMSWAP_LIMIT` |
| :--- | :--- | :--- |
| PROFILE_A_BASELINE | 4.0 | 4096m |
| PROFILE_B_MEDIUM | 2.0 | 2048m |
| PROFILE_C_SEVERE | 1.0 | 1024m |

El `.env` contiene el Perfil A como base y B/C en comentarios (D-05). `run_benchmarks.sh` (Paso 5) exportará las variables de cada perfil; las del shell tienen prioridad sobre `.env`.

## 4. Tuning interno proporcional (D-06)

Cada motor gestiona su propia caché. Si no se escala con el perfil, el cuello de botella dejaría de ser el cgroup y las medidas no serían comparables.

| Parámetro | Regla | A (4096 MB) | B (2048 MB) | C (1024 MB) |
| :--- | :--- | :--- | :--- | :--- |
| Mongo `--wiredTigerCacheSizeGB` (`MONGO_WT_CACHE_GB`) | ~50 % de la memoria | 2 | 1 | 0.5 |
| PG `shared_buffers` (`PG_SHARED_BUFFERS`) | 25 % | 1GB | 512MB | 256MB |
| PG `effective_cache_size` (`PG_EFFECTIVE_CACHE_SIZE`) | ~50 % | 2GB | 1GB | 512MB |
| PG `work_mem` (`PG_WORK_MEM`) | Fijo en todos los perfiles | 64MB | 64MB | 64MB |

- `effective_cache_size` es solo una pista para el planificador y no reserva memoria.
- **`work_mem` (D-11):** el `.env` fija 64MB. Q2 se evaluará con `4MB` y `64MB` inyectando `SET work_mem` por sesión en los Pasos 3 y 4, sin depender del `.env`.
- El mínimo de `wiredTigerCacheSizeGB` en MongoDB es 0.25, por lo que 0.5 es válido.

## 5. Telemetría

### cAdvisor (D-09, D-13)
- `--housekeeping_interval=1s` y `--allow_dynamic_housekeeping=false`: sin ellos refresca de forma dinámica, hasta cada 60s en reposo, y un scrape de 1s traería muestras repetidas.
- `--docker_only=true`: solo contenedores Docker.
- `privileged: true` por los permisos de WSL2, con montajes de `/`, `/sys`, `/var/lib/docker`, `/dev/disk` y `/dev/kmsg`.

### node-exporter
Con `pid: host` y `/` montado en `/host` en modo `ro`, para leer métricas reales del host. Se eliminó el modo `rslave` (D-16): Docker Desktop sobre WSL2 no soporta ese tipo de propagación de montaje y el contenedor no arrancaba.

### Prometheus (D-14)
- `scrape_interval: 1s`.
- `scrape_timeout: 1s`: es obligatorio, debe ser menor o igual al intervalo y el valor por defecto de 10s invalidaría la configuración.
- Jobs: `cadvisor` (`cadvisor:8080`) y `node-exporter` (`node-exporter:9100`).
- Retención de 30 días, con volumen `prometheus_data`.

### Telemetría sin límites (D-13)
cAdvisor, node-exporter y Prometheus no tienen límites de recursos. Se asume CPU suficiente en el host. El muestreo cada segundo consume CPU y puede competir con las BD limitadas.

## 6. Riesgos conocidos a verificar

1. **cAdvisor sobre Docker Desktop/WSL2:** puede no resolver el nombre del contenedor o las métricas de disco por los montajes `/var/lib/docker` y `/dev/disk`. **Estado: SIN CONFIRMAR.** La consulta de `container_memory_usage_bytes` falló por falta de URL-encoding en el `curl` (error del comando, no del sistema). Pendiente repetirla con `--data-urlencode`.
2. **Timeout de 1s:** si cAdvisor tarda más de 1s en responder `/metrics`, el target aparecerá en estado `down`. **Estado: no se materializó.** Ambos targets aparecen `up` (validado 2026-10-29).
3. **Telemetría sin límites:** puede introducir ruido en las medidas. Sigue vigente.

## 7. Cambio de perfil

```bash
# Ejemplo: Perfil C (el .env contiene el A)
export PROFILE_NAME=PROFILE_C_SEVERE CPU_LIMIT=1.0 MEM_LIMIT=1024m MEMSWAP_LIMIT=1024m
export MONGO_WT_CACHE_GB=0.5 PG_SHARED_BUFFERS=256MB PG_EFFECTIVE_CACHE_SIZE=512MB
docker compose --profile mongo up -d
```
