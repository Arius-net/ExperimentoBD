# Registro de Ejecución

Esta bitácora nos ayudará a saber exactamente en qué punto estamos, qué falló y cómo se solucionó. Se actualizará en cada iteración.

| ID | Componente / Tarea | Estado | Fecha/Hora | Notas / Errores / Soluciones |
| :--- | :--- | :--- | :--- | :--- |
| 00 | Inicialización del entorno documental | COMPLETADO | (Actual) | Se generaron los archivos de contexto y agente. |
| 01 | Crear `docker-compose.yml` y `.env` | COMPLETADO | 2026-10-29 17:21 | Validado en Docker Desktop/WSL2. Healthchecks de cAdvisor y Prometheus: 200. Targets de Prometheus: `up`. cgroups v2 en `bench_mongo`: `cpu.max=400000 100000`, `memory.max=4294967296`, `memory.swap.max=0`. Fallos y correcciones en las notas (F-01, F-02). Pendiente menor: confirmar que cAdvisor resuelve el nombre del contenedor (F-02). |
| 02 | Desarrollar generador de datos | COMPLETADO | 2026-10-30 23:50 | `synthetic_data_generator.py` ejecutado (500k registros, Perfil A). Match exacto: 1,506,682 ítems (media 3.01), suma `final_total` = 312,929,599.78, MD5 = `3f62c2184872f0cf11edab87531e73aa`, `completed` nov-2025 = 34,663. Mongo: Gen 18.4s, Insert 10.4s (48,018 docs/s), 236.0 MB datos / 37.4 MB índices. PG: Gen 17.9s, Serialización 14.8s, Insert 13.5s (36,995 docs/s), 764.7 MB datos / 38.8 MB índices. Fallo F-05 resuelto con `shm_size: 1g` (D-29). |
| 03 | Crear e indexar BDs (Piloto) | COMPLETADO | 2026-10-30 23:50 | Ejecutado e integrado al final del generador (D-22, D-27, D-28). Índices y `VACUUM (ANALYZE)` completados en ambos motores. |
| 04 | Definir queries y probarlas | COMPLETADO | 2026-10-30 23:52 | Creados y guardados `queries/mongo_q1.json`, `queries/postgres_q1.sql`, `queries/mongo_q2.json`, `queries/postgres_q2.sql` con `OFFSET 34653` y desempate determinista por `order_id`/`id` (D-30). |
| 05 | Desarrollar `benchmark_runner.py` | COMPLETADO | 2026-10-31 00:05 | `benchmark_runner.py` creado y validado con `py_compile`. Implementa `ThreadPoolExecutor` + `Barrier`, doble telemetría (Prometheus + `/sys/fs/cgroup`), detección booleana de `disk_spill` y escritura atómica con `fsync` de las 14 columnas (D-34 a D-36). |
| 06 | Desarrollar `run_benchmarks.sh` | COMPLETADO | 2026-10-31 00:07 | `run_benchmarks.sh` creado y guardado. Orquesta las 108 corridas con orden óptimo (`Engine -> Profile -> Query -> Concurrency -> Run`), checkpointing idempotente sobre el CSV, recuperación ante OOM y suite opcional `Q2_64MB`. |
| 07 | Ejecución celda piloto y validación CSV | EN PROGRESO | 2026-10-31 00:07 | Comandos de ejecución piloto (`mongo`, `PROFILE_A_BASELINE`, `Q1`, `C=10`, `warmup=15s`, `duration=30s`) e inspección de las 14 columnas entregados al usuario. |

