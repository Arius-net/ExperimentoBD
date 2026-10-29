# Estructura del Proyecto

Esta es la jerarquía de carpetas y archivos que mantendremos durante el desarrollo.

```text
proyecto_benchmark/
├── agente/
│   ├── indicaciones_iniciales.md      # Instrucciones de control de nuestro trabajo.
│   └── registro_ejecucion.md          # Bitácora paso a paso.
├── contexto/
│   ├── contexto_experimento.md        # Definición del dataset, queries y hardware.
│   ├── prerequisitos.md               # Instalaciones y dependencias.
│   └── estructura_proyecto.md         # Este archivo.
├── queries/
│   ├── mongo_q1.json
│   ├── mongo_q2.json
│   ├── postgres_q1.sql
│   └── postgres_q2.sql
├── docker-compose.yml                 # Declaración de DBs, Prometheus, cAdvisor.
├── requirements.txt                   # Dependencias de Python.
├── synthetic_data_generator.py        # Script creador de los 500k JSONs.
├── benchmark_runner.py                # Script (Python multi-thread/async) para lanzar cargas.
└── run_benchmarks.sh                  # Orquestador Bash (matriz, cgroups, caches, repeticiones).
```