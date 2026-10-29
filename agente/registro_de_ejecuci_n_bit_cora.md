# Registro de Ejecución

Esta bitácora nos ayudará a saber exactamente en qué punto estamos, qué falló y cómo se solucionó. Se actualizará en cada iteración.

| ID | Componente / Tarea | Estado | Fecha/Hora | Notas / Errores / Soluciones |
| :--- | :--- | :--- | :--- | :--- |
| 00 | Inicialización del entorno documental | COMPLETADO | (Actual) | Se generaron los archivos de contexto y agente. |
| 01 | Crear `docker-compose.yml` y `.env` | COMPLETADO | 2026-10-29 17:21 | Validado en Docker Desktop/WSL2. Healthchecks de cAdvisor y Prometheus: 200. Targets de Prometheus: `up`. cgroups v2 en `bench_mongo`: `cpu.max=400000 100000`, `memory.max=4294967296`, `memory.swap.max=0`. Fallos y correcciones en las notas (F-01, F-02). Pendiente menor: confirmar que cAdvisor resuelve el nombre del contenedor (F-02). |
| 02 | Desarrollar generador de datos | EN PROGRESO | 2026-10-29 17:50 | Estrategia aprobada (D-17 a D-27). Código en revisión por bloques, aún sin guardar. Bloques 1 a 4 APROBADOS (el 4 con el ajuste de tiempos separados de D-26, a re-mostrar). Bloque 5 (índices, ANALYZE, verificación con conteo de nov-2025, `main`) en revisión. |
| 03 | Crear e indexar BDs (Piloto) | PENDIENTE | | |
| 04 | Definir queries y probarlas | PENDIENTE | | |
| 05 | Desarrollar `benchmark_runner.py` | PENDIENTE | | |
| 06 | Desarrollar `run_benchmarks.sh` | PENDIENTE | | |
| 07 | Ejecución celda piloto y validación CSV | PENDIENTE | | |

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

### Observaciones abiertas
| ID | Observación | Estado |
| :--- | :--- | :--- |
| O-01 | **Q2 podría no ser un sort masivo.** Con el índice en `timestamp`, "100 más recientes de un segmento" se resuelve recorriendo el índice en orden inverso y filtrando hasta reunir 100, sin ordenar el conjunto. No presionaría memoria ni `work_mem` (4MB vs 64MB), contra lo definido en `contexto_del_experimento.md` ("Massive Sort & Top-K, Memory/Buffer Bound"). | RESUELTA: Q2 redefinida (D-20) para ordenar por `final_total` (sin índice) sobre un mes. |
| O-02 | **El Top-N heapsort usa memoria O(K), no O(filas).** Con `LIMIT 100`, PostgreSQL solo retiene 100 filas en el sort y Mongo fusiona `$sort`+`$limit` en un Top-K de 100. Cualquiera de los dos cabe en 4MB, por lo que `work_mem` de 4MB vs 64MB probablemente **no producirá diferencia** en Q2. La presión de Q2 vendría del escaneo y extracción de ~35.000 documentos del mes (CPU y buffers), no del sort. Además, el filtro por mes puede usar el índice de `timestamp` (rango de ~8 % de las filas). | RESUELTA: se usa `OFFSET` grande (Q2 definitiva en D-20); ver O-03 para los riesgos de calibración. |
| O-03 | **Calibración de Q2 con `OFFSET` (a verificar en el Paso 3).** (a) PostgreSQL no hace un sort completo: con `OFFSET 34900 LIMIT 100` el nodo Limit pasa un límite de 35.000 filas al sort (Top-N acotado). La memoria es ~35.000 × ancho de fila: si solo se proyectan `order_id`/`timestamp`/`final_total` (~100 B) son ~3,5 MB, justo en el borde de 4MB; si el sort arrastra el JSONB completo (~1-2 KB) son decenas de MB y 4MB desborda a disco mientras 64MB cabe. Lo confirmará `EXPLAIN (ANALYZE, BUFFERS)` (línea `Sort Method`). (b) MongoDB tiene un límite fijo de 100MB para `$sort` independiente del cgroup, así que las variantes de `work_mem` no tienen equivalente directo. (c) El mes tendrá ~41.100 órdenes y ~34.900 `completed` (± ~250 por azar): un `OFFSET` de 34.900 podría quedar fuera del total. Hay que calibrarlo con el conteo real. | CERRADA (2026-10-29 17:33): (a) Q2 proyecta `data` completo (D-24). (b) El límite fijo de 100MB de Mongo se acepta como comportamiento documentado a observar. (c) Conteo real impreso por el script (D-25). |

## Siguiente paso lógico
Paso 2: validar por bloques `synthetic_data_generator.py` (bloque 1 propuesto), guardarlo y ejecutarlo bajo Perfil A. Resolver O-01 antes del Paso 3. Pendiente menor: repetir la consulta F-02 con `--data-urlencode`.