## Notas Adicionales
*   *Espacio para registrar comandos útiles, configuraciones específicas descubiertas sobre la marcha, o razones técnicas por las que se modificó el código original.*
*   Entorno de ejecución confirmado: Docker Desktop sobre WSL 2 (cgroups v2). Riesgo a verificar: cAdvisor sobre Docker Desktop/WSL2 puede no resolver nombres de contenedor o métricas de disco por los montajes `/var/lib/docker` y `/dev/disk`. Se validará tras el primer `up`.
*   Riesgo a verificar: cAdvisor puede tardar más de 1s en responder a `/metrics`, lo que superaría `scrape_timeout: 1s` y marcaría el target como caído.
*   Para Q2 se evaluará `work_mem` = 4MB y 64MB vía `SET work_mem` por sesión (D-11).
*   Entorno Python real: Ubuntu sobre WSL2 con Python 3.12 (no 3.10). Hubo temas de compilación resueltos por el usuario durante la instalación (detalle no registrado). Las dependencias fijadas (Faker 19.13.0, pymongo 4.6.0, psycopg2-binary 2.9.9) quedaron instaladas sin errores. venv en `~/.venvs/benchmark` (fuera de OneDrive).
*   Documento nuevo `Contexto/telemetria_y_perfiles.md` (regla 5): telemetría, cgroups v2 y cálculos de tuning.

### Fallos y correcciones (Paso 1)
| ID | Qué falló | Corrección | Estado |
| :--- | :--- | :--- | :--- |
| F-01 | `node-exporter` no arrancaba con el volumen `/:/host:ro,rslave`: Docker Desktop sobre WSL2 no soporta `rslave`. | El usuario cambió el volumen a `/:/host:ro` en `docker-compose.yml`. Arrancó correctamente. | RESUELTO |
| F-02 | La consulta `container_memory_usage_bytes{name="bench_mongo"}` con `curl` dio `unexpected "="`. Causa: comando entregado sin URL-encoding (`{}`, `"` y `[]` los interpreta `curl`). Error del comando propuesto, no del sistema. | Usar `curl -sG ... --data-urlencode 'query=...'`. | PENDIENTE de repetir: no se confirmó que cAdvisor resuelva el nombre del contenedor. |
| F-03 | Problemas de compilación al instalar dependencias (Python 3.12). | Resueltos por el usuario (detalle no registrado). | RESUELTO |
| F-04 | `CREATE INDEX ... ((data->>'timestamp')::timestamptz)` (ejemplo de `contexto_del_experimento.md`) falla en PostgreSQL con `ERROR: functions in index expression must be marked IMMUTABLE`. Reproducido por el usuario. Causa: el cast texto a `timestamptz` es `STABLE`. | Se indexa el texto directamente: `((data->>'timestamp'))` (D-18). | RESUELTO |
| F-05 | Durante la post-carga en PostgreSQL (`VACUUM (ANALYZE)` / creación de índices paralelos), falló con error de memoria compartida (`could not resize shared memory segment...`). Causa: Docker asigna por defecto `/dev/shm` de 64MB, insuficiente para operaciones paralelas y `maintenance_work_mem` de PG. | El usuario añadió `shm_size: 1g` al servicio `postgres` en `docker-compose.yml` (D-29). | RESUELTO |

Cambios menores del usuario en `docker-compose.yml`: formato `[ a, b ]` en listas inline, sin efecto funcional.

