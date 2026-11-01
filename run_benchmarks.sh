#!/usr/bin/env bash
# =============================================================================
# run_benchmarks.sh - Orquestador resiliente de las 108 corridas del benchmark
# MongoDB vs PostgreSQL JSONB bajo contención de cgroups v2.
#
# Características de supervivencia (14+ horas):
#   1. Orden de bucles optimizado: Engine -> Profile -> Query -> Concurrency -> Run
#      (solo 6 recreaciones de contenedor en toda la maratón).
#   2. Checkpointing idempotente (Resume on Crash): verifica en benchmark_results.csv
#      si la tupla (engine, profile, query_id, concurrency, run_number) ya existe
#      y la salta automáticamente.
#   3. Ciclo estricto por corrida (420s):
#      Sanity/OOM Recovery -> Restart limpio BD + Drop Caches OS ->
#      Warmup (60s) + Medición (300s) -> Cooldown (60s).
#
# Modos de uso:
#   - Maratón completa (108 corridas):
#       ./run_benchmarks.sh
#   - Celda Piloto (Paso 6: 1 sola corrida, ej. 15s warmup / 30s medición):
#       ENGINES="mongo" PROFILES="PROFILE_A_BASELINE" QUERIES="Q1" \
#       CONCURRENCIES="10" RUNS="1" WARMUP_SECONDS=15 DURATION_SECONDS=30 \
#       COOLDOWN_SECONDS=5 ./run_benchmarks.sh
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Cargar variables base del .env
if [[ -f ".env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source ".env"
    set +a
fi

# Activar venv automáticamente si existe en ~/.venvs/benchmark y no hay otro activo
if [[ -z "${VIRTUAL_ENV:-}" && -f "$HOME/.venvs/benchmark/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/.venvs/benchmark/bin/activate"
fi

# -----------------------------------------------------------------------------
# Parámetros configurables por entorno (permiten Celda Piloto o sub-matrices)
# -----------------------------------------------------------------------------
CSV_FILE="${CSV_FILE:-$SCRIPT_DIR/benchmark_results.csv}"
LOG_FILE="${LOG_FILE:-$SCRIPT_DIR/benchmark_marathon.log}"

ENGINES_LIST=(${ENGINES:-mongo postgres})
PROFILES_LIST=(${PROFILES:-PROFILE_A_BASELINE PROFILE_B_MEDIUM PROFILE_C_SEVERE})
QUERIES_LIST=(${QUERIES:-Q1 Q2})
CONCURRENCIES_LIST=(${CONCURRENCIES:-1 10 50})
RUNS_LIST=(${RUNS:-1 2 3})

WARMUP_SECONDS="${WARMUP_SECONDS:-60}"
DURATION_SECONDS="${DURATION_SECONDS:-300}"
COOLDOWN_SECONDS="${COOLDOWN_SECONDS:-60}"

PG_WORK_MEM_DEFAULT="${PG_WORK_MEM_RUN:-4MB}"
RUN_Q2_64MB_SUITE="${RUN_Q2_64MB_SUITE:-false}"

# -----------------------------------------------------------------------------
# Funciones auxiliares
# -----------------------------------------------------------------------------
log() {
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] $*" | tee -a "$LOG_FILE"
}

export_profile_vars() {
    local profile="$1"
    case "$profile" in
        PROFILE_A_BASELINE)
            export PROFILE_NAME="PROFILE_A_BASELINE"
            export CPU_LIMIT="4.0"
            export MEM_LIMIT="4096m"
            export MEMSWAP_LIMIT="4096m"
            export MONGO_WT_CACHE_GB="2"
            export PG_SHARED_BUFFERS="1GB"
            export PG_EFFECTIVE_CACHE_SIZE="2GB"
            ;;
        PROFILE_B_MEDIUM)
            export PROFILE_NAME="PROFILE_B_MEDIUM"
            export CPU_LIMIT="2.0"
            export MEM_LIMIT="2048m"
            export MEMSWAP_LIMIT="2048m"
            export MONGO_WT_CACHE_GB="1"
            export PG_SHARED_BUFFERS="512MB"
            export PG_EFFECTIVE_CACHE_SIZE="1GB"
            ;;
        PROFILE_C_SEVERE)
            export PROFILE_NAME="PROFILE_C_SEVERE"
            export CPU_LIMIT="1.0"
            export MEM_LIMIT="1024m"
            export MEMSWAP_LIMIT="1024m"
            export MONGO_WT_CACHE_GB="0.5"
            export PG_SHARED_BUFFERS="256MB"
            export PG_EFFECTIVE_CACHE_SIZE="512MB"
            ;;
        *)
            log "ERROR: Perfil desconocido '$profile'"
            exit 1
            ;;
    esac
}

is_run_completed() {
    local engine="$1"
    local profile="$2"
    local qlabel="$3"
    local conc="$4"
    local run_num="$5"
    [[ -f "$CSV_FILE" ]] || return 1
    grep -qE "^[0-9]+,${engine},${profile},${qlabel},${conc},${run_num}," "$CSV_FILE"
}

wait_for_healthy() {
    local engine="$1"
    local cname="bench_${engine}"
    local max_wait=90
    local elapsed=0

    while (( elapsed < max_wait )); do
        local status
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cname" 2>/dev/null || echo "missing")"
        if [[ "$status" == "healthy" ]]; then
            return 0
        fi
        sleep 2
        (( elapsed += 2 ))
    done
    log "WARN: $cname no alcanzó estado 'healthy' tras ${max_wait}s (estado actual: ${status:-unknown})"
    return 1
}

