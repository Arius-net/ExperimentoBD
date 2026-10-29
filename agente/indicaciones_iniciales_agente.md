# Indicaciones Iniciales

Este archivo define las reglas y los pasos lógicos que nosotros (usuario y asistente) seguiremos para programar el benchmark.

## Reglas de Desarrollo
1.  **Iteración Modular:** No crearemos todo el código de una sola vez. Construiremos y probaremos módulo por módulo.
2.  **Registro Obligatorio:** Tras finalizar y probar un componente, anotaremos el resultado en `registro_ejecucion.md`.
3.  **Sin ORMs:** Todo el acceso a BD se hará vía raw queries para no contaminar métricas de CPU con deserializaciones complejas en Python.
4.  **Manejo de Errores:** Si un paso falla, se documenta el error en la bitácora, se plantea la solución y se actualiza el estado.

## Ruta de Trabajo (Roadmap)
*   [ ] **Paso 1: Configuración de Infraestructura.** Crear el `docker-compose.yml` (Mongo, Postgres, Prometheus, cAdvisor) con los perfiles parametrizados mediante variables de entorno `.env`.
*   [ ] **Paso 2: Generación de Datos.** Desarrollar `synthetic_data_generator.py` y probar inserción masiva en ambas bases de datos.
*   [ ] **Paso 3: Definición de Queries.** Crear las sentencias en la carpeta `queries/` y validar que extraen la información correcta (Ejecución piloto manual).
*   [ ] **Paso 4: Creación del Runner.** Programar `benchmark_runner.py` para inyectar carga (concurrencia de 1, 10, 50).
*   [ ] **Paso 5: Orquestador Bash.** Crear `run_benchmarks.sh` para automatizar la matriz y recolectar el CSV estructurado.
*   [ ] **Paso 6: Ejecución Piloto.** Correr 1 celda de prueba (Perfil A, Concurrencia 1, Q1) y verificar el CSV resultante.