## Decisiones Clave (Paso 1) - 2026-10-29
| ID | Decisión | Justificación |
| :--- | :--- | :--- |
| D-01 | **Nomenclatura:** se conservan los nombres existentes (`Contexto/`, `agente/`, `registro_de_ejecuci_n_bit_cora.md`). No se renombra nada. | Instrucción explícita del usuario. |
| D-02 | **Roadmap:** rige el de 6 pasos de `indicaciones_iniciales_agente.md` (incluye Paso 6: ejecución piloto obligatoria). Se descarta el orden de 5 pasos del prompt inicial. | Los documentos `.md` son la fuente de verdad. |
| D-03 | **Entorno:** host 100% Linux con cgroups v2 (VM VirtualBox o Docker Desktop sobre WSL2 configurado para cgroups v2). Se asume `sudo` para `drop_caches`. | Requisito del experimento. Windows no es el host de ejecución. |
| D-04 | **Versiones fijas:** `mongo:7.0` y `postgres:16`. | Reproducibilidad ("latest-stable" cambia en el tiempo). |
| D-05 | **Selección de perfil:** un único `.env` con el Perfil A por defecto y B/C documentados en comentarios. `run_benchmarks.sh` (Paso 5) exportará las variables de cada perfil, que tienen prioridad sobre `.env`. | No añade archivos y es la opción más limpia. |
| D-06 | **Tuning proporcional:** Mongo `wiredTigerCacheSizeGB` ~50% de la memoria. PG `shared_buffers` 25% y `effective_cache_size` ~50%. `work_mem` fijo e igual en todos los perfiles. | El cuello de botella debe ser el cgroup, no la configuración interna del motor. |
| D-07 | **Aislamiento:** `profiles:` de Compose (`--profile mongo` / `--profile postgres`), una sola BD activa por celda. Telemetría siempre activa. | Evita que ambas BD compitan por CPU, I/O y page cache del host. |
| D-08 | **Archivos extra aprobados:** `prometheus.yml` y `Contexto/telemetria_y_perfiles.md`. | `prometheus.yml` es imprescindible para el scrape de 1s. El `.md` documenta telemetría, perfiles y tuning (regla 5). |
| D-09 | **cAdvisor:** `--housekeeping_interval=1s` y `--allow_dynamic_housekeeping=false`. | Sin ellos cAdvisor puede refrescar sus métricas hasta cada 60s y el scrape de 1s traería muestras repetidas. |
| D-10 | **Swap:** `MEMSWAP_LIMIT` = `MEM_LIMIT` (0 swap). *(Actualizada por D-12: se eliminó `mem_swappiness`.)* | El playbook (Pregunta M2) busca provocar OOM o volcado a disco bajo estrés sin que el SO lo enmascare con swap. |
| D-11 | **`PG_WORK_MEM=64MB`** en `.env`. Q2 se evaluará con `work_mem` de `4MB` y `64MB`, inyectando `SET work_mem` por sesión (Fase 3), sin depender del `.env`. | El playbook exige dos variantes de estrés para Q2. Se aplicará en el runner/queries. |
| D-12 | **Se elimina `mem_swappiness: 0`** del compose. | Docker no soporta `--memory-swappiness` en cgroups v2 (puede ignorarlo o fallar al arrancar). `memswap_limit` = `mem_limit` ya fija `memory.swap.max=0`. |
| D-13 | **Telemetría:** `cadvisor:v0.49.1`, `node-exporter:v1.8.2`, `prometheus:v2.53.0`, `privileged: true` en cAdvisor y **sin límites** de CPU/memoria en la telemetría. | Reproducibilidad. `privileged` por permisos de WSL2. Se asume CPU suficiente en el host. |
| D-14 | **`prometheus.yml`:** `scrape_interval: 1s` y `scrape_timeout: 1s` (obligatorio, debe ser <= al intervalo). Jobs: `cadvisor:8080` y `node-exporter:9100`. | El timeout por defecto (10s) invalidaría la configuración. |
| D-15 | **`requirements.txt` mínimo** (Faker 19.13.0, pymongo 4.6.0, psycopg2-binary 2.9.9). `requests` y `pandas` (listados en prerrequisitos) se añadirán cuando se necesiten (Pasos 4-5). | Instrucción del usuario: solo lo estrictamente necesario para el Paso 2. Versiones tomadas de `prerrequisitos_y_configuraci_n.md`. |
| D-16 | **node-exporter:** volumen `/:/host:ro` (sin `rslave`). | Docker Desktop/WSL2 no soporta la propagación `rslave` (F-01). |