ensure_engine_up_with_profile() {
    local engine="$1"
    local profile="$2"

    export_profile_vars "$profile"
    log "Aplicando $profile en motor '$engine' (CPU=${CPU_LIMIT}, MEM=${MEM_LIMIT})..."

    # Garantizar aislamiento (D-07): apagar el motor opuesto si estuviera activo
    if [[ "$engine" == "mongo" ]]; then
        docker compose --profile postgres stop postgres >/dev/null 2>&1 || true
    else
        docker compose --profile mongo stop mongo >/dev/null 2>&1 || true
    fi

    # Recrear el contenedor del motor activo con los nuevos límites cgroups v2
    docker compose --profile "$engine" up -d --force-recreate "$engine"
    wait_for_healthy "$engine"
}

prepare_clean_run_state() {
    local engine="$1"
    local profile="$2"
    local cname="bench_${engine}"

    # 1. Reiniciar contenedor (o recrearlo si murió por OOM en la corrida previa)
    #    Esto vacía también la caché interna del motor (WiredTiger / shared_buffers)
    export_profile_vars "$profile"
    local running
    running="$(docker inspect -f '{{.State.Running}}' "$cname" 2>/dev/null || echo "false")"
    if [[ "$running" != "true" ]]; then
        log "Contenedor $cname detenido (posible OOM previo). Recreando..."
        docker compose --profile "$engine" up -d --force-recreate "$engine"
    else
        docker restart "$cname" >/dev/null
    fi
    wait_for_healthy "$engine"

    # 2. Flush de PageCache, dentries e inodos del kernel Linux
    sync
    if sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null; then
        log "Flush de caché OS completado (drop_caches=3)."
    else
        log "Solicitando sudo para drop_caches..."
        sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' || log "WARN: No se pudo ejecutar drop_caches."
    fi
}

run_single_cell() {
    local engine="$1"
    local profile="$2"
    local query_id="$3"
    local query_label="$4"
    local conc="$5"
    local run_num="$6"
    local pg_wmem="$7"
    local mongo_disk_flag="$8"

    if is_run_completed "$engine" "$profile" "$query_label" "$conc" "$run_num"; then
        log "[SKIP] Corrida ya registrada en CSV: ${engine},${profile},${query_label},C=${conc},run=${run_num}"
        return 0
    fi

    log "------------------------------------------------------------------------"
    log "PREPARANDO CORRIDA: engine=$engine | profile=$profile | query=$query_label | C=$conc | run=$run_num"
    prepare_clean_run_state "$engine" "$profile"

    # Ejecutar benchmark_runner.py (nunca aborta el bucle bash aunque retorne != 0)
    python3 "$SCRIPT_DIR/benchmark_runner.py" \
        --engine "$engine" \
        --profile "$profile" \
        --query-id "$query_id" \
        --query-label "$query_label" \
        --concurrency "$conc" \
        --run-number "$run_num" \
        --warmup "$WARMUP_SECONDS" \
        --duration "$DURATION_SECONDS" \
        --pg-work-mem "$pg_wmem" \
        "$mongo_disk_flag" \
        --output-csv "$CSV_FILE" \
        2>&1 | tee -a "$LOG_FILE" || log "WARN: benchmark_runner.py devolvió código de error, continuando maratón..."

    if (( COOLDOWN_SECONDS > 0 )); then
        log "Cooldown (${COOLDOWN_SECONDS}s)..."
        sleep "$COOLDOWN_SECONDS"
    fi
}

# -----------------------------------------------------------------------------
# Bucle Principal de las 108 Corridas
# -----------------------------------------------------------------------------
log "========================================================================"
log "INICIANDO ORQUESTADOR DE BENCHMARK (CSV: $CSV_FILE)"
log "========================================================================"

for engine in "${ENGINES_LIST[@]}"; do
    for profile in "${PROFILES_LIST[@]}"; do
        ensure_engine_up_with_profile "$engine" "$profile"

        for query_id in "${QUERIES_LIST[@]}"; do
            for conc in "${CONCURRENCIES_LIST[@]}"; do
                for run_num in "${RUNS_LIST[@]}"; do
                    run_single_cell \
                        "$engine" "$profile" "$query_id" "$query_id" \
                        "$conc" "$run_num" "$PG_WORK_MEM_DEFAULT" "--no-mongo-allow-disk-use"
                done
            done
        done

        # Suite opcional para contrastar Q2 con 64MB (PG) / allowDiskUse=true (Mongo)
        if [[ "$RUN_Q2_64MB_SUITE" == "true" ]]; then
            for conc in "${CONCURRENCIES_LIST[@]}"; do
                for run_num in "${RUNS_LIST[@]}"; do
                    run_single_cell \
                        "$engine" "$profile" "Q2" "Q2_64MB" \
                        "$conc" "$run_num" "64MB" "--mongo-allow-disk-use"
                done
            done
        fi
    done

    log "Apagando motor '$engine' al concluir sus perfiles..."
    docker compose --profile "$engine" stop "$engine" >/dev/null 2>&1 || true
done

log "========================================================================"
log "MARATÓN FINALIZADA. Resultados en: $CSV_FILE"
log "========================================================================"

