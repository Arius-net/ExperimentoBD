# Prerrequisitos del Sistema

## 1. Infraestructura de Hardware y SO
*   Sistema Operativo basado en Linux (Ubuntu 22.04 LTS o superior recomendado) para garantizar el funcionamiento correcto de `cgroups v2`.
*   Verificar que cgroups v2 está activo: `cat /sys/fs/cgroup/cgroup.controllers` (debe devolver una lista de controladores como `cpu io memory`).
*   Al menos 8GB de RAM disponibles y 4 cores lógicos para el perfil Baseline.

## 2. Herramientas a Instalar
*   **Docker:** Versión 24.x o superior.
*   **Docker Compose:** Versión V2.
*   **Python:** Versión 3.10 o superior.
*   **Python pip / venv:** Para gestión de paquetes.

## 3. Dependencias de Python (requirements.txt)
Las siguientes librerías serán necesarias para el desarrollo de los scripts:
*   `Faker==19.13.0` (Para generar los datos sintéticos).
*   `pymongo==4.6.0` (Driver nativo de MongoDB).
*   `psycopg2-binary==2.9.9` (Driver nativo de PostgreSQL).
*   `requests==2.31.0` (Para interactuar con la API de Prometheus y extraer métricas).
*   `pandas==2.1.3` (Para consolidación del CSV final, opcional pero útil).

## 4. Configuraciones a nivel Sistema Operativo
*   **Drop Caches:** El usuario que ejecute `run_benchmarks.sh` debe tener permisos de sudo sin contraseña para ejecutar `sync; echo 3 > /proc/sys/vm/drop_caches`.