## Decisiones Clave (Paso 2) - 2026-10-29
| ID | Decisión | Justificación |
| :--- | :--- | :--- |
| D-17 | **`timestamp` como cadena ISO-8601 en ambas BD**, UTC y de ancho fijo (`YYYY-MM-DDTHH:MM:SS.mmmZ`). | Documento idéntico bit a bit: se mide el parseo de JSONB vs BSON sin dar a Mongo la ventaja del tipo nativo. El ancho fijo garantiza que el orden alfabético sea el cronológico. |
| D-18 | **Índice PG sobre texto:** `CREATE INDEX idx_orders_timestamp ON orders ((data->>'timestamp'))`. Reemplaza el ejemplo con cast a `timestamptz` del contexto. | F-04. Evita funciones SQL ajenas al rendimiento puro. |
| D-19 | **Cardinalidades aprobadas:** 500.000 órdenes, 50.000 clientes, ~500 ubicaciones, 5.000 SKUs, 20 categorías, 100 marcas, 1 a 6 ítems (media 3), `completed` 85 % / `pending` 15 %, 365 días de fechas (2025), semilla fija 42. | El contexto solo fijaba volumen y media de ítems; el resto es propuesta aprobada por el usuario. |
| D-20 | **Q1** (aprobada): cantidad total de artículos e ingresos (`qty * price`) por `category`, solo `payment.status = 'completed'` (`$unwind` en Mongo, `jsonb_array_elements` en PG). **Q2 DEFINITIVA** (usuario, 2026-10-29 17:31; sustituye a las dos versiones anteriores): órdenes de noviembre de 2025 con `payment.status = 'completed'`, ordenar todo el resultado por `totals.final_total` DESC y aplicar un `OFFSET` grande (p. ej. resultados 34.900 a 35.000) para forzar el sort sobre toda la masa del mes. `final_total` no está indexado. | Definición de consultas para el Paso 3. Ver O-01 y O-02 (resueltas) y O-03. |
| D-21 | **Carga única bajo Perfil A (4 GB)** y se conservan los volúmenes (`mongo_data`, `pg_data`) para las lecturas. | Cargar 500k documentos con 1 GB sería lento o arriesgado. |
| D-22 | **Índices y `ANALYZE` integrados al final del generador.** Mongo: `timestamp` y `order_id`, individuales. PG: PK en `id` + `idx_orders_timestamp` (D-18) + `ANALYZE`. | La BD queda lista para consultar justo después de la carga. Se crean tras la carga (más rápido). |
| D-23 | **`--drop` activo por defecto** (con `--no-drop` para desactivarlo). | Evita datos duplicados al recargar. Cambio respecto a mi propuesta original (negarse si había datos), por indicación del usuario. |
| D-24 | **Q2 debe proyectar el documento JSONB completo (`data`) tras el Sort**, y en Mongo el documento completo. | Evita que PostgreSQL ordene tuplas estrechas (3 campos) y el sort quepa en 4MB. Se verificará con `EXPLAIN (ANALYZE, BUFFERS)` (O-03). |
| D-25 | **El script imprime el conteo real de `completed` de noviembre de 2025** al terminar la carga. | Calibrar el `OFFSET` de Q2 con datos reales (O-03 c). |
| D-26 | **Tiempos separados** en el reporte: generación en Python (`generate_order`) frente a inserción en la BD (driver + red + motor). Además se separa la serialización JSON/CSV de PostgreSQL (Python, 0 en Mongo, donde la codificación BSON ocurre dentro del driver). | Un benchmark estricto debe distinguir lo que es del script de lo que es del motor. Pedido por el usuario, ampliado a 3 cubetas por la serialización de PG. |
| D-27 | **PK de PostgreSQL creada tras la carga** (no al crear la tabla) y carga de PG en una sola transacción con `COPY` en formato CSV. | Igualar con Mongo (sin índices durante la inserción), índice mejor empaquetado, detección de UUIDs duplicados, y sin cargas parciales. Con `--skip-indexes` la tabla queda sin PK. |
| D-28 | **`VACUUM (ANALYZE)` en lugar de `ANALYZE`** al final de la carga de PostgreSQL (sustituye lo indicado en D-22). | Fija los hint bits tras el `COPY` masivo y construye el mapa de visibilidad, evitando escrituras en las primeras lecturas. Requiere `autocommit`. |
| D-29 | **`shm_size: 1g` en el contenedor `postgres`** (`docker-compose.yml`). | Evita agotamiento de `/dev/shm` (64MB por defecto en Docker) durante `VACUUM (ANALYZE)` y workers paralelos en PostgreSQL (resuelve F-05). |

## Decisiones Clave (Paso 3) - 2026-10-30
| ID | Decisión | Justificación |
| :--- | :--- | :--- |
| D-30 | **Calibración exacta de Q2 (`OFFSET 34653`, `LIMIT 10`) y desempate (`order_id` / `id` ASC).** | Con 34,663 órdenes `completed` en nov-2025, `34653 + 10 = 34663` fuerza al motor a ordenar el 100% del subconjunto en RAM y devolver las últimas 10 filas. El desempate garantiza determinismo ante colisiones en `final_total`. |

## Especificación de la Matriz y CSV Maestro (Pasos 4 y 5) - 2026-10-30
| ID | Especificación | Detalle |
| :--- | :--- | :--- |
| D-31 | **Matriz de 108 corridas:** 36 celdas × 3 repeticiones (`run_number` 1..3). | 2 motores (`mongo`, `postgres`) × 3 perfiles (`PROFILE_A_BASELINE`, `PROFILE_B_MEDIUM`, `PROFILE_C_SEVERE`) × 2 queries (`Q1`, `Q2`) × 3 concurrencias (`1`, `10`, `50`). |
| D-32 | **Ciclo por corrida (420s):** Drop Caches OS → Warmup 60s → Medición 300s → Cooldown 60s. | 108 × 420s = 12.6 h de ventana pura (~14+ h con recreación de contenedores y drops de caché). |
| D-33 | **Esquema exacto del CSV (14 columnas):** `timestamp_epoch,engine,profile,query_id,concurrency,run_number,throughput_qps,latency_p50_ms,latency_p95_ms,latency_p99_ms,cpu_throttled_seconds,max_rss_mb,disk_spill,status`. | Exigido por la rúbrica del usuario. |
| D-34 | **Parámetros adversos por defecto en las 108 corridas:** `work_mem='4MB'` en PostgreSQL y `allowDiskUse=False` en MongoDB. `disk_spill` se formatea como booleano estricto (`true` / `false`). | Provoca y captura de forma determinista el volcado a disco (`temp_files`/`temp_bytes` en PG) o el límite de 100MB en RAM de Mongo (código 292). |
| D-35 | **Cero dependencias extra para telemetría:** se usa `urllib.request` de la librería estándar + muestreo directo de `/sys/fs/cgroup/` cada 1s (`CgroupMonitor`). | Mantiene `requirements.txt` intacto y evita ceros falsos si cAdvisor en WSL2 omite etiquetas de contenedor. |
| D-36 | **Soporte limpio para evaluar `work_mem='64MB'` / `allowDiskUse=True` sin romper los bucles:** flag `--query-label` en `benchmark_runner.py` y variable `RUN_Q2_64MB_SUITE` en `run_benchmarks.sh`. | Mantiene las 108 corridas base intactas y permite ejecutar una pasada focalizada adicional (`Q2_64MB`) si se desea contrastar spill vs no-spill. |

## Siguiente paso lógico
Paso 6 (ID 07 en tabla): Ejecutar la celda piloto en WSL2 (`mongo`, `PROFILE_A_BASELINE`, `Q1`, `C=10`, `15s/30s`), validar que `benchmark_results_pilot.csv` (o `benchmark_results.csv`) tenga las 14 columnas pobladas correctamente y, tras el visto bueno, lanzar la maratón completa de 108 corridas.